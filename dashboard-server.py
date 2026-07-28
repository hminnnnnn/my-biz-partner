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
        p = subprocess.run(["claude", "--bg", prompt], cwd=ROOT,
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


# ═══════════════════════════════════════════════════════════════════
#  기록 스캔 — 파일 모드를 폐지하면서 래퍼(dashboard-data.js) 의존을 끊는다.
#
#  전에는 브라우저가 파일시스템을 못 훑어서 refresh-dashboard.sh 가 만든 래퍼에
#  기록 목록·본문을 통째로 박아 넣었다. 그래서 파트너가 기록을 남겨도 래퍼를 다시
#  만들기 전까지 화면에 안 보였다. 서버가 직접 훑으면 **항상 최신**이다.
#
#  규칙은 refresh-dashboard.sh 와 동일하게 맞춘다 (같은 화면을 두 곳에서 다르게 그리면 안 된다).
# ═══════════════════════════════════════════════════════════════════
NOTE_FOLDERS = ["inbox", "meetings", "customers", "ideas", "issues"]
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
    for folder in NOTE_FOLDERS:
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
    def do_POST(self):
        route = self.path.split("?", 1)[0]
        if route == "/api/jobs":
            return self._dispatch_job()
        if route == "/api/automations":
            return self._save_automation()
        if route == "/api/jobs/stop":
            return self._stop_job()
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
            n = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
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
            n = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:
            return self._json(400, {"error": "본문을 읽을 수 없습니다"})
        jid = str(payload.get("id") or "")
        if not re.fullmatch(r"[0-9a-fA-F-]{4,40}", jid):
            return self._json(400, {"error": "작업 id 가 올바르지 않습니다"})
        try:
            subprocess.run(["claude", "stop", jid], cwd=ROOT,
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
        # **working 만 큐를 막는다.** blocked 는 "답하고 더 기다리는 중" 인 경우가 많아서
        # (실측: 이미 정답을 낸 세션이 blocked 로 남아 있었다) 그것까지 막으면 사용자가 갇힌다.
        for s in list_sessions():
            if s.get("kind") == "background" and s.get("state") == "working":
                hint = ""
                if s.get("state") == "blocked" and not workspace_trusted():
                    hint = ("이 폴더가 아직 Claude Code 신뢰 승인 전이라 권한 물음에서 멈춰 있어요. "
                            "그만두기를 누르고, 이 폴더에서 claude 를 한 번 실행해 신뢰를 수락해 주세요.")
                elif s.get("state") == "blocked":
                    hint = "그 작업이 답을 기다리고 있어요. 답해 주거나 그만두기를 누르세요."
                return self._json(429, {
                    "error": "이미 진행 중인 작업이 있어요",
                    "hint": hint,
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
            log_activity({"kind": "request", "title": prompt.split("\n")[0][:60],
                          "state": "failed", "summary": (p.stdout or p.stderr or "")[:200]})
            return self._json(500, {"error": "작업 id 를 확인하지 못했어요",
                                    "raw": (p.stdout or p.stderr or "")[:300]})
        # 요청형도 내역에 남긴다 — "파트너가 어떻게 움직이는지" 는 자율형만의 이야기가 아니다
        log_activity({"kind": "request", "title": prompt.split("\n")[0][:60],
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
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  서버를 멈췄어요.")


if __name__ == "__main__":
    main()
