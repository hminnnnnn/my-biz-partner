#!/usr/bin/env bash
# refresh-dashboard.sh — state/*.json → state/dashboard-data.js 재생성 (파생 파일 동기화)
#
# STATE_CONTRACT ③: status.json 또는 team.json 을 바꾼 뒤 반드시 이 스크립트를 실행하세요.
# 래퍼(dashboard-data.js)를 손으로 쓰지 마세요 — 손 복사는 반드시 어긋납니다.
#
# 하는 일:
#   1) status.json + team.json 을 내용 그대로 window.DASHBOARD_DATA 로 래핑 (원본 불변)
#   2) [python3 있을 때] status.todos[] 의 open 항목 중 due<오늘 을 overdue 로 파생 판정
#   3) [python3 있을 때] notes/ 5분류 + notes/projects 스캔 → records 블록 주입
#   python3 가 없으면 1)만 수행(기본 래핑) — 대시보드가 자체 계산으로 보완하므로 동작에 지장 없음.
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

for f in state/status.json state/team.json; do
  if [ ! -f "$f" ]; then
    echo "✗ $f 가 없습니다. ./install.sh 를 먼저 실행하세요."
    exit 1
  fi
done

if command -v python3 > /dev/null 2>&1; then
  # JSON 유효성 검사 — 망가진 JSON이 대시보드로 흘러가지 않게.
  python3 -m json.tool state/status.json > /dev/null || { echo "✗ state/status.json 이 올바른 JSON이 아닙니다. 고친 뒤 다시 실행하세요."; exit 1; }
  python3 -m json.tool state/team.json   > /dev/null || { echo "✗ state/team.json 이 올바른 JSON이 아닙니다. 고친 뒤 다시 실행하세요."; exit 1; }

  # 래핑 + 파생(todos overdue 재계산 · notes 스캔 records) — 전부 파생값이라 원본 JSON은 건드리지 않음.
  python3 - <<'PY'
import json, os, re, datetime

def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)

status = load("state/status.json")
team   = load("state/team.json")

# --- (2) todos overdue 파생: open & due<오늘 → overdue (원본 파일엔 안 씀, 래퍼에만) ---
today = datetime.date.today().isoformat()
todos = status.get("todos")
if isinstance(todos, list):
    for t in todos:
        if isinstance(t, dict) and t.get("state") == "open":
            due = t.get("due")
            if isinstance(due, str) and due and due < today:
                t["state"] = "overdue"

# --- (3) notes/ 스캔 → records ---
FOLDERS = ["inbox", "meetings", "customers", "ideas", "issues"]

def file_date(name, path):
    m = re.match(r"(\d{4}-\d{2}-\d{2})", name)
    if m:
        return m.group(1)
    try:
        return datetime.date.fromtimestamp(os.path.getmtime(path)).isoformat()
    except OSError:
        return ""

def title_of(name):
    stem = re.sub(r"\.(md|csv|xlsx)$", "", name)
    return re.sub(r"^\d{4}-\d{2}-\d{2}-", "", stem)

# 내장 마크다운 뷰어용 내용 임베드 — 파일당 16KB 상한(초과분은 잘라내고 표시), 실패 시 생략(additive)
CONTENT_CAP = 16 * 1024

def read_content(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(CONTENT_CAP + 1)
        if len(text) > CONTENT_CAP:
            return text[:CONTENT_CAP] + "\n\n… (내용이 길어 여기까지만 표시 — '파일로 이동'으로 전체를 보세요)"
        return text
    except OSError:
        return None

# 표 형식(csv/xlsx) 파싱 — 표준 라이브러리만, 실패 시 None (항목은 유지, 뷰어는 폴백)
TABLE_MAX_ROWS, TABLE_MAX_COLS = 200, 24

def parse_csv(path):
    import csv as _csv
    try:
        with open(path, newline="", encoding="utf-8", errors="replace") as f:
            rows = []
            for i, row in enumerate(_csv.reader(f)):
                if i >= TABLE_MAX_ROWS: break
                rows.append([c for c in row[:TABLE_MAX_COLS]])
            return rows or None
    except Exception:
        return None

def parse_xlsx(path):
    # 첫 시트만 — zipfile + xml.etree 최소 파서 (openpyxl 등 외부 의존 금지)
    import zipfile, re as _re
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
            sheet = names[0] if names else None
            if not sheet: return None
            root = ET.fromstring(z.read(sheet))
            rows = []
            for row in root.iter(NS + "row"):
                if len(rows) >= TABLE_MAX_ROWS: break
                cells = {}
                for c in row.iter(NS + "c"):
                    ref = c.get("r") or ""
                    m = _re.match(r"([A-Z]+)", ref)
                    if not m: continue
                    ci = 0
                    for ch in m.group(1): ci = ci * 26 + (ord(ch) - 64)
                    ci -= 1
                    if ci >= TABLE_MAX_COLS: continue
                    v = c.find(NS + "v")
                    txt = v.text if v is not None and v.text is not None else ""
                    if c.get("t") == "s":
                        try: txt = shared[int(txt)]
                        except Exception: pass
                    elif c.get("t") == "inlineStr":
                        txt = "".join(t.text or "" for t in c.iter(NS + "t"))
                    cells[ci] = txt
                width = max(cells.keys()) + 1 if cells else 0
                rows.append([cells.get(i, "") for i in range(width)])
            return rows or None
    except Exception:
        return None

DOC_EXTS = (".md", ".csv", ".xlsx")

def kind_of(name):
    return "table" if (name.endswith(".csv") or name.endswith(".xlsx")) else "md"

counts, recent = {}, []
tree_notes = {}
for folder in FOLDERS:
    d = os.path.join("notes", folder)
    docs = []
    if os.path.isdir(d):
        for name in sorted(os.listdir(d)):
            if name.endswith(DOC_EXTS) and name != "README.md":
                docs.append(name)
    counts[folder] = len(docs)
    tree_notes[folder] = []
    for name in docs:
        p = "notes/%s/%s" % (folder, name)
        item = {"path": p, "title": title_of(name), "date": file_date(name, p),
                "folder": folder, "name": name, "kind": kind_of(name)}
        recent.append(item)
        tree_notes[folder].append({"path": p, "title": item["title"], "date": item["date"], "kind": item["kind"]})

recent.sort(key=lambda r: r["date"], reverse=True)
for fl in tree_notes.values():
    fl.sort(key=lambda r: r["date"], reverse=True)

# 모든 노트의 내용 맵 — 뷰어가 어느 항목(할 일 출처·이슈·칸반 티켓)에서든 열리게 한다.
# (최근 5건만 임베드하던 방식은 todos 출처에서 뷰어가 죽는 실결함을 낳아 전량 임베드로 교체)
contents = {}
for r in recent:
    if r.get("kind") == "table":
        tbl = parse_csv(r["path"]) if r["path"].endswith(".csv") else parse_xlsx(r["path"])
        if tbl is not None:
            contents[r["path"]] = {"title": r["title"], "table": tbl}
        continue
    c = read_content(r["path"])
    if c is not None:
        contents[r["path"]] = {"title": r["title"], "content": c}

recent = recent[:5]
for r in recent:
    e = contents.get(r["path"])
    if e and e.get("content") is not None:
        r["content"] = e["content"]

projects = []
tree_projects = {}
proot = os.path.join("notes", "projects")
if os.path.isdir(proot):
    for dirpath, _dirnames, filenames in os.walk(proot):
        for name in sorted(filenames):
            if name.endswith(DOC_EXTS) and name != "README.md":
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(dirpath, proot)
                slug = rel.split(os.sep)[0] if rel != "." else ""
                item = {"path": full, "title": title_of(name),
                        "date": file_date(name, full), "name": name, "kind": kind_of(name)}
                projects.append(item)
                tree_projects.setdefault(slug, []).append(
                    {"path": full, "title": item["title"], "date": item["date"], "kind": item["kind"]})
projects.sort(key=lambda r: r["date"], reverse=True)
for r in projects:
    if r.get("kind") == "table":
        tbl = parse_csv(r["path"]) if r["path"].endswith(".csv") else parse_xlsx(r["path"])
        if tbl is not None:
            contents[r["path"]] = {"title": r["title"], "table": tbl}
        continue
    c = read_content(r["path"])
    if c is not None:
        r["content"] = c
        contents[r["path"]] = {"title": r["title"], "content": c}

# 폴더 트리 (additive) — 기록 탭 폴더 브라우저용: notes 5분류 + projects/<슬러그>별 전 파일
tree = {"notes": tree_notes, "projects": tree_projects}

records = {"counts": counts, "recent": recent, "projects": projects, "contents": contents, "tree": tree}

# --- (4) 우리 팀 roster 파생: roles/*.md 의 스키마 헤더에서 name·관점 추출 (additive) ---
roster = []
rdir = os.path.join(".claude", "skills", "project-team", "roles")
if os.path.isdir(rdir):
    for fname in sorted(os.listdir(rdir)):
        if not fname.endswith(".md"):
            continue
        rid = fname[:-3]
        name, focus = rid, ""
        try:
            with open(os.path.join(rdir, fname), encoding="utf-8", errors="replace") as f:
                text = f.read(4096)
            m = re.search(r"\*\*name\*\*:\s*(.+)", text)
            if m:
                name = m.group(1).strip()
            m = re.search(r"##\s*①\s*관점 한 줄\s*\n+(.+)", text)
            if m:
                focus = re.sub(r"[*\"“”]", "", m.group(1)).strip()[:60]
        except OSError:
            pass
        # 역할 카드 클릭 상세용 — 역할 md 전문 임베드 (뷰어가 역할 소개·할 일·작동 시점을 보여줌)
        full = read_content(os.path.join(rdir, fname))
        entry = {"id": rid, "name": name, "focus": focus}
        if full is not None:
            entry["content"] = full
        roster.append(entry)

data = {"status": status, "team": team, "records": records}
if roster:
    data["roster"] = roster

with open("state/dashboard-data.js", "w", encoding="utf-8") as f:
    f.write("// 자동 생성 파일 (refresh-dashboard.sh). 직접 수정하지 마세요.\n")
    f.write("window.DASHBOARD_DATA = ")
    json.dump(data, f, ensure_ascii=False, indent=2)
    f.write(";\n")
PY
  echo "✓ state/dashboard-data.js 갱신 완료 (status + team 래핑 + records 파생 + todos overdue 재계산)"
else
  # python3 미설치 폴백: 기본 래핑만 (records·overdue 파생 생략 — 대시보드가 자체 보완).
  {
    printf '// 자동 생성 파일 (refresh-dashboard.sh). 직접 수정하지 마세요.\n'
    printf 'window.DASHBOARD_DATA = {\n'
    printf '  "status": '
    cat state/status.json
    printf ',\n  "team": '
    cat state/team.json
    printf '\n};\n'
  } > state/dashboard-data.js
  echo "✓ state/dashboard-data.js 갱신 완료 (python3 없음 — 기본 래핑만, records 파생 생략)"
fi
