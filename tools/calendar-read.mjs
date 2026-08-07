// 구글 캘린더 **읽기 전용** — 텔레그램 브리핑이 일정을 볼 수 있게 하는 창구.
//
// 왜 있나 (실사고 2026-08-05):
//   `daily-briefing` 스킬은 "구글 캘린더 실연동이 있으면 그것을 읽는다" 고 적어 두고
//   **실제로는 읽을 방법이 없었다.** 열쇠(state/google-calendar.json)에는 refresh_token 만
//   들어 있고, 실제 일정을 받으려면 구글 API 를 인증 호출해야 하는데 —
//     · `curl` 은 권한 거부 목록에 있고
//     · 호출용 스크립트도 허용 목록에 없었고
//     · 대시보드 서버는 결과를 메모리에만 두고 디스크에 안 남긴다
//   그래서 브리핑이 매번 "일정을 못 읽었습니다" 로 끝났다. 이 파일이 그 구멍을 메운다.
//
// **대시보드와 같은 일정을 보여준다.** 아래 규약은 `dashboard-server.py` 를 그대로 옮긴 것이다:
//   · 구글에서 **체크를 켜 둔 캘린더만**(`selected`) — 목록 조회가 막히면 기본 캘린더 하나로
//   · 한 캘린더가 실패해도 나머지는 보여준다
//   · 시각은 **로컬로 변환한 뒤** 쪼갠다 (문자열을 그냥 자르면 UTC 를 한국 시각처럼 보여준다)
//   이 둘이 갈라지면 화면과 브리핑이 다른 말을 하게 된다 — 고칠 때 함께 고칠 것.
//
// 쓰기는 하지 않는다. 만들기·고치기·지우기는 화면(브리지)만 한다.
//
//   bun tools/calendar-read.mjs              오늘부터 28일
//   bun tools/calendar-read.mjs --days 2     오늘·내일
//   bun tools/calendar-read.mjs --json       기계용 출력

import { readFileSync } from "node:fs";
import path from "node:path";

const ROOT = process.cwd();
const KEY = path.join(ROOT, "state", "google-calendar.json");
const WINDOW_DAYS = 28; // dashboard-server.py 의 CAL_WINDOW_DAYS 와 같아야 한다

const argv = process.argv.slice(2);
const asJson = argv.includes("--json");
const days = (() => {
  const i = argv.indexOf("--days");
  const n = i >= 0 ? Number(argv[i + 1]) : NaN;
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : WINDOW_DAYS;
})();

/** 실패는 **사용자 말로** 알리고 종료 코드로 구분한다 — 스킬이 폴백 여부를 판단한다.
 *  2 = 연결 안 됨(폴백으로 가라) · 3 = 연결은 됐는데 못 읽음(사실대로 말하라) */
function bail(code, msg) {
  console.log(msg);
  process.exit(code);
}

let key;
try {
  key = JSON.parse(readFileSync(KEY, "utf8"));
} catch {
  bail(2, "구글 캘린더가 연결돼 있지 않아요. (state/google-calendar.json 없음)");
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

async function api(tok, p) {
  const res = await fetch("https://www.googleapis.com/calendar/v3" + p, {
    headers: { Authorization: "Bearer " + tok },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body?.error?.message || `구글이 요청을 거절했어요 (${res.status})`);
  }
  return res.json();
}

/** 볼 캘린더 — 구글 사이드바에서 **켜 둔 것만**. 목록 조회가 막히면 기본 캘린더 하나로 좁힌다 */
async function calendars(tok) {
  try {
    const items = (await api(tok, "/users/me/calendarList")).items || [];
    const on = items.filter((c) => c.selected);
    return on.length ? on : items.filter((c) => c.primary);
  } catch {
    return [{ id: "primary", summary: "", accessRole: "owner", primary: true }];
  }
}

/** 구글 시각 문자열 → [YYYY-MM-DD, HH:MM|null]. **로컬로 변환한 뒤** 쪼갠다 */
function splitLocal(s, allDay) {
  if (!s) return [null, null];
  if (allDay || s.length === 10) return [s.slice(0, 10), null];
  const d = new Date(s);
  if (Number.isNaN(d.getTime())) return [s.slice(0, 10), null];
  const p = (n) => String(n).padStart(2, "0");
  return [`${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`, `${p(d.getHours())}:${p(d.getMinutes())}`];
}

const tok = await accessToken();
const now = new Date();
const from = new Date(now.getFullYear(), now.getMonth(), now.getDate());
const to = new Date(from.getTime() + (days + 1) * 864e5);

const q = new URLSearchParams({
  timeMin: from.toISOString(),
  timeMax: to.toISOString(),
  singleEvents: "true",
  orderBy: "startTime",
  maxResults: "500",
});

const out = [];
let failed = 0;
for (const c of await calendars(tok)) {
  try {
    const d = await api(tok, `/calendars/${encodeURIComponent(c.id)}/events?${q}`);
    for (const g of d.items || []) {
      const allDay = !g.start?.dateTime;
      const [date, time] = splitLocal(g.start?.dateTime || g.start?.date, allDay);
      if (!date) continue;
      out.push({
        date,
        time,
        title: g.summary || "(제목 없음)",
        allDay,
        calendar: c.summary || "",
        location: g.location || "",
      });
    }
  } catch {
    failed += 1; // 하나가 막혀도 나머지는 보여준다
  }
}
out.sort((a, b) => (a.date + (a.time || "99:99")).localeCompare(b.date + (b.time || "99:99")));

if (asJson) {
  console.log(JSON.stringify({ events: out, days, partial: failed > 0 }, null, 2));
  process.exit(0);
}

if (!out.length) {
  console.log(`앞으로 ${days}일 안에 등록된 일정이 없어요.`);
  process.exit(0);
}

const WD = ["일", "월", "화", "수", "목", "금", "토"];
let lastDate = "";
for (const e of out) {
  if (e.date !== lastDate) {
    const d = new Date(e.date + "T00:00:00");
    console.log(`\n${e.date} (${WD[d.getDay()]})`);
    lastDate = e.date;
  }
  const when = e.time ? e.time : "종일";
  const where = e.location ? `  @${e.location}` : "";
  const which = e.calendar ? `  [${e.calendar}]` : "";
  console.log(`  ${when.padEnd(5)} ${e.title}${where}${which}`);
}
if (failed) console.log(`\n(캘린더 ${failed}개는 읽지 못했어요 — 권한이 바뀌었을 수 있어요)`);
