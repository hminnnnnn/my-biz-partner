#!/usr/bin/env bash
# dashboard.sh — 대시보드를 로컬 웹서버로 띄웁니다. (더블클릭으로 열어도 되지만, 서버로 띄우면 더 좋습니다)
#
#   ./dashboard.sh          8787 포트로 띄우고 브라우저를 엽니다
#   ./dashboard.sh 9000     포트를 직접 지정
#
# 서버로 띄우면 달라지는 것:
#   · state/status.json · team.json 을 **직접** 읽습니다 → 파트너가 상태를 바꾸면
#     ./refresh-dashboard.sh 를 아직 안 돌렸어도 화면이 최신입니다.
#   · 노트 본문을 필요할 때 파일에서 바로 읽습니다 → 기록이 수백 건으로 늘어도 첫 화면이 느려지지 않습니다.
#   · 서버가 없어도 dashboard/index.html 을 더블클릭하면 지금까지처럼 그대로 동작합니다(폴백).
#
# 안전: **이 컴퓨터(127.0.0.1)에서만** 열립니다. 같은 와이파이의 다른 기기에서는 보이지 않습니다.
#       사업 데이터가 담긴 폴더를 공개하지 않기 위한 의도된 제약입니다.
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if ! command -v python3 > /dev/null 2>&1; then
  echo "✗ python3 가 없어서 서버를 띄울 수 없어요."
  echo "  대신 dashboard/index.html 을 더블클릭하면 그대로 쓸 수 있어요 (파일 모드)."
  exit 1
fi

if [ ! -f state/dashboard-data.js ]; then
  echo "· 상태 파일이 아직 없어요. ./refresh-dashboard.sh 를 먼저 한 번 돌립니다."
  ./refresh-dashboard.sh || true
fi

PORT="${1:-8787}"
# 포트가 이미 쓰이면 다음 포트로 (최대 20번 시도)
tries=0
while [ "$tries" -lt 20 ]; do
  if python3 - "$PORT" <<'PY' > /dev/null 2>&1
import socket, sys
s = socket.socket()
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    s.close()
PY
  then break; fi
  PORT=$((PORT + 1))
  tries=$((tries + 1))
done

URL="http://127.0.0.1:$PORT/dashboard/index.html"
echo ""
echo "  대시보드가 열렸어요 →  $URL"
echo "  이 창을 닫거나 Ctrl+C 를 누르면 서버가 멈춥니다. (이 컴퓨터에서만 열립니다)"
echo ""

# 브라우저 열기 — 서버가 뜨는 사이에 여는 게 자연스러워 살짝 늦춘다
( sleep 1; command -v open > /dev/null 2>&1 && open "$URL" ) &

# --bind 127.0.0.1 필수 — 기본값(0.0.0.0)이면 같은 네트워크의 다른 기기에서도 열린다.
exec python3 -m http.server "$PORT" --bind 127.0.0.1
