"""Activities: one gathering of a program, from draft to held (documents/program_agent.md).

A program's plan says what every session looks like; an activity is one actual session. It starts as a draft, long
before there is a 정모: who presents, what the topic is, when and where are open questions the program agent has to
settle with the members themselves. Those open questions are the plan's slots, copied into the draft with no value.
Each one is filled only when someone actually agreed — never inferred from what a member does for a living.

    {"id": "a_20260923_3fa1", "program_id": "p_…", "session": 2 | "stage": 3,
     "title": "…", "activity_type": "presentation", "mode": "offline",
     "entry_state", "exit_state", "check",        # what one session changes, copied from the plan
     "outline": "…",                              # the plan's agenda or the stage's outline, as text
     "slots": [{"name", "fill_by", "how", "fallback", "value": null, "note", "decided_at"}],
     "when": null, "location": null, "event": null,   # filled when the 정모 is created
     "status": "draft" | "scheduled" | "held" | "canceled", …}

Goals point at an activity by its id (harness/goals.py: activity_id), so the work of one session — recruiting a
host, settling the topic, choosing a time, checking readiness, judging afterwards — hangs together.

Status: draft (planning) → scheduled (a 정모 exists) → held (its time has passed) or canceled. The harness moves
scheduled → held; nothing else changes status on its own.

Usage (from the moca/ directory):
    python -m harness.activities            # every activity, as the program agent sees it
"""
import json
import os
import secrets
import sys
import time

from harness.tasks import ROOT

DATA = ROOT / "data" / "activities"
FILE = DATA / "activities.json"

STATUSES = {"draft": "기획 중", "scheduled": "정모 잡힘", "held": "지난 활동", "canceled": "취소됨"}
OPEN = ("draft", "scheduled")
MAX_DRAFTS = 2          # 한 프로그램이 동시에 기획 중인 활동 (다음 회차를 준비하며 지난 회차를 정리할 수 있게)
LIMITS = {"title": 60, "value": 200, "note": 300, "location": 80}
FMT = "%Y-%m-%d %H:%M:%S"


def load():
    if FILE.exists():
        return json.loads(FILE.read_text(encoding="utf-8"))
    return []


def save(records):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, FILE)


def get(activity_id, records=None):
    return next((a for a in (records if records is not None else load()) if a["id"] == activity_id), None)


def for_program(program_id, records=None, statuses=None):
    return [a for a in (records if records is not None else load()) if a["program_id"] == program_id
            and (statuses is None or a["status"] in statuses)]


def which(a):
    return f"{a['session']}회차" if a.get("session") else f"{a['stage']}단계"


def by_event(event, records=None):
    return next((a for a in (records if records is not None else load())
                 if a.get("event") == event and a["status"] in OPEN), None)


def _now():
    return time.strftime(FMT)


def _slots(plan_slots):
    return [{"name": s["name"], "fill_by": s["fill_by"], "how": s["how"], "fallback": s.get("fallback"),
             "value": None, "note": None, "decided_at": None} for s in plan_slots or []]


def draft(program_id, log=None):
    """Draft the program's next activity from its plan. Returns (activity, None) or (None, why not)."""
    from harness import programs as P
    p = next((x for x in P.load() if x["id"] == program_id), None)
    if p is None:
        return None, f"없는 프로그램입니다: {program_id}"
    if p["status"] not in P.OPEN_FOR_ACTIVITIES:
        return None, f"프로그램 {program_id}는 지금 '{P.STATUSES[p['status']]}' 상태입니다"
    plan = P.plan_of(p)
    if plan is None:
        return None, f"프로그램 {program_id}의 기획이 아직 없습니다"
    records = load()
    mine = for_program(program_id, records)
    drafts = [a for a in mine if a["status"] == "draft"]
    if len(drafts) >= MAX_DRAFTS:
        return None, (f"기획 중인 활동이 이미 {len(drafts)}개입니다 ({', '.join(which(a) for a in drafts)}). "
                      "먼저 진행하거나 취소하세요")
    used = [a for a in mine if a["status"] != "canceled"]
    common = {"id": f"a_{time.strftime('%Y%m%d')}_{secrets.token_hex(2)}", "program_id": program_id,
              "activity_type": plan.get("activity_type"), "type_label": plan.get("type_label"),
              "when": None, "location": None, "event": None, "status": "draft",
              "created_at": _now(), "updated_at": _now(), "history": [], "notes": None}
    if p["type"] == "recurring":
        rep = p["recurring"]["repeats"]
        if rep["kind"] == "constant" and len(used) >= rep["count"]:
            return None, f"프로그램 {program_id}는 {rep['count']}회로 정해져 있고 이미 {len(used)}회를 열었습니다"
        a = dict(common, session=len(used) + 1, title=plan["summary"][:LIMITS["title"]],
                 mode=plan["mode"], entry_state=plan["entry_state"], exit_state=plan["exit_state"],
                 check=plan["check"], slots=_slots(plan["slots"]),
                 outline=" → ".join(f"{x['title']} {x['minutes']}분" for x in plan["agenda"]))
    else:
        stages = plan["stages"]
        done = max((a.get("stage") or 0 for a in used), default=0)
        if done >= len(stages):
            return None, f"프로그램 {program_id}의 스케치는 {len(stages)}단계까지이고 모두 열었습니다"
        st = stages[done]
        a = dict(common, stage=st["n"], title=st["title"][:LIMITS["title"]], activity_type=st["activity_type"],
                 type_label=st.get("type_label"), mode=st["mode"], entry_state=st["entry_state"],
                 exit_state=st["exit_state"], check=st["check"], slots=_slots(st["slots"]), outline=st["outline"])
    records.append(a)
    save(records)
    if log:
        log(f"  활동 {a['id']} 기획 시작: {which(a)} {a['title']}")
    return a, None


def update(activity_id, slot=None, value=None, note=None, when=None, location=None, title=None, notes=None):
    """Fill in a slot or set a time/place on a draft. Returns (activity, None) or (None, why not)."""
    records = load()
    a = get(activity_id, records)
    if a is None:
        return None, f"없는 활동입니다: {activity_id}"
    if a["status"] not in ("draft", "scheduled"):
        return None, f"{activity_id}는 '{STATUSES[a['status']]}' 상태라 고칠 수 없습니다"
    changed = []
    if slot is not None:
        target = next((s for s in a["slots"] if s["name"] == slot), None)
        if target is None:
            return None, f"'{slot}'은 이 활동의 정할 것이 아닙니다: {[s['name'] for s in a['slots']]}"
        if not (value or "").strip():
            return None, "value가 비어 있습니다 (정해진 내용을 적으세요)"
        target.update(value=value.strip()[:LIMITS["value"]], note=(note or "").strip()[:LIMITS["note"]] or None,
                      decided_at=_now())
        changed.append(f"{slot}={target['value']}")
    for field, given, limit in (("when", when, None), ("location", location, LIMITS["location"]),
                                ("title", title, LIMITS["title"]), ("notes", notes, LIMITS["note"])):
        if given is None:
            continue
        if a["status"] == "scheduled" and field in ("when", "location"):
            return None, f"정모가 이미 잡혀 있어 {field}는 edit_event로 바꿔야 합니다"
        a[field] = given.strip()[:limit] if limit else given.strip()
        changed.append(f"{field}={a[field]}")
    if not changed:
        return None, "바꿀 내용을 하나 이상 주세요 (slot+value, when, location, title, notes)"
    a["updated_at"] = _now()
    a["history"].append({"at": a["updated_at"], "what": "; ".join(changed)})
    save(records)
    return a, None


def unfilled(a):
    return [s for s in a["slots"] if not s["value"]]


def check_schedulable(activity_id, mode=None, when=None):
    """May a 정모 be created for this activity now? Returns (activity, None) or (None, why not)."""
    from harness import programs as P
    a = get(activity_id)
    if a is None:
        return None, (f"없는 활동입니다: {activity_id}. 정모는 활동으로만 열 수 있으니 draft_activity로 "
                      "활동을 먼저 기획하세요")
    if a["status"] != "draft":
        return None, f"{activity_id}는 '{STATUSES[a['status']]}' 상태입니다 (기획 중인 활동에만 정모를 엽니다)"
    p = next((x for x in P.load() if x["id"] == a["program_id"]), None)
    if p is None or p["status"] not in P.OPEN_FOR_ACTIVITIES:
        return None, f"활동의 프로그램 {a['program_id']}가 정모를 열 수 있는 상태가 아닙니다"
    missing = unfilled(a)
    if missing:
        return None, ("아직 정해지지 않은 것이 있습니다: "
                      + "; ".join(f"{s['name']}({s['fill_by']}: {s['how'][:60]})" for s in missing)
                      + ". 당사자에게 확인해 update_activity로 채운 뒤에 정모를 여세요")
    if mode and mode not in P.PLAN_MODES[a["mode"]]:
        return None, f"활동의 기획은 {a['mode']}인데 정모의 mode가 {mode}입니다"
    if when and a["when"] and when[:16] != a["when"][:16]:
        return None, f"활동에 정해 둔 시각({a['when']})과 정모의 when({when})이 다릅니다"
    return a, None


def schedule(activity_id, event, when, location):
    """The 정모 exists: the draft becomes a scheduled session (and its program starts, if it hadn't)."""
    from harness import programs as P
    records = load()
    a = get(activity_id, records)
    a.update(event=event, when=when, location=location, status="scheduled", updated_at=_now())
    a["history"].append({"at": a["updated_at"], "what": f"정모 '{event}' 개설 ({when}, {location})"})
    save(records)
    P.started(a["program_id"], f"{which(a)} 정모 '{event}'")
    return a


def cancel(activity_id=None, event=None, reason=None):
    records = load()
    a = get(activity_id, records) if activity_id else by_event(event, records)
    if a is None:
        return None
    a.update(status="canceled", updated_at=_now())
    a["history"].append({"at": a["updated_at"], "what": f"취소: {reason or ''}"})
    save(records)
    return a


def rename_event(old, new):
    records = load()
    a = by_event(old, records)
    if a is None:
        return None
    a["event"] = new
    a["history"].append({"at": _now(), "what": f"정모 이름 변경: {old} → {new}"})
    save(records)
    return a


def sweep(log=None):
    """Sessions whose time has passed are held: the program agent then judges them against their exit state."""
    records, now, changed = load(), _now(), []
    for a in records:
        if a["status"] == "scheduled" and a.get("when") and a["when"][:16] < now[:16]:
            a.update(status="held", updated_at=now)
            a["history"].append({"at": now, "what": "정모 시각이 지남 (결과 확인 차례)"})
            changed.append(a)
    if changed:
        save(records)
        if log:
            for a in changed:
                log(f"  활동 {a['id']} ({which(a)} {a['title']}) 종료 — 결과를 확인할 차례")
    return changed


TYPE_LABELS = {"individual_study": "각자 스터디", "presentation": "발표", "hands_on": "실습", "discussion": "토론",
               "show_and_tell": "결과물 공유", "clinic": "질문·상담", "collab_project": "함께 만들기",
               "social": "친목", "other": "기타"}
MODE_LABELS = {"offline": "오프라인", "online": "온라인", "either": "온·오프라인 중 택일"}


def kind(a):
    return a.get("type_label") or TYPE_LABELS.get(a.get("activity_type"), a.get("activity_type") or "종류 미정")


def line(a, full=True):
    out = (f"- {a['id']} [{which(a)} · {STATUSES[a['status']]} · {kind(a)}"
           f" · {MODE_LABELS.get(a.get('mode'), a.get('mode'))}] {a['title']}")
    if a.get("when") or a.get("location"):
        out += f" ({a.get('when') or '시각 미정'}, {a.get('location') or '장소 미정'})"
    if a.get("event"):
        out += f" — 정모 '{a['event']}'"
    if not full:
        return out
    out += f"\n    이 회차: {a['entry_state']} → {a['exit_state']}\n    확인: {a['check']}"
    if a.get("outline"):
        out += f"\n    진행: {a['outline']}"
    for s in a["slots"]:
        state = f"정함: {s['value']}" + (f" ({s['note']})" if s["note"] else "") if s["value"] \
            else f"아직 미정 — {s['fill_by']}: {s['how']}"
        out += f"\n    · {s['name']}: {state}"
    if a.get("notes"):
        out += f"\n    메모: {a['notes']}"
    return out


def block(program_id):
    """For the program agent's input: its activities, newest first."""
    mine = sorted(for_program(program_id), key=lambda a: a["created_at"], reverse=True)
    if not mine:
        return "## 이 프로그램의 활동\n(아직 없음 — draft_activity로 다음 회차를 기획해라)"
    return "\n".join(["## 이 프로그램의 활동 (최근 순)"] + [line(a) for a in mine])


TOOLS = [
    {"type": "function", "name": "draft_activity",
     "description": "이 프로그램의 다음 활동(회차·단계)을 기획으로 연다. 기획(템플릿·스케치)에서 형식과 "
                    "'정할 것'(슬롯)을 그대로 가져온다. 정모는 아직 만들지 않는다.",
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"type": "function", "name": "update_activity",
     "description": "기획 중인 활동에 정해진 것을 기록한다. 슬롯은 당사자가 실제로 동의했을 때만 채운다 "
                    "(직업이나 관심만 보고 정하지 마라). 시각·장소·제목·메모도 여기서 적는다.",
     "parameters": {"type": "object", "properties": {
         "activity_id": {"type": "string"},
         "slot": {"type": "string", "description": "채울 '정할 것'의 이름 (활동에 있는 그대로)"},
         "value": {"type": "string", "description": "정해진 내용 (누가·무엇으로)"},
         "note": {"type": "string", "description": "어떻게 정해졌는지 (누가 언제 동의했는지 등)"},
         "when": {"type": "string", "description": "정모 예정 시각 'YYYY-MM-DD HH:MM'"},
         "location": {"type": "string", "description": "장소 (온라인이면 접속 방식)"},
         "title": {"type": "string"}, "notes": {"type": "string", "description": "이 회차에 대한 메모"}},
         "required": ["activity_id"]}},
    {"type": "function", "name": "cancel_activity",
     "description": "기획 중인 활동을 접는다. 이미 정모가 잡혔으면 cancel_event를 쓴다.",
     "parameters": {"type": "object", "properties": {
         "activity_id": {"type": "string"}, "reason": {"type": "string"}}, "required": ["activity_id", "reason"]}},
]


class Tools:
    """Tool handlers for the program agent (admin/goal_loop.py runs them as tool tasks)."""

    def __init__(self, program_id, log=None, dry_run=False):
        self.program_id = program_id
        self.log = log
        self.dry_run = dry_run

    def _mine(self, activity_id):
        a = get(activity_id)
        if a is None:
            return f"없는 활동입니다: {activity_id}"
        if a["program_id"] != self.program_id:
            return f"{activity_id}는 다른 프로그램의 활동입니다"
        return None

    def draft_activity(self, **_):
        if self.dry_run:
            return "(dry-run) 활동 기획 요청을 확인했습니다"
        a, problem = draft(self.program_id, self.log)
        if problem:
            return f"draft_activity 실패: {problem}"
        return f"draft_activity 완료: {a['id']} ({which(a)}) 기획을 열었습니다\n{line(a)}"

    def update_activity(self, activity_id=None, **fields):
        problem = self._mine(activity_id)
        if problem:
            return f"update_activity 실패: {problem}"
        if self.dry_run:
            return f"(dry-run) {activity_id} 수정 요청을 확인했습니다: {fields}"
        a, problem = update(activity_id, **{k: v for k, v in fields.items()
                                            if k in ("slot", "value", "note", "when", "location", "title", "notes")})
        if problem:
            return f"update_activity 실패: {problem}"
        left = unfilled(a)
        return (f"update_activity 완료: {activity_id}\n{line(a)}"
                + (f"\n아직 정할 것: {', '.join(s['name'] for s in left)}" if left else "\n정할 것을 모두 정했습니다"))

    def cancel_activity(self, activity_id=None, reason=None, **_):
        problem = self._mine(activity_id)
        if problem:
            return f"cancel_activity 실패: {problem}"
        a = get(activity_id)
        if a["status"] != "draft":
            return f"cancel_activity 실패: {activity_id}는 '{STATUSES[a['status']]}' 상태입니다"
        if self.dry_run:
            return f"(dry-run) {activity_id} 취소 요청을 확인했습니다"
        cancel(activity_id, reason=reason)
        return f"cancel_activity 완료: {activity_id}를 접었습니다 ({reason})"

    def handlers(self):
        return {"draft_activity": self.draft_activity, "update_activity": self.update_activity,
                "cancel_activity": self.cancel_activity}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    records = load()
    print("\n".join(line(a) for a in records) or "(활동 없음)")


if __name__ == "__main__":
    main()
