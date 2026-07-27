#!/usr/bin/env python3
"""대시보드 로컬 서버 — 정적 파일 + 파트너(Claude Code) 호출 브리지.

./dashboard.sh 가 이 파일을 실행합니다. 직접 실행할 일은 없습니다.

무엇을 하나
  1) 이 폴더를 정적으로 서빙한다 (기존 python3 -m http.server 와 동일)
  2) POST /api/ask  → `claude -p <프롬프트>` 를 이 폴더에서 실행하고 결과를 스트리밍한다
     → 대시보드 화면의 버튼이 파트너에게 일을 시킬 수 있게 된다.

왜 안전한가 (다층 방어 — 셸을 여는 일이므로 하나에 기대지 않는다)
  · 127.0.0.1 에만 바인딩          → 같은 네트워크의 다른 기기가 접근 못 함
  · Host 헤더 검증                 → DNS 리바인딩 차단
  · Origin 헤더 검증               → 다른 사이트가 띄운 요청 차단
  · 세션 토큰(기동 시 난수)         → 토큰은 우리 HTML 안에만 있고, 다른 오리진은 CORS 때문에
                                     그 HTML을 읽을 수 없다. 커스텀 헤더라 단순 폼 CSRF 도 불가.
  · 셸 미경유(argv 로 전달)         → 프롬프트에 셸 메타문자가 있어도 명령으로 해석되지 않음
  · --dangerously-skip-permissions 미사용 → .claude/settings.json 의 권한 선언을 그대로 따른다
                                     (Edit(../**) 거부 = 이 폴더 밖은 못 고침)
"""
import http.server
import json
import os
import re
import secrets
import socketserver
import subprocess
import sys
import threading
import time
import uuid

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
TOKEN = secrets.token_urlsafe(24)
ALLOWED_HOSTS = {"127.0.0.1:%d" % PORT, "localhost:%d" % PORT}
ALLOWED_ORIGINS = {"http://127.0.0.1:%d" % PORT, "http://localhost:%d" % PORT}

MAX_PROMPT = 8000          # 프롬프트 길이 상한 (사고 방지)
TIMEOUT_SEC = 300          # 한 번의 호출 상한 5분
MAX_CONCURRENT = 2         # 동시 실행 상한 — 구독 사용량이 폭주하지 않게

_running = threading.Semaphore(MAX_CONCURRENT)


def claude_available():
    from shutil import which
    return which("claude") is not None


def workspace_trusted():
    """~/.claude.json 에 이 폴더가 신뢰 승인돼 있는지.

    승인 전에는 .claude/settings.json 의 permissions.allow 가 **전부 무시**된다.
    그러면 파트너가 파일을 읽지도 못해 답이 이상해지므로, 화면에서 먼저 안내해야 한다.
    """
    try:
        p = os.path.expanduser("~/.claude.json")
        with open(p, encoding="utf-8") as f:
            cfg = json.load(f)
        projects = cfg.get("projects") or {}
        # macOS 는 /tmp → /private/tmp 처럼 심볼릭 링크가 흔하다. 표기 경로와 실경로 둘 다 확인한다
        # (한쪽만 보면 신뢰돼 있는데도 "승인 전" 으로 잘못 안내한다 — 실측).
        for cand in {ROOT, os.path.realpath(ROOT)}:
            if (projects.get(cand) or {}).get("hasTrustDialogAccepted"):
                return True
        return False
    except Exception:
        return False


def _transcript_dir():
    """세션 전사가 쌓이는 곳. Claude Code 는 cwd 를 '-' 로 치환한 폴더명을 쓴다."""
    enc = os.path.realpath(ROOT).replace("/", "-")
    return os.path.expanduser("~/.claude/projects/" + enc)


def _live_line(session_id):
    """전사에서 '지금 무엇을 하는 중' 한 줄 + 마지막 답변을 뽑는다.

    `claude logs` 는 ANSI 터미널 화면 덤프라 화면에 못 넣는다(실측). 전사 JSONL 이
    도구 호출과 깨끗한 텍스트를 준다.
    """
    path = os.path.join(_transcript_dir(), "%s.jsonl" % session_id)
    if not os.path.isfile(path):
        return {"doing": None, "answer": None}
    doing, answer = None, None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"assistant"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "assistant":
                    continue
                for b in ((d.get("message") or {}).get("content") or []):
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use":
                        inp = b.get("input") or {}
                        target = inp.get("file_path") or inp.get("path") or inp.get("pattern") or ""
                        if isinstance(target, str) and target.startswith(ROOT):
                            target = target[len(ROOT):].lstrip("/")
                        doing = {"tool": b.get("name"), "target": target[:80] if isinstance(target, str) else ""}
                    elif b.get("type") == "text" and (b.get("text") or "").strip():
                        answer = b["text"].strip()
    except OSError:
        pass
    return {"doing": doing, "answer": (answer[-1200:] if answer else None)}


_sessions_cache = {"at": 0.0, "data": None}


def list_sessions():
    """이 사업 폴더에서 도는 세션 목록 + 각자의 라이브 상태.

    폴링마다 프로세스를 띄우지 않도록 2초 캐시를 둔다.
    """
    now = time.time()
    if _sessions_cache["data"] is not None and now - _sessions_cache["at"] < 2.0:
        return _sessions_cache["data"]
    out = []
    try:
        p = subprocess.run(
            ["claude", "agents", "--json", "--all", "--cwd", ROOT],
            capture_output=True, text=True, timeout=20,
        )
        raw = json.loads(p.stdout or "[]")
    except Exception:
        raw = []
    for s in raw if isinstance(raw, list) else []:
        sid = s.get("sessionId") or ""
        live = _live_line(sid) if sid else {"doing": None, "answer": None}
        out.append({
            "id": s.get("id") or sid[:8],
            "sessionId": sid,
            "kind": s.get("kind"),
            "state": s.get("state") or s.get("status"),
            "name": s.get("name"),
            "startedAt": s.get("startedAt"),
            "doing": live["doing"],
            "answer": live["answer"],
        })
    _sessions_cache["at"] = now
    _sessions_cache["data"] = out
    return out


class Handler(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    # 로그를 조용히 — 정적 파일 요청까지 다 찍으면 터미널이 시끄럽다
    def log_message(self, fmt, *args):
        if self.path.startswith("/api/"):
            sys.stderr.write("  [파트너] %s\n" % (fmt % args))

    # ── 보안 검사 ────────────────────────────────────────────
    def _origin_ok(self):
        host = self.headers.get("Host", "")
        if host not in ALLOWED_HOSTS:
            return False, "Host 헤더가 허용 목록에 없습니다"
        origin = self.headers.get("Origin")
        if origin is not None and origin not in ALLOWED_ORIGINS:
            return False, "Origin 이 허용되지 않습니다"
        return True, ""

    def _token_ok(self):
        return secrets.compare_digest(self.headers.get("X-Dashboard-Token", ""), TOKEN)

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ── 정적: index.html 에 세션 토큰을 주입해 내려준다 ───────
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/sessions":
            ok, why = self._origin_ok()
            if not ok:
                return self._json(403, {"error": why})
            return self._json(200, {"sessions": list_sessions()})
        if path == "/api/status":
            ok, why = self._origin_ok()
            if not ok:
                return self._json(403, {"error": why})
            return self._json(200, {
                "mode": "server",
                "claude": claude_available(),
                "trusted": workspace_trusted(),
                "root": ROOT,
            })
        if path in ("/dashboard/index.html", "/dashboard/", "/"):
            return self._serve_index()
        return super().do_GET()

    def _serve_index(self):
        idx = os.path.join(ROOT, "dashboard", "index.html")
        if not os.path.isfile(idx):
            self.send_error(404, "index.html not found")
            return
        with open(idx, encoding="utf-8") as f:
            html = f.read()
        inject = (
            '<script>window.__DASH_BRIDGE__={token:%s,claude:%s,trusted:%s};</script>'
            % (json.dumps(TOKEN), json.dumps(claude_available()), json.dumps(workspace_trusted()))
        )
        html = html.replace("<head>", "<head>" + inject, 1)
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ── 파트너 호출 ──────────────────────────────────────────
    def do_POST(self):
        route = self.path.split("?", 1)[0]
        if route == "/api/jobs":
            return self._dispatch_job()
        if route != "/api/ask":
            return self._json(404, {"error": "not found"})

        ok, why = self._origin_ok()
        if not ok:
            return self._json(403, {"error": why})
        if not self._token_ok():
            return self._json(403, {"error": "세션 토큰이 없거나 틀립니다"})

        try:
            n = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:
            return self._json(400, {"error": "본문을 읽을 수 없습니다"})

        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            return self._json(400, {"error": "프롬프트가 비었습니다"})
        if len(prompt) > MAX_PROMPT:
            return self._json(400, {"error": "프롬프트가 너무 깁니다(%d자 상한)" % MAX_PROMPT})
        if not claude_available():
            return self._json(503, {"error": "claude 명령을 찾을 수 없어요. Claude Code 설치 후 다시 시도하세요."})

        # 이어가기: 파트너가 되물으면 사용자가 답할 수 있어야 한다(한 번 쏘고 끝나면 실제 일이 안 끝난다).
        session = str(payload.get("session") or "").strip()
        if session and not re.fullmatch(r"[0-9a-fA-F-]{36}", session):
            return self._json(400, {"error": "세션 형식이 올바르지 않습니다"})

        if not _running.acquire(blocking=False):
            return self._json(429, {"error": "이미 실행 중인 요청이 있어요. 끝난 뒤에 다시 시도하세요."})

        try:
            self._stream_claude(prompt, session)
        finally:
            _running.release()

    # ── 백그라운드 작업 보내기 (W22) ─────────────────────────
    # `claude --bg` 는 즉시 반환한다. 세션 주인은 Claude Code 이지 브라우저가 아니므로
    # **화면을 닫아도 작업은 계속 산다.** 진행 상황은 /api/sessions 로 읽는다.
    def _dispatch_job(self):
        ok, why = self._origin_ok()
        if not ok:
            return self._json(403, {"error": why})
        if not self._token_ok():
            return self._json(403, {"error": "세션 토큰이 없거나 틀립니다"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:
            return self._json(400, {"error": "본문을 읽을 수 없습니다"})

        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            return self._json(400, {"error": "지시문이 비었습니다"})
        if len(prompt) > MAX_PROMPT:
            return self._json(400, {"error": "지시문이 너무 깁니다(%d자 상한)" % MAX_PROMPT})
        if not claude_available():
            return self._json(503, {"error": "claude 명령을 찾을 수 없어요."})

        # 순차 실행(대표 결정): 이미 도는 백그라운드 작업이 있으면 새로 보내지 않는다.
        # 사용량이 예측 가능하고 같은 파일을 동시에 고쳐 충돌하는 일이 없다.
        for s in list_sessions():
            if s.get("kind") == "background" and s.get("state") in ("working", "blocked"):
                return self._json(429, {
                    "error": "이미 진행 중인 작업이 있어요",
                    "running": {"id": s.get("id"), "name": s.get("name"), "state": s.get("state")},
                })
        try:
            p = subprocess.run(
                ["claude", "--bg", prompt],
                cwd=ROOT, capture_output=True, text=True, timeout=60,
            )
        except Exception as e:
            return self._json(500, {"error": "작업을 보내지 못했어요: %s" % e})
        # 출력 예: "backgrounded · 8fa7b75e"
        m = re.search(r"backgrounded\s*[·|]?\s*(?:\x1b\[[0-9;]*m)?([0-9a-f]{6,})", p.stdout or "")
        job_id = m.group(1) if m else None
        _sessions_cache["data"] = None          # 새 작업이 바로 목록에 뜨도록 캐시를 버린다
        if not job_id:
            return self._json(500, {"error": "작업 id 를 확인하지 못했어요",
                                    "raw": (p.stdout or p.stderr or "")[:300]})
        return self._json(200, {"id": job_id})

    def _stream_claude(self, prompt, session=""):
        """claude -p 를 실행하고 stdout 을 그대로 흘려보낸다 (chunked).

        셸을 거치지 않는다(shell=False + argv 리스트) — 프롬프트에 `;` `|` `$()` 가 있어도
        명령으로 해석되지 않고 순수 문자열로 전달된다.
        """
        # 이어가기용 세션 id — 새 대화면 우리가 정해서 알려 주고, 이어가기면 그대로 쓴다
        sid = session or str(uuid.uuid4())
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("X-Session-Id", sid)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def chunk(text):
            data = text.encode("utf-8")
            self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
            self.wfile.flush()

        proc = None
        try:
            argv = ["claude", "-p", prompt, "--output-format", "text"]
            argv += (["--resume", sid] if session else ["--session-id", sid])
            proc = subprocess.Popen(
                argv,
                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
            timer = threading.Timer(TIMEOUT_SEC, proc.kill)
            timer.start()
            try:
                for line in proc.stdout:
                    # 신뢰 미승인 경고는 사용자에게 뜻이 통하는 문장으로 바꿔 준다
                    if "has not been trusted" in line:
                        line = ("[안내] 이 폴더가 아직 Claude Code 에서 신뢰 승인되지 않아 권한 설정이 "
                                "무시됩니다. 터미널에서 이 폴더로 이동해 `claude` 를 한 번 실행하고 "
                                "신뢰 대화상자를 수락해 주세요.\n")
                    chunk(line)
            finally:
                timer.cancel()
            proc.wait()
            if proc.returncode not in (0, None):
                chunk("\n[오류] 파트너가 코드 %s 로 종료했어요.\n" % proc.returncode)
        except BrokenPipeError:
            pass                       # 사용자가 화면을 닫음 — 정상
        except Exception as e:
            try:
                chunk("\n[오류] %s\n" % e)
            except Exception:
                pass
        finally:
            if proc and proc.poll() is None:
                proc.kill()
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except Exception:
                pass


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        """브라우저가 탭을 닫으면 연결이 끊긴다 — 정상 상황이므로 스택 트레이스를 찍지 않는다.
        (참가자 터미널에 파이썬 트레이스백이 쏟아지면 무언가 고장난 줄 안다)"""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def main():
    os.chdir(ROOT)
    with Server(("127.0.0.1", PORT), Handler) as httpd:
        url = "http://127.0.0.1:%d/dashboard/index.html" % PORT
        print("")
        print("  대시보드가 열렸어요 →  %s" % url)
        print("  이 창을 닫거나 Ctrl+C 를 누르면 서버가 멈춥니다. (이 컴퓨터에서만 열립니다)")
        if not claude_available():
            print("  · claude 명령이 없어 '파트너에게 시키기' 버튼은 비활성으로 표시됩니다.")
        elif not workspace_trusted():
            print("  · 이 폴더가 아직 Claude Code 신뢰 승인 전이에요. 터미널에서 `claude` 를 한 번")
            print("    실행하고 신뢰 대화상자를 수락하면 파트너가 파일을 제대로 읽습니다.")
        print("")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  서버를 멈췄어요.")


if __name__ == "__main__":
    main()
