#!/usr/bin/env bash
# refresh-dashboard.sh — state/*.json → state/dashboard-data.js 재생성 (파생 파일 동기화)
#
# STATE_CONTRACT ③: status.json 또는 team.json 을 바꾼 뒤 반드시 이 스크립트를 실행하세요.
# 래퍼(dashboard-data.js)를 손으로 쓰지 마세요 — 손 복사는 반드시 어긋납니다.
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

for f in state/status.json state/team.json; do
  if [ ! -f "$f" ]; then
    echo "✗ $f 가 없습니다. ./install.sh 를 먼저 실행하세요."
    exit 1
  fi
done

# JSON 유효성 검사 — 망가진 JSON이 대시보드로 흘러가지 않게.
# (python3 가 있으면 검사, 없으면 건너뜀 — 검사 불가가 설치 실패로 이어지면 안 됨)
if command -v python3 > /dev/null 2>&1; then
  python3 -m json.tool state/status.json > /dev/null || { echo "✗ state/status.json 이 올바른 JSON이 아닙니다. 고친 뒤 다시 실행하세요."; exit 1; }
  python3 -m json.tool state/team.json > /dev/null || { echo "✗ state/team.json 이 올바른 JSON이 아닙니다. 고친 뒤 다시 실행하세요."; exit 1; }
fi

{
  printf '// 자동 생성 파일 (refresh-dashboard.sh). 직접 수정하지 마세요.\n'
  printf 'window.DASHBOARD_DATA = {\n'
  printf '  "status": '
  cat state/status.json
  printf ',\n  "team": '
  cat state/team.json
  printf '\n};\n'
} > state/dashboard-data.js

echo "✓ state/dashboard-data.js 갱신 완료 (status.json + team.json 그대로 래핑)"
