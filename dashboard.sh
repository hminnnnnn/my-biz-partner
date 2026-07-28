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

# 상태 파일이 없으면 한 번 만들어 둔다. (대시보드는 서버가 JSON 을 직접 읽지만,
# 텔레그램 쪽 규약과 초기 상태 생성을 위해 스크립트는 계속 유지한다.)
if [ ! -f state/status.json ]; then
  ./refresh-dashboard.sh > /dev/null 2>&1 || true
fi

PORT="${1:-8787}"

# 포트가 이미 쓰이는 경우 — **조용히 다음 포트로 넘어가지 않는다.**
# 실측 사고: 두 번 실행하면 8788·8789… 로 말없이 밀려서, 사용자는 옛 탭(죽은 주소)을 보며
# "왜 안 바뀌지" 하게 된다. 이미 우리 대시보드가 떠 있으면 **그걸 다시 열어 주는 게** 맞다.
probe() {   # 이 포트에 우리 대시보드가 이미 떠 있으면 0
  curl -fsS --max-time 1 "http://127.0.0.1:$1/api/status" 2>/dev/null | grep -q '"mode"'
}
port_free() {
  python3 - "$1" <<'PY' > /dev/null 2>&1
import socket, sys
s = socket.socket()
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    s.close()
PY
}

if ! port_free "$PORT"; then
  if probe "$PORT"; then
    echo ""
    echo "  대시보드가 이미 켜져 있어요 (포트 $PORT). 새로 띄우지 않고 그 창을 엽니다."
    echo ""
    command -v open > /dev/null 2>&1 && open "http://127.0.0.1:$PORT/dashboard/index.html"
    exit 0
  fi
  # 우리 것이 아닌 다른 프로그램이 쓰고 있다 — 밀려나되 **반드시 알린다**
  ALT="$PORT"
  tries=0
  while [ "$tries" -lt 20 ] && ! port_free "$ALT"; do
    ALT=$((ALT + 1)); tries=$((tries + 1))
  done
  if [ "$tries" -ge 20 ]; then
    echo "✗ $PORT 부터 20개 포트가 전부 사용 중이에요. 다른 포트를 지정해 주세요:  ./dashboard.sh 9000"
    exit 1
  fi
  echo ""
  echo "  ⚠ 포트 $PORT 는 다른 프로그램이 쓰고 있어서 $ALT 로 엽니다."
  echo "     (예전에 열어 둔 대시보드 탭이 있다면 그 탭은 더 이상 갱신되지 않습니다 — 새 창을 쓰세요.)"
  PORT="$ALT"
fi

URL="http://127.0.0.1:$PORT/dashboard/index.html"

# 브라우저 열기 — 서버가 뜨는 사이에 여는 게 자연스러워 살짝 늦춘다
( sleep 1; command -v open > /dev/null 2>&1 && open "$URL" ) &

# 브리지 서버: 정적 서빙 + '파트너에게 시키기'(claude -p) 호출 엔드포인트.
# 127.0.0.1 바인딩 · Host/Origin 검증 · 세션 토큰 · 셸 미경유 — 자세한 이유는 파일 상단 주석 참고.
if [ -f "$ROOT/dashboard-server.py" ]; then
  exec python3 "$ROOT/dashboard-server.py" "$PORT"
fi

# 폴백: 브리지 파일이 없으면 예전처럼 정적 서버만 (대시보드는 파일 모드로 동작).
echo ""
echo "  대시보드가 열렸어요 →  $URL"
echo "  이 창을 닫거나 Ctrl+C 를 누르면 서버가 멈춥니다. (이 컴퓨터에서만 열립니다)"
echo ""
# --bind 127.0.0.1 필수 — 기본값(0.0.0.0)이면 같은 네트워크의 다른 기기에서도 열린다.
exec python3 -m http.server "$PORT" --bind 127.0.0.1
