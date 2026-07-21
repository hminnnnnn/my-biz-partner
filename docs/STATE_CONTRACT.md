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
└── dashboard-data.js   위 둘을 래핑한 JS 파일 (대시보드가 읽는 유일한 파일)
```

## ② 형식

- `status.json`, `team.json` 은 **순수 JSON**.
- `dashboard-data.js` 는 두 JSON을 하나의 전역 객체로 감싼 **JS 래퍼**:

  ```js
  window.DASHBOARD_DATA = {
    "status": { /* status.json 내용 그대로 */ },
    "team":   { /* team.json 내용 그대로 */ }
  };
  ```

- **왜 JS 래퍼인가:** 대시보드는 로컬 파일(`file://`)로 열린다. 브라우저는 `file://` 에서
  `fetch()` 로 JSON을 못 읽는다(CORS 제약). 그래서 `<script src>` 로 불러올 수 있는 JS 형태로
  한 벌 더 만든다. 서버가 필요 없다.

## ③ 쓰기 — 누가, 언제

| 파일 | 쓰는 주체 | 쓰는 시점 |
|---|---|---|
| `status.json` | **메인 에이전트** | 세션 시작 시 / 기본 업무(기록·브리핑·초안·이슈·리마인더) 실행 시마다 |
| `team.json` | **project-team 스킬** | 팀 구성 시, 그리고 **단계 전환마다** |
| `dashboard-data.js` | 위 둘 중 JSON을 바꾼 쪽 | **JSON을 갱신할 때마다 반드시 함께 재생성** |

> **철칙:** `status.json` 또는 `team.json` 을 바꾸면 **같은 동작 안에서 `./refresh-dashboard.sh`
> 를 실행**해 `dashboard-data.js` 를 재생성한다. 둘이 어긋나면 대시보드가 낡은 값을 보여준다.

재생성은 **반드시 스크립트로** 한다 — 래퍼를 손으로 합성하면 어긋난다(실제 QA 주행에서
수동 재생성이 `lastActiveAt` 드리프트를 일으킨 사례가 관찰되어 스크립트로 강제한다).
`refresh-dashboard.sh` 는 두 JSON의 유효성을 검사한 뒤 내용 그대로 래핑한다.
install.sh 도 마지막 단계에서 같은 스크립트를 호출한다.

## ④ 읽기

- **대시보드는 `state/dashboard-data.js` 만 읽는다.** 에이전트 내부·다른 파일에 결합하지 않는다.
- 대시보드는 5초마다 새로고침해서 최신 상태를 반영한다.
- 이 단방향 규칙(쓰기: 에이전트 → 파일, 읽기: 파일 → 대시보드) 덕분에 대시보드는 에이전트와
  독립적으로 동작한다.

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
| `updatedAt` | string | 이 파일 마지막 갱신 시각 (ISO 8601) |

`activities[].type` 권장값: `"note"` \| `"briefing"` \| `"draft"` \| `"issue"` \| `"reminder"`.

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
| `activeProject.members` | array | 팀원. 항목: `{ "role": string, "task": string, "status": string }` |
| `activeProject.log` | array | 진행 로그. 항목: `{ "time": "HH:MM", "message": string }` |
| `history` | array | 완료된 프로젝트 요약 목록. 항목: `{ "goal": string, "startedAt": string(ISO 8601), "completedAt": string(ISO 8601), "summary": string }` |
| `updatedAt` | string | 마지막 갱신 시각 (ISO 8601) |

`members[].status` 권장값: `"waiting"` \| `"working"` \| `"done"` \| `"blocked"`.

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

### dashboard-data.js (파생 파일)

```js
// 자동 생성 파일. 직접 수정하지 마세요.
window.DASHBOARD_DATA = {
  "status": { /* 위 status.json 예시 그대로 */ },
  "team":   { /* 위 team.json 예시 그대로 */ }
};
```

`window.DASHBOARD_DATA.status` 와 `window.DASHBOARD_DATA.team` 두 갈래로 대시보드가 접근한다.
설치 직후(데이터 없음) 상태에서는 `activeProject` 가 `null`, `today.activities` 가 `[]` 이다 —
대시보드는 이때 "아직 활동이 없어요" 안내를 보여준다.
