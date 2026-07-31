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
import collections
import datetime
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
BG_CONCURRENT = 2          # 동시에 도는 백그라운드 작업 상한 — 구독 사용량 통제(대표 조정 지점)

_running = threading.Semaphore(MAX_CONCURRENT)

# 대기열 — **메모리에만** 있다. 서버(터미널)를 끄면 대기 중인 것은 사라진다.
# 화면 문구도 "서버가 켜져 있는 동안" 을 반드시 함께 말한다(정직 고지).
_job_queue = collections.deque()
_queue_lock = threading.Lock()
_qid_jobs = {}             # qid → jobId. 큐를 떠난 뒤에도 '그만두기' 가 이어지도록


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
            capture_output=True, text=True, timeout=20, start_new_session=True,
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


# ============================================================
# 자동화 (W22-C) — 정해진 때에 파트너가 스스로 일한다
# ------------------------------------------------------------
# 왜 이게 필요한가: reminder 스킬이 스스로 적어 둔 한계다 —
#   "정해진 시각에 자동으로 알림을 울리는 상시 스케줄러는 이 버전에 없다.
#    (상시 구동 자동 알림은 다음 단계 로드맵.)"
# 즉 지어낸 기능이 아니라 제품이 이미 약속한 항목이다.
#
# 안전장치(대표 지시: 자율은 기본 꺼짐):
#   · 출고 시 전부 꺼짐  · 동시 1건  · 하루 실행 상한  · 연속 실패 3회면 자동 정지
#   · 한 일은 전부 활동 내역에 남는다(요청형/자율형 구분)
# ============================================================
AUTOMATIONS_PATH = os.path.join(ROOT, "state", "automations.json")
ACTIVITY_PATH = os.path.join(ROOT, "state", "activity.json")
_state_lock = threading.Lock()

# 출고 템플릿 — 전부 **실주행 유즈케이스**에서 뽑았다(창작 금지 규율).
DEFAULT_AUTOMATIONS = [
    {
        "id": "reminder-due",
        "name": "정시 알림",
        # 근거(내부): reminder/SKILL.md §한계 — 상시 스케줄러 부재. 화면 문구는 참가자 언어로.
        "why": "기한이 지나도 아무도 알려주지 않으면 놓치게 됩니다. 정해진 시각에 오늘 챙길 것을 한 번 짚어 드려요.",
        "skill": "reminder",
        "skillLabel": "리마인더",
        "trigger": {"kind": "daily", "at": "09:00"},
        "prompt": ("오늘이 기한이거나 기한이 지난 리마인더·이슈가 있는지 state/status.json 의 todos 에서 확인해줘.\n"
                   "있으면 무엇을 언제까지 해야 하는지 짧게 알려주고, 관련 원본 기록이 있으면 함께 짚어줘.\n"
                   "없으면 '오늘 기한은 없어요' 한 줄이면 충분해."),
        "deliver": "notify",
    },
    {
        "id": "morning-brief",
        "name": "아침 브리핑",
        # 근거(내부): 페르소나 3종 실주행 UC-1 · 평가 최고 가치
        "why": "하루를 여는 1분. 오늘 일정과 챙길 일을 엮어 아침에 한 번 정리해 드립니다.",
        "skill": "daily-briefing",
        "skillLabel": "일정 브리핑",
        "trigger": {"kind": "daily", "at": "08:00"},
        "prompt": ("오늘 아침 브리핑을 만들어줘. 일정(캘린더 또는 notes/calendar-fallback.md)과\n"
                   "오늘 챙길 할 일을 엮어서 짧게 정리해줘. 미팅이 있으면 그 상대와 관련된 지난 기록도 한 줄로 붙여줘."),
        "deliver": "both",
    },
    {
        "id": "open-issues",
        "name": "미해결 이슈 점검",
        # 근거(내부): P-B UC-1 — 열려 있던 반품 이슈가 브리핑에 안 섞여 놓친 사례
        "why": "열어 둔 문제는 잊히기 쉽습니다. 하루를 닫기 전에 아직 안 끝난 것을 훑어 드려요.",
        "skill": "issue-tracker",
        "skillLabel": "이슈 관리",
        "trigger": {"kind": "daily", "at": "18:00"},
        "prompt": ("notes/issues/ 의 이슈 중 아직 닫히지 않은 것을 훑어줘.\n"
                   "각각 마지막으로 움직인 게 언제인지, 지금 무엇이 막고 있는지 한 줄씩 정리해줘.\n"
                   "오래 멈춰 있는 게 있으면 그걸 먼저 짚어줘. 없으면 없다고 말해줘."),
        "deliver": "notify",
    },
    {
        "id": "weekly-wrap",
        "name": "이번 주 마감 정리",
        # 근거(내부): P-C UC-1 — "벽 화이트보드 안 봐도 이번 주가 한 문장" (평가 최고 가치)
        "why": "이번 주에 뭐가 몰려 있는지 한 문장으로. 주 시작에 한 주를 그려 드립니다.",
        "skill": "daily-briefing · recall",
        "skillLabel": "일정 브리핑 · 기록 회상",
        "trigger": {"kind": "weekly", "day": 1, "at": "09:00"},   # 1=월요일
        "prompt": ("이번 주에 마감이 걸린 일들을 한자리에 모아줘.\n"
                   "기한 순으로 정리하고, 각각 관련된 지난 기록·미해결 이슈가 있으면 함께 엮어줘.\n"
                   "마지막에 '이번 주는 한 문장으로 무엇인지' 를 한 줄로 요약해줘."),
        "deliver": "both",
    },
]
WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]


def _read_json(path, fallback):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return fallback


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_automations():
    """정의를 읽고, 출고 템플릿 중 빠진 것은 **꺼진 상태로** 채워 넣는다."""
    data = _read_json(AUTOMATIONS_PATH, {"schemaVersion": 1, "items": []})
    items = data.get("items")
    if not isinstance(items, list):
        items = []
    # **제품이 소유한 필드는 템플릿에서 갱신한다.**
    # 이름·설명("왜 있나")·스킬은 우리 카피다. 한 번 저장된 파일이 옛 문구를 계속 물고 있으면
    # 업데이트를 배포해도 참가자 화면은 안 바뀐다(실측: 내부 코드명이 그대로 남아 있었다).
    # 사용자가 소유한 것(enabled·trigger·prompt·deliver·실행 기록)은 절대 덮어쓰지 않는다.
    PRODUCT_OWNED = ("name", "why", "skill", "skillLabel")
    by_id = {t["id"]: t for t in DEFAULT_AUTOMATIONS}
    for it in items:
        tpl = by_id.get(it.get("id")) if isinstance(it, dict) else None
        if not tpl:
            continue
        for k in PRODUCT_OWNED:
            if k in tpl:
                it[k] = tpl[k]

    have = {i.get("id") for i in items if isinstance(i, dict)}
    for tpl in DEFAULT_AUTOMATIONS:
        if tpl["id"] in have:
            continue
        it = dict(tpl)
        it["enabled"] = False          # 대표 지시: 자율은 기본 꺼짐
        it["lastRun"] = None
        it["failStreak"] = 0
        it["runsToday"] = 0
        it["runsDate"] = None
        items.append(it)
    data["items"] = items
    data["schemaVersion"] = 1
    return data


def save_automations(data):
    data["updatedAt"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    _write_json(AUTOMATIONS_PATH, data)


def log_activity(entry):
    """파트너가 한 일을 남긴다. 요청형/자율형을 구분해 적는다(대표 지시 E4)."""
    with _state_lock:
        data = _read_json(ACTIVITY_PATH, {"schemaVersion": 1, "items": []})
        items = data.get("items")
        if not isinstance(items, list):
            items = []
        entry = dict(entry)
        entry.setdefault("at", datetime.datetime.now().astimezone().isoformat(timespec="seconds"))
        items.append(entry)
        data["items"] = items[-300:]          # 최근 300건만 보관
        data["schemaVersion"] = 1
        data["updatedAt"] = entry["at"]
        _write_json(ACTIVITY_PATH, data)


def activity_view():
    """내역을 주면서, 아직 'started' 인 항목의 결말을 세션 상태로 메꾼다.

    서버가 작업 완료를 따로 감시하지 않으므로, 조회 시점에 맞춰 본다(값을 지어내지 않는다).
    """
    data = _read_json(ACTIVITY_PATH, {"items": []})
    items = list(data.get("items") or [])
    by_job = {}
    for s in list_sessions():
        if s.get("id"):
            by_job[s["id"]] = s
    for it in items:
        if it.get("state") != "started" or not it.get("jobId"):
            continue
        s = by_job.get(it["jobId"])
        if not s:
            continue
        st = s.get("state")
        if st in ("done", "blocked", "failed"):
            it["state"] = st
            if s.get("answer") and not it.get("summary"):
                it["summary"] = s["answer"][:160]
    return items[-80:][::-1]


def _next_due(trigger, after):
    """다음 실행 시각. 과거로 밀린 것을 몰아서 터뜨리지 않고 **앞으로**만 계산한다."""
    try:
        hh, mm = [int(x) for x in str(trigger.get("at") or "09:00").split(":")[:2]]
    except Exception:
        hh, mm = 9, 0
    kind = trigger.get("kind")
    base = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if kind == "weekly":
        want = int(trigger.get("day", 1))                 # 1=월 … 7=일
        want_idx = (want - 1) % 7
        delta = (want_idx - base.weekday()) % 7
        cand = base + datetime.timedelta(days=delta)
        if cand <= after:
            cand += datetime.timedelta(days=7)
        return cand
    cand = base
    if cand <= after:
        cand += datetime.timedelta(days=1)
    return cand


def automations_view():
    """화면에 줄 형태 — 다음 실행 시각을 계산해 붙인다."""
    data = load_automations()
    now = datetime.datetime.now().astimezone()
    out = []
    for it in data["items"]:
        v = dict(it)
        v["nextRun"] = _next_due(it.get("trigger") or {}, now).isoformat(timespec="minutes") if it.get("enabled") else None
        out.append(v)
    return out


_sched_stop = threading.Event()


def _scheduler_loop():
    """30초마다 깨어나 실행할 자동화가 있는지 본다.

    서버가 꺼져 있던 동안의 시각은 **건너뛴다** — 노트북을 열자마자 밀린 알림이
    한꺼번에 쏟아지는 게 더 나쁘다(정직·놀람 방지).
    """
    marks = {}          # id → 마지막으로 지나간 예정 시각
    while not _sched_stop.wait(30):
        try:
            now = datetime.datetime.now().astimezone()
            data = load_automations()
            changed = False
            for it in data["items"]:
                if not it.get("enabled"):
                    continue
                if it.get("failStreak", 0) >= 3:
                    continue                        # 연속 실패 → 자동 정지
                today = now.date().isoformat()
                if it.get("runsDate") != today:
                    it["runsDate"] = today
                    it["runsToday"] = 0
                    changed = True
                if it.get("runsToday", 0) >= int(it.get("dailyLimit", 3)):
                    continue
                due = _next_due(it.get("trigger") or {}, now - datetime.timedelta(days=8))
                # due 는 now 이후의 첫 예정. 직전 예정은 한 주기 앞.
                step = datetime.timedelta(days=7) if (it.get("trigger") or {}).get("kind") == "weekly" else datetime.timedelta(days=1)
                prev = due - step
                key = prev.isoformat()
                if marks.get(it["id"]) == key:
                    continue
                if not (0 <= (now - prev).total_seconds() <= 600):
                    marks.setdefault(it["id"], key)     # 창을 벗어난 건 조용히 지나간 것으로 표시
                    continue
                marks[it["id"]] = key
                # 순차 실행 — 이미 도는 백그라운드 작업이 있으면 이번 회차는 거른다
                busy = any(s.get("kind") == "background" and s.get("state") == "working"
                           for s in list_sessions())
                if busy:
                    log_activity({"kind": "auto", "title": it.get("name"), "why": it.get("name"),
                                  "state": "skipped", "summary": "다른 작업이 진행 중이라 이번 회차는 건너뛰었어요"})
                    continue
                _run_automation(it)
                it["runsToday"] = it.get("runsToday", 0) + 1
                changed = True
            if changed:
                save_automations(data)
        except Exception as e:
            sys.stderr.write("  [자동화] 스케줄러 오류: %s\n" % e)


def _run_automation(it):
    """자동화 1건 실행 — 백그라운드로 보내고 활동 내역에 남긴다."""
    prompt = ("자동화 실행입니다 (대표가 미리 켜 둔 '%s').\n\n%s\n\n"
              "결과 처리: %s") % (
        it.get("name"), it.get("prompt") or "",
        {"note": "결과를 notes/inbox/ 에 기록으로 남겨줘.",
         "notify": "결과를 짧게 알려주기만 하고 파일은 만들지 마.",
         "both": "결과를 짧게 알려주고 notes/inbox/ 에도 기록으로 남겨줘."}.get(it.get("deliver"), "결과를 짧게 알려줘."))
    try:
        p = subprocess.run(["claude", "--bg", prompt], cwd=ROOT, start_new_session=True,
                           capture_output=True, text=True, timeout=60)
        m = re.search(r"backgrounded\s*[·|]?\s*(?:\x1b\[[0-9;]*m)?([0-9a-f]{6,})", p.stdout or "")
        job = m.group(1) if m else None
        _sessions_cache["data"] = None
        it["failStreak"] = 0
        it["lastRun"] = {"at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                         "ok": bool(job), "jobId": job}
        log_activity({"kind": "auto", "title": it.get("name"), "why": it.get("name"),
                      "state": "started" if job else "failed", "jobId": job,
                      "summary": "" if job else (p.stdout or p.stderr or "")[:200]})
    except Exception as e:
        it["failStreak"] = it.get("failStreak", 0) + 1
        it["lastRun"] = {"at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                         "ok": False, "error": str(e)[:200]}
        log_activity({"kind": "auto", "title": it.get("name"), "why": it.get("name"),
                      "state": "failed", "summary": str(e)[:200]})


# ============================================================
# 작업 대기열 (W25) — 막지 말고 받아 둔다
# ------------------------------------------------------------
# 전에는 이미 도는 작업이 있으면 429 로 돌려보냈다. 대표 피드백: 그러면 사용자가
# 기억했다가 다시 눌러야 한다. 이제는 **접수하고 순서대로 시작**한다.
# 상한(BG_CONCURRENT)은 남긴다 — 무제한으로 띄우면 구독 사용량을 통제할 수 없다.
# ============================================================
_pending_starts = {}       # jobId → 보낸 시각


def _bg_working():
    """지금 슬롯을 차지한 백그라운드 작업 수.

    `blocked` 는 세지 않는다 — 답을 기다리는 세션이 큐를 영원히 막으면 사용자가 갇힌다
    (W22 실측 결함: 이미 답을 낸 세션이 blocked 로 남아 있었다).
    방금 보낸 작업은 세션 목록에 아직 안 뜬다 — 그동안 안 세면 상한을 넘겨 띄우게 되므로
    목록에 나타나거나 20초가 지날 때까지 차지한 것으로 센다.
    """
    live = list_sessions()
    ids = {s.get("id") for s in live}
    n = sum(1 for s in live
            if s.get("kind") == "background" and s.get("state") == "working")
    now = time.time()
    for jid, at in list(_pending_starts.items()):
        if jid in ids or now - at > 20:
            _pending_starts.pop(jid, None)
        else:
            n += 1
    return n


def _launch_bg(prompt):
    """`claude --bg` 로 보내고 (작업 id, 원문) 을 돌려준다. id 를 못 읽으면 (None, 원문)."""
    try:
        p = subprocess.run(["claude", "--bg", prompt], cwd=ROOT, start_new_session=True,
                           capture_output=True, text=True, timeout=60)
    except Exception as e:
        return None, str(e)[:300]
    # 출력 예: "backgrounded · 8fa7b75e"
    m = re.search(r"backgrounded\s*[·|]?\s*(?:\x1b\[[0-9;]*m)?([0-9a-f]{6,})", p.stdout or "")
    _sessions_cache["data"] = None          # 새 작업이 바로 목록에 뜨도록 캐시를 버린다
    if m:
        _pending_starts[m.group(1)] = time.time()
    return (m.group(1) if m else None), (p.stdout or p.stderr or "")[:300]


def queued_view():
    """대기열 화면용. 이미 출발한 항목은 position 0 + jobId 로 보이다가, 라이브 카드에 뜨면 빠진다."""
    out, pos = [], 0
    with _queue_lock:
        for it in _job_queue:
            started = bool(it.get("jobId"))
            if not started:
                pos += 1
            out.append({"qid": it["qid"], "title": it["title"],
                        "position": 0 if started else pos, "jobId": it.get("jobId")})
    return out


def _queue_pump():
    """2초마다 슬롯을 보고 대기열 앞에서 하나씩 내보낸다."""
    while not _sched_stop.wait(2):
        try:
            with _queue_lock:
                idle = not _job_queue
            if idle:
                continue                # 대기열이 비면 세션 조회조차 하지 않는다(불필요한 프로세스 금지)
            busy = _bg_working()        # 여기서 warm-up 목록도 함께 정리된다
            with _queue_lock:
                # 출발한 항목은 **라이브 카드로 넘어갈 때까지** 남겨 둔다 — 화면 갱신(5초)보다
                # 먼저 지우면 사용자 눈에는 일이 사라진 것으로 보인다.
                while (_job_queue and _job_queue[0].get("jobId")
                       and _job_queue[0]["jobId"] not in _pending_starts):
                    _job_queue.popleft()
                pending = next((it for it in _job_queue if not it.get("jobId")), None)
            if pending is None or busy >= BG_CONCURRENT:
                continue
            job_id, raw = _launch_bg(pending["prompt"])
            with _queue_lock:
                if job_id:
                    pending["jobId"] = job_id
                    _qid_jobs[pending["qid"]] = job_id
                    if len(_qid_jobs) > 50:         # 오래된 것부터 버린다(메모리 상한)
                        for k in list(_qid_jobs)[:-50]:
                            _qid_jobs.pop(k, None)
                else:
                    try:
                        _job_queue.remove(pending)  # 못 보낸 것은 버린다 — 무한 재시도 금지
                    except ValueError:
                        pass
            log_activity({"kind": "request", "title": pending["title"],
                          "state": "started" if job_id else "failed",
                          "jobId": job_id, "summary": "" if job_id else raw[:200]})
        except Exception as e:
            sys.stderr.write("  [대기열] 오류: %s\n" % e)


# ═══════════════════════════════════════════════════════════════════
#  기록 스캔 — 파일 모드를 폐지하면서 래퍼(dashboard-data.js) 의존을 끊는다.
#
#  전에는 브라우저가 파일시스템을 못 훑어서 refresh-dashboard.sh 가 만든 래퍼에
#  기록 목록·본문을 통째로 박아 넣었다. 그래서 파트너가 기록을 남겨도 래퍼를 다시
#  만들기 전까지 화면에 안 보였다. 서버가 직접 훑으면 **항상 최신**이다.
#
#  규칙은 refresh-dashboard.sh 와 동일하게 맞춘다 (같은 화면을 두 곳에서 다르게 그리면 안 된다).
# ═══════════════════════════════════════════════════════════════════
#  폴더는 **고정 목록이 아니다.** notes/ 아래에 실제로 있는 폴더를 그대로 읽는다.
#  이유: 폴더 이름은 사람마다 달라야 한다. 사업하는 사람에겐 "고객사"가 맞지만 자격증을 준비하는
#  사람에게 `customers/` 는 거짓말이다(실측: 개인 사용자 주행에서 스터디 멤버가 customers/ 로 갔다).
#  화면은 서버가 준 키를 그대로 순회하므로(RecordsTab), 여기만 동적이면 새 폴더가 바로 보인다.
DEFAULT_NOTE_FOLDERS = ["inbox", "issues"]   # 설치 직후 기본. 나머지는 각자 만든다.


def note_folders():
    """notes/ 아래 실재하는 폴더를 이름순으로. `projects/` 는 아래에서 따로 훑으므로 제외."""
    d = os.path.join(ROOT, "notes")
    try:
        names = sorted(n for n in os.listdir(d)
                       if not n.startswith(".") and n != "projects"
                       and os.path.isdir(os.path.join(d, n)))
    except OSError:
        names = []
    return names or DEFAULT_NOTE_FOLDERS


DOC_EXTS = (".md", ".csv", ".xlsx")
CONTENT_CAP = 256 * 1024        # 서버는 필요할 때 한 건씩만 읽으므로 래퍼(16KB)보다 넉넉하게
TABLE_MAX_ROWS, TABLE_MAX_COLS = 2000, 64


def _title_of(name):
    stem = re.sub(r"\.(md|csv|xlsx)$", "", name)
    return re.sub(r"^\d{4}-\d{2}-\d{2}-", "", stem)


def _file_date(name, path):
    m = re.match(r"(\d{4}-\d{2}-\d{2})", name)
    if m:
        return m.group(1)
    try:
        return datetime.date.fromtimestamp(os.path.getmtime(path)).isoformat()
    except OSError:
        return ""


def _kind_of(name):
    return "table" if name.endswith((".csv", ".xlsx")) else "md"


def _safe_note_path(rel):
    """notes/ 안으로만 허용한다. `..` 로 폴더 밖을 읽으려는 시도를 막는다."""
    if not rel:
        return None
    rel = rel.lstrip("/")
    full = os.path.realpath(os.path.join(ROOT, rel))
    base = os.path.realpath(os.path.join(ROOT, "notes"))
    if not (full == base or full.startswith(base + os.sep)):
        return None
    return full if os.path.isfile(full) else None


def _parse_csv(path):
    import csv as _csv
    try:
        with open(path, newline="", encoding="utf-8", errors="replace") as f:
            rows = []
            for i, row in enumerate(_csv.reader(f)):
                if i >= TABLE_MAX_ROWS:
                    break
                rows.append(list(row[:TABLE_MAX_COLS]))
            return rows or None
    except Exception:
        return None


def _parse_xlsx(path):
    """첫 시트만 — zipfile + xml.etree 최소 파서 (외부 의존 금지)."""
    import zipfile
    import xml.etree.ElementTree as ET
    NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    try:
        with zipfile.ZipFile(path) as z:
            shared = []
            if "xl/sharedStrings.xml" in z.namelist():
                root = ET.fromstring(z.read("xl/sharedStrings.xml"))
                for si in root.findall(NS + "si"):
                    shared.append("".join(t.text or "" for t in si.iter(NS + "t")))
            names = sorted(n for n in z.namelist() if n.startswith("xl/worksheets/sheet"))
            if not names:
                return None
            root = ET.fromstring(z.read(names[0]))
            rows = []
            for row in root.iter(NS + "row"):
                if len(rows) >= TABLE_MAX_ROWS:
                    break
                cells = {}
                for c in row.iter(NS + "c"):
                    ref = c.get("r") or ""
                    m = re.match(r"([A-Z]+)", ref)
                    if not m:
                        continue
                    ci = 0
                    for ch in m.group(1):
                        ci = ci * 26 + (ord(ch) - 64)
                    ci -= 1
                    if ci >= TABLE_MAX_COLS:
                        continue
                    v = c.find(NS + "v")
                    txt = v.text if v is not None and v.text is not None else ""
                    if c.get("t") == "s":
                        try:
                            txt = shared[int(txt)]
                        except Exception:
                            pass
                    elif c.get("t") == "inlineStr":
                        txt = "".join(t.text or "" for t in c.iter(NS + "t"))
                    cells[ci] = txt
                width = max(cells.keys()) + 1 if cells else 0
                rows.append([cells.get(i, "") for i in range(width)])
            return rows or None
    except Exception:
        return None


def scan_records():
    """기록 목록·폴더 트리. **본문은 담지 않는다** — 첫 화면이 기록 수에 비례해 느려지지 않게."""
    counts, items, tree_notes = {}, [], {}
    for folder in note_folders():
        d = os.path.join(ROOT, "notes", folder)
        names = []
        if os.path.isdir(d):
            names = sorted(n for n in os.listdir(d) if n.endswith(DOC_EXTS) and n != "README.md")
        counts[folder] = len(names)
        tree_notes[folder] = []
        for name in names:
            rel = "notes/%s/%s" % (folder, name)
            it = {"path": rel, "title": _title_of(name), "date": _file_date(name, os.path.join(d, name)),
                  "folder": folder, "name": name, "kind": _kind_of(name)}
            items.append(it)
            tree_notes[folder].append(it)

    projects, tree_projects = [], {}
    proot = os.path.join(ROOT, "notes", "projects")
    if os.path.isdir(proot):
        for dirpath, _dirs, filenames in os.walk(proot):
            for name in sorted(filenames):
                if not name.endswith(DOC_EXTS) or name == "README.md":
                    continue
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, ROOT)
                sub = os.path.relpath(dirpath, proot)
                slug = sub.split(os.sep)[0] if sub != "." else ""
                it = {"path": rel, "title": _title_of(name), "date": _file_date(name, full),
                      "folder": "projects", "name": name, "kind": _kind_of(name), "slug": slug}
                projects.append(it)
                tree_projects.setdefault(slug, []).append(it)

    items.sort(key=lambda r: r["date"], reverse=True)
    projects.sort(key=lambda r: r["date"], reverse=True)
    for fl in tree_notes.values():
        fl.sort(key=lambda r: r["date"], reverse=True)
    for fl in tree_projects.values():
        fl.sort(key=lambda r: r["date"], reverse=True)

    # 홈의 KPI(total)·막대(counts)·히트맵이 **같은 모집단**을 써야 한다.
    # 실측 결함: KPI 는 32(projects 포함), 막대 합계는 19(projects 제외) 였다 — 같은 화면 두 숫자.
    if projects:
        counts["projects"] = len(projects)
    return {"counts": counts, "items": items, "projects": projects,
            "tree": {"notes": tree_notes, "projects": tree_projects},
            "total": len(items) + len(projects)}


def read_note(rel):
    full = _safe_note_path(rel)
    if not full:
        return None
    name = os.path.basename(full)
    out = {"path": rel, "title": _title_of(name), "kind": _kind_of(name),
           "date": _file_date(name, full)}
    if out["kind"] == "table":
        out["table"] = _parse_csv(full) if full.endswith(".csv") else _parse_xlsx(full)
        if out["table"] is None:
            out["error"] = "표를 읽지 못했어요 (형식을 확인해 주세요)"
        return out
    try:
        with open(full, encoding="utf-8", errors="replace") as f:
            text = f.read(CONTENT_CAP + 1)
        if len(text) > CONTENT_CAP:
            text = text[:CONTENT_CAP] + "\n\n… (내용이 길어 여기까지만 표시)"
        out["content"] = text
    except OSError as e:
        out["error"] = str(e)
    return out


def search_notes(q, limit=40):
    """기록 전문 검색 — 제목과 본문을 함께 본다.

    브라우저가 모든 본문을 들고 있을 필요가 없다(예전엔 래퍼에 전량 임베드했다).
    서버가 파일을 읽으므로 기록이 수백 건이어도 첫 화면이 무거워지지 않는다.
    """
    q = (q or "").strip()
    if len(q) < 2:
        return []
    ql = q.lower()
    rec = scan_records()
    hits = []
    for it in rec["items"] + rec["projects"]:
        title_hit = ql in it["title"].lower()
        snippet = ""
        body_hit = False
        if it["kind"] == "md":
            full = _safe_note_path(it["path"])
            if full:
                try:
                    with open(full, encoding="utf-8", errors="replace") as f:
                        text = f.read(CONTENT_CAP)
                    i = text.lower().find(ql)
                    if i >= 0:
                        body_hit = True
                        a0, b0 = max(0, i - 60), min(len(text), i + len(q) + 60)
                        snippet = ("…" if a0 else "") + " ".join(text[a0:b0].split()) + ("…" if b0 < len(text) else "")
                except OSError:
                    pass
        if not (title_hit or body_hit):
            continue
        hits.append({**it, "snippet": snippet, "where": "title" if title_hit else "body"})
        if len(hits) >= limit:
            break
    # 제목 일치를 먼저
    hits.sort(key=lambda h: (0 if h["where"] == "title" else 1, h["date"]), reverse=False)
    return hits


def read_roster():
    """우리 팀 직무 표준 — .claude/skills/project-team/roles/*.md 헤더에서 뽑는다."""
    out = []
    rdir = os.path.join(ROOT, ".claude", "skills", "project-team", "roles")
    if not os.path.isdir(rdir):
        return out
    for fname in sorted(os.listdir(rdir)):
        if not fname.endswith(".md"):
            continue
        try:
            with open(os.path.join(rdir, fname), encoding="utf-8", errors="replace") as f:
                head = f.read(4000)
        except OSError:
            continue
        # 실제 파일 형식은 `- **name**: 팀장(PM)` 이다 — `**` 를 안 걸러 **한 번도 매칭되지 않았다**
        # (실측 결함: 화면에 영어 파일명 8개가 그대로 나갔다).
        def grab(key):
            m = re.search(r"^-\s*\**\s*%s\s*\**\s*:\s*(.+)$" % key, head, re.M)
            return m.group(1).strip() if m else ""

        # 관점 한 줄은 `## ① 관점 한 줄` 다음의 인용문에 있다
        pm = re.search(r"##[^\n]*관점[^\n]*\n+>\s*(.+)", head)
        out.append({
            "file": fname[:-3],
            "name": grab("name") or fname[:-3],
            "role": grab("name") or fname[:-3],          # 화면에 보이는 직무명
            "goal": grab("goal"),
            "perspective": (pm.group(1).strip() if pm else "") or grab("profile"),
        })
    return out


# ============================================================
# 캘린더 (W26) — 홈의 '전체 일정'
# ------------------------------------------------------------
# 소스 우선순위:
#   1) state/calendar.json 의 icsUrl (구글 캘린더 '비밀 주소(iCal)' — OAuth 불필요.
#      이 파일은 **에이전트(setup-calendar 스킬)가 쓴다** — 대시보드 쓰기 경계 밖)
#   2) notes/calendar-fallback.md (수기 일정 — 있는 그대로 보여준다. 형식을 추측해
#      구조화하지 않는다: 지어낸 일정이 빈 화면보다 나쁘다)
# ICS 반복 일정(RRULE)은 DAILY·WEEKLY 만 창 안에서 전개한다. MONTHLY·YEARLY 는
# 시작 회차만 표시한다(전개 규칙이 복잡해 틀린 날짜를 만들 수 있다 — 덜 보여주는 쪽 선택).
# ============================================================
CALENDAR_CFG = os.path.join(ROOT, "state", "calendar.json")
CAL_FALLBACK = os.path.join(ROOT, "notes", "calendar-fallback.md")
_ics_cache = {"url": None, "at": 0.0, "text": None, "error": None}
ICS_TTL = 600                    # 10분 캐시 — 홈 폴링마다 원격을 두드리지 않는다
ICS_MAX_BYTES = 2 * 1024 * 1024
CAL_WINDOW_DAYS = 28


def _fetch_ics(url):
    now = time.time()
    if _ics_cache["url"] == url and now - _ics_cache["at"] < ICS_TTL:
        return _ics_cache["text"], _ics_cache["error"]
    text, err = None, None
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "my-biz-partner-dashboard"})
        with urllib.request.urlopen(req, timeout=8) as r:
            text = r.read(ICS_MAX_BYTES).decode("utf-8", errors="replace")
    except Exception as e:
        err = str(e)[:200]
    _ics_cache.update({"url": url, "at": now, "text": text, "error": err})
    return text, err


def _ics_unfold(text):
    """ICS 는 75바이트에서 줄을 접는다 — 이어지는 줄(공백 시작)을 붙여서 편다."""
    out = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and out:
            out[-1] += raw[1:]
        else:
            out.append(raw)
    return out


def _ics_dt(val):
    """DTSTART 값 → (date, time|None). Z(UTC)는 로컬로 바꾼다. TZID 는 로컬로 근사한다."""
    val = (val or "").strip()
    m = re.match(r"^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})?(Z?))?$", val)
    if not m:
        return None, None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not m.group(4):
        return datetime.date(y, mo, d), None
    hh, mm = int(m.group(4)), int(m.group(5))
    dt = datetime.datetime(y, mo, d, hh, mm)
    if m.group(7) == "Z":
        dt = dt.replace(tzinfo=datetime.timezone.utc).astimezone()
        return dt.date(), "%02d:%02d" % (dt.hour, dt.minute)
    return dt.date(), "%02d:%02d" % (hh, mm)


def _rrule_parts(line):
    out = {}
    for piece in line.split(";"):
        if "=" in piece:
            k, v = piece.split("=", 1)
            out[k.upper()] = v
    return out


_BYDAY_IDX = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def parse_ics_events(text, start, end):
    """VEVENT 목록 → [{date, time, title}] (창 [start, end] 안만).

    RECURRENCE-ID(개별 회차 수정)와 EXDATE(회차 취소)를 존중한다 —
    안 하면 옮긴 미팅이 두 번 나온다.
    """
    events = []            # (uid, date, time, title, is_override)
    overrides = set()      # (uid, date) — 개별 수정 회차가 원 회차를 대체
    cur = None
    for line in _ics_unfold(text):
        u = line.upper()
        if u == "BEGIN:VEVENT":
            cur = {"exdates": set()}
            continue
        if cur is None:
            continue
        if u == "END:VEVENT":
            uid = cur.get("uid") or ""
            title = (cur.get("summary") or "(제목 없음)").strip()[:120]
            d0, t0 = cur.get("dtstart", (None, None))
            if d0 is not None:
                rec_id = cur.get("recurrence_id")
                if rec_id:
                    overrides.add((uid, rec_id))
                    if start <= d0 <= end:
                        events.append((uid, d0, t0, title, True))
                else:
                    rr = cur.get("rrule")
                    occs = [d0]
                    if rr:
                        occs = _expand_rrule(d0, rr, start, end)
                    for od in occs:
                        if start <= od <= end and od.isoformat() not in cur["exdates"]:
                            events.append((uid, od, t0, title, False))
            cur = None
            continue
        key, _, val = line.partition(":")
        base = key.split(";", 1)[0].upper()
        if base == "SUMMARY":
            cur["summary"] = val
        elif base == "UID":
            cur["uid"] = val.strip()
        elif base == "DTSTART":
            cur["dtstart"] = _ics_dt(val)
        elif base == "RRULE":
            cur["rrule"] = _rrule_parts(val)
        elif base == "RECURRENCE-ID":
            rd, _rt = _ics_dt(val)
            cur["recurrence_id"] = rd
        elif base == "EXDATE":
            for piece in val.split(","):
                xd, _xt = _ics_dt(piece)
                if xd:
                    cur["exdates"].add(xd.isoformat())
    # 개별 수정 회차가 있으면 원 회차를 뺀다
    out = []
    for uid, d, t, title, is_override in events:
        if not is_override and (uid, d) in overrides:
            continue
        out.append({"date": d.isoformat(), "time": t, "title": title})
    out.sort(key=lambda e: (e["date"], e["time"] or ""))
    return out[:200]


def _expand_rrule(d0, rr, start, end):
    """DAILY·WEEKLY 만 전개한다. 그 외(MONTHLY·YEARLY·복잡 규칙)는 시작 회차만."""
    freq = (rr.get("FREQ") or "").upper()
    if freq not in ("DAILY", "WEEKLY"):
        return [d0]
    try:
        interval = max(1, int(rr.get("INTERVAL", "1")))
    except ValueError:
        interval = 1
    until = None
    if rr.get("UNTIL"):
        until, _t = _ics_dt(rr["UNTIL"])
    count = None
    if rr.get("COUNT"):
        try:
            count = int(rr["COUNT"])
        except ValueError:
            count = None
    days = None
    if freq == "WEEKLY":
        byday = [b for b in (rr.get("BYDAY") or "").split(",") if b in _BYDAY_IDX]
        days = {_BYDAY_IDX[b] for b in byday} or {d0.weekday()}
    occs, n, cursor = [], 0, d0
    limit_date = min(end, until) if until else end
    # COUNT 는 시작일부터 세므로 창 앞 구간도 세며 지나간다 (창 안만 담는다)
    while cursor <= limit_date and n < 500:
        if freq == "DAILY":
            hits = [cursor]
            step = datetime.timedelta(days=interval)
        else:
            week_start = cursor - datetime.timedelta(days=cursor.weekday())
            hits = sorted(week_start + datetime.timedelta(days=i) for i in days)
            hits = [h for h in hits if h >= d0]
            step = datetime.timedelta(days=7 * interval)
        for h in hits:
            if count is not None and n >= count:
                break
            if h > limit_date:
                continue
            n += 1
            if h >= start:
                occs.append(h)
        if count is not None and n >= count:
            break
        cursor += step
    return occs


def _gcal_split_local(s, all_day):
    """구글 시각 문자열 → (`YYYY-MM-DD`, `HH:MM`).

    캘린더마다 시간대 설정이 달라 `...Z` 로 올 때도 `...+09:00` 으로 올 때도 있다.
    **반드시 로컬 시각으로 변환한 뒤** 쪼갠다 — 문자열을 그냥 자르면 UTC 를
    한국 시각처럼 보여주는 사고가 난다(실측).
    """
    if not s:
        return None, None
    if all_day or len(s) == 10:
        return s[:10], None
    try:
        # 파이썬 3.9 의 fromisoformat 은 'Z' 를 못 읽는다 (맥 기본 python3 이 3.9 다)
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone()
        return dt.date().isoformat(), dt.strftime("%H:%M")
    except ValueError:
        return s[:10], None


def calendar_view():
    """홈의 '다가오는 일정' 과 텔레그램 브리핑이 함께 쓰는 창구.

    **구글 연결이 1순위다.** 예전엔 이 함수가 ICS·수기 파일만 봐서, 구글을 연결해도
    홈 목록은 "아직 일정이 연결되지 않았어요" 라고 말하고 브리핑도 구글 일정을 못 읽었다
    (같은 화면에서 격자에는 일정이 가득한데 옆 카드는 미연결이라 말하는 모순).
    """
    today = datetime.date.today()
    end = today + datetime.timedelta(days=CAL_WINDOW_DAYS)
    cfg = _read_json(CALENDAR_CFG, {}) or {}
    ics_url = cfg.get("icsUrl") if isinstance(cfg, dict) else None
    fallback_text = None
    try:
        with open(CAL_FALLBACK, encoding="utf-8", errors="replace") as f:
            fallback_text = f.read(8192)
    except OSError:
        pass

    key = _gcal_key()
    if key and key.get("refresh_token"):
        try:
            raw = gcal_list(
                today.isoformat() + "T00:00:00Z",
                (end + datetime.timedelta(days=1)).isoformat() + "T00:00:00Z",
            )
        except Exception as e:
            # 연결은 했는데 못 읽었다 — 빈 화면인 척하지 않고 사실대로
            return {"source": "google-error", "error": str(e), "events": [], "fallback": fallback_text}
        evs = []
        for g in raw:
            d, t = _gcal_split_local(g.get("start"), g.get("allDay"))
            if not d:
                continue
            evs.append({"date": d, "time": t, "title": g.get("title") or "(제목 없음)"})
        evs.sort(key=lambda e: (e["date"], e["time"] or "99:99"))
        return {"source": "google", "events": evs, "windowDays": CAL_WINDOW_DAYS}

    if isinstance(ics_url, str) and ics_url.startswith(("https://", "http://")):
        text, err = _fetch_ics(ics_url)
        if text:
            return {"source": "ics", "events": parse_ics_events(text, today, end),
                    "windowDays": CAL_WINDOW_DAYS}
        # 연동은 했는데 못 읽었다 — 빈 화면인 척하지 않고 사실대로 + 폴백을 보여준다
        return {"source": "ics-error", "error": err or "캘린더 주소를 읽지 못했어요",
                "events": [], "fallback": fallback_text}
    if fallback_text:
        return {"source": "fallback", "events": [], "fallback": fallback_text}
    return {"source": "none", "events": []}


# ============================================================
# 대화 이력 (W26) — 대화 도크가 지난 대화를 다시 보여줄 수 있게
# ------------------------------------------------------------
# 세션 전사 JSONL 에서 **사람이 주고받은 텍스트만** 뽑는다 (도구 호출·결과는 제외 —
# 그건 팀 탭 라이브 뷰의 몫이다). 전사는 이 폴더에서 돈 세션 것만 읽을 수 있다.
# ============================================================
def _parse_chat_file(path, limit=120):
    msgs = []
    idx_by_id = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                t = d.get("type")
                if t not in ("user", "assistant"):
                    continue
                m = d.get("message") or {}
                content = m.get("content")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = "\n".join(
                        b.get("text") for b in content
                        if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
                    )
                else:
                    text = ""
                text = (text or "").strip()
                if not text:
                    continue
                entry = {"role": t, "text": text[:8000], "at": d.get("timestamp")}
                mid = m.get("id")
                # 어시스턴트 메시지는 스트리밍 중 같은 id 로 여러 줄 쌓인다 — 마지막 것이 전체다
                if t == "assistant" and mid and mid in idx_by_id:
                    msgs[idx_by_id[mid]] = entry
                    continue
                if t == "assistant" and mid:
                    idx_by_id[mid] = len(msgs)
                msgs.append(entry)
    except OSError:
        return []
    return msgs[-limit:]


def read_chat(session_id):
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", session_id or ""):
        return None
    path = os.path.join(_transcript_dir(), "%s.jsonl" % session_id)
    if not os.path.isfile(path):
        return {"session": session_id, "messages": []}
    return {"session": session_id, "messages": _parse_chat_file(path)}



# ─────────────────────────────────────────────────────────────────────
# 구글 캘린더 — 저장된 열쇠로 읽고 쓴다 (표준 라이브러리만, pip 불필요)
#
# 쓰기 경계: **구글 캘린더는 우리 상태가 아니다.** 폰·웹·다른 앱이 이미 동시에 쓰고,
# 충돌 처리는 구글이 한다. 그래서 화면이 직접 고쳐도 된다.
# (반면 status.json·team.json 은 여전히 에이전트만 쓴다 — 그 경계는 넓히지 않는다.)
#
# 실측으로 확정한 것:
#  · refresh_token 으로 access_token 재발급이 동작한다 (앱 게시 완료 → 7일 만료 없음)
#  · 쓰기는 넣고 → 되읽고 → 지운다 로 검사해야 조용한 실패를 잡는다
# ─────────────────────────────────────────────────────────────────────
import urllib.request as _urlreq
import urllib.error as _urlerr
import urllib.parse as _urlparse

GCAL_KEY = os.path.join(ROOT, "state", "google-calendar.json")
_gcal_token = {"value": None, "expires": 0.0}


def _gcal_key():
    try:
        with open(GCAL_KEY, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _gcal_access_token():
    """access_token 은 1시간짜리다. 만료 전까지 재사용한다."""
    if _gcal_token["value"] and _gcal_token["expires"] > time.time() + 60:
        return _gcal_token["value"]
    k = _gcal_key()
    if not k or not k.get("refresh_token"):
        raise RuntimeError("아직 캘린더가 연결되지 않았어요")
    body = _urlparse.urlencode({
        "client_id": k["client_id"], "client_secret": k["client_secret"],
        "refresh_token": k["refresh_token"], "grant_type": "refresh_token",
    }).encode()
    req = _urlreq.Request("https://oauth2.googleapis.com/token", data=body,
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
    with _urlreq.urlopen(req, timeout=15) as r:
        d = json.loads(r.read().decode("utf-8"))
    if not d.get("access_token"):
        raise RuntimeError("캘린더 연결이 끊겼어요. 구글에서 접근을 취소하셨을 수 있어요.")
    _gcal_token["value"] = d["access_token"]
    _gcal_token["expires"] = time.time() + int(d.get("expires_in", 3600))
    return d["access_token"]


def _gcal_call(path, method="GET", payload=None):
    tok = _gcal_access_token()
    data = json.dumps(payload).encode() if payload is not None else None
    req = _urlreq.Request("https://www.googleapis.com/calendar/v3" + path, data=data, method=method,
                          headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    try:
        with _urlreq.urlopen(req, timeout=20) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except _urlerr.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
        except Exception:
            msg = ""
        raise RuntimeError(msg or "구글 캘린더가 요청을 거절했어요")


def _gcal_to_event(g, cal=None):
    """구글 일정 → 화면이 쓰는 모양.

    `start`/`end` 는 구글이 준 **문자열 그대로** 둔다. 캘린더마다 시간대 설정이 달라서
    (실측: 같은 계정에서도 어떤 캘린더는 `Asia/Seoul`, 어떤 캘린더는 `UTC`) 구글이
    `...+09:00` 로 줄 때도 `...Z` 로 줄 때도 있다. 둘 다 오프셋을 품은 값이라
    화면의 `new Date(...)` 가 알아서 그 지역 시각으로 그린다.
    여기서 문자열을 잘라 시각을 만들면 **UTC 를 한국 시각처럼 보여주는 사고**가 난다(실측).
    """
    st, en = g.get("start") or {}, g.get("end") or {}
    all_day = not st.get("dateTime")
    e = {
        "id": g.get("id", ""),
        "title": g.get("summary") or "(제목 없음)",
        "start": st.get("dateTime") or st.get("date") or "",
        "end": en.get("dateTime") or en.get("date") or "",
        "allDay": all_day,
    }
    if cal:
        e["calendarId"] = cal.get("id", "")
        e["calendarName"] = cal.get("summary") or ""
        # 읽기 전용 캘린더(공휴일·공유받은 것)의 일정은 화면에서 고칠 수 없다
        e["writable"] = cal.get("accessRole") in ("owner", "writer")
    return e


_gcal_cals = {"value": None, "at": 0.0}


def gcal_calendars():
    """볼 캘린더 목록.

    **구글에서 체크를 꺼 둔 캘린더는 가져오지 않는다**(`selected` 가 false).
    대표 결정: 무엇을 볼지는 구글 사이드바에서 이미 고르고 있으니 그걸 그대로 따른다 —
    화면에 또 다른 켜기/끄기 설정을 만들지 않는다.

    권한이 좁은 옛 연결(`calendar.events` 만 받은 참가자)은 목록 조회가 403 이다.
    그때는 **기본 캘린더 하나**로 돌아간다 — 연결이 깨지는 게 아니라 좁아질 뿐이다.
    """
    if _gcal_cals["value"] is not None and time.time() - _gcal_cals["at"] < 600:
        return _gcal_cals["value"]
    try:
        items = _gcal_call("/users/me/calendarList").get("items", [])
        cals = [c for c in items if c.get("selected")]
        if not cals:  # 전부 꺼져 있으면 최소한 기본 캘린더는 본다
            cals = [c for c in items if c.get("primary")]
    except Exception:
        cals = [{"id": "primary", "summary": "", "accessRole": "owner", "primary": True}]
    _gcal_cals["value"] = cals
    _gcal_cals["at"] = time.time()
    return cals


def _gcal_body(payload):
    b = {}
    if payload.get("title") is not None:
        b["summary"] = payload["title"]
    all_day = bool(payload.get("allDay"))
    for side in ("start", "end"):
        v = payload.get(side)
        if not v:
            continue
        b[side] = {"date": v[:10]} if all_day else {"dateTime": v, "timeZone": "Asia/Seoul"}
    return b


def gcal_connection():
    k = _gcal_key()
    if not k or not k.get("refresh_token"):
        return {"status": "never"}
    try:
        _gcal_access_token()
        return {"status": "connected", "account": k.get("account")}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


def _cal_path(cid):
    return "/calendars/" + _urlparse.quote(str(cid or "primary"), safe="")


def gcal_list(frm, to):
    """켜 둔 캘린더들의 일정을 **합쳐서** 준다.

    한 캘린더가 실패해도(권한 회수·삭제 등) 나머지는 보여준다 — 하나 때문에
    화면 전체가 비면 사용자는 "연동이 깨졌다"고 읽는다.
    """
    q = _urlparse.urlencode({
        "timeMin": frm, "timeMax": to, "singleEvents": "true",
        "orderBy": "startTime", "maxResults": "500",
    })
    out = []
    for cal in gcal_calendars():
        try:
            d = _gcal_call(_cal_path(cal.get("id")) + "/events?" + q)
        except Exception:
            continue
        out.extend(_gcal_to_event(x, cal) for x in d.get("items", []))
    out.sort(key=lambda e: (e.get("start") or "", e.get("title") or ""))
    return out


class CalendarReadOnly(RuntimeError):
    """우리가 막은 것 — 구글이 거절한 게 아니다. 상태 코드를 502 로 내보내면 안 된다."""


def _writable_or_die(cid):
    """읽기 전용 캘린더(공휴일·공유받은 것)에 쓰려 하면 **사용자 말로** 막는다."""
    if not cid or cid == "primary":
        return
    for c in gcal_calendars():
        if c.get("id") == cid:
            if c.get("accessRole") not in ("owner", "writer"):
                raise CalendarReadOnly("이 캘린더는 읽기 전용이라 여기서는 못 고쳐요")
            return


def gcal_create(payload):
    cid = payload.get("calendarId") or "primary"
    _writable_or_die(cid)
    cal = next((c for c in gcal_calendars() if c.get("id") == cid), None)
    return _gcal_to_event(_gcal_call(_cal_path(cid) + "/events", "POST", _gcal_body(payload)), cal)


def gcal_update(payload):
    cid = payload.get("calendarId") or "primary"
    _writable_or_die(cid)
    cal = next((c for c in gcal_calendars() if c.get("id") == cid), None)
    eid = _urlparse.quote(str(payload.get("id", "")))
    return _gcal_to_event(_gcal_call(_cal_path(cid) + "/events/" + eid, "PATCH", _gcal_body(payload)), cal)


def gcal_delete(eid, cid=None):
    """지우기 전 원본을 돌려준다 — 되돌리기에 쓴다."""
    cid = cid or "primary"
    _writable_or_die(cid)
    cal = next((c for c in gcal_calendars() if c.get("id") == cid), None)
    q = _urlparse.quote(str(eid))
    before = None
    try:
        before = _gcal_to_event(_gcal_call(_cal_path(cid) + "/events/" + q), cal)
    except Exception:
        pass
    _gcal_call(_cal_path(cid) + "/events/" + q, "DELETE")
    return before


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

    def _read_body(self):
        """요청 본문을 바이트로 읽는다 — `Content-Length` 와 **chunked** 양쪽.

        chunked 를 처리 안 하면 길이가 0 으로 보여 **본문이 통째로 유실된다**.
        지금은 브라우저가 문자열 본문에 Content-Length 를 붙여서 안 드러나지만,
        보내는 쪽이 바뀌면 조용히 깨지는 자리다(같은 뿌리로 501 사고가 났다).
        """
        self._body_read = True
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            out = bytearray()
            while True:
                line = self.rfile.readline(65536)
                if not line:
                    break
                try:
                    size = int(line.strip().split(b";")[0] or b"0", 16)
                except ValueError:
                    break
                if size == 0:
                    self.rfile.readline(65536)
                    break
                left = size
                while left > 0:
                    got = self.rfile.read(min(left, 65536))
                    if not got:
                        return bytes(out)
                    out += got
                    left -= len(got)
                self.rfile.readline(65536)
            return bytes(out)
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        return self.rfile.read(n) if n > 0 else b""

    def _drain(self):
        """안 읽은 요청 본문을 비운다.

        **왜 필요한가 (실측 결함)**: 인증·Origin 검사에서 거부할 때 본문을 읽지 않고
        응답하면, keep-alive 연결에 남은 본문 바이트를 서버가 **다음 요청으로 파싱**한다.
        그러면 멀쩡한 그다음 요청이 `501 Unsupported method` 로 죽는다.
        사용자에게는 "토큰이 어긋난 뒤로 화면이 이상한 오류를 뱉는다" 로 보인다.
        """
        if self.command not in ("POST", "PUT", "PATCH", "DELETE"):
            return
        if getattr(self, "_body_read", False):
            return
        self._read_body()   # 읽어서 버린다 — 소켓에 안 남기는 게 목적이다

    def _json(self, code, obj):
        self._drain()
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ── 정적: index.html 에 세션 토큰을 주입해 내려준다 ───────
    def do_GET(self):
        self._body_read = False   # 핸들러는 연결 내내 재사용된다 — 요청마다 초기화
        path = self.path.split("?", 1)[0]
        if path == "/api/automations":
            ok, why = self._origin_ok()
            if not ok:
                return self._json(403, {"error": why})
            return self._json(200, {"items": automations_view()})
        if path == "/api/activity":
            ok, why = self._origin_ok()
            if not ok:
                return self._json(403, {"error": why})
            return self._json(200, {"items": activity_view()})
        if path == "/api/records":
            ok, why = self._origin_ok()
            if not ok:
                return self._json(403, {"error": why})
            return self._json(200, scan_records())
        if path == "/api/note":
            ok, why = self._origin_ok()
            if not ok:
                return self._json(403, {"error": why})
            from urllib.parse import urlparse, parse_qs, unquote
            q = parse_qs(urlparse(self.path).query)
            rel = unquote((q.get("path") or [""])[0])
            note = read_note(rel)
            if note is None:
                return self._json(404, {"error": "그 경로의 기록을 찾지 못했어요", "path": rel})
            return self._json(200, note)
        if path == "/api/search":
            ok, why = self._origin_ok()
            if not ok:
                return self._json(403, {"error": why})
            from urllib.parse import urlparse, parse_qs, unquote
            q = parse_qs(urlparse(self.path).query)
            term = unquote((q.get("q") or [""])[0])
            return self._json(200, {"q": term, "hits": search_notes(term)})
        if path == "/api/roster":
            ok, why = self._origin_ok()
            if not ok:
                return self._json(403, {"error": why})
            return self._json(200, {"roster": read_roster()})
        if path == "/api/calendar/events":
            ok, why = self._origin_ok()
            if not ok:
                return self._json(403, {"error": why})
            from urllib.parse import urlparse as _up, parse_qs as _pq
            q = _pq(_up(self.path).query)
            conn = gcal_connection()
            if conn.get("status") != "connected":
                return self._json(200, {"events": [], "connection": conn})
            try:
                evs = gcal_list((q.get("from") or [""])[0], (q.get("to") or [""])[0])
            except Exception as e:
                return self._json(200, {"events": [], "connection": {"status": "error", "reason": str(e)}})
            return self._json(200, {"events": evs, "connection": conn})
        if path == "/api/calendar":
            ok, why = self._origin_ok()
            if not ok:
                return self._json(403, {"error": why})
            return self._json(200, calendar_view())
        if path == "/api/chat/history":
            ok, why = self._origin_ok()
            if not ok:
                return self._json(403, {"error": why})
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            sid = (q.get("session") or [""])[0]
            chat = read_chat(sid)
            if chat is None:
                return self._json(400, {"error": "세션 형식이 올바르지 않습니다"})
            return self._json(200, chat)
        if path == "/api/state":
            ok, why = self._origin_ok()
            if not ok:
                return self._json(403, {"error": why})
            return self._json(200, {
                "status": _read_json(os.path.join(ROOT, "state", "status.json"), None),
                "team": _read_json(os.path.join(ROOT, "state", "team.json"), None),
            })
        if path == "/api/sessions":
            ok, why = self._origin_ok()
            if not ok:
                return self._json(403, {"error": why})
            return self._json(200, {"sessions": list_sessions(), "queued": queued_view()})
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
        # 루트로 들어오면 **리다이렉트**한다 — 여기서 index.html 을 그대로 내주면
        # 상대 경로 자산(gyeol.tokens.css · gyeol.components.css · gyeol.js)이
        # `/gyeol.*` 로 풀려 전부 404 가 되고 **스타일 없는 화면**이 뜬다(실측 결함).
        # 주소창에 127.0.0.1:포트 만 치는 건 아주 흔한 행동이라 반드시 살려야 한다.
        if path in ("/", "/dashboard", "/dashboard/"):
            self.send_response(302)
            self.send_header("Location", "/dashboard/index.html")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/dashboard/index.html":
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
    def do_PATCH(self):
        self._body_read = False   # 핸들러는 연결 내내 재사용된다 — 요청마다 초기화
        route = self.path.split("?", 1)[0]
        if route != "/api/calendar/events":
            return self._json(404, {"error": "not found"})
        return self._calendar_write("PATCH", route)

    def do_DELETE(self):
        self._body_read = False   # 핸들러는 연결 내내 재사용된다 — 요청마다 초기화
        route = self.path.split("?", 1)[0]
        if route != "/api/calendar/events":
            return self._json(404, {"error": "not found"})
        return self._calendar_write("DELETE", route)

    def _calendar_write(self, method, route):
        """구글 캘린더 쓰기. 실패는 **사용자 말로** 돌려준다."""
        ok, why = self._origin_ok()
        if not ok:
            return self._json(403, {"error": why})
        if not self._token_ok():
            return self._json(403, {"error": "세션 토큰이 없거나 틀립니다"})
        try:
            body = json.loads(self._read_body().decode("utf-8") or "{}")
        except Exception:
            return self._json(400, {"error": "본문을 읽을 수 없습니다"})
        try:
            if method == "DELETE":
                from urllib.parse import urlparse as _up, parse_qs as _pq
                _q = _pq(_up(self.path).query)
                eid = (_q.get("id") or [""])[0]
                cid = (_q.get("calendarId") or [""])[0]
                if not eid:
                    return self._json(400, {"error": "어느 일정인지 알 수 없어요"})
                # 되돌리기가 **같은 캘린더로** 복원되도록 원본에 calendarId 를 실어 돌려준다
                return self._json(200, {"removed": gcal_delete(eid, cid)})
            if method == "PATCH":
                return self._json(200, gcal_update(body))
            if route == "/api/calendar/undo":
                return self._json(200, gcal_create(body))
            return self._json(200, gcal_create(body))
        except CalendarReadOnly as e:
            return self._json(403, {"error": str(e)})
        except Exception as e:
            return self._json(502, {"error": str(e)})

    def do_POST(self):
        self._body_read = False   # 핸들러는 연결 내내 재사용된다 — 요청마다 초기화
        route = self.path.split("?", 1)[0]
        if route == "/api/jobs":
            return self._dispatch_job()
        if route == "/api/automations":
            return self._save_automation()
        if route == "/api/jobs/stop":
            return self._stop_job()
        if route in ("/api/calendar/events", "/api/calendar/undo"):
            return self._calendar_write("POST", route)
        if route != "/api/ask":
            return self._json(404, {"error": "not found"})

        ok, why = self._origin_ok()
        if not ok:
            return self._json(403, {"error": why})
        if not self._token_ok():
            return self._json(403, {"error": "세션 토큰이 없거나 틀립니다"})

        try:
            payload = json.loads(self._read_body().decode("utf-8") or "{}")
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


    # ── 자동화 켜기/끄기·수정 (W22-C) ─────────────────────────
    # 자동화 설정은 **사업 상태가 아니라 사용자의 환경 설정**이므로 대시보드가 쓴다.
    # status.json·team.json 은 여전히 에이전트만 쓴다(STATE_CONTRACT 경계).
    def _save_automation(self):
        ok, why = self._origin_ok()
        if not ok:
            return self._json(403, {"error": why})
        if not self._token_ok():
            return self._json(403, {"error": "세션 토큰이 없거나 틀립니다"})
        try:
            payload = json.loads(self._read_body().decode("utf-8") or "{}")
        except Exception:
            return self._json(400, {"error": "본문을 읽을 수 없습니다"})
        aid = str(payload.get("id") or "")
        if not aid:
            return self._json(400, {"error": "id 가 필요합니다"})
        with _state_lock:
            data = load_automations()
            target = None
            for it in data["items"]:
                if it.get("id") == aid:
                    target = it
                    break
            if target is None:
                return self._json(404, {"error": "그 자동화를 찾지 못했어요"})
            if "enabled" in payload:
                target["enabled"] = bool(payload["enabled"])
                target["failStreak"] = 0          # 다시 켜면 실패 기록은 초기화
            if isinstance(payload.get("prompt"), str) and payload["prompt"].strip():
                target["prompt"] = payload["prompt"].strip()[:4000]
            tr = payload.get("trigger")
            if isinstance(tr, dict):
                cur = target.get("trigger") or {}
                if tr.get("kind") in ("daily", "weekly"):
                    cur["kind"] = tr["kind"]
                if re.fullmatch(r"[0-2]?\d:[0-5]\d", str(tr.get("at") or "")):
                    cur["at"] = str(tr["at"])
                if str(tr.get("day", "")).isdigit():
                    cur["day"] = max(1, min(7, int(tr["day"])))
                target["trigger"] = cur
            if payload.get("deliver") in ("note", "notify", "both"):
                target["deliver"] = payload["deliver"]
            save_automations(data)
        return self._json(200, {"ok": True, "items": automations_view()})

    # ── 작업 그만두기 ────────────────────────────────────────
    # blocked(답을 기다리는) 세션은 순차 큐를 영구히 막는다 — 화면에서 끊을 수 있어야 한다.
    def _stop_job(self):
        ok, why = self._origin_ok()
        if not ok:
            return self._json(403, {"error": why})
        if not self._token_ok():
            return self._json(403, {"error": "세션 토큰이 없거나 틀립니다"})
        try:
            payload = json.loads(self._read_body().decode("utf-8") or "{}")
        except Exception:
            return self._json(400, {"error": "본문을 읽을 수 없습니다"})
        jid = str(payload.get("id") or "")
        # 대기열 항목은 `qid` 로 온다 — 아직 안 나간 것은 목록에서 빼면 끝이다(멈출 프로세스가 없다).
        qid = str(payload.get("qid") or "")
        key = qid or jid
        if not re.fullmatch(r"[0-9a-fA-F-]{4,40}", key):
            return self._json(400, {"error": "작업 id 가 올바르지 않습니다"})
        cancelled = False
        with _queue_lock:
            for it in list(_job_queue):
                if it.get("qid") != key:
                    continue
                if it.get("jobId"):
                    jid = it["jobId"]           # 이미 출발했으면 아래에서 그 작업을 멈춘다
                else:
                    _job_queue.remove(it)
                    cancelled = True
                break
            else:
                jid = _qid_jobs.get(key, jid)   # 큐를 떠난 뒤에 눌러도 이어지도록
        if cancelled:
            log_activity({"kind": "request", "title": "대기 취소 (%s)" % key,
                          "state": "done", "summary": "사용자가 대기열에서 뺐어요"})
            return self._json(200, {"ok": True, "cancelled": "queued"})
        if not re.fullmatch(r"[0-9a-fA-F-]{4,40}", jid):
            return self._json(400, {"error": "작업 id 가 올바르지 않습니다"})
        try:
            subprocess.run(["claude", "stop", jid], cwd=ROOT, start_new_session=True,
                           capture_output=True, text=True, timeout=30)
        except Exception as e:
            return self._json(500, {"error": "그만두지 못했어요: %s" % e})
        _sessions_cache["data"] = None
        log_activity({"kind": "request", "title": "작업 그만두기 (%s)" % jid,
                      "state": "done", "summary": "사용자가 화면에서 중단"})
        return self._json(200, {"ok": True})

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
            payload = json.loads(self._read_body().decode("utf-8") or "{}")
        except Exception:
            return self._json(400, {"error": "본문을 읽을 수 없습니다"})

        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            return self._json(400, {"error": "지시문이 비었습니다"})
        if len(prompt) > MAX_PROMPT:
            return self._json(400, {"error": "지시문이 너무 깁니다(%d자 상한)" % MAX_PROMPT})
        if not claude_available():
            return self._json(503, {"error": "claude 명령을 찾을 수 없어요."})

        # 검증을 통과하면 **무조건 접수**한다(W25 — 이전의 429 를 대체).
        # 슬롯이 비면 지금 보내고, 차 있으면 대기열에 넣는다. 순서는 들어온 순서.
        title = prompt.split("\n")[0][:60]
        with _queue_lock:
            waiting = any(not it.get("jobId") for it in _job_queue)
        if waiting or _bg_working() >= BG_CONCURRENT:
            qid = uuid.uuid4().hex[:8]
            with _queue_lock:
                _job_queue.append({"qid": qid, "prompt": prompt, "title": title})
                position = sum(1 for it in _job_queue if not it.get("jobId"))
            return self._json(200, {
                "queued": True, "qid": qid, "position": position,
                # 대기열은 메모리에만 있다 — 이 전제를 응답에도 담는다(화면 문구와 같은 말)
                "note": "이 서버(터미널)가 켜져 있는 동안 순서대로 시작돼요.",
            })

        job_id, raw = _launch_bg(prompt)
        if not job_id:
            log_activity({"kind": "request", "title": title,
                          "state": "failed", "summary": raw[:200]})
            return self._json(500, {"error": "작업 id 를 확인하지 못했어요", "raw": raw})
        # 요청형도 내역에 남긴다 — "파트너가 어떻게 움직이는지" 는 자율형만의 이야기가 아니다
        log_activity({"kind": "request", "title": title,
                      "state": "started", "jobId": job_id})
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
                # ★ 파트너(claude)를 **자기 세션·프로세스 그룹**으로 떼어낸다.
                #   안 떼면 claude 가 이 서버의 프로세스 그룹을 물려받는다. 그러면
                #   누군가 그 그룹을 향해 신호를 보낼 때 **대시보드 서버까지 같이 죽는다**
                #   (실측: 실증 스위트에서 브리지가 SIGTERM/SIGKILL 로 사라짐).
                #   참가자 입장에서도 같은 문제다 — 작업 하나 멈추자고 대시보드가 꺼지면 안 된다.
                start_new_session=True,
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
        t = threading.Thread(target=_scheduler_loop, daemon=True)
        t.start()
        # 대기열은 이 서버가 살아 있는 동안만 존재한다 — 끄면 대기 중인 것은 사라진다
        threading.Thread(target=_queue_pump, daemon=True).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  서버를 멈췄어요.")


if __name__ == "__main__":
    main()
