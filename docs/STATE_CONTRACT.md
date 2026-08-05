# 상태 계약 v0 (STATE_CONTRACT)

메인 에이전트·project-team 스킬(**쓰기**)과 대시보드(**읽기**)가 공유하는 유일한 접점.
이 문서가 스키마의 **기준(SSOT)**이다. `CLAUDE.md`, `.claude/skills/project-team/SKILL.md`,
`dashboard/index.html` 은 모두 여기의 필드명·타입을 그대로 따른다.

---

## ① 위치

모든 상태 파일은 프로젝트의 `state/` 폴더에 있다.

```
state/
├── status.json         메인 에이전트 상태 (에이전트가 씀)
├── team.json           프로젝트 팀 진행현황 (project-team 스킬이 씀)
├── dashboard-data.js   위 둘을 래핑한 JS 파일 (파일 모드에서 대시보드가 읽는 유일한 파일)
├── automations.json    자동화 설정 (W22 — **대시보드/브리지가 씀**. 아래 ⑥)
├── activity.json       파트너 활동 내역 (W22 — **브리지가 씀**. 아래 ⑥)
├── calendar.json       캘린더 연동 설정 (W26 — **에이전트(setup-calendar)가 씀**. 아래 ⑦)
└── google-calendar.json  구글 OAuth 토큰 (W27 — **에이전트(setup-calendar 방법A)가 씀**. 아래 ⑦-1)
```

> `automations.json`·`activity.json` 은 **사업 상태가 아니라 사용자 환경 설정·실행 기록**이라
> 예외적으로 대시보드(브리지)가 쓴다. `status.json`·`team.json` 은 **여전히 에이전트만** 쓴다 —
> 이 경계를 넘지 않는다. (`run-api.sh` 가 자동화를 저장한 뒤 `status.json` 이 그대로인지 검사한다.)

## ② 형식

- `status.json`, `team.json` 은 **순수 JSON**.
- `dashboard-data.js` 는 두 JSON을 하나의 전역 객체로 감싼 **JS 래퍼**:

  ```js
  window.DASHBOARD_DATA = {
    "status":  { /* status.json 내용 그대로 (todos[] 의 open 항목 중 due<오늘 은 overdue 로 파생) */ },
    "team":    { /* team.json 내용 그대로 */ },
    "records": { /* refresh-dashboard.sh 가 notes/ 스캔으로 파생 주입 (아래 ⑤) */ }
  };
  ```

  - `status`·`team` 은 두 JSON을 **내용 그대로** 래핑한 것이다(원본 불변).
  - `records` 와 todos overdue 판정은 `refresh-dashboard.sh` 가 계산해 넣는 **파생값**이다.
  - `records.recent[]`·`records.projects[]` 항목에는 **`content`**(파일 내용, 파일당 16KB 상한 —
    초과분은 잘라내고 안내 문구)가 additive로 포함된다. 대시보드 내장 마크다운 뷰어가 이 값을
    렌더한다. (W21 이전에 있던 "파일로 이동" 앵커는 한 번도 동작하지 않아 걷어냈다 —
    본문 안 경로를 누르면 리딩 페인에서 이어 읽는다.)
  - 스캔 대상 확장자는 `.md` + 표 형식 `.csv`·`.xlsx`(첫 시트, 200행×24열 상한 — python 표준
    라이브러리 파싱, 실패 시 항목 유지·내용 생략). 표 파일의 contents 항목은 `content` 대신
    **`table`**(2차원 배열)을 갖고, 대시보드 뷰어가 표로 렌더한다.
  - **`records.contents`** — `{경로: {title, content}}` 맵으로 **스캔된 모든 노트·산출물**의 내용을
    담는다(뷰어가 할 일 출처·이슈·칸반 티켓 어디에서든 열리게 하는 단일 조회처). 최근 5건만
    임베드하던 초기 방식이 "할 일에서 뷰어가 안 열리는" 실결함을 낳아 전량 임베드로 확장됨.
  - **`roster[].content`** — 각 역할 md 전문(16KB 상한). "우리 팀" 역할 카드 클릭 상세(역할 소개·
    할 일·작동 시점)를 대시보드가 렌더하는 데 쓴다. content 없으면 카드 상세는 요약만.

    래퍼에만 존재하고 원본 JSON에는 쓰지 않는다. python3 가 없는 환경이면 이 파생은 생략되고
    (기본 래핑만) — 대시보드가 자체 계산으로 보완하므로 동작에 지장이 없다(하위호환).

- **왜 JS 래퍼인가 (역사·롤백):** 래퍼는 `file://` 로 열리는 구 대시보드를 위해 만들어졌다 —
  브라우저는 `file://` 에서 `fetch()` 로 JSON을 못 읽어(CORS) `<script src>` 형태가 유일한
  통로였다. ADR-0008 이후 **새 대시보드는 래퍼를 읽지 않지만**, 롤백용 구 대시보드
  초기 상태 생성을 위해 래퍼와 이 파생 절차는 그대로 유지한다.

## ③ 쓰기 — 누가, 언제

| 파일 · 필드 | 쓰는 주체 | 쓰는 시점 |
|---|---|---|
| `status.json` (todos 외) | **메인 에이전트** | 세션 시작 시 / 기본 업무(기록·브리핑·초안·이슈·리마인더) 실행 시마다 |
| `status.json.todos[]` | **reminder · issue-tracker 스킬** (메인 에이전트가 실행) **+ project-team 스킬(이월 항목만)** | reminder: 등록 시 `open` 추가 / 완료 시 `done`. issue-tracker: 개설 시 `open` 추가 / 해결 시 `done`. project-team: 완료 3분류에서 이월 확정 시 `open` 추가 |
| `team.json` (기존 필드) | **project-team 스킬** | 팀 구성 시, 그리고 **단계 전환마다** |
| `team.json.activeProject.{health,progress,outputs}` | **project-team 스킬** | health: 팀 구성 시 `"순항"`, 단계 전환·리스크 시 재평가 / progress: 단계 전환마다(단계 파생) / outputs: 산출물 저장 시마다 추가 |
| `team.json.staff[]` | **project-team 스킬** | 채용 시 생성(`staff-guide.md` 규약 — W26: 필요 역량만 성장형 채용), 대표의 변경 지시·회고 경험 누적 시 수정. 프로젝트 종료와 무관하게 유지(재직) |
| `team.json.tickets[]` | **project-team 스킬** (W26 신설) | 편성 시 멤버별 발행, 착수/막힘/완료 시 status·outputs 갱신. 새 프로젝트 시작 시 직전 프로젝트의 done 티켓 정리 |
| `state/calendar.json` | **에이전트(setup-calendar 스킬)** (W26 신설) | 구글 캘린더 비밀 주소(ICS)를 연결할 때. **대시보드는 읽기만 한다** |
| `dashboard-data.js` (`status`·`team` 래핑) | 위 JSON을 바꾼 쪽 | **JSON을 갱신할 때마다 반드시 함께 재생성** |
| `automations.json` | **대시보드(브리지)** — 예외 | 사용자가 자동화를 켜거나 시각·문구를 바꿀 때 (`POST /api/automations`) |
| `activity.json` | **브리지** — 예외 | 자동화가 돌 때, 그리고 화면에서 일을 시킬 때(요청형도 남긴다 — "파트너가 어떻게 움직이는지"는 자율형만의 이야기가 아니다) |
| `dashboard-data.js` 파생 (`records`, todos overdue 재계산) | **`refresh-dashboard.sh`** | 래핑할 때마다 자동 — `notes/` 스캔 결과와 overdue 판정을 주입(파생, 원본 JSON은 불변) |

> **철칙:** `status.json` 또는 `team.json` 을 바꾸면 **같은 동작 안에서 `./refresh-dashboard.sh`
> 를 실행**해 `dashboard-data.js` 를 재생성한다. 둘이 어긋나면 대시보드가 낡은 값을 보여준다.

재생성은 **반드시 스크립트로** 한다 — 래퍼를 손으로 합성하면 어긋난다(실제 QA 주행에서
수동 재생성이 `lastActiveAt` 드리프트를 일으킨 사례가 관찰되어 스크립트로 강제한다).
`refresh-dashboard.sh` 는 두 JSON의 유효성을 검사한 뒤 내용 그대로 래핑한다.
install.sh 도 마지막 단계에서 같은 스크립트를 호출한다.

## ④ 읽기

대시보드 실행은 **서버 모드 단일 경로**다(ADR-0008 — 종전 "파일 모드는 항상 유지한다"를 대체).
쓰기 방향은 그대로다(에이전트 → 파일).

| 모드 | 어떻게 여나 | 무엇을 읽나 |
|---|---|---|
| **서버 모드 (유일)** | `./dashboard.sh` (`http://127.0.0.1`) | 브리지 API 로 **파일을 직접**: `/api/state`(status·team) · `/api/records`(notes/ 서버 스캔) · `/api/note`(본문 지연 로드, csv/xlsx 표 포함) · `/api/search`(본문 전문 검색) · `/api/calendar`(W26 — calendar.json 의 ICS + 폴백 파일) · `/api/chat/history`(W26 — 대화 도크 이력, 세션 전사에서) · `/api/calendar/events`(W27 — 구글 캘린더 GET/POST/PATCH/DELETE) · `/api/calendar/undo`(W27 — 지운 일정 복원) |


- **새 대시보드는 `dashboard-data.js` 를 읽지 않는다.** 서버가 notes/ 를 직접 스캔하므로
  `refresh-dashboard.sh` 를 돌리지 않아도 새 기록이 화면에 바로 뜬다.
- **`refresh-dashboard.sh` 와 래퍼는 계속 유지한다** — ① 초기 상태 생성(`dashboard.sh` 가 첫 실행 시
  호출) ② 롤백용 구 대시보드의 유일한 데이터 통로 ③ 에이전트 규약(스킬)이 이 명령을 의무화하고
  있다. 래퍼가 낡아도 **새 대시보드 화면은 낡지 않는다**(직독).
- 갱신은 서버 모드 주기 폴링으로 리로드 없이 제자리 갱신된다(스크롤·펼침 유지). 팀 탭 라이브
  세션은 5초 주기.
- 화면의 기한 계산 기준일은 `status.today.date` 다. 이 값이 실제 오늘과 다르면 대시보드가
  **낡은 데이터임을 화면에 고지한다**(숨기지 않는다).
- 이 단방향 규칙(쓰기: 에이전트 → 파일, 읽기: 파일 → 대시보드) 덕분에 대시보드는 에이전트와
  독립적으로 동작한다.

### 대시보드에서 파트너 부르기 (W21 — 단방향 규칙은 그대로다)

서버 모드에서는 화면의 버튼이 파트너에게 일을 시킬 수 있다(`./dashboard.sh` → `POST /api/ask`
→ `claude -p`). **이때도 대시보드는 파일을 쓰지 않는다** — 요청만 보내고, 쓰는 것은 여전히
에이전트 하나다. 그래서 단일 작성자 원칙이 유지된다.

```
대시보드 --(요청)--> 에이전트 --(쓰기)--> state/*.json + notes/ --(읽기)--> 대시보드
```

- 요청 문장은 화면에서 **언제나 열어볼 수 있다**(‘보낸 말 보기’). 사용자가 모르는 말이 대신 나가지 않는다.
- 파트너는 되묻는다(권한 승인·범위 확인). 러너에서 그대로 답해 대화를 잇는다.
- **폴더 신뢰 승인이 필요하다** — 터미널에서 그 폴더의 `claude` 를 한 번 실행해 신뢰 대화상자를
  수락해야 `.claude/settings.json` 의 권한 선언이 적용된다. 승인 전에는 파트너가 읽기만 하고
  파일을 바꾸지 못한다(물어보기만 한다).
- 상태를 바꾼 뒤 `./refresh-dashboard.sh` 실행은 여전히 의무다. 그래서 그 명령이
  `.claude/settings.json` 의 allow 목록에 있어야 한다(없으면 매번 승인 프롬프트에 막힌다 — 실측 결함으로 교정).

### 백그라운드 작업과 라이브 세션 (W22)

화면의 행동 버튼은 **두 갈래**로 갈린다. 어느 쪽이든 파일을 쓰는 건 에이전트다.

| 갈래 | 언제 | 엔드포인트 | 수명 |
|---|---|---|---|
| **즉답** | 바로 읽고 싶은 것 (답장 초안·지난 맥락 찾기) | `POST /api/ask` → `claude -p` 스트리밍 | **창을 닫으면 멈춘다** |
| **맡김** | 상태를 바꾸거나 오래 걸리는 것 (완료 처리·내일로·다음 단계·여기까지 정리) | `POST /api/jobs` → `claude --bg` | **화면을 닫아도 계속 돈다** |

- 실행은 Claude Code 의 백그라운드 에이전트 레지스트리를 그대로 쓴다
  (`claude --bg` / `claude agents --json --all --cwd` / `claude stop`).
- **접수는 즉시, 실행은 동시 2건까지** (W25 — 대표 피드백으로 W22 의 "순차 1건·429 거절"을 대체):
  슬롯이 비면 바로 `claude --bg`(응답 `{id}`), 차 있으면 브리지의 메모리 대기열에 쌓였다가
  자동으로 시작된다(응답 `{queued, qid, position}`). 슬롯 판정은 background `working` 수만
  센다(`blocked` 는 답을 기다리는 상태라 슬롯을 차지하지 않는다 — 실측). 대기열은 서버 메모리에만
  있으므로 **대시보드 서버가 켜져 있는 동안**만 유효하다 — 화면·응답 문구가 이 전제를 말한다.
  대기 취소는 `POST /api/jobs/stop {qid}`(디스패치 전엔 큐 제거, 승격 후엔 `claude stop`).
- 라이브 상태·결과는 **세션 전사 JSONL** 에서 읽는다. `claude logs` 는 ANSI 터미널 덤프라 쓰지 않는다.
- 화면 문구는 이 표와 일치해야 한다. "화면을 닫아도 계속 돌아가요" 를 즉답 경로에 붙이면 거짓말이 된다.

---

## ⑥ 자동화 (W22)

`state/automations.json` — 사용자가 켠 자동화. **출고 시 전부 꺼져 있다.**
읽을 때 출고 템플릿 중 빠진 항목을 **꺼진 상태로** 채워 넣으므로, 파일이 없어도 목록은 완전하다.

```jsonc
{
  "schemaVersion": 1,
  "updatedAt": "2026-07-28T10:12:03+09:00",
  "items": [
    {
      "id": "morning-brief",        // 고정 식별자 (출고 4종: reminder-due · morning-brief
                                    //              · open-issues · weekly-wrap)
      "name": "아침 브리핑",
      "why": "페르소나 3종 실주행에서 모두 UC-1 로 등장 · 평가 최고 가치",
                                    // **왜 있나** — 근거. 화면에 그대로 보여준다.
                                    // 근거를 못 대는 자동화는 올리지 않는다(창작 금지).
      "skill": "daily-briefing",    // 어떤 스킬을 기대하는지 (명세와 대조용)
      "trigger": { "kind": "daily", "at": "08:00" },
                                    // kind: "daily" | "weekly"(+ "day": 1=월)
      "prompt": "…",                // 파트너에게 보낼 말 (사용자가 고칠 수 있다)
      "deliver": "notify" | "note" | "both",
      "enabled": false,             // 출고 기본값 — 켜는 건 사용자
      "lastRun": null,              // { at, ok, error? }
      "failStreak": 0,              // 연속 실패 3회면 자동 정지
      "runsToday": 0, "runsDate": null   // 하루 상한
    }
  ]
}
```

`state/activity.json` — 파트너가 실제로 한 일 (최근 300건 보관, 화면에는 최근 80건 역순).

```jsonc
{
  "schemaVersion": 1,
  "updatedAt": "…",
  "items": [
    {
      "at": "2026-07-28T08:30:04+09:00",
      "kind": "auto" | "request",     // 자율형 / 내가 시킨 것 — 화면에서 배지로 구분
      "title": "아침 브리핑",
      "why": "…",
      "state": "started" | "done" | "blocked" | "failed" | "skipped",
      "jobId": "…",                   // 결말을 나중에 메꾸기 위한 연결고리
      "summary": "…"                  // 완료 시 답변 앞부분(160자)
    }
  ]
}
```

- 스케줄러는 **서버가 꺼져 있던 동안의 시각을 건너뛴다**(밀린 알림이 쏟아지지 않게).
- 안전장치: 동시 1건 · 하루 상한 · **연속 실패 3회면 자동 정지**하고 화면에 그 사실을 표시한다.
- `running` 항목은 조회 시점에 세션 상태로 결말을 메꾼다 — **값을 지어내지 않는다.**
- **화면 규약(W26)**: 자동화 탭은 **등록된 것**(켜져 있거나 실행 이력이 있는 것)만 목록에
  보여준다. 출고 템플릿은 '추가' 픽커 안에서만 보인다 — 데이터 계약(빠진 템플릿을 꺼진 상태로
  채워 넣는 읽기 규칙)은 그대로다.

---

## ⑦ 캘린더 연동 설정 (W26)

`state/calendar.json` — **에이전트(setup-calendar 스킬)가 쓴다.** 대시보드(브리지)는 읽기만
한다(쓰기 예외 2종 규칙 불변). 브리지는 `icsUrl` 이 있으면 원격 ICS 를 가져와(10분 캐시)
`/api/calendar` 로 파싱 결과를 준다. 반복 일정(RRULE)은 DAILY·WEEKLY 만 창(28일) 안에서
전개하고, MONTHLY·YEARLY 는 시작 회차만 표시한다(틀린 날짜를 만들지 않기 위한 보수적 선택).

```json
{ "icsUrl": "https://calendar.google.com/calendar/ical/…/basic.ics",
  "updatedAt": "2026-07-30T10:00:00+09:00" }
```

- 비밀 주소는 개인정보다 — 이 파일은 폴더 밖으로 내보내지 않는다.
- `icsUrl` 이 없거나 파일이 없으면 `/api/calendar` 는 `notes/calendar-fallback.md` 원문을
  준다(구조를 추측해 파싱하지 않는다 — 지어낸 일정이 빈 화면보다 나쁘다).
- **소스 차이 정직 고지**: 텔레그램 브리핑(daily-briefing)은 여전히 폴백 파일 기준이다.
  대시보드 일정 화면과 소스가 다르다는 사실을 setup-calendar 코치가 사용자에게 말한다.

## ⑦-1 구글 캘린더 실연동 (W27) — **쓰기 경계의 유일한 예외**

`state/google-calendar.json` — OAuth 토큰. **에이전트(setup-calendar 방법 A, `tools/google-connect/connect.mjs`)가 쓴다.**
브리지(`dashboard-server.py`)는 이 파일을 **읽어서** 구글 캘린더 API 를 직접 호출한다.

```
GET    /api/calendar/events?from=&to=   일정 읽기
POST   /api/calendar/events             만들기
PATCH  /api/calendar/events             고치기
DELETE /api/calendar/events?id=         지우기
POST   /api/calendar/undo               지운 일정을 그대로 다시 만들기 (5초 되돌리기)
```

**어느 캘린더를 보는가.** `users/me/calendarList` 로 목록을 받아 **구글에서 켜 둔 것만**
(`selected: true`) 합쳐서 보여준다. 구글 사이드바에서 체크를 끈 캘린더는 **가져오지 않는다**
(대표 결정 — 무엇을 볼지는 이미 구글에서 고르고 있으니 화면에 또 다른 설정을 만들지 않는다).
일정마다 `calendarId`·`calendarName`·`writable` 이 실려 온다. `writable` 은 그 캘린더의
`accessRole` 이 `owner`/`writer` 일 때만 참이다 — 공휴일·공유받은 캘린더는 **읽기 전용**이라
화면에서 못 고치고, 굳이 쓰려 하면 브리지가 403 으로 막는다.

> 권한이 좁은 **옛 연결**(`calendar.events` 만 받은 경우)은 목록 조회가 403 이다.
> 그때는 조용히 **기본 캘린더 하나**로 돌아간다 — 연결이 깨지는 게 아니라 좁아질 뿐이다.
> 넓히려면 `setup-calendar` 를 다시 돌리면 된다(승인 1회). `connect.mjs` 는 저장된 scope 가
> 지금 필요한 scope 와 다르면 **알아서 재승인을 받는다**.

> **시각 문자열은 구글이 준 그대로 둔다.** 캘린더마다 시간대 설정이 다를 수 있어
> (실측: 같은 계정에서 어떤 캘린더는 `Asia/Seoul`, 어떤 캘린더는 `UTC`) 구글이 `+09:00` 로
> 줄 때도 `Z` 로 줄 때도 있다. 둘 다 오프셋을 품고 있으니 화면이 파싱해 그 지역 시각으로 그린다.
> **문자열을 잘라 시각을 만들지 마라** — UTC 를 한국 시각처럼 보여주는 사고가 난다(실측).

**왜 이것만 예외인가.** ⑥의 쓰기 예외 2종(`automations.json`·`activity.json`)은 *우리 파일*이다.
구글 캘린더는 **우리 파일이 아니라 외부 시스템**이다. 충돌 해소·권한·이력은 구글이 책임진다.
따라서:

- `status.json`·`team.json` 은 **여전히 에이전트만** 쓴다. 이 경계는 **넓어지지 않았다.**
- 화면에서 고칠 수 있는 일정은 **구글에서 온 것뿐**이다. 우리 할 일(`status.json.todos`)의
  기한과 ICS 일정은 격자에 **보이기만** 하고 화면에서 못 고친다(고치려면 파트너에게 말한다).
- 지우기는 사고 비용이 커서 **5초 되돌리기**를 반드시 함께 제공한다.

> 다음에 다른 외부 시스템(Gmail·Drive 등)을 붙일 때 이 절이 선례다. 판단 기준은 하나 —
> **"우리 상태 파일인가, 남의 시스템인가."** 우리 파일이면 에이전트만 쓴다.

---

## JSON 스키마

### status.json

| 필드 | 타입 | 설명 |
|---|---|---|
| `schemaVersion` | number | 스키마 버전. 현재 `1` |
| `agent.name` | string | 에이전트 표시 이름 |
| `agent.status` | string | `"online"` \| `"idle"` \| `"working"` \| `"offline"` |
| `agent.identityLoaded` | boolean | `identity/IDENTITY.md` 가 채워졌는지 |
| `agent.businessName` | string \| null | 사업명 (IDENTITY에서 읽음, 없으면 null) |
| `session.startedAt` | string \| null | 세션 시작 시각 (ISO 8601) |
| `session.lastActiveAt` | string \| null | 마지막 활동 시각 (ISO 8601) |
| `today.date` | string \| null | 오늘 날짜 `YYYY-MM-DD` |
| `today.activities` | array | 오늘 한 일들. 항목: `{ "time": "HH:MM", "type": string, "summary": string }` |
| `today.counts` | object | `{ "notes", "briefings", "drafts", "issues", "reminders" }` 각 number |
| `todos` | array | **(신설·additive)** 열린 리마인더·이슈 목록. 없어도 됨(하위호환). 항목: 아래 |
| `updatedAt` | string | 이 파일 마지막 갱신 시각 (ISO 8601) |

`activities[].type` 권장값: `"note"` \| `"briefing"` \| `"draft"` \| `"issue"` \| `"reminder"`.

**`todos[]` 항목 스키마 (신설 · additive):** reminder·issue-tracker 스킬이 등록/개설 시 `open` 으로
추가하고, 완료/해결 시 `state` 를 `done` 으로 바꾼다. daily-briefing 은 이 배열을 읽어 열린 항목을
표면화한다(폴더 스캔 폴백은 이 필드가 없을 때만). **이 필드가 없어도 대시보드·브리핑은 동작한다.**

| 키 | 타입 | 설명 |
|---|---|---|
| `kind` | string | `"reminder"` \| `"issue"` |
| `title` | string | 한 줄 제목 |
| `due` | string \| null | 기한 `YYYY-MM-DD`. 이슈처럼 기한이 없으면 `null` |
| `source` | string | 원본 노트 경로 (예: `"notes/inbox/2026-07-21-리마인더-견적서.md"`) |
| `state` | string | `"open"` \| `"overdue"` \| `"done"`. (`overdue` 는 due<오늘 인 open — refresh-dashboard.sh 가 래퍼에서 파생 판정) |

```json
"todos": [
  { "kind": "reminder", "title": "견적서 확인", "due": "2026-07-21",
    "source": "notes/inbox/2026-07-20-리마인더-견적서.md", "state": "open" },
  { "kind": "issue", "title": "반품 클레임 — A고객", "due": null,
    "source": "notes/issues/2026-07-20-반품-클레임.md", "state": "open" }
]
```

**예시:**

```json
{
  "schemaVersion": 1,
  "agent": {
    "name": "내 비즈니스 파트너",
    "status": "online",
    "identityLoaded": true,
    "businessName": "OO마케팅 컨설팅"
  },
  "session": {
    "startedAt": "2026-07-20T09:00:00+09:00",
    "lastActiveAt": "2026-07-20T09:32:00+09:00"
  },
  "today": {
    "date": "2026-07-20",
    "activities": [
      { "time": "09:12", "type": "briefing", "summary": "오늘 일정 브리핑" },
      { "time": "09:32", "type": "note", "summary": "A업체 단가조정 요청 기록" }
    ],
    "counts": { "notes": 3, "briefings": 1, "drafts": 0, "issues": 1, "reminders": 2 }
  },
  "updatedAt": "2026-07-20T09:32:10+09:00"
}
```

### team.json

| 필드 | 타입 | 설명 |
|---|---|---|
| `schemaVersion` | number | 스키마 버전. 현재 `1` |
| `activeProject` | object \| null | 진행 중 프로젝트. 없으면 `null` |
| `activeProject.goal` | string | 목표 한 줄 |
| `activeProject.startedAt` | string | 시작 시각 (ISO 8601) |
| `activeProject.status` | string | `"in-progress"` \| `"paused"` \| `"done"` |
| `activeProject.phase` | string | 현재 단계 이름 |
| `activeProject.phases` | array of string | 전체 단계 목록 (순서대로) |
| `activeProject.health` | string | **(신설·additive)** `"순항"` \| `"주의"` \| `"지연"`. 없어도 됨 |
| `activeProject.progress` | number | **(신설·additive)** `0~100` 정수. 단계 기반 파생(완료 단계 ÷ 전체 × 100). 없어도 됨 |
| `activeProject.outputs` | array of string | **(신설·additive)** 산출물 파일 경로 배열. 없어도 됨 |
| `activeProject.members` | array | 팀원. 항목: `{ "role": string, "task": string, "status": string, "name"?: string }` — `name` 은 **(신설·additive)** 소집된 스태프 이름(`staff[]` 와 연결). 없어도 됨 |
| `activeProject.log` | array | 진행 로그. 항목: `{ "time": "HH:MM", "message": string }` |
| `history` | array | 완료된 프로젝트 요약 목록. 항목: `{ "goal": string, "startedAt": string(ISO 8601), "completedAt": string(ISO 8601), "summary": string }` |
| `pipeline` | array | **(선택·additive)** 대기 중 프로젝트 목록(activeProject와 동일 골격). 현재 미사용 — 다중 프로젝트 대비 예약. 없어도 됨 |
| `staff` | array | **(신설·additive)** 상비 팀원 캐릭터 ("재직" — 프로젝트 종료와 무관하게 유지. W26: 필요 역량만 성장형 채용 — 인원 수 가변). 없어도 됨(하위호환). 항목: 아래 |
| `tickets` | array | **(W26 신설·additive)** 팀원 작업 티켓 — 칸반보드의 단위. **쓰는 쪽(project-team)은 편성 시 반드시 발행한다** — 소집 멤버 수만큼. `members[]` 파생 렌더는 **옛 파일을 읽을 때의 폴백**이지 생략 허가가 아니다(실주행 2회에서 이 줄을 생략 허가로 읽었다). 항목: 아래 |
| `updatedAt` | string | 마지막 갱신 시각 (ISO 8601) |

`members[].status` 권장값: `"waiting"` \| `"working"` \| `"done"` \| `"blocked"`.

`activeProject.log[]` 항목에 **(W26 신설·additive)** `who`(팀원 닉네임 — `staff[].name` 매칭)를
넣을 수 있다: `{ "time": "11:20", "who": "Quinn", "message": "리서치 완료" }`. 없어도 동작한다.

**`tickets[]` 항목 스키마 (W26 신설 · additive):** project-team 스킬이 발행·갱신한다.
업무의 맥락(왜·무엇을 근거로·무엇이 나왔나)이 티켓 안에 들어 있는 것이 계약이다.

| 키 | 타입 | 설명 |
|---|---|---|
| `id` | string | `<프로젝트 슬러그>-t<N>` |
| `title` | string | 맡은 작업 한 줄 |
| `assignee` | string | 담당 팀원 닉네임 (`staff[].name`) |
| `role` | string | 역할 표시명 |
| `goal` | string | 왜 이 일을 하나 1줄 |
| `context` | string | 입력 계보 — 어떤 기록·산출물을 근거로 시작하나 |
| `sources` | array of string | 참고한 기록 경로 |
| `outputs` | array of string | 산출물 경로 (저장 시마다 추가) |
| `status` | string | `"waiting"` \| `"working"` \| `"blocked"` \| `"done"` |
| `project` | string | 소속 프로젝트 goal |
| `openedAt` / `updatedAt` | string | ISO 8601 |

**`staff[]` 항목 스키마 (신설 · additive):** project-team 스킬이 채용 시 생성한다 (`staff-guide.md` 규약).
대시보드는 이 배열이 있으면 "우리 팀" 카드에 사람(이름·아바타·MBTI)을 얹고, `members[].name` 과 매칭해
프로젝트 멤버를 사람 카드로 렌더한다. **없어도 v2와 동일하게 동작한다.**

| 키 | 타입 | 설명 |
|---|---|---|
| `roleFile` | string | `roles/<파일>.md` 매칭 키 (예: `"researcher"`) |
| `role` | string | 역할 표시명 (예: `"리서처"`) |
| `name` | string | 영어 닉네임 (예: `"Luna"` — 호명은 닉네임 그대로) |
| `mbti` | string | MBTI 4글자 (업무 스타일 표현 수단 — 심리 진단 아님) |
| `style` | string | 업무 스타일 한 문단 (보고 톤 / 판단 습관 / 주의점) |
| `avatar` | object | `{ "initial": 닉네임 첫 글자, "color": "#hex", "emoji": "🔍" }` + 선택 키 `"image"`(캐릭터 이미지 파일 경로 — 있으면 대시보드가 우선 사용, 없으면 색+이니셜 배지) |
| `appearance` | string | 외형 텍스트 묘사 (이미지 생성 없음 — 로드맵 소재) |
| `relationships` | array | `{ "with": roleFile, "type": "tension"\|"synergy", "note": 서사 1줄, "effect": 절차 효과 1줄 }` |
| `hiredAt` | string | 채용일 `YYYY-MM-DD` |

**예시:**

```json
{
  "schemaVersion": 1,
  "activeProject": {
    "goal": "다음 달 신제품 런칭 기획",
    "startedAt": "2026-07-20T10:00:00+09:00",
    "status": "in-progress",
    "phase": "리서치",
    "phases": ["기획", "리서치", "디자인", "개발", "정리"],
    "health": "순항",
    "progress": 20,
    "outputs": ["notes/projects/2026-07-신제품-런칭/00-계획.md"],
    "members": [
      { "role": "PM",     "task": "전체 일정·범위 정리", "status": "done" },
      { "role": "리서치", "task": "경쟁사·시장 조사",    "status": "working" },
      { "role": "디자인", "task": "컨셉 시안",           "status": "waiting" },
      { "role": "개발",   "task": "랜딩 페이지",         "status": "waiting" }
    ],
    "log": [
      { "time": "10:00", "message": "팀 구성 완료 — PM/리서치/디자인/개발" },
      { "time": "10:05", "message": "기획 단계 완료, 리서치 착수" }
    ]
  },
  "history": [],
  "updatedAt": "2026-07-20T10:05:00+09:00"
}
```

> **하위호환 원칙(additive):** 위에서 **(신설·additive)** 로 표시된 필드
> (`status.json.todos[]`, `team.json.activeProject.{health,progress,outputs}`,
> `team.json.staff[]`, `members[].name`)는 **없어도 대시보드가 정상 동작**한다.
> 기존 필드는 하나도 바뀌지 않았다. 옛 데이터 파일도 그대로 읽힌다.

### dashboard-data.js (파생 파일)

```js
// 자동 생성 파일. 직접 수정하지 마세요.
window.DASHBOARD_DATA = {
  "status":  { /* 위 status.json 예시 그대로 (todos[] open 중 due<오늘 은 overdue 로 파생) */ },
  "team":    { /* 위 team.json 예시 그대로 */ },
  "records": { /* refresh-dashboard.sh 파생 (아래) */ }
};
```

`window.DASHBOARD_DATA.status`·`.team`·`.records` 세 갈래로 대시보드가 접근한다.
설치 직후(데이터 없음) 상태에서는 `activeProject` 가 `null`, `today.activities` 가 `[]` 이다 —
대시보드는 이때 "아직 활동이 없어요" 안내를 보여준다.

### records (파생 · refresh-dashboard.sh 주입)

`notes/` **아래 실재하는 폴더 전부**와 `notes/projects/` 를 스캔해 만든 **읽기 전용 파생 블록**.
원본 JSON에는 쓰지 않는다.

> **폴더는 고정 목록이 아니다 (W28).** 기본은 `inbox`·`issues` 둘뿐이고 나머지는 각자 만든다
> (`고객사/`·`영상/`·`자격증/`). 서버(`note_folders()`)와 래퍼가 **같은 규칙**으로 실재 폴더를 읽고,
> 화면은 서버가 준 키를 그대로 순회한다(`Object.entries` · `FOLDER_LABEL[f] ?? f`).
> **따라서 아래 `counts` 의 키는 사람마다 다르다.** python3 가 없으면 생략된다(대시보드는 `records || {}` 로 방어 — 없어도 동작).

| 키 | 타입 | 설명 |
|---|---|---|
| `counts` | object | **폴더별 파일 수. 키는 고정이 아니다** — 그 사람의 `notes/` 에 있는 폴더 이름이 그대로 키가 된다. 예: `{ "inbox": 2, "issues": 1, "고객사": 4 }` |
| `recent` | array | 최근 파일 최대 5개. 항목: `{ "path", "title", "date", "folder", "name", "kind" }` — `kind`: `"md"` \| `"table"`(csv·xlsx) |
| `projects` | array | `notes/projects/` 하위 산출물. 항목: `{ "path", "title", "date", "name", "kind" }` |
| `tree` | object | **(신설·additive)** 기록 탭 폴더 브라우저용 전 파일 목록: `{ "notes": {분류: [{path,title,date,kind}]}, "projects": {슬러그: [...]} }` |

```js
"records": {
  "counts": { "inbox": 2, "issues": 1, "고객사": 4, "미팅": 3 },   // 키는 사람마다 다르다
  "recent": [
    { "path": "notes/issues/2026-07-19-반품.md", "title": "반품",
      "date": "2026-07-19", "folder": "issues", "name": "2026-07-19-반품.md" }
  ],
  "projects": [
    { "path": "notes/projects/2026-07-가을밤잼-런칭/01-리서치.md", "title": "01-리서치",
      "date": "2026-07-20", "name": "01-리서치.md" }
  ]
}
```
