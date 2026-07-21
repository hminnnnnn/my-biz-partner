#!/usr/bin/env bash
#
# install.sh — 내 비즈니스 파트너 설치 스크립트
# 하는 일: 환경 점검 → notes/·state/ 초기화(멱등) → 다음 단계 안내
# 여러 번 실행해도 안전합니다. 이미 있는 기록·상태는 절대 덮어쓰지 않습니다.

set -e

# 스크립트가 있는 폴더를 기준으로 동작 (어디서 실행하든 안전)
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

echo ""
echo "=== 내 비즈니스 파트너 설치 ==="
echo "설치 위치: $ROOT"
echo ""

# --- 1. 환경 점검: macOS ---
if [ "$(uname)" != "Darwin" ]; then
  echo "✗ 이 스크립트는 macOS 전용입니다. (현재: $(uname))"
  echo "  워크숍은 Mac 기준으로 진행됩니다. 도움이 필요하면 진행자에게 알려 주세요."
  exit 1
fi
echo "✓ macOS 확인됨 ($(sw_vers -productVersion 2>/dev/null || echo 'macOS'))"

# --- 도우미: 폴더가 없을 때만 만든다 ---
ensure_dir() {
  if [ -d "$1" ]; then
    echo "  · 이미 있음(보존): $1"
  else
    mkdir -p "$1"
    echo "  + 생성: $1"
  fi
}

# --- 도우미: 파일이 없을 때만 내용으로 만든다 (표준입력에서 읽음) ---
ensure_file() {
  # $1 = 파일 경로
  if [ -f "$1" ]; then
    echo "  · 이미 있음(보존): $1"
    cat > /dev/null   # 표준입력 소진
  else
    cat > "$1"
    echo "  + 생성: $1"
  fi
}

# --- 2. notes/ 하위 구조 ---
echo ""
echo "[1/3] 기록 폴더(notes/) 준비"
for d in inbox meetings customers ideas issues; do
  ensure_dir "notes/$d"
  [ -f "notes/$d/.gitkeep" ] || : > "notes/$d/.gitkeep"
done

# 캘린더 폴백 파일 (연동 실패 시 daily-briefing이 읽음)
ensure_file "notes/calendar-fallback.md" <<'FALLBACK'
# 이번 주 일정 (캘린더 폴백)

> 구글 캘린더 연동이 아직/안 되면, 여기에 이번 주 일정을 붙여넣으세요.
> "내일 브리핑해줘"라고 하면 파트너가 캘린더 대신 이 파일을 읽습니다.
> 형식은 자유입니다. 아래는 예시일 뿐, 지우고 당신 일정으로 바꾸세요.

## 예시 (지우고 사용)
- 월 10:00 A업체 미팅 (단가 재협의)
- 화 15:00 B고객 코칭
- 목 11:00 세무사 통화
FALLBACK

# --- 3. state/ 초기 파일 ---
echo ""
echo "[2/3] 상태 파일(state/) 준비"
ensure_dir "state"

NOW="$(date "+%Y-%m-%dT%H:%M:%S%z")"

ensure_file "state/status.json" <<STATUS
{
  "schemaVersion": 1,
  "agent": {
    "name": "내 비즈니스 파트너",
    "status": "offline",
    "identityLoaded": false,
    "businessName": null
  },
  "session": {
    "startedAt": null,
    "lastActiveAt": null
  },
  "today": {
    "date": null,
    "activities": [],
    "counts": { "notes": 0, "briefings": 0, "drafts": 0, "issues": 0, "reminders": 0 }
  },
  "updatedAt": "$NOW"
}
STATUS

ensure_file "state/team.json" <<TEAM
{
  "schemaVersion": 1,
  "activeProject": null,
  "history": [],
  "updatedAt": "$NOW"
}
TEAM

# dashboard-data.js 는 두 JSON에서 파생되는 파일 — 재생성은 전용 스크립트가 담당한다.
# (원본 JSON은 보존되므로 데이터 손실 없음 — 대시보드가 낡은 값을 보지 않게 하기 위함)
chmod +x refresh-dashboard.sh 2>/dev/null || true
./refresh-dashboard.sh

# --- 완료 안내 ---
echo ""
echo "[3/3] 완료!"
echo ""
echo "==================================================================="
echo " 설치가 끝났습니다. 이제 순서대로 아래를 진행하세요:"
echo ""
echo "  1) 텔레그램 연결   →  docs/SETUP_TELEGRAM.md 를 따라 하세요"
echo "  2) 나를 소개하기   →  identity/IDENTITY.md 의 4칸을 채우세요"
echo "                         (막막하면 텔레그램에 '정체성 같이 채워줘' 라고 하세요)"
echo "  3) 캘린더 연결(선택) →  docs/SETUP_CALENDAR.md"
echo "  4) 대시보드 열기   →  dashboard/index.html 을 더블클릭"
echo ""
echo "  준비되면 텔레그램에 이렇게 말 걸어 보세요:"
echo "    \"안녕, 내 사업 알고 있어?\"   \"내일 일정 브리핑해줘\""
echo "==================================================================="
echo ""
