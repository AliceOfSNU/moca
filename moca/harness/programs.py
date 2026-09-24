"""Programs: what 모카 plans to run for its 모임, and why (documents/program_activitiy.md).

The chain is Goal → Hypothesis → Program → Activity. A program is a logical unit of activity: a concrete purpose,
grounded in hypotheses that evidence already supports, that leads to a series of activities. Two types:

- linear (단계형): moves forward in order from an entry state to an exit state, with a measurable change and a
  finish line within a reasonable time.
- recurring (정기): one main activity repeated on a rough interval (카공 각자 스터디 정모), templated enough that a
  newcomer gets the format after one or two times. Several main activities are several programs.

A program must say why it has to exist: every program rests on at least one hypothesis that is supported or
confirmed when the program is created. If a grounding hypothesis later drops below that, nothing is changed
automatically — the program is flagged, for 운영 모카 and on the dashboard, and whoever runs it decides.

A program is created with its plan null — `recurring.activity_template` or `linear.sketch`. The planner
(harness/planner.py) fills it at the end of a 운영 round; 운영 모카 only sees it.

Every 정모 모카 opens is an Activity of a program (see check_activity): create_event names the program, and the
harness refuses it unless the program is planned or active and has its plan. The first activity makes a planned
program active. A one-off 정모 is a recurring program that repeats once. Programs are internal: members don't
see them, and 모카 doesn't mention them in the chat.

One record per program in data/programs/programs.json:

    {"id": "p_20260922_3fa1", "type": "linear" | "recurring",
     "title": "…", "purpose": "무엇이 누구에게 어떻게 달라지는지",
     "goal_id": "g_…" | null,
     "rationale": {"hypotheses": [{"id": "h_…", "why": "이 가설이 이 프로그램을 왜 필요하게 만드는지"}],
                   "reasoning": "왜 다른 방식이 아니라 이 형식인지"},
     "users": {"members": ["{s0}"], "criteria": "누구를 위한 것인지", "size": {"min": 3, "max": 8}},
     "linear": {"entry_state", "exit_state", "measure", "duration_days", "sketch": null} | null,
     "recurring": {"interval_days", "repeats": {"kind": "constant"|"conditional"|"infinite", "count", "condition"},
                   "format", "activity_template": null} | null,
     "subjects": ["하루"],                      # the names behind {s…} in every text field
     "status": "planned", "created_by": "…", "created_at": "…", "updated_at": "…", "started_at": null,
     "history": []}

Names follow the knowledge rule: stored as placeholders, shown by name only for members who allowed 모임 운영 use,
"한 멤버" otherwise.

Usage (from the moca/ directory):
    python -m harness.programs            # every program, as 운영 모카 sees it
"""
import difflib
import json
import os
import re
import secrets
import sys
import time

from harness.tasks import ROOT

DATA = ROOT / "data" / "programs"
FILE = DATA / "programs.json"

TYPES = {"linear": "단계형", "recurring": "정기"}
STATUSES = {"planned": "계획됨", "active": "진행 중", "paused": "멈춤", "finished": "끝남", "dropped": "그만둠"}
LIVE = ("planned", "active", "paused")
REPEAT_KINDS = {"constant": "정해진 횟수", "conditional": "조건이 맞는 동안", "infinite": "끝없이"}
GROUNDING = ("supported", "confirmed")   # hypothesis statuses a program may rest on
LIMITS = {"title": 40, "purpose": 300, "why": 200, "reasoning": 400, "criteria": 200, "entry_state": 200,
          "exit_state": 200, "measure": 250, "format": 200, "condition": 200}
RANGES = {"duration_days": (1, 180), "interval_days": (1, 90), "count": (1, 100), "size": (1, 50)}
MAX_LIVE = 10
SIMILAR = 0.72


def load():
    if FILE.exists():
        return json.loads(FILE.read_text(encoding="utf-8"))
    return []


def save(records):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, FILE)  # the dashboard reads (and writes) this file too


def live(records=None):
    return [p for p in (records if records is not None else load()) if p["status"] in LIVE]


def _placehold(text, subjects):
    """Member names → {s0}, {s1}, … with one numbering shared by every field of the program."""
    for i, name in sorted(enumerate(subjects), key=lambda x: len(x[1]), reverse=True):
        text = re.sub(re.escape(name) + r"(님)?", f"{{s{i}}}", text)
    return text


def show(p, text):
    from harness import knowledge  # the same consent rule as knowledge, read at display time
    return knowledge.render({"statement": text or "", "subjects": p.get("subjects", [])})


def _squash(text):
    return re.sub(r"[\s.,!?~…'\"]+", "", text or "")


def _similar(a, b):
    a, b = _squash(a), _squash(b)
    return bool(a and b) and difflib.SequenceMatcher(None, a, b).ratio() >= SIMILAR


def _int(value, key, label):
    lo, hi = RANGES[key]
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        return None, f"{label}는 {lo}~{hi} 사이의 정수여야 합니다"
    return value, None


def create(type=None, title=None, purpose=None, hypotheses=None, reasoning=None, members=None, criteria=None,
           size_min=None, size_max=None, entry_state=None, exit_state=None, measure=None, duration_days=None,
           interval_days=None, repeat_kind=None, repeat_count=None, repeat_condition=None, format=None,
           goal_id=None, created_by="admin"):
    """Store a program if it passes the rules. Returns (record, None) or (None, why it was refused)."""
    from chatbot.profiles import profiles
    from harness import goals as G
    from harness import hypotheses as H

    def clean(v):
        return v.strip() if isinstance(v, str) else ""
    text = {"title": clean(title), "purpose": clean(purpose), "reasoning": clean(reasoning),
            "criteria": clean(criteria), "entry_state": clean(entry_state), "exit_state": clean(exit_state),
            "measure": clean(measure), "format": clean(format), "condition": clean(repeat_condition)}
    links = [{"id": clean(x.get("id")), "why": clean(x.get("why"))} for x in hypotheses or [] if isinstance(x, dict)]
    members = [m for m in (members or []) if m]

    if type not in TYPES:
        return None, f"type은 {list(TYPES)} 중 하나여야 합니다"
    for field in ("title", "purpose", "reasoning", "criteria"):
        if not text[field]:
            return None, f"{field}가 비어 있습니다"
    for field, value in list(text.items()) + [("why", x["why"]) for x in links]:
        if len(value) > LIMITS[field]:
            return None, f"{field}는 {LIMITS[field]}자 이내여야 합니다"

    # why it has to exist: hypotheses that evidence already supports
    if not links:
        return None, ("프로그램은 존재 근거가 되는 가설이 하나 이상 있어야 합니다 (hypotheses). "
                      "뒷받침된 가설이 없다면 먼저 가설을 확인하세요")
    if len({x["id"] for x in links}) != len(links):
        return None, "같은 가설을 두 번 적었습니다"
    by_id = {h["id"]: h for h in H.load()}
    for x in links:
        h = by_id.get(x["id"])
        if not h:
            return None, f"없는 가설 id입니다: {x['id']}"
        if h["status"] not in GROUNDING:
            return None, (f"가설 {x['id']}는 지금 '{H.STATUSES[h['status']]}' 상태입니다. 프로그램은 뒷받침됨 이상인 "
                          "가설에만 근거를 둘 수 있습니다")
        if not x["why"]:
            return None, f"가설 {x['id']}가 이 프로그램을 왜 필요하게 만드는지(why) 적으세요"
    if goal_id and not any(g["id"] == goal_id for g in G.load()):
        return None, f"없는 목표 id입니다: {goal_id}"

    # who it is for
    roster = profiles()
    unknown = [m for m in members if m not in roster]
    if unknown:
        return None, f"모르는 멤버입니다: {unknown}. member_search로 앱 이름을 확인하세요"
    size = {}
    for key, value, label in (("min", size_min, "size_min"), ("max", size_max, "size_max")):
        if value is not None:
            size[key], problem = _int(value, "size", label)
            if problem:
                return None, problem
    if "min" in size and "max" in size and size["min"] > size["max"]:
        return None, "size_min이 size_max보다 큽니다"

    if type == "linear":
        for field in ("entry_state", "exit_state", "measure"):
            if not text[field]:
                return None, f"단계형 프로그램은 {field}가 필요합니다 (시작 전과 끝난 뒤, 그 변화를 무엇으로 확인하는지)"
        duration_days, problem = _int(duration_days, "duration_days", "duration_days")
        if problem:
            return None, problem + " (합리적인 기간 안에 끝나야 합니다)"
        if any(v not in (None, "") for v in (interval_days, repeat_kind, repeat_count, repeat_condition, format)):
            return None, "단계형 프로그램에는 정기 프로그램 항목(interval_days, repeat_*, format)을 쓰지 않습니다"
    else:
        if not text["format"]:
            return None, "정기 프로그램은 format이 필요합니다: 반복되는 주된 활동 하나를, 처음 온 사람도 알 수 있게"
        interval_days, problem = _int(interval_days, "interval_days", "interval_days")
        if problem:
            return None, problem
        if repeat_kind not in REPEAT_KINDS:
            return None, f"repeat_kind는 {list(REPEAT_KINDS)} 중 하나여야 합니다"
        if repeat_kind == "constant":
            repeat_count, problem = _int(repeat_count, "count", "repeat_count")
            if problem:
                return None, problem
        elif repeat_count is not None:
            return None, "repeat_count는 repeat_kind가 constant일 때만 적습니다"
        if repeat_kind == "conditional" and not text["condition"]:
            return None, "repeat_kind가 conditional이면 repeat_condition(언제까지 이어가거나 멈추는지)이 필요합니다"
        if repeat_kind != "conditional" and text["condition"]:
            return None, "repeat_condition은 repeat_kind가 conditional일 때만 적습니다"
        if any(v not in (None, "") for v in (entry_state, exit_state, measure, duration_days)):
            return None, "정기 프로그램에는 단계형 항목(entry_state, exit_state, measure, duration_days)을 쓰지 않습니다"

    # names anywhere in the text become placeholders too, tagged as a target or not
    written = " ".join(list(text.values()) + [x["why"] for x in links])
    subjects = list(dict.fromkeys(members + [n for n in roster if n and len(n) > 1 and n in written
                                             and n not in members]))
    ph = {k: _placehold(v, subjects) for k, v in text.items()}

    records = load()
    alive = live(records)
    if len(alive) >= MAX_LIVE:
        return None, f"끝나지 않은 프로그램이 이미 {len(alive)}개입니다. 새로 만들기 전에 기존 프로그램을 정리하세요"
    twin = next((p for p in alive if _similar(p["title"], ph["title"]) or _similar(p["purpose"], ph["purpose"])), None)
    if twin:
        return None, f"비슷한 프로그램이 이미 있습니다: {twin['id']} \"{show(twin, twin['title'])}\""

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    record = {
        "id": f"p_{time.strftime('%Y%m%d')}_{secrets.token_hex(2)}", "type": type,
        "title": ph["title"], "purpose": ph["purpose"], "goal_id": goal_id or None,
        "rationale": {"hypotheses": [{"id": x["id"], "why": _placehold(x["why"], subjects)} for x in links],
                      "reasoning": ph["reasoning"]},
        "users": {"members": [f"{{s{subjects.index(m)}}}" for m in members], "criteria": ph["criteria"],
                  "size": {"min": size.get("min"), "max": size.get("max")}},
        "linear": {"entry_state": ph["entry_state"], "exit_state": ph["exit_state"], "measure": ph["measure"],
                   "duration_days": duration_days, "sketch": None} if type == "linear" else None,
        "recurring": {"interval_days": interval_days,
                      "repeats": {"kind": repeat_kind, "count": repeat_count if repeat_kind == "constant" else None,
                                  "condition": ph["condition"] or None},
                      "format": ph["format"], "activity_template": None} if type == "recurring" else None,
        "subjects": subjects, "status": "planned", "created_by": created_by,
        "created_at": now, "updated_at": now, "started_at": None, "history": []}
    records.append(record)
    save(records)
    return record, None


# --- activities: every 정모 모카 opens is one ------------------------------------------------------------
# A 정모 is always an Activity of a program: create_event names the program (and, for a linear program, the
# stage), and the harness refuses a 정모 whose program isn't running, has no plan yet, has used up its repeats,
# or whose stage would skip ahead. A one-off 정모 (a 친목 번개) is a recurring program that repeats once.
# Each activity: {"event", "when", "session" | "stage", "post_title", "status": "scheduled"|"canceled", …}.

OPEN_FOR_ACTIVITIES = ("planned", "active")
PLAN_MODES = {"offline": ("offline",), "online": ("online",), "either": ("offline", "online", "hybrid")}


def plan_of(p):
    return p["recurring"]["activity_template"] if p["type"] == "recurring" else p["linear"]["sketch"]


# --- the program's face in the app: a 정모 members can sign up to --------------------------------------
# A program is otherwise invisible to members. Its container 정모 gives it one place in the app: the 참석 button
# is "I'm in", the attendee list is the roster, the linked post is the invitation, and 소모임 opens a chat room
# for its participants. It is not a gathering — no one meets at its date, which is only the program's horizon.
# The app can never change a 정모's date, so the harness picks it rather than the model.

MARK = "📋 "                              # every container 정모 starts with this, so no one takes it for a session
PLACE = "참가 등록 (실제 모임 아님)"        # the app's 장소 field (20 units), where the caveat fits
HORIZON_DAYS = 90                        # for programs that don't end on their own
EVENT_UNITS = 20                         # the app keeps 20 UTF-16 units of 정모 이름 and 장소 (measured 2026-09-24)


def container_name(p):
    from somoim.chat import units
    name = MARK + show(p, p["title"])
    return name if units(name) <= EVENT_UNITS else None


def container_when(p, now=None):
    """The program's horizon: when it should be over. Linear programs end at their duration, a fixed-count
    recurring program after its repeats, and anything open-ended gets a horizon it can be renewed past."""
    import datetime as dt
    now = now or dt.datetime.now()
    start = dt.datetime.strptime(p["started_at"], "%Y-%m-%d %H:%M:%S") if p.get("started_at") else now
    if p["type"] == "linear":
        days = (start - now).days + p["linear"]["duration_days"]
    else:
        rep = p["recurring"]["repeats"]
        days = rep["count"] * p["recurring"]["interval_days"] if rep["kind"] == "constant" else HORIZON_DAYS
    days = max(7, min(days, 179))  # at least a week out, inside the app's 180-day limit
    return (now + dt.timedelta(days=days)).replace(hour=20, minute=0, second=0, microsecond=0)


def check_container(program_id):
    """May this program open its sign-up 정모? Returns (program, None) or (None, why not)."""
    p = next((x for x in load() if x["id"] == program_id), None)
    if p is None:
        return None, f"없는 프로그램입니다: {program_id}"
    if p["status"] not in OPEN_FOR_ACTIVITIES:
        return None, f"프로그램 {program_id}는 지금 '{STATUSES[p['status']]}' 상태입니다"
    if plan_of(p) is None:
        return None, f"프로그램 {program_id}의 기획이 아직 없습니다"
    if p.get("container"):
        return None, (f"이 프로그램의 참가 등록 정모는 이미 있습니다: '{p['container']['event']}' "
                      f"({p['container']['when']})")
    if container_name(p) is None:
        return None, (f"프로그램 이름이 길어 정모 이름({EVENT_UNITS}자)에 담기지 않습니다: '{show(p, p['title'])}'. "
                      "로하에게 이름을 줄여 달라고 부탁하세요")
    return p, None


def set_container(program_id, event, when, post_title):
    records = load()
    p = next(x for x in records if x["id"] == program_id)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    p["container"] = {"event": event, "when": when, "post_title": post_title, "opened_at": now}
    p["updated_at"] = now
    save(records)
    return p


def containers(records=None):
    """{정모 이름: 프로그램 id} for the sign-up 정모 — they are not gatherings, so they pay no 별조각 and
    their attendee list is a program roster, not who came to something."""
    return {p["container"]["event"]: p["id"] for p in (records if records is not None else load())
            if p.get("container")}


def started(program_id, reason):
    """A program's first activity makes it active."""
    records = load()
    p = next(x for x in records if x["id"] == program_id)
    if p["status"] == "planned":
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        p.setdefault("history", []).append({"at": now, "from": "planned", "to": "active", "reason": reason})
        p.update(status="active", started_at=now, updated_at=now)
        save(records)
    return p


def finish(program_id, status, reason, by="harness"):
    """End a program: finished (its goal was reached) or dropped. Its agent is torn down with it."""
    if status not in ("finished", "dropped"):
        return None, f"status는 finished나 dropped여야 합니다: {status}"
    records = load()
    p = next((x for x in records if x["id"] == program_id), None)
    if p is None:
        return None, f"없는 프로그램입니다: {program_id}"
    if p["status"] not in LIVE:
        return None, f"프로그램 {program_id}는 이미 '{STATUSES[p['status']]}' 상태입니다"
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    p.setdefault("history", []).append({"at": now, "from": p["status"], "to": status, "reason": reason, "by": by})
    p.update(status=status, updated_at=now, finished_at=now)
    save(records)
    return p, None


def flags(p, by_id=None):
    """Why this program's grounds look shaky now: a grounding hypothesis that fell below supported, or is gone."""
    from harness import hypotheses as H
    by_id = by_id if by_id is not None else {h["id"]: h for h in H.load()}
    out = []
    for x in p["rationale"]["hypotheses"]:
        h = by_id.get(x["id"])
        if not h:
            out.append(f"근거 가설 {x['id']}가 없어졌습니다")
        elif h["status"] not in GROUNDING:
            out.append(f"근거 가설 {x['id']}가 지금 '{H.STATUSES[h['status']]}' 상태입니다")
    return out


def line(p, by_id=None):
    from harness import hypotheses as H
    by_id = by_id if by_id is not None else {h["id"]: h for h in H.load()}
    users = p["users"]
    who = ", ".join(show(p, m) for m in users["members"])
    size = users["size"]
    size_text = "" if size["min"] is None and size["max"] is None else \
        f" · {size['min'] if size['min'] is not None else '?'}~{size['max'] if size['max'] is not None else '?'}명"
    out = (f"- {p['id']} [{TYPES[p['type']]} · {STATUSES[p['status']]}] {show(p, p['title'])}"
           + (f" (목표 {p['goal_id']})" if p.get("goal_id") else "")
           + f"\n  목적: {show(p, p['purpose'])}"
           + f"\n  대상: {show(p, users['criteria'])}{' — ' + who if who else ''}{size_text}")
    if p["type"] == "linear":
        L = p["linear"]
        out += (f"\n  시작 전: {show(p, L['entry_state'])} → 끝난 뒤: {show(p, L['exit_state'])} ({L['duration_days']}일)"
                f"\n  확인: {show(p, L['measure'])}")
    else:
        R = p["recurring"]
        rep = R["repeats"]
        times = {"constant": f"{rep['count']}회", "conditional": f"조건: {show(p, rep['condition'])}",
                 "infinite": "끝없이"}[rep["kind"]]
        out += f"\n  형식: {show(p, R['format'])} (약 {R['interval_days']}일마다, {times})"
    for x in p["rationale"]["hypotheses"]:
        h = by_id.get(x["id"])
        state = H.STATUSES[h["status"]] if h else "없어짐"
        out += f"\n  근거 {x['id']} [{state}]: {show(p, x['why'])}"
    plan = p["recurring"]["activity_template"] if p["type"] == "recurring" else p["linear"]["sketch"]
    if plan and p["type"] == "recurring":
        agenda = " → ".join(f"{a['title']} {a['minutes']}분" for a in plan["agenda"])
        slots = ", ".join(f"{x['name']}({x['fill_by']})" for x in plan["slots"])
        out += (f"\n  템플릿 v{plan['version']}: {plan['summary']} ({plan['duration_minutes']}분)"
                f"\n    순서: {agenda}" + (f"\n    정할 것: {slots}" if slots else ""))
    elif plan:
        out += f"\n  스케치 v{plan['version']}: {plan['summary']}" + "".join(
            f"\n    {st['n']}. (day {st['day']}) {st['title']} → {st['exit_state']}" for st in plan["stages"])
    else:
        out += "\n  (기획은 아직 — 하네스가 운영 라운드에 채운다. 채워지기 전에는 정모를 열 수 없다)"
    from harness import activities as A
    acts = [a for a in A.for_program(p["id"]) if a["status"] != "canceled"]
    for a in acts:
        out += f"\n  활동 {A.line(a, full=False)[2:]}"
    c = p.get("container")
    out += (f"\n  참가 등록 정모: '{c['event']}' ({c['when']}까지) · 소개 글 「{c['post_title']}」" if c
            else "\n  참가 등록 정모: 아직 없음 (멤버에게 처음 물을 때 소개 글과 함께 연다)")
    if p.get("agent"):
        out += f"\n  담당 에이전트: 목표 {p['agent']['goal_id']}"
    for f in flags(p, by_id):
        out += f"\n  ⚠ {f} — 이 프로그램을 계속할지 다시 볼 것"
    return out


def block():
    """For 운영 모카's input: programs not finished or dropped, newest first."""
    alive = sorted(live(), key=lambda p: p["updated_at"], reverse=True)
    if not alive:
        return "## 모카의 프로그램\n(아직 없음)"
    from harness import hypotheses as H
    by_id = {h["id"]: h for h in H.load()}
    return "\n".join(["## 모카의 프로그램 (끝나지 않은 것, 최근 순)"] + [line(p, by_id) for p in alive])


TOOL = {
    "type": "function", "name": "propose_program",
    "description": "프로그램 하나를 만든다. 뒷받침된 가설에 근거해, 구체적인 목적을 가진 일련의 활동 계획을 기록한다. "
                   "하네스가 규칙을 확인하고 저장한다. 앱은 건드리지 않고 멤버에게도 보이지 않는다.",
    "parameters": {"type": "object", "properties": {
        "type": {"type": "string", "enum": list(TYPES),
                 "description": "linear=단계형 (순서대로 나아가 분명한 끝에 닿는다), recurring=정기 (주된 활동 하나를 주기적으로 반복)"},
        "title": {"type": "string", "description": f"프로그램 이름 ({LIMITS['title']}자 이내)"},
        "purpose": {"type": "string", "description": f"구체적인 목적: 누구에게 무엇이 어떻게 달라지는지 ({LIMITS['purpose']}자 이내)"},
        "hypotheses": {"type": "array", "description": "존재 근거가 되는 가설. 하나 이상, 모두 뒷받침됨 이상이어야 한다",
                       "items": {"type": "object", "properties": {
                           "id": {"type": "string", "description": "가설 id (h_…)"},
                           "why": {"type": "string",
                                   "description": f"이 가설이 이 프로그램을 왜 필요하게 만드는지 ({LIMITS['why']}자 이내)"}},
                                 "required": ["id", "why"]}},
        "reasoning": {"type": "string",
                      "description": f"같은 가설에 대응할 여러 방법 중 왜 이 형식인지 ({LIMITS['reasoning']}자 이내)"},
        "members": {"type": "array", "items": {"type": "string"},
                    "description": "참여할 만한 멤버의 앱 이름 (있으면). 확실하지 않으면 비우고 criteria로 설명해라"},
        "criteria": {"type": "string", "description": f"누구를 위한 프로그램인지 ({LIMITS['criteria']}자 이내)"},
        "size_min": {"type": "integer", "description": "알맞은 최소 인원 (선택)"},
        "size_max": {"type": "integer", "description": "알맞은 최대 인원 (선택)"},
        "entry_state": {"type": "string", "description": "단계형만: 시작 전 참가자의 상태"},
        "exit_state": {"type": "string", "description": "단계형만: 끝난 뒤 참가자의 상태"},
        "measure": {"type": "string", "description": "단계형만: 끝난 상태에 닿았는지 무엇으로 확인하는지"},
        "duration_days": {"type": "integer", "description": "단계형만: 전체 기간(일)"},
        "interval_days": {"type": "integer", "description": "정기만: 대략 며칠마다 반복하는지. 정확히 지킬 필요는 없다"},
        "repeat_kind": {"type": "string", "enum": list(REPEAT_KINDS),
                        "description": "정기만: constant=정해진 횟수, conditional=조건이 맞는 동안, infinite=끝없이. "
                                       "한 번뿐인 정모(친목 번개 등)는 constant에 repeat_count 1"},
        "repeat_count": {"type": "integer", "description": "정기이고 constant일 때만: 횟수"},
        "repeat_condition": {"type": "string", "description": "정기이고 conditional일 때만: 언제까지 이어가거나 멈추는지"},
        "format": {"type": "string",
                   "description": "정기만: 반복되는 주된 활동 하나. 처음 온 사람이 한두 번 참여로 알 수 있게"}},
        "required": ["type", "title", "purpose", "hypotheses", "reasoning", "criteria"]},
}


class Proposals:
    """Tool handler for the goal loop: the model proposes, the harness decides."""

    def __init__(self, goal_id=None, log=None, dry_run=False):
        self.goal_id = goal_id
        self.log = log
        self.dry_run = dry_run

    def __call__(self, **args):
        if self.dry_run:
            return f"(dry-run) 프로그램 요청을 확인했습니다: {args.get('title')}"
        fields = {k: v for k, v in args.items() if k in TOOL["parameters"]["properties"]}
        record, problem = create(**fields, goal_id=self.goal_id,
                                 created_by=f"goal:{self.goal_id}" if self.goal_id else "admin")
        if problem:
            if self.log:
                self.log(f"  프로그램 거절: {problem}")
            return f"프로그램을 만들지 않았습니다: {problem}"
        if self.log:
            self.log(f"  프로그램 {record['id']}: {show(record, record['title'])}")
        return (f"프로그램 {record['id']}를 만들었습니다 (상태: 계획됨). 기획(템플릿·스케치)은 하네스가 이번 운영 "
                "회차 끝에 채우고, 채워지면 이 목표를 깨웁니다. 정모는 그 뒤에 이 program_id로 엽니다. "
                "멤버에게는 보이지 않습니다.")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    records = load()
    print("\n\n".join(line(p) + f"\n  이유: {show(p, p['rationale']['reasoning'])}" for p in records) or "(프로그램 없음)")


if __name__ == "__main__":
    main()
