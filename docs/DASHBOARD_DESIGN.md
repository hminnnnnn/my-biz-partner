# 대시보드 디자인 규약 (DASHBOARD_DESIGN)

> `dashboard/index.html` 을 고치는 **모든 세션·에이전트의 헌법**이다. 데이터 규약은
> `STATE_CONTRACT.md`, 디자인 시스템 상세는 gyeol-design-system 스킬(`references/` 포함)이 기준.
> 2026-07-23 GYEOL 전면 개편 + 홈 UX 라운드 기준.

## 0. 작업 전 의무

1. **gyeol-design-system 스킬을 로드**하고(프로젝트 `.claude/skills/`), SKILL.md 규칙을 따른다.
2. 이 문서의 보존 계약(§3)을 읽는다.
3. 끝나면 **`tools/dashboard-e2e/run.sh` 를 실행해 전항 PASS** 를 확인한다 — 기본 P-B(채용 전)
   + `demo/P-C_모먼트`(소집 중, 팀 탭 섹션 유효)로 두 번(개발 레포에서만 —
   이 스위트는 배포물 밖 `tools/` 에 있다). 시각 변경은 스크린샷 육안 검수를 추가한다
   (히트테스트는 DOM 기반이라 CSS가 안 떠도 통과한다 — 실제 사고 사례 있음).

## 1. GYEOL 4대 불가침 (스킬 요약 — 위반 시 개편 무효)

- **하드코딩 금지**: UI 코드에 hex·px·radius 값 직접 쓰지 않는다. 전부 `--g-*` 토큰.
  (허용된 예외: 반응형 브레이크포인트, 1px 헤어라인, 상태점 등 장식 고정치수. 색 예외는 단 하나 —
  아바타 배경의 `staff[].avatar.color` **데이터값** 인라인 주입. 코드 리터럴 색은 예외 없음)
- **`gyeol.tokens.css`·`gyeol.components.css` 직접 수정 금지** — 생성물/원본. 토큰이 더 필요하면
  스킬의 tokens.json → build-tokens.mjs 경로로.
- **원시 토큰 화면 사용 금지** — `--g-blue-500` 이 아니라 `--g-brand`, `--g-gray-*` 가 아니라 `--g-fg-*`.
- **한 화면(탭)에 `.g-btn--primary` 는 최대 1회** — 현재 홈의 "지금 보기"가 유일한 primary.

브랜드는 `G.setBrand()` 씨앗 1점만. 밀도는 `data-density="compact"` 고정, 테마는 OS 추종 +
사이드바 토글(G.prefs 저장) — 라이트/다크 **양쪽 모두** 성립해야 한다.

## 2. UX 문법 (홈 라운드에서 확립 — 새 화면·개편 시 동일 문법 적용)

- **판단 우선 위계**: 화면 최상단은 "사용자가 지금 결정/행동할 것" 1건. 홈의 `.decide` 카드가 기준 —
  선정 규칙은 전부 데이터 파생(지난 일 > 오늘 마감 > 주의 프로젝트, 해당 없으면 미표시).
  상수로 지어낸 사유·문구로 긴급함을 연출하지 않는다.
- **리듬 차등**: 모든 카드를 같은 무게로 나열하지 않는다. 훑는 수치(KPI)는 밴드로 강등, 결정·리스트가
  주인공. 강조 배경(soft 톤)은 화면당 한 곳.
- **모션은 토큰으로만**: `--g-motion-tap/state/reveal/sheet`. reduced-motion 분기는 토큰 레이어 몫 —
  컴포넌트에서 재분기 금지. hover 는 `--g-layer-hover`, press 는 `--g-layer-press`.
- **읽기 전용**: 쓰기 UI를 만들지 않는다(상태는 에이전트가 쓴다 — STATE_CONTRACT).
- **정직**: 제품(스킬)에 존재하지 않는 기능·개념을 화면 문구로 약속하지 않는다.
  화면 문구가 약속하는 기능은 반드시 스킬 명세(예: 채용 배너 ↔ project-team `staff-guide.md`)와
  짝이 있어야 한다. 판정 전에 최신 패킷·스킬 문서를 대조할 것(워킹트리 오판 사고 전력 — W16 §정정).

## 3. 보존 계약 (제거·개명 금지 — e2e 스위트가 이 훅으로 검증한다)

- 탭: `#home #todos #project #team #kanban #records` **6탭** (팀 탭은 W17 — docs/11_팀탭_전달패키지.md
  명세·대표 승인). IA 변경은 대표 승인 필요. **로스터(우리 팀)는 팀 탭 소관 — 프로젝트 탭에 되돌리지 않는다.**
- 데이터 훅: `.todo-row` `.todo(.open)` `[data-exp]` `[data-viewer]` `[data-goto]` `[data-copy]`
  `[data-project-open]` `[data-project-back]` `[data-role]` `[data-ticket]` `[data-modal-backdrop]`
  `#modal #modal-title #modal-body` `.role-card` `.proj-card` `.decide` `.staff-top` `.hire-note` `.team-summary`.
- 동작: 변경 감지 갱신(`dataSig`/프로브 — 15초마다 데이터만 재확인, 바뀐 경우에만 갱신. 무조건
  리로드로 되돌리지 말 것: 대표가 불편 피드백으로 걷어낸 방식), 모달 로직(자체 구현 유지),
  **`.modal-backdrop[hidden]{display:none!important}`** (display:flex 가 [hidden]을 이겨 투명
  클릭-삼킴 레이어가 되는 사고 전력), `.stagger` 등장(opacity:0→forwards — 헤드리스 캡처 시
  최종 상태 강제 필요).
- 데이터는 `../state/dashboard-data.js` 만 읽는다(file:// CORS 때문에 JS 래퍼 — STATE_CONTRACT ③).

## 4. 작업 절차

1. `skeleton/dashboard/index.html` 수정 (gyeol.* 파일은 불변)
2. `node -e "new Function(...)"` 문법 체크 → `tools/dashboard-e2e/run.sh` 28/28
3. 시각 변경 시 라이트+다크 스크린샷 육안 (배경 애니메이션 함정 §3 참고)
4. `tools/sync-demo-kits.sh` 로 키트 반영 → skeleton 레포 커밋·푸시
5. 사용자에게 보여줄 땐 전/후 스크린샷 비교(`docs/work/W16_.../screenshots/uxpass-*` 선례)
