---
name: project-team
description: "무거운 프로젝트성 업무를 팀으로 진행. 대표가 '다음 달 신제품 런칭 기획 진행해줘', '새 서비스 출시 준비 시작하자', '이거 프로젝트로 크게 진행해줘' 처럼 여러 단계·여러 산출물이 필요한 큰 일을 맡길 때만 사용한다. PM·리서치·디자인·개발 역할의 서브에이전트 팀을 구성하고 진행현황을 state/team.json 에 기록해 대시보드에 보이게 한다."
---

# project-team — 프로젝트 팀 모드 (L3 · 피날레)

## 언제 쓰나 (판단 기준)
**여러 단계 · 여러 산출물 · 여러 역할**이 필요한 일에만. 예: "신제품 런칭 기획", "새 서비스 출시".
산출물이 하나이고 한 번에 끝나면 팀을 꾸리지 말고 기본 스킬로 단독 처리한다.
애매하면 대표에게 물어본다: "팀을 꾸려 단계별로 진행할까요, 제가 바로 정리해드릴까요?"

## 이 스킬이 하는 일
1. **목표 확인** — 대표의 목표 한 줄을 확정한다.
2. **팀 설계** — 목표에 맞게 단계(phases)와 역할(members)을 정한다. 기본형:
   - phases: `["기획", "리서치", "디자인", "개발", "정리"]`
   - members: `PM`(총괄·일정), `리서치`(조사), `디자인`(시안), `개발`(구현) — 불필요한 역할은 뺀다.
   - **기본형은 예시일 뿐이다. 대표의 업종·목표에 맞게 역할명과 단계를 바꿔 쓴다.**
     (예: 코칭 프로그램 런칭이면 `개발`→`운영`(모집·결제·안내 준비), 오프라인 팝업이면
     `개발`→`공간/물류`. 소프트웨어가 아닌 일에 "개발"을 억지로 끼우지 않는다.)
3. **팀 구성 기록** — `state/team.json` 을 아래 스키마로 채운다(활성 프로젝트 시작). 그리고
   **`state/dashboard-data.js` 를 재생성**한다. → 이 순간 대시보드에 팀이 뜬다(와우 W5).
4. **단계 실행 루프** — 각 단계를 진행한다(직접 수행하거나 Claude Code의 서브에이전트에 위임).
   한 단계가 끝날 때마다:
   - 해당 member의 `status` 를 `waiting`→`working`→`done` 으로 갱신,
   - `activeProject.phase` 를 다음 단계로 전환,
   - `activeProject.log` 에 `{time, message}` 한 줄 추가,
   - **`state/team.json` 저장 + `state/dashboard-data.js` 재생성** (단계 전환마다 필수).
5. **완료** — 모든 단계가 끝나면 `activeProject.status` 를 `"done"` 으로, 이어서 해당 프로젝트를
   `history` 항목 `{goal, startedAt, completedAt, summary}` 로 옮기고 `activeProject` 를 `null` 로
   정리한 뒤 `./refresh-dashboard.sh` 실행.
6. **산출물 정리** — 결과물(기획서·리서치 요약 등)은 **`notes/projects/<YYYY-MM-프로젝트슬러그>/`**
   에 단계별 번호 파일로 저장한다 (예: `notes/projects/2026-09-그룹코칭-런칭/01-기획.md`).
   완료 후 대표에게 텔레그램용 짧은 요약으로 보고한다.

## 상태 기록 규약 (기준: `docs/STATE_CONTRACT.md`)
이 스킬은 `state/team.json` 을 쓰는 **유일한** 주체다. 필드는 계약 문서를 그대로 따른다:

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

- `members[].status`: `waiting` | `working` | `done` | `blocked`
- `activeProject.status`: `in-progress` | `paused` | `done`
- **철칙:** `team.json` 을 바꿀 때마다 같은 동작 안에서 `state/dashboard-data.js` 를 재생성한다
  (형식은 `docs/STATE_CONTRACT.md` ②).

## 보고 예시 (텔레그램)
```
'신제품 런칭 기획' 팀 꾸렸어요. 대시보드에서 진행현황 보실 수 있어요.
• PM: 일정·범위 정리 (완료)
• 리서치: 경쟁사·시장 조사 (진행 중)
• 디자인/개발: 대기
리서치 끝나면 시안 단계로 넘어가면서 다시 알려드릴게요.
```
