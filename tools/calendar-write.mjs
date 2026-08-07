// 구글 캘린더 **쓰기** — 파트너가 일정을 직접 만들고·고치고·지우는 창구.
//
// 왜 있나 (대표 판단 2026-08-07):
//   "일정도 당연히 비즈니스 파트너가 조율하고 바꾸고 하는 건데 왜 안 된다고 하는 건지 모르겠어."
//   그동안 안 된 건 설계가 아니라 **도구 공백**이었다 — `curl` 은 권한 거부 목록에 있고,
//   `tools/calendar-read.mjs` 는 스스로 "쓰기는 하지 않는다"고 적은 읽기 전용이라,
//   브리핑이 "만들기·고치기·지우기는 화면(대시보드)만 한다"고 고지하고 있었다.
//
//   **쓰기 경계는 안 넓어진다.** 구글 캘린더는 우리 상태 파일이 아니라 외부 시스템이고,
//   `CLAUDE.md` 가 이미 명시적 예외로 빼 뒀다(대시보드가 화면에서 직접 쓰는 근거와 같다).
//   `state/*.json` 은 여전히 에이전트·대시보드 각자의 규약대로만 쓴다.
//
// **대시보드와 같은 규약으로 쓴다.** 아래는 `dashboard-server.py` 의
// `_gcal_body` · `_writable_or_die` · `gcal_create/update/delete` 를 그대로 옮긴 것이다:
//   · 구글에서 **체크를 켜 둔 캘린더만**(`selected`) 대상 — 목록이 막히면 기본 캘린더 하나로
//   · 쓰기 전에 `accessRole` 을 확인한다 — `owner`/`writer` 가 아니면 **우리가 먼저** 막는다
//   · 지우기 전에 원본을 돌려준다 — 되돌리기(undo)에 쓴다
//   이 둘이 갈라지면 화면과 파트너가 서로 다른 결과를 만든다 — 고칠 때 함께 고칠 것.
//
// ★★ 종일(all-day) `end` 규약 — 여기서 실사고가 났다(2026-08-05, 4건):
//   구글의 `end` 는 **배타적(exclusive)** 이다. "8/10 하루짜리" 종일 일정의 end 는 **8/11**.
//   사람이 말하는 "8월 10일 하루"는 **포함형(inclusive)** 이다.
//   현 계약은 **부르는 쪽이 변환하고 서버(`_gcal_body`)는 통과시킨다** —
//   화면은 `dashboard-app/src/lib/calendar.ts` 의 `inclusiveEndToGcal`/`gcalEndToInclusive` 로 한다.
//   → **이 파일도 부르는 쪽이다.** CLI 인자는 포함형(사람 말)으로 받고, 구글로 보낼 때 **+1일** 한다.
//     `--end` 를 안 주면 하루짜리로 본다(= start 와 같은 날 → 구글에는 start+1).
//     되읽어 사람에게 보여줄 때는 반대로 **-1일** 해서 포함형으로 좁힌다.
//   그리고 **종일 날짜에 `new Date(...).toISOString()` 을 쓰지 않는다** —
//   KST 에서 하루 앞으로 밀리고, 재저장할수록 누적된다(실측). 날짜 산술은 UTC 로만 한다.
//
//   bun tools/calendar-write.mjs create --title "팀 미팅" --start "2026-08-10T15:00" --end "2026-08-10T16:00"
//   bun tools/calendar-write.mjs create --title "워크숍" --start "2026-08-10" --all-day
//   bun tools/calendar-write.mjs update --id <일정id> --start "2026-08-10T16:00" --end "2026-08-10T17:00"
//   bun tools/calendar-write.mjs delete --id <일정id>
//   bun tools/calendar-write.mjs undo --json '<delete 가 뱉은 JSON>'
//
//   고칠·지울 일정의 `id` 는 `bun tools/calendar-read.mjs --json` 으로 먼저 확인한다.

import { readFileSync } from "node:fs";
import path from "node:path";

const ROOT = process.cwd();
const KEY = path.join(ROOT, "state", "google-calendar.json");
const TZ = "Asia/Seoul"; // dashboard-server.py 의 _gcal_body 와 같아야 한다

const argv = process.argv.slice(2);
const cmd = argv[0];

/** `--이름 값` 을 읽는다. 값이 없으면 undefined */
function opt(name) {
  const i = argv.indexOf("--" + name);
  return i >= 0 ? argv[i + 1] : undefined;
}
function flag(name) {
  return argv.includes("--" + name);
}

const USAGE = `구글 캘린더에 일정을 만들고·고치고·지웁니다.

  bun tools/calendar-write.mjs create --title "제목" --start <시작> [--end <끝>] [--all-day] [--calendar <캘린더id>]
  bun tools/calendar-write.mjs update --id <일정id> [--title "제목"] [--start <시작>] [--end <끝>] [--all-day] [--calendar <캘린더id>]
  bun tools/calendar-write.mjs delete --id <일정id> [--calendar <캘린더id>]
  bun tools/calendar-write.mjs undo   --json '<delete 가 돌려준 JSON>'

시각 쓰는 법
  시간이 있는 일정 : "2026-08-10T15:00"  (한국 시각으로 읽습니다. "2026-08-10 15:00" 도 됩니다)
  종일 일정        : "2026-08-10" + --all-day
  종일 --end 는 마지막 날을 그대로 적습니다 — "8/10 하루"는 --start 2026-08-10 (--end 생략).
  8/10~8/12 사흘이면 --start 2026-08-10 --end 2026-08-12.

그 밖에
  --json      결과를 기계용 JSON 으로 출력 (undo 에서는 되돌릴 일정 JSON 을 받는 자리)
  --out-json  undo 의 결과를 JSON 으로 출력

  고치거나 지울 일정의 id 는 먼저 이렇게 확인합니다:
    bun tools/calendar-read.mjs --json

종료 코드
  0 성공 · 1 사용법 오류 · 2 캘린더 미연결 · 3 연결 끊김 · 4 읽기 전용 캘린더 · 5 구글이 거절`;

if (!cmd || cmd === "--help" || cmd === "-h" || cmd === "help") {
  console.log(USAGE);
  process.exit(0);
}
if (!["create", "update", "delete", "undo"].includes(cmd)) {
  console.log(`'${cmd}' 는 모르는 명령이에요.\n\n` + USAGE);
  process.exit(1);
}

/** 실패는 **사용자 말로** 알리고 종료 코드로 구분한다 — 스킬이 다음 행동을 판단한다.
 *  2 = 연결 안 됨 · 3 = 연결이 끊김 · 4 = 읽기 전용 캘린더(우리가 막음) · 5 = 구글이 거절 */
function bail(code, msg) {
  console.log(msg);
  process.exit(code);
}

let key;
try {
  key = JSON.parse(readFileSync(KEY, "utf8"));
} catch {
  bail(2, "구글 캘린더가 연결돼 있지 않아요. (\"캘린더 연결\" 이라고 하시면 이어 드릴게요)");
}
if (!key?.refresh_token) bail(2, "구글 캘린더 연결 열쇠가 비어 있어요.");

/** access_token 은 1시간짜리다 — 매번 새로 받는다(이 스크립트는 한 번 돌고 끝난다) */
async function accessToken() {
  const res = await fetch("https://oauth2.googleapis.com/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      client_id: key.client_id,
      client_secret: key.client_secret,
      refresh_token: key.refresh_token,
      grant_type: "refresh_token",
    }),
  });
  const d = await res.json().catch(() => ({}));
  if (!d.access_token) {
    bail(3, "캘린더 연결이 끊겼어요. 구글에서 접근을 취소하셨을 수 있어요. (\"캘린더 연결\" 이라고 하시면 다시 이어 드릴게요)");
  }
  return d.access_token;
}

/** 구글 캘린더 API 호출. 실패는 status 를 실어 던진다 — 403 은 위에서 사람 말로 바꾼다 */
async function api(tok, p, method = "GET", body) {
  const res = await fetch("https://www.googleapis.com/calendar/v3" + p, {
    method,
    headers: {
      Authorization: "Bearer " + tok,
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  if (res.status === 204) return {}; // DELETE 는 본문이 없다
  if (!res.ok) {
    const d = await res.json().catch(() => ({}));
    const err = new Error(d?.error?.message || `구글이 요청을 거절했어요 (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return res.json().catch(() => ({}));
}

/** 쓸 수 있는 캘린더 목록. 규약은 dashboard-server.py 의 gcal_calendars() 와 같다 —
 *  **구글 사이드바에서 켜 둔 것만**, 목록 조회가 막히면 기본 캘린더 하나로 좁힌다 */
let _cals = null;
async function calendars(tok) {
  if (_cals) return _cals;
  try {
    const items = (await api(tok, "/users/me/calendarList")).items || [];
    const on = items.filter((c) => c.selected);
    _cals = on.length ? on : items.filter((c) => c.primary);
  } catch {
    _cals = [{ id: "primary", summary: "", accessRole: "owner", primary: true }];
  }
  return _cals;
}

/** 읽기 전용 캘린더(공휴일·공유받은 것)에 쓰려 하면 **구글의 403 을 기다리지 않고** 우리가 막는다.
 *  문구는 dashboard-server.py 의 _writable_or_die 와 **같은 말**이어야 한다 */
async function writableOrDie(tok, cid) {
  if (!cid || cid === "primary") return;
  for (const c of await calendars(tok)) {
    if (c.id === cid) {
      if (!["owner", "writer"].includes(c.accessRole)) {
        bail(4, "이 캘린더는 읽기 전용이라 여기서는 못 고쳐요");
      }
      return;
    }
  }
}

async function calName(tok, cid) {
  const c = (await calendars(tok)).find((x) => x.id === (cid || "primary"));
  return c?.summary || "";
}

// ── 날짜·시각 ────────────────────────────────────────────────────────────────

const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;
const NAIVE = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(:\d{2})?$/;

/** 'YYYY-MM-DD' 를 n일 옮긴다. **UTC 산술만** 쓴다 — 로컬 시간대를 타면 그게 곧 하루 밀림이다.
 *  (dashboard-app/src/lib/calendar.ts 의 shiftDate 와 같은 구현) */
function shiftDate(ymd, n) {
  const d = new Date(`${String(ymd).slice(0, 10)}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

/** 시각이 있는 일정의 한쪽. 오프셋이 없는 값은 **그대로 보내고 timeZone 을 함께 실어**
 *  구글이 한국 시각으로 읽게 한다 — 여기서 Date 로 바꾸면 시간대 사고가 난다 */
function timeField(v) {
  const s = String(v).trim();
  const m = NAIVE.exec(s);
  if (m) return { dateTime: `${m[1]}T${m[2]}${m[3] || ":00"}`, timeZone: TZ };
  return { dateTime: s, timeZone: TZ }; // 이미 오프셋(+09:00·Z)이 붙어 있으면 구글이 그걸 쓴다
}

/** 구글 시각 문자열 → [YYYY-MM-DD, HH:MM]. **로컬로 변환한 뒤** 쪼갠다
 *  (문자열을 그냥 자르면 UTC 를 한국 시각처럼 보여준다 — calendar-read.mjs 와 같은 규약) */
function splitLocal(s) {
  if (!s) return ["", ""];
  const d = new Date(s);
  if (Number.isNaN(d.getTime())) return [String(s).slice(0, 10), ""];
  const p = (n) => String(n).padStart(2, "0");
  return [
    `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`,
    `${p(d.getHours())}:${p(d.getMinutes())}`,
  ];
}

const WD = ["일", "월", "화", "수", "목", "금", "토"];
function ymd(s) {
  if (!s) return "";
  const d = new Date(`${s}T00:00:00`);
  return Number.isNaN(d.getTime()) ? s : `${s}(${WD[d.getDay()]})`;
}

// ── 본문 조립 ────────────────────────────────────────────────────────────────

/** CLI 인자 → 구글 본문. `_gcal_body` 와 같은 모양이되 **종일 end 를 +1 한다**.
 *
 *  ★ 여기가 포함형(사람 말) → 배타적(구글) 변환이 일어나는 **유일한 지점**이다.
 *    서버 `_gcal_body` 는 변환을 안 한다(부르는 쪽이 이미 했다고 본다).
 *
 *  @param opts.endIsExclusive  이미 구글 규약인 값을 그대로 보낼 때(undo). 그때는 +1 하지 않는다.
 */
function buildBody({ title, start, end, allDay }, opts = {}) {
  const b = {};
  if (title !== undefined) b.summary = title;
  if (allDay) {
    if (start) b.start = { date: String(start).slice(0, 10) };
    // --end 를 안 주면 **하루짜리**로 본다: 마지막 날 = 시작일
    const lastDay = end ? String(end).slice(0, 10) : start ? String(start).slice(0, 10) : "";
    if (lastDay) {
      b.end = { date: opts.endIsExclusive ? lastDay : shiftDate(lastDay, 1) };
    }
  } else {
    if (start) b.start = timeField(start);
    if (end) b.end = timeField(end);
  }
  return b;
}

/** 구글이 돌려준 일정 → 사람이 읽는 한 줄. 종일 end 는 **-1 해서 포함형으로 좁힌다** */
function describe(g) {
  const st = g.start || {};
  const en = g.end || {};
  const title = g.summary || "(제목 없음)";
  if (!st.dateTime) {
    const s = String(st.date || "").slice(0, 10);
    const last = en.date ? shiftDate(en.date, -1) : s; // 구글(배타적) → 사람(포함형)
    const span = !last || last === s ? ymd(s) : `${ymd(s)} ~ ${ymd(last)}`;
    return `${span} 종일 · ${title}`;
  }
  const [sd, stime] = splitLocal(st.dateTime);
  const [ed, etime] = splitLocal(en.dateTime);
  const tail = etime ? (ed && ed !== sd ? ` ~ ${ymd(ed)} ${etime}` : `~${etime}`) : "";
  return `${ymd(sd)} ${stime}${tail} · ${title}`;
}

/** 되돌리기·보고용 요약 모양. dashboard-server.py 의 _gcal_to_event 와 **같은 필드**다.
 *  `start`/`end` 는 **구글이 준 문자열 그대로** 둔다 — 종일 end 는 배타적인 채로 남는다.
 *  그래야 undo 가 손대지 않고 그대로 되돌릴 수 있다(날짜를 다시 만지면 하루가 어긋난다). */
function toEvent(g, cid, name) {
  const st = g.start || {};
  const en = g.end || {};
  return {
    id: g.id || "",
    title: g.summary || "(제목 없음)",
    start: st.dateTime || st.date || "",
    end: en.dateTime || en.date || "",
    allDay: !st.dateTime,
    calendarId: cid || "primary",
    calendarName: name || "",
  };
}

function esc(s) {
  return String(s).replace(/'/g, `'\\''`); // 셸에 그대로 붙여넣을 수 있게
}

// ── 실행 ─────────────────────────────────────────────────────────────────────

const asJson = cmd !== "undo" && flag("json");
const outJson = flag("out-json");
const cid = opt("calendar") || "primary";

const tok = await accessToken();

/** 구글이 403 을 주면 읽기 전용 캘린더인 경우가 대부분이다 — 사람 말로 바꿔서 알린다 */
function onGoogleError(e) {
  if (e?.status === 403) {
    bail(4, `이 캘린더는 읽기 전용이라 여기서는 못 고쳐요 (구글: ${e.message})`);
  }
  bail(5, e?.message || "구글이 요청을 거절했어요");
}

const CAL_PATH = (c) => "/calendars/" + encodeURIComponent(c || "primary");

try {
  if (cmd === "create") {
    const title = opt("title");
    const start = opt("start");
    if (!title || !start) {
      bail(1, "제목(--title)과 시작(--start)이 필요해요.\n\n" + USAGE);
    }
    // --all-day 를 안 붙였어도 시작이 날짜뿐이면 종일로 본다
    const allDay = flag("all-day") || DATE_ONLY.test(String(start).trim());
    await writableOrDie(tok, cid);
    const body = buildBody({ title, start, end: opt("end"), allDay });
    if (!body.end) bail(1, "끝나는 시각(--end)이 필요해요. 종일 일정이면 --all-day 를 붙여 주세요.");
    const g = await api(tok, CAL_PATH(cid) + "/events", "POST", body);
    const name = await calName(tok, cid);
    if (asJson) {
      console.log(JSON.stringify({ ok: true, action: "create", event: toEvent(g, cid, name) }, null, 2));
    } else {
      console.log(`일정을 만들었어요 — ${describe(g)}${name ? `  [${name}]` : ""}  (id: ${g.id})`);
    }
  }

  if (cmd === "update") {
    const id = opt("id");
    if (!id) bail(1, "어느 일정인지 알려주세요 (--id). `bun tools/calendar-read.mjs --json` 으로 확인할 수 있어요.");
    const start = opt("start");
    const end = opt("end");
    const title = opt("title");
    if (title === undefined && !start && !end) {
      bail(1, "바꿀 내용이 없어요 — --title · --start · --end 중 하나는 주세요.");
    }
    const allDay = flag("all-day") || (start ? DATE_ONLY.test(String(start).trim()) : false);
    await writableOrDie(tok, cid);
    // PATCH 는 준 것만 바꾼다 — 시각을 안 주면 원래 시각이 그대로 남는다
    const body = buildBody({ title, start, end, allDay });
    const g = await api(tok, CAL_PATH(cid) + "/events/" + encodeURIComponent(id), "PATCH", body);
    const name = await calName(tok, cid);
    if (asJson) {
      console.log(JSON.stringify({ ok: true, action: "update", event: toEvent(g, cid, name) }, null, 2));
    } else {
      console.log(`일정을 고쳤어요 — ${describe(g)}${name ? `  [${name}]` : ""}  (id: ${g.id})`);
    }
  }

  if (cmd === "delete") {
    const id = opt("id");
    if (!id) bail(1, "어느 일정인지 알려주세요 (--id). `bun tools/calendar-read.mjs --json` 으로 확인할 수 있어요.");
    await writableOrDie(tok, cid);
    const name = await calName(tok, cid);
    // **지우기 전에 원본을 읽어 둔다** — 되돌리기의 유일한 재료다(gcal_delete 와 같은 순서)
    let before = null;
    try {
      before = await api(tok, CAL_PATH(cid) + "/events/" + encodeURIComponent(id));
    } catch {
      before = null; // 원본을 못 읽어도 지우는 건 진행한다 — 다만 되돌리기는 안내하지 않는다
    }
    await api(tok, CAL_PATH(cid) + "/events/" + encodeURIComponent(id), "DELETE");
    const ev = before ? toEvent(before, cid, name) : null;
    if (asJson) {
      console.log(JSON.stringify({ ok: true, action: "delete", event: ev }, null, 2));
    } else {
      console.log(`일정을 지웠어요 — ${before ? describe(before) : id}${name ? `  [${name}]` : ""}`);
      if (ev) {
        console.log("되돌리려면 이 명령을 그대로 실행하세요 (JSON 은 구글 원본이라 날짜를 손대면 하루가 어긋나요):");
        console.log(`  bun tools/calendar-write.mjs undo --json '${esc(JSON.stringify(ev))}'`);
      }
    }
  }

  if (cmd === "undo") {
    const raw = opt("json") || opt("event");
    if (!raw) bail(1, "되돌릴 일정 JSON 이 필요해요 — delete 가 알려준 명령을 그대로 실행해 주세요.");
    let ev;
    try {
      ev = JSON.parse(raw);
    } catch {
      bail(1, "되돌릴 일정 JSON 을 읽지 못했어요. delete 가 알려준 문장을 그대로 붙여넣어 주세요.");
    }
    if (ev?.event) ev = ev.event; // {event:{…}} 로 감싸 온 것도 받는다
    if (!ev?.start) bail(1, "되돌릴 일정에 시작 시각이 없어요.");
    const target = opt("calendar") || ev.calendarId || "primary";
    await writableOrDie(tok, target);
    // ★ **변환하지 않는다.** 이 값들은 구글이 준 원본이라 종일 end 가 이미 배타적이다.
    //   여기서 다시 +1 하면 하루가 늘어난다(서버 undo 도 gcal_create 로 그대로 통과시킨다).
    const body = buildBody(
      { title: ev.title, start: ev.start, end: ev.end, allDay: !!ev.allDay },
      { endIsExclusive: true },
    );
    const g = await api(tok, CAL_PATH(target) + "/events", "POST", body);
    const name = await calName(tok, target);
    if (outJson) {
      console.log(JSON.stringify({ ok: true, action: "undo", event: toEvent(g, target, name) }, null, 2));
    } else {
      console.log(`되돌렸어요 — ${describe(g)}${name ? `  [${name}]` : ""}  (id: ${g.id})`);
    }
  }
} catch (e) {
  onGoogleError(e);
}
