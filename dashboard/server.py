"""모카 dashboard: a local, developer-only view of the loop (documents/dashboard.md in the moca project).

Deliberately separate from the moca code: it imports nothing from it. Its only contract is the files moca keeps
under data/ — goals, steps, tasks, knowledge, hypotheses, programs, consent and the loop's runtime status. It reads
them, and writes exactly four things, each checked with the same rules as the harness: a new top-level goal into
data/goals/goals.json, a new hypothesis into data/hypotheses/hypotheses.json (for seeding 모카's first ones), a
new program into data/programs/programs.json, and a request (with a note) that the planner rewrite a program's plan.

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
# the harness's hypothesis rules (moca: harness/hypotheses.py), repeated for the same reason
HYPO_KINDS = {"need": "원하는 것", "behavior": "행동 패턴", "relationship": "멤버 사이의 관계",
              "commitment": "참여 의지", "mechanism": "모임 운영 방식의 효과"}
HYPO_STATUSES = {"refuted": "폐기", "weakened": "약해짐", "open": "검증 전", "supported": "뒷받침됨",
                 "confirmed": "충분히 뒷받침됨"}
HYPO_LIMITS = {"claim": 200, "reasoning": 400, "test": 250}
HYPO_MAX_ACTIVE = 15
HYPO_SIMILAR = 0.72
SEEDED_BY = "로하(대시보드)"
# the harness's program rules (moca: harness/programs.py), repeated for the same reason
PROG_TYPES = {"linear": "단계형", "recurring": "정기"}
PROG_STATUSES = {"planned": "계획됨", "active": "진행 중", "paused": "멈춤", "finished": "끝남", "dropped": "그만둠"}
PROG_LIVE = ("planned", "active", "paused")
PROG_REPEAT_KINDS = {"constant": "정해진 횟수", "conditional": "조건이 맞는 동안", "infinite": "끝없이"}
PROG_GROUNDING = ("supported", "confirmed")
PROG_LIMITS = {"title": 40, "purpose": 300, "why": 200, "reasoning": 400, "criteria": 200, "entry_state": 200,
               "exit_state": 200, "measure": 250, "format": 200, "condition": 200}
PROG_RANGES = {"duration_days": (1, 180), "interval_days": (1, 90), "count": (1, 100), "size": (1, 50)}
PROG_MAX_LIVE = 10
PLAN_NOTE_LIMIT = 500

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
            DATA / "runtime" / "status.json", DATA / "runtime" / "presence.json",
            DATA / "hypotheses" / "hypotheses.json", DATA / "programs" / "programs.json"] + sorted((DATA / "tasks").glob("t_*.json"))


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


PRIVATE_CHANNELS = ("member_note",)   # shown only while the member allows 모임 운영 use
PUBLIC_CHANNELS = ("intro", "membership", "post")  # 가입인사, who joined, board posts: names shown for everyone
GROUNDING = ("reported", "observed")  # what may ground a hypothesis — never 모카's own inference


def _visible_knowledge(names):
    """moca's knowledge.visible: newest per key, newest version per group, no tombstones, consent for notes."""
    records = _jsonl(DATA / "knowledge" / "records.jsonl")
    newest = {r["origin"]["key"]: r["id"] for r in records if r.get("origin", {}).get("key")}
    version = {r["origin"]["group"]: r["origin"].get("version") for r in records if r.get("origin", {}).get("group")}
    return [r for r in records
            if not r["origin"].get("retracted")
            and (not r["origin"].get("key") or newest[r["origin"]["key"]] == r["id"])
            and (not r["origin"].get("group") or version[r["origin"]["group"]] == r["origin"].get("version"))
            and (r["origin"].get("channel") not in PRIVATE_CHANNELS or all(x in names.allowed for x in r["subjects"]))]


def _statement(r, names):
    return names.fill(r["statement"], r["subjects"], public=r["origin"].get("channel") in PUBLIC_CHANNELS)


class Names:
    """Placeholders ({s0}, {s1}, …) filled in the way 운영 모카 sees them."""

    def __init__(self):
        consent = _json(DATA / "members" / "scope_consent.json", {})
        self.allowed = {m for m, e in consent.items() if e.get("sharing") is True}

    def show(self, name):
        return name if name in self.allowed else ANONYMOUS

    def fill(self, text, subjects, public=False):
        """`public`: an intro fact, which names its member for everyone (moca: harness/knowledge.py)."""
        shown = [n if public or n in self.allowed else None for n in subjects or []]

        def one(m):
            i = int(m.group(1))
            name = shown[i] if i < len(shown) else None  # never leak an unresolved placeholder
            word = ANONYMOUS if name is None else name + (m.group(2) or "")  # '님' only with a shown name
            return word + (_particle(word, m.group(3)) if m.group(3) else "")
        return re.sub(r"\{s(\d+)\}(님)?(은|는|이|가|을|를|과|와)?", one, text or "")


PARTICLES = {"은": ("은", "는"), "는": ("은", "는"), "이": ("이", "가"), "가": ("이", "가"),
             "을": ("을", "를"), "를": ("을", "를"), "과": ("과", "와"), "와": ("과", "와")}


def _particle(word, written):
    """'한 멤버는', '김범진은': the particle is chosen again for the name actually shown."""
    with_final, without = PARTICLES[written]
    last = (word or "")[-1:]
    if "가" <= last <= "힣":
        return with_final if (ord(last) - 0xAC00) % 28 else without
    return written


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
    for r in _visible_knowledge(names):
        records.append({"id": r["id"], "statement": _statement(r, names),
                        "basis": r["basis"], "created_at": r["created_at"],
                        "task": r["origin"].get("task") or (f"게시글 「{r['origin']['title']}」" if r["origin"].get("title")
                                                             else r["origin"].get("channel")),
                        "channel": r["origin"].get("channel"), "sources": len(r.get("source_refs", []))})
    records.sort(key=lambda r: r["created_at"])
    by_basis, by_task = {}, {}
    for r in records:
        by_basis[r["basis"]] = by_basis.get(r["basis"], 0) + 1
        by_task[r["task"]] = by_task.get(r["task"], 0) + 1
    return records, {"total": len(records), "by_basis": by_basis, "by_task": by_task,
                     "today": sum(1 for r in records if r["created_at"].startswith(time.strftime("%Y-%m-%d")))}


def _roster():
    """Everyone who has spoken in the 모임 chat (the app makes each new member say hello), whispers folded in."""
    return sorted({m["sender"].replace("(귓속말)", "").strip() for m in _jsonl(DATA / "chat" / "transcript.jsonl")
                   if not m.get("mine") and m.get("sender")})


def _hypotheses(names):
    knowledge = {r["id"]: _statement(r, names) for r in _visible_knowledge(names)}
    out = []
    for h in _json(DATA / "hypotheses" / "hypotheses.json", []):
        grounds = h.get("grounds", {})
        out.append(dict(h, claim_shown=names.fill(h["claim"], h.get("members")),
                        members_shown=[names.show(m) for m in h.get("members", [])],
                        kind_label=HYPO_KINDS.get(h["kind"], h["kind"]),
                        status_label=HYPO_STATUSES.get(h["status"], h["status"]),
                        grounds_shown=[{"id": k, "statement": knowledge.get(k, "(지워진 지식)")}
                                       for k in grounds.get("knowledge", [])],
                        evidence_shown=[dict(e, statement=knowledge.get(e["ref"], "(지금은 보이지 않는 지식 — 집계에서 빠짐)"),
                                             visible=e["ref"] in knowledge) for e in h.get("evidence", [])]))
    return sorted(out, key=lambda h: h["created_at"], reverse=True)


def _hypothesis_options(names):
    """What the seeding form can offer: knowledge to cite, 정모 and votes to tag."""
    return {"kinds": HYPO_KINDS, "limits": HYPO_LIMITS,
            "knowledge": [{"id": r["id"], "statement": _statement(r, names), "basis": r["basis"]}
                          for r in reversed(_visible_knowledge(names)) if r["basis"] in GROUNDING],
            "events": [e["name"] for e in _json(DATA / "events" / "index.json", {}).get("events", [])],
            "votes": [v["title"] for v in _json(DATA / "votes" / "index.json", {}).get("votes", [])]}


def _programs(names):
    """Programs with their text filled in as 운영 모카 sees it, their grounding hypotheses' current state, and a flag
    for each hypothesis that has since fallen below supported (the harness only flags; it never changes a program)."""
    hypos = {h["id"]: h for h in _json(DATA / "hypotheses" / "hypotheses.json", [])}
    knowledge = {r["id"]: _statement(r, names) for r in _visible_knowledge(names)}
    out = []
    for p in _json(DATA / "programs" / "programs.json", []):
        subjects = p.get("subjects", [])

        def fill(text):
            return names.fill(text, subjects) if text else text
        grounds, flags = [], []
        for x in p["rationale"]["hypotheses"]:
            h = hypos.get(x["id"])
            grounds.append({"id": x["id"], "why": fill(x["why"]), "status": h["status"] if h else "missing",
                            "claim": names.fill(h["claim"], h.get("members")) if h else "(없어진 가설)"})
            if not h:
                flags.append(f"근거 가설 {x['id']}가 없어졌습니다")
            elif h["status"] not in PROG_GROUNDING:
                flags.append(f"근거 가설 {x['id']}가 지금 '{HYPO_STATUSES.get(h['status'], h['status'])}' 상태입니다")
        shown = {"title": fill(p["title"]), "purpose": fill(p["purpose"]), "reasoning": fill(p["rationale"]["reasoning"]),
                 "criteria": fill(p["users"]["criteria"]),
                 "members": [names.fill(m, subjects) for m in p["users"]["members"]]}
        if p.get("linear"):
            shown.update({k: fill(p["linear"][k]) for k in ("entry_state", "exit_state", "measure")})
        if p.get("recurring"):
            shown.update(format=fill(p["recurring"]["format"]), condition=fill(p["recurring"]["repeats"].get("condition")))
        plan = (p.get("recurring") or {}).get("activity_template") or (p.get("linear") or {}).get("sketch")
        plan_grounds = [dict(x, statement=knowledge.get(x["id"], "(지금은 보이지 않는 지식)"))
                        for x in (plan or {}).get("grounds", {}).get("knowledge", [])]
        out.append(dict(p, shown=shown, grounds_shown=grounds, flags=flags, plan=plan, plan_grounds_shown=plan_grounds,
                        type_label=PROG_TYPES.get(p["type"], p["type"]),
                        status_label=PROG_STATUSES.get(p["status"], p["status"])))
    return sorted(out, key=lambda p: p["created_at"], reverse=True)


def _program_options(names):
    """What the program form can offer: hypotheses a program may rest on, and goals to attach it to."""
    return {"types": PROG_TYPES, "repeat_kinds": PROG_REPEAT_KINDS, "limits": PROG_LIMITS, "ranges": PROG_RANGES,
            "max_live": PROG_MAX_LIVE,
            "hypotheses": [{"id": h["id"], "claim": names.fill(h["claim"], h.get("members")), "status": h["status"]}
                           for h in _json(DATA / "hypotheses" / "hypotheses.json", []) if h["status"] in PROG_GROUNDING],
            "goals": [{"id": g["id"], "objective": g["objective"]} for g in _json(DATA / "goals" / "goals.json", [])
                      if g["status"] not in FINISHED]}


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
            "hypotheses": _hypotheses(names), "hypothesis_options": _hypothesis_options(names),
            "programs": _programs(names), "program_options": _program_options(names),
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


# --- the second write: a hypothesis seeded by the developer --------------------------------------

def _squash(text):
    return re.sub(r"[\s.,!?~…'\"]+", "", text or "")


def _templatize(text, names):
    """Member names in the claim become {s0}, {s1}, … (longest first), exactly as moca stores them."""
    subjects = []
    for name in sorted(set(names), key=len, reverse=True):
        if name and name in text:
            text = re.sub(re.escape(name) + r"(님)?", f"{{s{len(subjects)}}}", text)
            subjects.append(name)
    return text, subjects


def add_hypothesis(body):
    """Validate and append a hypothesis. The harness's rules, with one difference: a hypothesis seeded here may
    rest on the developer's own observation instead of a stored knowledge record, since 모카 has little knowledge
    yet. It says so in `grounds.source`, and 모카 sees who set it. Returns (record, None) or (None, problem)."""
    import difflib

    def get(k):
        v = body.get(k)
        return v.strip() if isinstance(v, str) else ""
    claim, kind, reasoning, test = get("claim"), get("kind"), get("reasoning"), get("test")
    lists = {k: [x.strip() for x in body.get(k) or [] if isinstance(x, str) and x.strip()]
             for k in ("members", "knowledge_ids", "events", "votes")}
    if not claim:
        return None, "가설(claim)을 적어야 합니다"
    if kind not in HYPO_KINDS:
        return None, f"종류(kind)는 {list(HYPO_KINDS)} 중 하나여야 합니다"
    for field, value in (("claim", claim), ("reasoning", reasoning), ("test", test)):
        if len(value) > HYPO_LIMITS[field]:
            return None, f"{field}는 {HYPO_LIMITS[field]}자 이내여야 합니다"
    if not reasoning:
        return None, "근거(reasoning)를 적어야 합니다. 왜 이렇게 보는지"
    if not test:
        return None, "확인 방법(test)을 적어야 합니다. 무엇을 보면 뒷받침되거나 약해지는지"
    roster = set(_roster())
    unknown = [m for m in lists["members"] if m not in roster]
    if unknown:
        return None, f"모임 채팅에서 본 적 없는 이름입니다: {unknown}. 앱에 보이는 이름 그대로 적으세요"
    known = {r["id"]: r for r in _visible_knowledge(Names())}
    missing = [k for k in lists["knowledge_ids"] if k not in known]
    if missing:
        return None, f"없는 지식 id입니다: {missing}"
    inferred = [k for k in lists["knowledge_ids"] if known[k]["basis"] not in GROUNDING]
    if inferred:
        return None, f"모카의 추론(inferred)은 근거가 될 수 없습니다: {inferred}"
    opts = _hypothesis_options(Names())
    unknown = [e for e in lists["events"] if e not in opts["events"]] + [v for v in lists["votes"] if v not in opts["votes"]]
    if unknown:
        return None, f"없는 정모나 투표입니다: {unknown}"

    template, subjects = _templatize(claim, set(lists["members"]) | {n for n in roster if len(n) > 1 and n in claim})
    path = DATA / "hypotheses" / "hypotheses.json"
    with _write_lock:
        records = _json(path, [])  # re-read right before writing: the loop saves this file too
        live = [h for h in records if h["status"] != "refuted"]
        if len(live) >= HYPO_MAX_ACTIVE:
            return None, f"폐기되지 않은 가설이 이미 {len(live)}개입니다 (최대 {HYPO_MAX_ACTIVE})"
        a = _squash(template)
        twin = next((h for h in live if set(h["members"]) == set(subjects)
                     and difflib.SequenceMatcher(None, _squash(h["claim"]), a).ratio() >= HYPO_SIMILAR), None)
        if twin:
            return None, f"비슷한 가설이 이미 있습니다: {twin['id']}"
        now = time.strftime(FMT)
        record = {"id": f"h_{time.strftime('%Y%m%d')}_{secrets.token_hex(2)}", "kind": kind, "claim": template,
                  "members": subjects, "events": lists["events"], "votes": lists["votes"],
                  "grounds": {"knowledge": lists["knowledge_ids"], "reasoning": reasoning, "source": "developer"},
                  "test": test, "status": "open", "confidence": None, "evidence": [],
                  "goal_id": None, "created_by": SEEDED_BY, "created_at": now, "updated_at": now}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".dashboard.tmp")
        tmp.write_text(json.dumps(records + [record], ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    return record, None


# --- the third write: a program --------------------------------------------------------------------

def _placehold(text, subjects):
    """Member names → {s0}, {s1}, … with one numbering shared by every field of the program."""
    for i, name in sorted(enumerate(subjects), key=lambda x: len(x[1]), reverse=True):
        text = re.sub(re.escape(name) + r"(님)?", f"{{s{i}}}", text)
    return text


def add_program(body):
    """Validate and append a program with the harness's rules (moca: harness/programs.py create()).
    Returns (record, None) or (None, problem)."""
    import difflib

    def text(k):
        v = body.get(k)
        return v.strip() if isinstance(v, str) else ""

    def number(k):
        v = body.get(k)
        return None if v in (None, "") else v

    def in_range(v, key, label):
        lo, hi = PROG_RANGES[key]
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            return f"{label}는 {lo}~{hi} 사이의 정수여야 합니다"
        return None

    kind = body.get("type")
    t = {k: text(k) for k in ("title", "purpose", "reasoning", "criteria", "entry_state", "exit_state", "measure", "format")}
    t["condition"] = text("repeat_condition")
    links = [{"id": (x.get("id") or "").strip(), "why": (x.get("why") or "").strip()}
             for x in body.get("hypotheses") or [] if isinstance(x, dict)]
    members = [m.strip() for m in body.get("members") or [] if isinstance(m, str) and m.strip()]
    goal_id = text("goal_id") or None
    duration, interval, count = number("duration_days"), number("interval_days"), number("repeat_count")
    size_min, size_max, repeat_kind = number("size_min"), number("size_max"), body.get("repeat_kind") or None

    if kind not in PROG_TYPES:
        return None, f"종류(type)는 {list(PROG_TYPES)} 중 하나여야 합니다"
    for field in ("title", "purpose", "reasoning", "criteria"):
        if not t[field]:
            return None, f"{field}를 적어야 합니다"
    for field, value in list(t.items()) + [("why", x["why"]) for x in links]:
        if len(value) > PROG_LIMITS[field]:
            return None, f"{field}는 {PROG_LIMITS[field]}자 이내여야 합니다"
    if not links:
        return None, "존재 근거가 되는 가설을 하나 이상 골라야 합니다 (뒷받침됨 이상)"
    hypos = {h["id"]: h for h in _json(DATA / "hypotheses" / "hypotheses.json", [])}
    for x in links:
        h = hypos.get(x["id"])
        if not h:
            return None, f"없는 가설 id입니다: {x['id']}"
        if h["status"] not in PROG_GROUNDING:
            return None, f"가설 {x['id']}는 지금 '{HYPO_STATUSES.get(h['status'])}' 상태입니다. 뒷받침됨 이상만 근거가 됩니다"
        if not x["why"]:
            return None, f"가설 {x['id']}가 이 프로그램을 왜 필요하게 만드는지(why) 적어야 합니다"
    if goal_id and not any(g["id"] == goal_id for g in _json(DATA / "goals" / "goals.json", [])):
        return None, f"없는 목표 id입니다: {goal_id}"
    roster = set(_roster())
    unknown = [m for m in members if m not in roster]
    if unknown:
        return None, f"모임 채팅에서 본 적 없는 이름입니다: {unknown}. 앱에 보이는 이름 그대로 적으세요"
    for v, label in ((size_min, "최소 인원"), (size_max, "최대 인원")):
        if v is not None and (problem := in_range(v, "size", label)):
            return None, problem
    if size_min is not None and size_max is not None and size_min > size_max:
        return None, "최소 인원이 최대 인원보다 큽니다"
    if kind == "linear":
        for field in ("entry_state", "exit_state", "measure"):
            if not t[field]:
                return None, f"단계형 프로그램은 {field}가 필요합니다"
        if problem := in_range(duration, "duration_days", "기간(duration_days)"):
            return None, problem
        interval = repeat_kind = count = None
        t["format"] = t["condition"] = ""
    else:
        if not t["format"]:
            return None, "정기 프로그램은 형식(format)이 필요합니다"
        if problem := in_range(interval, "interval_days", "주기(interval_days)"):
            return None, problem
        if repeat_kind not in PROG_REPEAT_KINDS:
            return None, f"반복(repeat_kind)은 {list(PROG_REPEAT_KINDS)} 중 하나여야 합니다"
        if repeat_kind == "constant":
            if problem := in_range(count, "count", "횟수(repeat_count)"):
                return None, problem
        else:
            count = None
        if repeat_kind == "conditional" and not t["condition"]:
            return None, "조건(repeat_condition)을 적어야 합니다: 언제까지 이어가거나 멈추는지"
        if repeat_kind != "conditional":
            t["condition"] = ""
        duration = None
        t["entry_state"] = t["exit_state"] = t["measure"] = ""

    written = " ".join(list(t.values()) + [x["why"] for x in links])
    subjects = list(dict.fromkeys(members + [n for n in roster if len(n) > 1 and n in written and n not in members]))
    ph = {k: _placehold(v, subjects) for k, v in t.items()}
    path = DATA / "programs" / "programs.json"
    with _write_lock:
        records = _json(path, [])  # re-read right before writing: the loop saves this file too
        alive = [p for p in records if p["status"] in PROG_LIVE]
        if len(alive) >= PROG_MAX_LIVE:
            return None, f"끝나지 않은 프로그램이 이미 {len(alive)}개입니다 (최대 {PROG_MAX_LIVE})"
        similar = lambda a, b: bool(_squash(a) and _squash(b)) and \
            difflib.SequenceMatcher(None, _squash(a), _squash(b)).ratio() >= HYPO_SIMILAR
        twin = next((p for p in alive if similar(p["title"], ph["title"]) or similar(p["purpose"], ph["purpose"])), None)
        if twin:
            return None, f"비슷한 프로그램이 이미 있습니다: {twin['id']}"
        now = time.strftime(FMT)
        record = {
            "id": f"p_{time.strftime('%Y%m%d')}_{secrets.token_hex(2)}", "type": kind,
            "title": ph["title"], "purpose": ph["purpose"], "goal_id": goal_id,
            "rationale": {"hypotheses": [{"id": x["id"], "why": _placehold(x["why"], subjects)} for x in links],
                          "reasoning": ph["reasoning"]},
            "users": {"members": [f"{{s{subjects.index(m)}}}" for m in members], "criteria": ph["criteria"],
                      "size": {"min": size_min, "max": size_max}},
            "linear": {"entry_state": ph["entry_state"], "exit_state": ph["exit_state"], "measure": ph["measure"],
                       "duration_days": duration, "sketch": None} if kind == "linear" else None,
            "recurring": {"interval_days": interval,
                          "repeats": {"kind": repeat_kind, "count": count, "condition": ph["condition"] or None},
                          "format": ph["format"], "activity_template": None} if kind == "recurring" else None,
            "subjects": subjects, "status": "planned", "created_by": SEEDED_BY,
            "created_at": now, "updated_at": now, "started_at": None, "history": []}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".dashboard.tmp")
        tmp.write_text(json.dumps(records + [record], ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    return record, None


def request_plan(body):
    """Ask the planner (moca: harness/planner.py) to write a program's plan again, with a note. The planner picks
    it up at the next 운영 round. Returns (record, None) or (None, problem)."""
    pid = (body.get("id") or "").strip()
    note = (body.get("note") or "").strip()
    if len(note) > PLAN_NOTE_LIMIT:
        return None, f"메모는 {PLAN_NOTE_LIMIT}자 이내여야 합니다"
    path = DATA / "programs" / "programs.json"
    with _write_lock:
        records = _json(path, [])
        p = next((x for x in records if x["id"] == pid), None)
        if not p:
            return None, f"없는 프로그램입니다: {pid}"
        if p["status"] not in PROG_LIVE:
            return None, "끝났거나 그만둔 프로그램입니다"
        p["plan_request"] = {"note": note, "at": time.strftime(FMT), "by": SEEDED_BY}
        tmp = path.with_suffix(".dashboard.tmp")
        tmp.write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    return p, None


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
        path = urlparse(self.path).path
        if path not in ("/api/goals", "/api/hypotheses", "/api/programs", "/api/programs/plan"):
            return self._send(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self._send(400, {"error": "JSON이 아닙니다"})
        if path == "/api/hypotheses":
            record, problem = add_hypothesis(body)
            if problem:
                return self._send(400, {"error": problem})
            return self._send(201, {"hypothesis": record})
        if path == "/api/programs/plan":
            record, problem = request_plan(body)
            if problem:
                return self._send(400, {"error": problem})
            return self._send(200, {"program": record})
        if path == "/api/programs":
            record, problem = add_program(body)
            if problem:
                return self._send(400, {"error": problem})
            return self._send(201, {"program": record})
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
