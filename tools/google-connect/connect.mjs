#!/usr/bin/env node
/**
 * 구글 캘린더 연결 — 대표가 하는 일은 **구글 로그인 한 번**뿐.
 *
 *   로그인(사람)  →  아래 전부 자동
 *   ① 구글 클라우드 프로젝트 만들기   ④ 앱 열쇠(OAuth 클라이언트) 발급
 *   ② 캘린더 API 켜기                 ⑤ 앱 게시 (안 하면 7일마다 연결이 끊긴다)
 *   ③ 동의 화면 구성                  ⑥ 캘린더 권한 승인 → 연결 열쇠 저장
 *                                     ⑦ 읽기·쓰기 왕복 확인
 *
 *   node tools/google-connect/connect.mjs            연결 (중간부터 이어서도 됨)
 *   node tools/google-connect/connect.mjs --verify   연결이 살아 있는지만 확인
 *   node tools/google-connect/connect.mjs --show     지금 어디까지 됐는지
 *
 * 실측으로 확정한 규칙 (추측 금지):
 *  · 화면 이동은 **URL 직행** — 콘솔 UI 가 바뀌어도 경로는 안정적이다
 *  · 같은 글자가 여러 요소에 걸리면 **가장 작은(=가장 구체적인) 것**을 고른다
 *  · 라디오·체크박스는 **JS click()** — 라벨 안에 링크가 있어 좌표 클릭이 링크로 샌다
 *  · 드롭다운 항목은 **.cdk-overlay-container 안**에서만 찾는다 (본문에 같은 글자가 또 있다)
 *  · 앱 비밀번호는 **만든 직후 화면에 딱 한 번**만 뜬다 — 그 자리에서 읽는다
 *  · 구글 콘솔은 "만들었다"와 "쓸 수 있다" 사이에 시차가 있다 — 고정 대기 대신 **될 때까지 다시 본다**
 *  · **크롬은 우리가 평범하게 띄운다.** Playwright 가 띄우면 자동화 플래그가 붙어
 *    구글 로그인이 거부된다(signin/rejected). 로그인은 사람이, 자동화는 그 뒤에 붙는다
 *  · connectOverCDP 는 **Node 에서만** 동작한다 (Bun 은 웹소켓 계층에서 깨진다 — 실측)
 */
import { chromium } from 'playwright-core';
import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import crypto from 'node:crypto';
import fs from 'node:fs';

const HERE     = path.dirname(fileURLToPath(import.meta.url));
const KIT      = path.resolve(HERE, '../..');
const KEYFILE  = path.join(KIT, 'state', 'google-calendar.json');
const PROFILE  = process.env.PROFILE_DIR ?? path.join(HERE, '.chrome-profile');
const APP_NAME = '내 비즈니스 파트너';
const SCOPE    = 'https://www.googleapis.com/auth/calendar.events';
const CHROME   = process.env.CHROME_PATH ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const PORT     = Number(process.env.CDP_PORT ?? 9222);

const say = (m = '') => console.log(m);
const ok  = (n, d = '') => say(`  ✅ ${n}${d ? '  — ' + d : ''}`);
const bad = (n, d = '') => say(`  ❌ ${n}${d ? '  — ' + d : ''}`);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const load = () => { try { return JSON.parse(fs.readFileSync(KEYFILE, 'utf8')); } catch { return {}; } };
const save = (o) => {
  fs.mkdirSync(path.dirname(KEYFILE), { recursive: true });
  fs.writeFileSync(KEYFILE, JSON.stringify(o, null, 2));
  fs.chmodSync(KEYFILE, 0o600);      // 열쇠 파일은 본인만 읽게
};
let S = load();

/* ─────────────────── 화면 조작 공용 ─────────────────── */

function driver(page) {
  const d = {
    page,
    text: () => page.evaluate(() => document.body?.innerText ?? ''),

    /** URL 직행 + 화면이 자리잡을 시간 + 쿠키 배너 치우기 */
    async go(url, settle = 9000) {
      await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
      await page.waitForTimeout(settle);
      await d.dismissCookie();
    },

    /** 새 프로필이면 쿠키 동의 배너가 화면 아래를 덮고 그 위의 버튼 클릭을 먹는다.
     *  참가자는 전부 새 프로필이라 **반드시** 겪는다 — 먼저 치운다. */
    async dismissCookie() {
      if (!/쿠키를 사용합니다|uses cookies|쿠키 사용/.test(await d.text().catch(() => ''))) return false;
      return d.tap(/^확인$|^모두 수락$|^Accept all$|^OK$/, { wait: 1600, quiet: true });
    },

    /** 글자가 맞는 것 중 **가장 작은** 요소 (작을수록 구체적) */
    find: (re, minY = 0) => page.evaluate(([src, my]) => {
      const rx = new RegExp(src), out = [];
      document.querySelectorAll('*').forEach((e) => {
        if (e.children.length > 3) return;
        const t = (e.innerText || e.getAttribute('aria-label') || '').trim();
        if (!rx.test(t)) return;
        const b = e.getBoundingClientRect();
        if (b.width < 8 || b.height < 8 || b.y < my) return;
        const s = getComputedStyle(e);
        if (s.visibility === 'hidden' || s.display === 'none' || +s.opacity === 0) return;
        out.push({ t: t.slice(0, 45), x: Math.round(b.x + b.width / 2), y: Math.round(b.y + b.height / 2), a: b.width * b.height });
      });
      return out.sort((p, q) => p.a - q.a);
    }, [re.source, minY]),

    /** 글자로 눌러본다. 없으면 false — 화면마다 있을 수도 없을 수도 있다 */
    async tap(re, { wait = 4000, minY = 0, quiet = false } = {}) {
      const hits = await d.find(re, minY);
      if (!hits.length) return false;
      await page.mouse.click(hits[0].x, hits[0].y);
      if (!quiet) say(`       · 눌렀어요: "${hits[0].t}"`);
      await page.waitForTimeout(wait);
      return hits[0].t;
    },

    /** 조건이 참이 될 때까지 페이지를 다시 열어보며 기다린다 */
    async until(url, test, { tries = 8, gap = 5000, what = '' } = {}) {
      for (let i = 0; i < tries; i++) {
        await d.go(url, i === 0 ? 9000 : 4000);
        if (await test(await d.text(), page.url())) return true;
        if (i === 1 && what) say(`       · ${what} 기다리는 중…`);
        await sleep(gap);
      }
      return false;
    },

    /** 본문 입력칸 (상단 검색창 제외) */
    firstInput: (minY = 150) => page.evaluate((my) => {
      const e = [...document.querySelectorAll('input')].find((x) => {
        const b = x.getBoundingClientRect();
        return b.y > my && b.width > 100 && x.type !== 'search' && x.type !== 'hidden';
      });
      if (!e) return null;
      const b = e.getBoundingClientRect();
      return { x: Math.round(b.x + b.width / 2), y: Math.round(b.y + b.height / 2) };
    }, minY),

    /** 열려 있는 드롭다운(cdk 오버레이) 안에서만 고른다 */
    async pickOption(re) {
      const opts = await page.evaluate((src) => {
        const rx = new RegExp(src), out = [];
        document.querySelectorAll('.cdk-overlay-container [role="option"], .cdk-overlay-container cfc-option, .cdk-overlay-container mat-option').forEach((e) => {
          const t = (e.innerText || '').trim();
          if (!rx.test(t)) return;
          const b = e.getBoundingClientRect();
          if (b.width < 8) return;
          out.push({ t, x: Math.round(b.x + b.width / 2), y: Math.round(b.y + b.height / 2) });
        });
        return out;
      }, re.source);
      if (!opts.length) return false;
      await page.mouse.click(opts[0].x, opts[0].y);
      await page.waitForTimeout(2200);
      return opts[0].t;
    },

    /** 라디오·체크박스는 좌표가 아니라 JS click — 라벨 안 링크로 새는 걸 막는다 */
    radio: (re) => page.evaluate((src) => {
      const rx = new RegExp(src);
      const r = [...document.querySelectorAll('input[type="radio"]')].find((e) =>
        rx.test(document.querySelector(`label[for="${e.id}"]`)?.innerText || e.closest('label')?.innerText || ''));
      if (!r) return false;
      r.click();
      return true;
    }, re.source),

    checkbox: () => page.evaluate(() => {
      const e = document.querySelector('input.mdc-checkbox__native-control, input[type="checkbox"][required], input[type="checkbox"]');
      if (!e) return false;
      if (!e.checked) e.click();
      return e.checked;
    }),
  };
  return d;
}

/* ─────────────────── 사람: 구글 로그인 ─────────────────── */

const portAlive = async () => {
  try { return (await fetch(`http://127.0.0.1:${PORT}/json/version`)).ok; } catch { return false; }
};

/**
 * ★ 크롬은 **우리가 평범하게** 띄운다. Playwright 로 띄우면 안 된다.
 *
 *   Playwright 가 브라우저를 띄우면 `--enable-automation` 같은 자동화 플래그가 붙고,
 *   그러면 `navigator.webdriver === true` 가 되어 **구글 로그인이 거부된다**
 *   (accounts.google.com/v3/signin/rejected · "브라우저 또는 앱이 안전하지 않을 수 있습니다").
 *   실측으로 확인한 제약이다. 우회 대상이 아니라 설계가 따라야 할 조건이다:
 *   **로그인은 평범한 크롬에서 사람이 하고, 자동화는 그 뒤에 붙는다.**
 */
async function openAndLogin() {
  if (!fs.existsSync(CHROME))
    throw new Error(`크롬을 못 찾았어요: ${CHROME}\n  크롬을 설치하시거나 CHROME_PATH 로 경로를 알려 주세요.`);

  say('\n═══ 구글 캘린더 연결 ═══\n');

  const reused = await portAlive();
  if (reused) {
    say('  이미 열려 있는 크롬 창을 씁니다.');
  } else {
    fs.mkdirSync(PROFILE, { recursive: true });
    const proc = spawn(CHROME, [
      `--remote-debugging-port=${PORT}`,
      `--user-data-dir=${PROFILE}`,
      '--no-first-run', '--no-default-browser-check', '--lang=ko-KR',
      'https://accounts.google.com/',
    ], { detached: true, stdio: 'ignore' });
    proc.unref();
    for (let i = 0; i < 40 && !(await portAlive()); i++) await sleep(500);
    if (!await portAlive()) throw new Error('크롬을 띄우지 못했어요.');
  }

  // 화면 문구와 실제 실행 경로가 어긋나면 안 된다 — 새로 띄웠을 때만 "열렸어요"라고 말한다
  if (!reused) {
    say('  크롬 창이 열렸어요. **그 창에서 구글 로그인만 해주세요.**');
    say('  평소 쓰시는 크롬과 별개인 창이라 기존 로그인·북마크는 건드리지 않아요.');
  } else {
    say('  그 창에서 구글에 로그인돼 있어야 해요. 아직이면 지금 로그인해 주세요.');
  }
  say('  로그인하시면 나머지는 제가 알아서 진행할게요.\n');

  const browser = await chromium.connectOverCDP(`http://127.0.0.1:${PORT}`);
  const ctx = browser.contexts()[0];
  const page = ctx.pages().at(-1) ?? await ctx.newPage();
  const d = driver(page);

  for (let i = 0; i < 150; i++) {                 // 최대 10분
    const acct = await page.evaluate(() => {
      const b = [...document.querySelectorAll('button,a')].find((e) => /계정:|Account:/.test(e.getAttribute('aria-label') || ''));
      const m = (b?.getAttribute('aria-label') || '').match(/[\w.+-]+@[\w.-]+\.\w+/);
      return m ? m[0] : null;
    }).catch(() => null);
    if (acct) return { browser, ctx, d, account: acct };

    // 로그인이 끝났는지 보려면 콘솔로 한 번씩 데려가 본다 (로그인 전이면 다시 로그인 화면으로 튕긴다)
    if (i % 5 === 4) await d.go('https://console.cloud.google.com/home/dashboard?hl=ko', 5000).catch(() => {});
    if (i % 8 === 7) say(`  · 로그인 기다리는 중… (${Math.round((i + 1) * 4 / 60)}분)`);
    await sleep(4000);
  }
  await browser.close();
  throw new Error('로그인이 확인되지 않았어요. 창에서 로그인하신 뒤 다시 실행해 주세요.');
}

/* ─────────────────── 자동: 구글 클라우드 준비 ─────────────────── */

async function setupCloud(d, account) {
  /* ① 프로젝트 — 이름을 넣으면 ID 는 구글이 정한다. **화면에 뜬 ID 를 읽어서** 쓴다 */
  if (!S.project_id) {
    await d.go('https://console.cloud.google.com/projectcreate?hl=ko');

    // 이 입력칸은 formcontrolname·aria-label 이 없다. 연결된 <label> 로 잡는다
    const nameBox = d.page.getByRole('textbox', { name: /프로젝트 이름|Project name/ }).first();
    if (!await nameBox.count()) throw new Error('프로젝트 이름 칸을 못 찾았어요.');
    const wanted = `bizpartner-${new Date().toISOString().slice(2, 10).replace(/-/g, '')}${crypto.randomBytes(2).toString('hex')}`;
    await nameBox.fill(wanted);                    // fill 은 기존 기본값을 지우고 넣는다
    await d.page.waitForTimeout(1500);

    const shown = (await d.text()).match(/프로젝트 ID[:\s]*([a-z][a-z0-9-]{4,29})/)?.[1]
               ?? (await d.text()).match(/Project ID[:\s]*([a-z][a-z0-9-]{4,29})/)?.[1]
               ?? wanted;
    await d.tap(/^만들기$|^CREATE$/, { wait: 15000 });

    // "만들었다"와 "쓸 수 있다"는 다르다 — 콘솔이 이 프로젝트를 인식할 때까지 확인한다
    const live = await d.until(`https://console.cloud.google.com/home/dashboard?hl=ko&project=${shown}`,
      (t, u) => u.includes(shown) && !/프로젝트 선택|Select a project|찾을 수 없|not found/.test(t),
      { what: '프로젝트가 준비되기를' });
    if (!live) throw new Error(`프로젝트가 만들어지지 않았어요 (${shown}).`);

    S = { ...S, project_id: shown, account }; save(S);
  }
  ok('① 구글 클라우드 프로젝트', S.project_id);

  /* ② 캘린더 API */
  const LIB = `https://console.cloud.google.com/apis/library/calendar-json.googleapis.com?hl=ko&project=${S.project_id}`;
  // 켜지면 라이브러리 화면의 버튼이 "사용" → **"관리"** 로 바뀐다. 이게 가장 확실한 신호다(실측).
  // 상세 화면으로 넘어간 경우엔 사용 중지·측정항목·할당량이 보인다.
  const isOn = (t) => /(^|\n)\s*(관리|Manage)\s*(\n|$)/.test(t)
                   || /사용 중지|측정항목|할당량|사용자 인증 정보 만들기|Disable|Metrics|Quotas/.test(t);
  if (!S.api_enabled) {
    if (!await d.until(LIB, (t) => !/프로젝트 선택|Select a project/.test(t), { what: 'API 화면이 열리기를' }))
      throw new Error('캘린더 API 화면에 프로젝트가 잡히지 않았어요.');
    if (!isOn(await d.text())) {
      if (!await d.tap(/^사용$|^ENABLE$/, { wait: 12000 })) throw new Error('"사용" 버튼을 못 찾았어요.');
      if (!await d.until(LIB, isOn, { what: 'API 가 켜지기를' })) throw new Error('캘린더 API 가 켜지지 않았어요.');
    }
    S = { ...S, api_enabled: true }; save(S);
  }
  ok('② 캘린더 API 켜기');

  /* ③ 동의 화면 — 4단계 마법사 */
  const BRAND = `https://console.cloud.google.com/auth/branding?hl=ko&project=${S.project_id}`;
  if (!S.consent_done) {
    await d.go(BRAND);
    if (/시작하기|Get started/.test(await d.text())) await d.tap(/^시작하기$|^GET STARTED$/, { wait: 8000 });

    await d.page.locator('input[formcontrolname="displayName"]').first().fill(APP_NAME);
    await d.page.waitForTimeout(900);

    // 지원 이메일은 드롭다운 — 열고 오버레이 안에서 고른다
    const sel = await d.page.evaluate(() => {
      const e = document.querySelector('cfc-select,[formcontrolname="userSupportEmail"]');
      if (!e) return null;
      const b = e.getBoundingClientRect();
      return { x: Math.round(b.x + b.width / 2), y: Math.round(b.y + b.height / 2) };
    });
    if (sel) { await d.page.mouse.click(sel.x, sel.y); await d.page.waitForTimeout(2400); await d.pickOption(/@/); }
    await d.tap(/^다음$|^NEXT$/, { wait: 4000 });

    if (!await d.radio(/외부|External/)) throw new Error('사용자 유형(외부)을 고르지 못했어요.');
    await d.page.waitForTimeout(1800);
    await d.tap(/^다음$|^NEXT$/, { wait: 4000 });

    const mail = await d.firstInput(150);
    if (mail) { await d.page.mouse.click(mail.x, mail.y); await d.page.keyboard.type(account); await d.page.waitForTimeout(1200); }
    await d.tap(/^다음$|^NEXT$/, { wait: 4500 });

    if (!await d.checkbox()) throw new Error('정책 동의 체크를 못 했어요.');
    await d.page.waitForTimeout(1500);
    await d.tap(/^만들기$|^CREATE$/, { wait: 14000 });

    await d.go(BRAND);
    if (/아직 구성되지 않음|not configured/.test(await d.text())) throw new Error('동의 화면이 구성되지 않았어요.');
    S = { ...S, consent_done: true }; save(S);
  }
  ok('③ 동의 화면 구성');

  /* ④ 앱 열쇠 — 비밀번호는 만든 직후 화면에 딱 한 번만 뜬다 */
  if (!S.client_secret) {
    await d.go(`https://console.cloud.google.com/auth/clients/create?hl=ko&project=${S.project_id}`);

    const type = await d.page.evaluate(() => {
      const e = [...document.querySelectorAll('cfc-select,mat-select,[role="combobox"]')]
        .find((x) => { const b = x.getBoundingClientRect(); return b.y > 150 && b.width > 100; });
      if (!e) return null;
      const b = e.getBoundingClientRect();
      return { x: Math.round(b.x + b.width / 2), y: Math.round(b.y + b.height / 2) };
    });
    if (!type) throw new Error('애플리케이션 유형 선택칸을 못 찾았어요.');
    await d.page.mouse.click(type.x, type.y);
    await d.page.waitForTimeout(2400);
    if (!await d.pickOption(/데스크톱|Desktop/)) throw new Error('"데스크톱 앱"을 고르지 못했어요.');

    const nameIn = await d.firstInput(150);
    if (nameIn) {
      await d.page.mouse.click(nameIn.x, nameIn.y);
      await d.page.keyboard.press('Meta+A');
      await d.page.keyboard.type('biz-partner-desktop');
      await d.page.waitForTimeout(1000);
    }
    await d.tap(/^만들기$|^CREATE$/, { wait: 9000 });

    const shown = await d.text();
    const id  = shown.match(/[0-9]{10,}-[a-z0-9]+\.apps\.googleusercontent\.com/)?.[0];
    const sec = shown.match(/GOCSPX-[A-Za-z0-9_-]{10,}/)?.[0];
    if (!id || !sec) throw new Error('앱 열쇠를 화면에서 못 읽었어요. 창을 닫지 마시고 다시 실행해 주세요.');
    S = { ...S, client_id: id, client_secret: sec }; save(S);
  }
  ok('④ 앱 열쇠 발급', `비밀번호 ${S.client_secret.length}자 확보`);

  /* ⑤ 앱 게시 — 안 하면 연결이 7일마다 끊긴다 */
  if (!S.published) {
    await d.go(`https://console.cloud.google.com/auth/audience?hl=ko&project=${S.project_id}`);
    await d.tap(/^앱 게시$|^PUBLISH APP$/, { wait: 5000 });
    await d.tap(/^확인$|^CONFIRM$/, { wait: 9000 });
    if (!/프로덕션 단계|테스트로 돌아가기|In production/.test(await d.text()))
      throw new Error('앱 게시가 확인되지 않았어요.');
    S = { ...S, published: true }; save(S);
  }
  ok('⑤ 앱 게시', '7일마다 끊기는 문제 없음');
}

/* ─────────────────── 자동: 캘린더 권한 승인 ─────────────────── */

async function grantAccess(ctx) {
  if (S.refresh_token) { ok('⑥ 캘린더 권한 승인', '이미 승인돼 있어요'); return; }

  const verifier  = crypto.randomBytes(32).toString('base64url');
  const challenge = crypto.createHash('sha256').update(verifier).digest('base64url');

  let hand;
  const codeP = new Promise((r) => (hand = r));
  const srv = createServer((req, res) => {
    const code = new URL(req.url, 'http://127.0.0.1').searchParams.get('code');
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    res.end('<meta charset="utf-8"><h2 style="font-family:system-ui;padding:40px">연결됐어요. 이 창은 닫으셔도 됩니다.</h2>');
    if (code) hand(code);
  });
  await new Promise((r) => srv.listen(0, '127.0.0.1', r));
  const redirect = `http://127.0.0.1:${srv.address().port}`;

  const page = await ctx.newPage();
  const d = driver(page);
  await d.go('https://accounts.google.com/o/oauth2/v2/auth?' + new URLSearchParams({
    client_id: S.client_id, redirect_uri: redirect, response_type: 'code', scope: SCOPE,
    access_type: 'offline', prompt: 'consent', code_challenge: challenge, code_challenge_method: 'S256',
  }), 4000);

  /* 동의는 화면이 몇 개 나올지 정해져 있지 않다(계정 선택 · 미검증 경고 · 권한 승인).
     순서를 가정하지 않고 **매번 화면을 읽어 지금 눌러야 할 것**을 고른다. 문구는 전부 실측값이다. */
  const STEPS = [
    { name: '보안 경고 통과', when: /확인하지 않은 앱|확인되지 않은|hasn't verified/, act: async () => {
        if (!/고급 설정 숨기기|Hide Advanced/.test(await d.text())) await d.tap(/^고급$|^고급 설정$|^Advanced$/, { wait: 1500 });
        return d.tap(/\(으\)로 이동|안전하지 않음|Go to .*unsafe/, { wait: 4000 });
      } },
    { name: '계정 선택', when: /계정을 선택|Choose an account/, act: () => d.tap(/^[\w.+-]+@[\w.-]+\.\w+$/, { wait: 4500 }) },
    { name: '권한 승인', when: /액세스 요청|정보에 액세스|액세스하려고|액세스할 수 있는 항목|신뢰할 수 있는지|wants access/,
      act: () => d.tap(/^계속$|^허용$|^Continue$|^Allow$/, { wait: 4000 }) },
  ];

  let code = null;
  for (let round = 0; round < 18 && !code; round++) {
    code = await Promise.race([codeP, sleep(2500).then(() => null)]);
    if (code) break;
    const t = await d.text().catch(() => '');
    const step = STEPS.find((s) => s.when.test(t));
    if (step) { say(`       [${step.name}]`); await step.act(); }
    else await d.tap(/^계속$|^허용$|^Continue$|^Allow$/, { wait: 3000, quiet: true });
  }
  code = code ?? await Promise.race([codeP, sleep(10000).then(() => null)]);
  srv.close();
  await page.close().catch(() => {});
  if (!code) throw new Error('캘린더 권한 승인이 끝나지 않았어요. 열린 창에서 "계속"을 눌러 주세요.');

  const tk = await (await fetch('https://oauth2.googleapis.com/token', {
    method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ code, client_id: S.client_id, client_secret: S.client_secret,
      redirect_uri: redirect, grant_type: 'authorization_code', code_verifier: verifier }),
  })).json();
  if (!tk.refresh_token) throw new Error(`연결 열쇠를 못 받았어요: ${tk.error ?? ''} ${tk.error_description ?? ''}`);

  S = { ...S, refresh_token: tk.refresh_token, scope: SCOPE, connected_at: new Date().toISOString() };
  save(S);
  ok('⑥ 캘린더 권한 승인', '연결 열쇠 저장');
}

/* ─────────────────── 연결 확인 (읽기·쓰기 왕복) ─────────────────── */

async function verify() {
  if (!S.refresh_token) { bad('연결 열쇠 없음', '먼저 연결부터 해주세요'); return false; }

  const r = await (await fetch('https://oauth2.googleapis.com/token', {
    method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ client_id: S.client_id, client_secret: S.client_secret,
      refresh_token: S.refresh_token, grant_type: 'refresh_token' }),
  })).json();
  if (!r.access_token) { bad('출입증 갱신', `${r.error ?? ''} ${r.error_description ?? ''}`); return false; }
  ok('⑦-1 저장된 열쇠로 출입증 재발급');

  const api = (p, init = {}) => fetch('https://www.googleapis.com/calendar/v3' + p, {
    ...init, headers: { Authorization: `Bearer ${r.access_token}`, 'Content-Type': 'application/json', ...(init.headers ?? {}) } });

  const list = await (await api(`/calendars/primary/events?timeMin=${new Date().toISOString()}&maxResults=5&singleEvents=true&orderBy=startTime`)).json();
  if (!Array.isArray(list.items)) { bad('⑦-2 캘린더 읽기', list.error?.message ?? '알 수 없음'); return false; }
  ok('⑦-2 캘린더 읽기', `다가오는 일정 ${list.items.length}건`);
  list.items.slice(0, 3).forEach((e) =>
    say(`         · ${(e.start?.dateTime ?? e.start?.date ?? '').slice(0, 16)}  ${(e.summary ?? '(제목 없음)').slice(0, 32)}`));

  // 쓰기는 **넣고 → 되읽고 → 지운다**. 넣기만 하고 성공이라 하면 조용한 실패를 놓친다
  const st = new Date(Date.now() + 86400000); st.setMinutes(0, 0, 0);
  const made = await (await api('/calendars/primary/events', { method: 'POST', body: JSON.stringify({
    summary: '[연결 확인] 파트너가 만든 일정', description: '연결되는지 보려고 만든 일정이에요. 바로 지웁니다.',
    start: { dateTime: st.toISOString(), timeZone: 'Asia/Seoul' },
    end:   { dateTime: new Date(st.getTime() + 3600000).toISOString(), timeZone: 'Asia/Seoul' },
  }) })).json();
  if (!made.id) { bad('⑦-3 캘린더 쓰기', made.error?.message ?? '알 수 없음'); return false; }

  const back = await (await api(`/calendars/primary/events/${made.id}`)).json();
  const good = back.id === made.id;
  await api(`/calendars/primary/events/${made.id}`, { method: 'DELETE' });
  good ? ok('⑦-3 캘린더 쓰기', '넣고 · 되읽고 · 지웠어요') : bad('⑦-3 캘린더 쓰기', '되읽기 실패');
  return good;
}

/* ─────────────────── 실행 ─────────────────── */

const DONE = [['project_id', '프로젝트'], ['api_enabled', '캘린더 API'], ['consent_done', '동의 화면'],
              ['client_secret', '앱 열쇠'], ['published', '앱 게시'], ['refresh_token', '캘린더 권한']];

if (process.argv.includes('--show')) {
  say('\n═══ 지금 어디까지 됐나 ═══');
  DONE.forEach(([k, label]) => say(`  ${S[k] ? '✅' : '⬜️'} ${label}`));
  say(`\n  열쇠 파일: ${fs.existsSync(KEYFILE) ? KEYFILE : '(아직 없음)'}`);
  process.exit(0);
}

if (process.argv.includes('--verify')) {
  say('\n═══ 연결 확인 ═══');
  process.exit(await verify() ? 0 : 1);
}

let browser = null;
try {
  const opened = await openAndLogin();
  browser = opened.browser;
  say(`\n  로그인 확인했어요 — ${opened.account}`);
  say('  여기서부터는 제가 합니다.\n');

  await setupCloud(opened.d, opened.account);
  await grantAccess(opened.ctx);
  const good = await verify();

  say('\n═══ 결과 ═══');
  if (good) {
    say('  연결 끝났어요. 대표님이 하신 건 로그인 한 번뿐이에요.');
    say('  · 권한 범위 : 캘린더 일정 읽기·쓰기 (메일·연락처는 안 봐요)');
    say('  · 열쇠 위치 : state/google-calendar.json  — 이 폴더 밖으로 나가지 않아요');
    say('  · 이제 텔레그램에서 "내일 일정 브리핑해줘" 라고 해보세요.');
  } else {
    say('  마지막 확인에서 걸렸어요. 다시 실행하면 남은 단계부터 이어서 합니다.');
  }
  await browser.close();
  process.exit(good ? 0 : 1);
} catch (e) {
  bad('막혔어요', e.message);
  say('\n  지금까지 된 것:');
  DONE.forEach(([k, label]) => say(`    ${S[k] ? '✅' : '⬜️'} ${label}`));
  say('\n  다시 실행하면 **끝난 단계는 건너뛰고** 막힌 곳부터 이어서 해요:');
  say('    node tools/google-connect/connect.mjs');
  say('  계속 같은 곳에서 막히면 docs/SETUP_CALENDAR.md 의 손으로 하는 절차를 보세요.');
  await browser?.close().catch(() => {});
  process.exit(1);
}
