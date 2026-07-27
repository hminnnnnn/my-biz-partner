# 대시보드 디자인 규약 (DASHBOARD_DESIGN)

> `dashboard/index.html` 을 고치는 **모든 세션·에이전트의 헌법**이다. 데이터 규약은
> `STATE_CONTRACT.md`, 디자인 시스템 상세는 gyeol-design-system 스킬(`references/` 포함)이 기준.
> 2026-07-27 기준 — GYEOL 전면 개편(7/23) + 팀 탭(W17) + 기록 탭 개편(W18) +
> **W20 전면 개편(셸 레이어·탭별 심화·서버 모드)** + **W21 작업 공간 전환(위키 링크·파트너 호출·
> 할 일 큐+작업대·프로젝트 진행 이야기)** 누적 반영.

## 0. 작업 전 의무

1. **gyeol-design-system 스킬을 로드**하고(프로젝트 `.claude/skills/`), SKILL.md 규칙을 따른다.
   차트·KPI·히트맵·스파크라인을 손대면 **dataviz 스킬도 로드**한다(마크 규격·색 역할·범례 규칙).
2. 이 문서의 보존 계약(§3)을 읽는다.
3. 끝나면 아래 **둘 다** 전항 PASS 를 확인한다 (개발 레포에서만 — 스위트는 배포물 밖 `tools/` 에 있다):
   - `tools/dashboard-e2e/run.sh [키트]` — **file:// 모드**. 세 키트 전부 통과해야 한다:
     `demo/P-A_마중물컨설팅`(빈 데이터) **63** · `demo/P-B_별빛담은`(풀 데이터) **98** ·
     `demo/P-C_모먼트`(소집 중) **95**.
   - `tools/dashboard-e2e/run-server.sh [키트]` — **서버 모드**. 2회차 구성(직독 증거 · 임베드 0
     지연 로드)으로 **101 + 92**.
   기대값은 전부 **키트 데이터에서 파생**한다 — 데이터명(‘추석’·‘밤잼’)을 하네스에 박제하지 말 것
   (다른 키트에서 허위 FAIL 10건이 났던 전력).
4. 시각 변경은 **스크린샷 육안 검수**를 추가한다: `tools/capture.sh <출력디렉터리> [키트] [탭...]`
   (라이트/다크 × 6탭). 히트테스트는 DOM 기반이라 CSS가 안 떠도 통과한다 — 실제 사고 사례 있음.
   캡처·e2e 는 `identity/`(아바타 SVG)·`notes/` 도 복사해야 한다 — 빠뜨리면 아바타가 전부 깨져
   보여서 제품 결함으로 오판한다(실제 사례).

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
- **읽기 전용**: 대시보드가 파일을 직접 쓰지 않는다(상태는 에이전트가 쓴다 — STATE_CONTRACT).
  W21 의 파트너 호출은 이 규칙을 깨지 않는다 — **요청만 보내고 쓰는 건 여전히 에이전트**다.
  대시보드에서 상태 파일을 직접 편집하는 UI 는 여전히 금지다.
- **데이터 없는 시각화를 지어내지 않는다** (W20 에서 확립):
  · 델타·추이는 **실측 가능한 것만** 붙인다. 상태 파일에 어제 값이 없으면 '오늘 활동' 델타는 만들지 않는다.
  · 같은 화면에 같은 이름의 수치가 두 개 나오면 결함이다 — 모집단과 기간 지평선을 통일한다
    (실측: '쌓인 기록 19건' 옆에 '추이 합계 17건').
  · WIP 상한처럼 계약에 없는 값을 임의로 정해 경고를 만들지 않는다. 대신 실측값(지난 기한 N)을 쓴다.
  · 완료 시각이 없으면 "완료 · 오늘" 이라 쓰지 않는다. 추정 매칭은 **근거를 화면에 밝힌다**.
  · 마크 규격은 dataviz 기준: 얇은 마크 · 단일 계열엔 범례 없음 · 텍스트는 계열색이 아닌 텍스트 토큰 ·
    순차 스케일은 한 색조 명도 단계(`color-mix` 로 토큰만) · 이중 축 금지 · 호버는 기본 제공 +
    같은 정보를 `aria-label` 로도 준다(색·호버만으로 정보를 가두지 않는다).
- **정직**: 제품(스킬)에 존재하지 않는 기능·개념을 화면 문구로 약속하지 않는다.
  화면 문구가 약속하는 기능은 반드시 스킬 명세(예: 채용 배너 ↔ project-team `staff-guide.md`)와
  짝이 있어야 한다. 판정 전에 최신 패킷·스킬 문서를 대조할 것(워킹트리 오판 사고 전력 — W16 §정정).

## 3. 보존 계약 (제거·개명 금지 — e2e 스위트가 이 훅으로 검증한다)

- 탭: `#home #todos #project #team #kanban #records` **6탭** (팀 탭은 W17 — docs/11_팀탭_전달패키지.md
  명세·대표 승인). IA 변경은 대표 승인 필요. **로스터(우리 팀)는 팀 탭 소관 — 프로젝트 탭에 되돌리지 않는다.**
- 데이터 훅: `.todo-row` `.todo(.open)`(`.todo.open`·`[data-exp]` 는 홈 목록·칸반·아코디언 소관 —
  할 일 탭은 W21 에서 '선택' 문법으로 바뀌었다) `[data-exp]` `[data-viewer]` `[data-copy]`
  `[data-project-open]` `[data-project-back]` `[data-role]` `[data-ticket]` `[data-modal-backdrop]`
  `#modal #modal-title #modal-body` `.role-card` `.proj-card` `.decide` `.staff-top` `.hire-note` `.team-summary`
  `[data-rec-folder]`(폴더 토글 — 아코디언 단일 펼침. 클릭=열림 시 첫 파일 자동 선택·이미 열렸으면 접기)
  `[data-rec-file]`(파일 선택 — 트리 파일행 `.rec-file-row` + 최근 항목 `.rec-recent-row` 공용, 리딩 페인 교체)
  `.rec-nav-item`(폴더 토글 행) `.rec-file-row` `.rec-read`(인라인 리딩 페인 — 기록 탭 본문은 모달이 아니라
  이 페인에 렌더한다: 대표 지시. 2단 구조 = `.rec-tree` 폴더 토글 트리(좌) + `.rec-read` 리딩 페인(주인공, 우))
  `.rec-table`(표 뷰어) `.webview`(링크 웹뷰 — iframe sandbox 필수).
- **W20 셸·시각화·필터 훅** (제거·개명 금지):
  `#cmdk`·`#cmdk-input`·`#cmdk-list`·`[data-cmdk-run]`(⌘K 팔레트 — 닫힘 = `[hidden]`+`display:none!important`),
  `.stale-note`(신선도 고지), `.main[data-tab]`+`--shell-max`(탭별 유동 폭), `#view-slot`(셸 1회 마운트 +
  뷰 슬롯 교체 — 전체 재렌더로 되돌리지 말 것: 스크롤·포커스가 날아간다),
  `[data-tf]`·`#todo-q`·`#todo-body`(할 일 필터·부분 교체), `[data-kf]`(칸반 필터),
  `#rec-q`·`#rec-body`(기록 전문 검색), `[data-fold]`+`.fold-b`(접힘 섹션),
  `[data-todo-jump]`(백링크·시간축 → 할 일 교차 이동), `[data-sort]`(표 정렬),
  `[data-tip]`+`#viz-tip`(시각화 호버 툴팁 — 위임 1개), `[data-viz]`(차트 식별 훅: `due`·`act`·`phase`·`rec`),
  `.mode-pill`(실행 모드 표시), `.bucket`(기한 버킷 — 완료는 이 안에 넣지 않는다).
- **실행 모드 2종은 둘 다 유지한다**(STATE_CONTRACT ④): `file://` 파일 모드가 폴백이고,
  `http://127.0.0.1` 서버 모드가 상태 파일을 직독한다. 서버는 **반드시 127.0.0.1 바인딩**
  (`--bind` 없이 띄우면 같은 네트워크에 사업 데이터가 열린다).
- **본문 접근은 `hasBody()`/`putBody()` 를 거친다** — `contentByPath[path]` 존재 여부로 판정하면
  본문 없이 키만 있는 항목에서 지연 로드가 건너뛰어 뷰어가 폴백 문구만 띄운다(실측 결함).
- **W21 훅** (제거·개명 금지):
  `[data-inlink]`(본문 안 경로 → 리딩 페인. `'파일로 이동'` 앵커의 대체재 — 그 앵커는
  한 번도 동작한 적이 없어 걷어냈다. **상대경로 href 로 되살리지 말 것**: 페이지가
  `dashboard/index.html` 이라 `dashboard/notes/…` 로 풀려 404 다),
  `.todo-split`·`.todo-queue`·`[data-todo-sel]`·`.q-row(.sel)`·`.todo-bench`·`.bench-title`·`.bench-why`
  (할 일 큐 + 작업대 — 클릭은 '펼침' 이 아니라 '선택'),
  `.story`·`.st-node`(프로젝트 진행 이야기), `.nextmove`(다음 한 수),
  `[data-fold="proj-outs"]`(산출물 원본은 **접힘이 기본** — 주인공은 이야기다),
  `[data-ask]`·`#runner`·`#runner-input`·`[data-runner-close|refresh|send]`(파트너 러너).
- **파트너 호출(브리지) 규약** — `dashboard-server.py`:
  · 대시보드는 파일을 직접 쓰지 않는다. **에이전트에게 요청만** 보낸다(단일 작성자 유지).
  · 보안은 다층이고 **하나도 빼지 않는다**: 127.0.0.1 바인딩 · Host 검증 · Origin 검증 ·
    세션 토큰(커스텀 헤더) · 셸 미경유(argv) · `--dangerously-skip-permissions` 금지.
  · 프롬프트는 `ASKS` 사전 한곳에 모은다. 각 항목에 기대는 스킬을 적어 명세와 대조할 수 있게 한다.
  · 화면은 **보낸 말을 언제나 열어볼 수 있어야** 한다(모르는 말이 대신 나가지 않는다).
  · 파트너는 되묻는다 — 러너에 이어가기 입력을 유지할 것(한 번 쏘고 끝나면 일이 안 끝난다).
- **마크다운 렌더러 보존**: 인용구·문단 이어붙이기·목록 이어진 문장·`---`.
  한 줄씩 `<p>` 로 끊는 방식으로 되돌리면 줄 넘어가는 `**굵게**` 가 별표째 노출된다(실측).
- 동작: 변경 감지 갱신(`dataSig`/프로브 — 15초마다 데이터만 재확인, 바뀐 경우에만 갱신. 무조건
  리로드로 되돌리지 말 것: 대표가 불편 피드백으로 걷어낸 방식), 모달 로직(자체 구현 유지),
  **`.modal-backdrop[hidden]{display:none!important}`** (display:flex 가 [hidden]을 이겨 투명
  클릭-삼킴 레이어가 되는 사고 전력), `.stagger` 등장(opacity:0→forwards — 헤드리스 캡처 시
  최종 상태 강제 필요).
- 데이터는 `../state/dashboard-data.js` 만 읽는다(file:// CORS 때문에 JS 래퍼 — STATE_CONTRACT ③).

## 4. 작업 절차

1. `skeleton/dashboard/index.html` 수정 (gyeol.* 파일은 불변)
2. `node -e "new Function(...)"` 문법 체크
3. `tools/sync-demo-kits.sh` 로 키트 반영 (스위트·캡처는 키트를 읽는다 — 먼저 돌려야 한다)
4. `tools/dashboard-e2e/run.sh` 세 키트 전항 PASS (P-A 63 · P-B 98 · P-C 95) +
   `tools/dashboard-e2e/run-server.sh` 전항 PASS (101 + 92)
5. 시각 변경 시 `tools/capture.sh` 로 라이트+다크 육안 (배경 애니메이션 함정 §3 참고)
6. skeleton 레포 커밋·푸시
7. 사용자에게 보여줄 땐 전/후 스크린샷 비교(`docs/work/W16_.../screenshots/uxpass-*` 선례)
