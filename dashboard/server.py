"""모카 dashboard: a local, developer-only view of the loop (documents/dashboard.md in the moca project).

Deliberately separate from the moca code: it imports nothing from it. Its only contract is the files moca keeps
under data/ — goals, steps, tasks, knowledge, consent and the loop's runtime status. It reads them, and writes
exactly one thing: a new top-level goal into data/goals/goals.json (checked with the same rules as the harness).

Member names are shown the way 운영 모카 sees them: filled in only for members who allowed 모임 운영 use,
'한 멤버' for everyone else (data/members/scope_consent.json).

Run (from the illit/ directory):
    python -m dashboard                      # http://127.0.0.1:8765, reads ../moca/data next to this package
    python -m dashboard --data PATH --port N
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import secrets
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = pathlib.Path(__file__).resolve().parent
STATIC = HERE / "static"
DEFAULT_DATA = HERE.parent / "moca" / "data"
ANONYMOUS = "한 멤버"
FMT = "%Y-%m-%d %H:%M:%S"
ALIVE_WITHIN = 5 * 60          # the loop counts as running if it showed a sign of life this recently
FINISHED = ("achieved", "closed")
# the harness's limits for a goal (moca: harness/goals.py) — repeated here because this package can't import it
OBJECTIVE_LIMIT = 200
MAX_CRITERIA = 5
GOAL_ID = re.compile(r"^g_[a-z0-9_]{1,40}$")

DATA = DEFAULT_DATA
_write_lock = threading.Lock()


# --- reading moca's files ---------------------------------------------------------------------

def _json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default


def _jsonl(path):
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass  # a line being written right now
    return out


def _sources():
    return [DATA / "goals" / "goals.json", DATA / "goals" / "steps.jsonl", DATA / "tasks" / "log.jsonl",
            DATA / "knowledge" / "records.jsonl", DATA / "members" / "scope_consent.json",
            DATA / "runtime" / "status.json", DATA / "runtime" / "presence.json"] + sorted((DATA / "tasks").glob("t_*.json"))


def fingerprint():
    """Changes whenever any file the dashboard shows changes."""
    parts = []
    for p in _sources():
        try:
            st = p.stat()
            parts.append(f"{p.name}:{st.st_mtime_ns}:{st.st_size}")
        except FileNotFoundError:
            parts.append(f"{p.name}:-")
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


class Names:
    """Placeholders ({s0}, {s1}, …) filled in the way 운영 모카 sees them."""

    def __init__(self):
        consent = _json(DATA / "members" / "scope_consent.json", {})
        self.allowed = {m for m, e in consent.items() if e.get("sharing") is True}

    def fill(self, text, subjects):
        text = text or ""
        for i, name in enumerate(subjects or []):
            text = text.replace(f"{{s{i}}}", name if name in self.allowed else ANONYMOUS)
        return re.sub(r"\{s\d+\}", ANONYMOUS, text)  # a placeholder we can't resolve never shows a name


def _tasks(names):
    """Every task, live or consumed: live ones from their files, the rest rebuilt from the task log."""
    tasks = {}
    for e in _jsonl(DATA / "tasks" / "log.jsonl"):
        t = tasks.setdefault(e["task"], {"id": e["task"], "events": [], "consumed": False})
        t["events"].append(e)
        ev = e["event"]
        if ev == "created":
            t.update(kind="group_chat", executor={"type": "agent", "name": "chat_moca"}, status="queued",
                     instruction=e.get("instruction"), goal_id=e.get("goal_id"), created_at=e["at"],
                     created_by=e.get("created_by"), deadline=e.get("deadline"))
        elif ev == "started":
            t["status"] = "running"
        elif ev == "finished":
            t.update(status=e["status"], outcome=e.get("outcome"), finished_at=e["at"], finished_by=e.get("finished_by"))
        elif ev == "consumed":
            t["consumed"] = True
            t["summary"] = e.get("summary")
            t["summary_subjects"] = e.get("summary_subjects")
        elif ev == "tool":
            t.update(kind="tool", executor={"type": "tool", "name": e.get("tool")}, status=e["status"],
                     goal_id=e.get("goal_id"), created_at=e["at"], arguments=e.get("arguments"), result=e.get("result"))
    for path in (DATA / "tasks").glob("t_*.json"):
        live = _json(path, None)
        if not live:
            continue
        t = tasks.setdefault(live["id"], {"id": live["id"], "events": [], "consumed": False})
        t.update(kind="group_chat", executor=live["spec"]["executor"], status=live["status"],
                 instruction=live["spec"].get("instruction"), goal_id=live.get("goal_id"),
                 created_at=live.get("created_at"), deadline=live.get("deadline"), file=live)
        r = live.get("result")
        if r:
            t["outcome"] = r.get("outcome")
            t["summary"] = names.fill(r.get("summary"), r.get("summary_subjects"))
            t["file"] = dict(live, result=dict(r, summary=t["summary"], knowledge=[
                dict(k, statement=names.fill(k["statement"], k["subjects"])) for k in r.get("knowledge", [])]))
    for t in tasks.values():
        t.setdefault("kind", "unknown")
        t.setdefault("status", "unknown")
        t.setdefault("created_at", t["events"][0]["at"] if t["events"] else "")
    return sorted(tasks.values(), key=lambda t: t["created_at"] or "")


def _steps(names):
    steps = []
    for i, s in enumerate(_jsonl(DATA / "goals" / "steps.jsonl")):
        s = dict(s, n=i)
        if s.get("kind") == "task_result":
            s["summary"] = names.fill(s.get("summary"), s.get("summary_subjects"))
        steps.append(s)
    return steps


def _knowledge(names):
    records = []
    for r in _jsonl(DATA / "knowledge" / "records.jsonl"):
        records.append({"id": r["id"], "statement": names.fill(r["statement"], r["subjects"]),
                        "basis": r["basis"], "created_at": r["created_at"], "task": r["origin"].get("task"),
                        "channel": r["origin"].get("channel"), "sources": len(r.get("source_refs", []))})
    records.sort(key=lambda r: r["created_at"])
    by_basis, by_task = {}, {}
    for r in records:
        by_basis[r["basis"]] = by_basis.get(r["basis"], 0) + 1
        by_task[r["task"]] = by_task.get(r["task"], 0) + 1
    return records, {"total": len(records), "by_basis": by_basis, "by_task": by_task,
                     "today": sum(1 for r in records if r["created_at"].startswith(time.strftime("%Y-%m-%d")))}


def _loop():
    """Is the loop running, and what is it doing? From the runtime files moca writes."""
    status = _json(DATA / "runtime" / "status.json", {})
    presence = _json(DATA / "runtime" / "presence.json", {})
    signs = []
    if presence.get("last_seen"):
        signs.append(presence["last_seen"])
    for p in (DATA / "runtime" / "status.json", DATA / "goals" / "steps.jsonl"):
        try:
            signs.append(p.stat().st_mtime)
        except FileNotFoundError:
            pass
    last = max(signs) if signs else None
    alive = bool(last and time.time() - last < ALIVE_WITHIN)
    return {"alive": alive, "last_sign": dt.datetime.fromtimestamp(last).strftime(FMT) if last else None,
            "activity": status.get("activity") if alive else "stopped", "since": status.get("since"),
            "detail": status.get("detail", {}), "sleeps": presence.get("sleeps", [])[-5:]}


def _flow(steps, tasks):
    """The multi-agent message flow: 운영 모카's steps and every hand-over of a task, in time order."""
    flow = []
    for s in steps:
        if s.get("kind") == "task_result":
            flow.append({"at": s["at"], "goal_id": s["goal_id"], "from": "harness", "to": "admin", "type": "result",
                         "label": f"결과 전달 {s['task_id']}: {s.get('outcome')}", "detail": s.get("summary"), "ref": s["n"]})
        elif s.get("kind") == "cooldown":
            flow.append({"at": s["at"], "goal_id": s["goal_id"], "from": "harness", "to": "harness", "type": "cooldown",
                         "label": "쉬어 감", "detail": s.get("reason"), "ref": s["n"]})
        else:
            flow.append({"at": s["at"], "goal_id": s["goal_id"], "from": "admin", "to": "harness", "type": "step",
                         "label": f"{s.get('kind')} → {s.get('verdict')}", "detail": s.get("reason"), "ref": s["n"]})
    for t in tasks:
        for e in t["events"]:
            ev = e["event"]
            item = {"at": e["at"], "goal_id": t.get("goal_id"), "task": t["id"], "type": ev}
            if ev == "created":
                item.update(**{"from": "harness", "to": "chat"}, label=f"작업 전달 {t['id']}", detail=t.get("instruction"))
            elif ev == "started":
                item.update(**{"from": "chat", "to": "group"}, label="모임 채팅에 질문", detail=t.get("instruction"))
            elif ev == "finished":
                item.update(**{"from": "chat", "to": "harness"}, label=f"결과 보고 {t['id']}: {e.get('outcome')}",
                            detail=f"마무리: {e.get('finished_by')}")
            elif ev == "consumed":
                item.update(**{"from": "harness", "to": "harness"}, label=f"작업 정리 {t['id']}", detail=None)
            elif ev == "tool":
                item.update(**{"from": "harness", "to": "tool"}, label=f"도구 {e.get('tool')}: {e['status']}",
                            detail=(e.get("result") or "")[:200])
            else:
                continue
            flow.append(item)
    flow.sort(key=lambda f: f["at"])
    return flow


def state():
    names = Names()
    goals = _json(DATA / "goals" / "goals.json", [])
    steps = _steps(names)
    tasks = _tasks(names)
    results = {s["task_id"]: s["summary"] for s in steps if s.get("kind") == "task_result"}
    for t in tasks:
        if t["id"] in results:
            t["summary"] = results[t["id"]]
        elif t.get("summary") and "{s" in t["summary"]:
            t["summary"] = names.fill(t["summary"], t.get("summary_subjects"))
    knowledge, stats = _knowledge(names)
    return {"now": time.strftime(FMT), "data": str(DATA), "loop": _loop(), "goals": goals, "steps": steps,
            "tasks": tasks, "knowledge": knowledge, "knowledge_stats": stats, "flow": _flow(steps, tasks),
            "can_add_goal": not any(g["parent_id"] is None and g["status"] not in FINISHED for g in goals)}


# --- the one write: a new top-level goal -------------------------------------------------------

def add_goal(body):
    """Validate and append a top-level goal to goals.json. Returns (goal, None) or (None, problem)."""
    objective = (body.get("objective") or "").strip()
    criteria = [c.strip() for c in body.get("completion_criteria") or [] if isinstance(c, str) and c.strip()]
    goal_id = (body.get("id") or "").strip()
    if not objective or len(objective) > OBJECTIVE_LIMIT:
        return None, f"목표(objective)는 1~{OBJECTIVE_LIMIT}자여야 합니다"
    if not 1 <= len(criteria) <= MAX_CRITERIA:
        return None, f"완료 기준은 1~{MAX_CRITERIA}개여야 합니다"
    if any(len(c) > OBJECTIVE_LIMIT for c in criteria):
        return None, f"완료 기준 하나는 {OBJECTIVE_LIMIT}자 이내여야 합니다"
    if goal_id and not GOAL_ID.match(goal_id):
        return None, "id는 g_로 시작하고 소문자·숫자·밑줄만 쓸 수 있습니다 (예: g_first_study)"
    path = DATA / "goals" / "goals.json"
    with _write_lock:
        goals = _json(path, [])  # re-read right before writing: the loop saves this file too
        if any(g["parent_id"] is None and g["status"] not in FINISHED for g in goals):
            return None, "이미 진행 중인 최상위 목표가 있습니다. 그 목표가 끝난 뒤에 새로 추가하세요"
        goal_id = goal_id or f"g_{dt.datetime.now():%Y%m%d_%H%M}_{secrets.token_hex(1)}"
        if any(g["id"] == goal_id for g in goals):
            return None, f"{goal_id} 목표가 이미 있습니다"
        goal = {"id": goal_id, "parent_id": None, "objective": objective, "completion_criteria": criteria,
                "status": "active", "outcome": None, "created_at": time.strftime(FMT), "created_by": "모임장(대시보드)",
                "wait": None, "last_step_at": None, "cooldown_until": None}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".dashboard.tmp")
        tmp.write_text(json.dumps(goals + [goal], ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)  # never leaves a half-written goals.json behind
    return goal, None


# --- http ----------------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "moca-dashboard"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/state":
            return self._send(200, state())
        if path == "/api/stream":
            return self._stream()
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        file = (STATIC / name).resolve()
        if STATIC not in file.parents or not file.is_file():
            return self._send(404, {"error": "not found"})
        ctype = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}.get(file.suffix, "application/octet-stream")
        self._send(200, file.read_bytes(), ctype + "; charset=utf-8")

    def do_POST(self):
        if urlparse(self.path).path != "/api/goals":
            return self._send(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self._send(400, {"error": "JSON이 아닙니다"})
        goal, problem = add_goal(body)
        if problem:
            return self._send(HTTPStatus.CONFLICT if "진행 중" in problem else 400, {"error": problem})
        self._send(201, {"goal": goal})

    def _stream(self):
        """Server-sent events: a full snapshot whenever any watched file changes (checked every second)."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last, beat = None, time.time()
        try:
            while True:
                fp = fingerprint()
                if fp != last:
                    self.wfile.write(b"event: state\ndata: " + json.dumps(state(), ensure_ascii=False).encode() + b"\n\n")
                    self.wfile.flush()
                    last, beat = fp, time.time()
                elif time.time() - beat > 15:
                    # keep-alive, and refreshes "loop alive" even when no file changed
                    self.wfile.write(b"event: state\ndata: " + json.dumps(state(), ensure_ascii=False).encode() + b"\n\n")
                    self.wfile.flush()
                    beat = time.time()
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


def main():
    global DATA
    ap = argparse.ArgumentParser(description="모카 dashboard (developer only, binds to localhost)")
    ap.add_argument("--data", default=str(DEFAULT_DATA), help="moca's data/ directory")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    DATA = pathlib.Path(args.data).resolve()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    print(f"모카 대시보드: http://127.0.0.1:{args.port}  (data: {DATA})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
