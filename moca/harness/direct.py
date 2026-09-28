"""1:1 메시지를 모카가 먼저 보내는 일 — 프로그램에 신청한 멤버에게만 (documents/program_agent.md).

Until now every 1:1 began with the member. The loop answers whoever wrote, and the only messages 모카 sent
first were the harness's own: the consent notice after a 가입인사, a tier notice. So there was no way to tell
one member what to prepare and another what to expect — the guidance had to go on the board, one text for
everyone, ending in "공유 희망 여부는 별도로 알려 주시면".

This is that message, as a task, like a Group Chat Task but addressed to one person. What keeps it from
becoming a broadcast channel is that the harness, not the model, decides who may receive one:

- it belongs to a program that is running (or to one of that program's activities);
- the member is on that 정모's attendee list — read from the app, never taken from the model's word, and read
  again right before the message goes out, because a member can cancel in between;
- the member agreed to 1:1 conversations (chatbot/dm.py's gate), allowed what they say there to be used for
  모임 운영 (chatbot/memory_consent.py — the answer to this message goes back to 운영 모카, so that consent is
  the whole question), and has not opted out of these messages;
- nothing is already outstanding to that member for this program, and one activity may write to a member once;
- the harness itself prepends the line naming the program and how to stop these, so a member can always tell
  why they were written to without having to trust that the model said so.

The model chooses what to say and to whom; every one of those checks is the harness's.

    {"id": "t_…", "spec": {"executor": {"type": "agent", "name": "dm_moca"},
                           "target": {"channel": "direct", "member": "…", "program_id": "p_…",
                                      "activity_id": "a_…" | null},
                           "instruction": "무엇을 알리고 무엇을 확인할지 한 문장"},
     "status": …, "result": …}
"""
import json
import time

from harness import tasks as T

OPTOUT = T.DATA_ROOT / "members" / "dm_optout.json"
DM_MOCA = {"type": "agent", "name": "dm_moca"}
MAX_OPEN_PER_MEMBER = 1       # 한 멤버에게 답을 기다리는 안내는 하나
MAX_PER_ACTIVITY = 1          # 한 활동에서 한 멤버에게 한 번
MAX_OPEN = 3                  # 한 번에 여러 사람에게 벌어져 있는 안내
INSTRUCTION_LIMIT = 300
DEFAULT_HOURS = 48
STOP_WORDS = ("안내 그만", "안내 중단", "그만 보내", "안 받을", "받지 않을")


# --- 무엇에 대한 안내인지: 프로그램과 활동 -----------------------------------------------------------

def _program(program_id):
    from harness import programs as P
    p = next((x for x in P.load() if x["id"] == program_id), None)
    if p is None:
        return None, f"없는 프로그램입니다: {program_id}"
    # "계획됨"도 포함한다: 프로그램은 첫 회차 정모가 열릴 때 비로소 "진행 중"이 되는데(harness/activities.py),
    # 참가자에게 준비를 안내해야 하는 시점은 그 전이다. 멈춘·끝난·그만둔 프로그램은 제외된다.
    if p["status"] not in P.OPEN_FOR_ACTIVITIES:
        return None, (f"프로그램 {program_id}는 지금 '{P.STATUSES[p['status']]}' 상태입니다. 참가 신청을 받는 "
                      "프로그램의 참가자에게만 1:1로 먼저 말을 걸 수 있습니다")
    return p, None


def event_of(program_id, activity_id=None):
    """The 정모 whose attendee list decides who may be written to: the activity's own 정모 for an activity,
    the program's 참가 등록 정모 otherwise. Returns (정모 이름, None) or (None, why not)."""
    from harness import activities as A
    p, problem = _program(program_id)
    if problem:
        return None, problem
    if activity_id:
        a = A.get(activity_id)
        if a is None:
            return None, f"없는 활동입니다: {activity_id}"
        if a["program_id"] != program_id:
            return None, f"활동 {activity_id}는 프로그램 {program_id}의 것이 아닙니다"
        if not a.get("event"):
            return None, (f"활동 {activity_id}는 아직 정모로 열리지 않았습니다. 참가자가 정해지기 전에는 1:1로 "
                          "먼저 말을 걸 수 없습니다")
        return a["event"], None
    if not p.get("container"):
        return None, (f"프로그램 {program_id}의 참가 등록 정모가 아직 없습니다. 신청한 사람이 없으니 1:1로 먼저 "
                      "말을 걸 수 없습니다 (open_program_signup)")
    return p["container"]["event"], None


# --- 신청했는가: 하네스가 읽는다 ---------------------------------------------------------------------

def signed_up(event_name):
    """Members the harness has observed signing up for this 정모, from its own knowledge records.
    Cheap and app-free, so it screens a task at creation; the list is re-read from the app before sending."""
    from harness import knowledge
    out = set()
    for k in knowledge.load_all():
        o = k.get("origin") or {}
        if o.get("channel") == "event" and o.get("event") == event_name:
            out |= {s for s in k.get("subjects") or []}
    return out


def attending(events_ui, event_name):
    """Who is on the 정모's attendee list right now, straight from the app ([] means nobody, None means the
    list could not be read — and a list that could not be read is never treated as permission)."""
    try:
        return events_ui.participants(event_name)
    except Exception:
        return None


# --- 받지 않기로 한 사람 -----------------------------------------------------------------------------

def load_optout():
    return json.loads(OPTOUT.read_text(encoding="utf-8")) if OPTOUT.exists() else {}


def set_optout(member, how="1:1 대화", on=True):
    records = load_optout()
    if on:
        records[member] = {"at": time.strftime(T.FMT), "how": how}
    else:
        records.pop(member, None)
    OPTOUT.parent.mkdir(parents=True, exist_ok=True)
    OPTOUT.write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
    return records


def opted_out(member):
    return member in load_optout()


def asked_to_stop(text):
    """A member's own words ending these. Matched by the harness so it does not depend on the model noticing."""
    squashed = (text or "").replace(" ", "")
    return any(w.replace(" ", "") in squashed for w in STOP_WORDS)


# --- 작업 ------------------------------------------------------------------------------------------

def is_direct(task):
    spec = task["spec"]
    return spec["executor"] == DM_MOCA and spec.get("target", {}).get("channel") == "direct"


def direct_tasks(statuses=T.OPEN):
    return [t for t in T.all_tasks() if is_direct(t) and t["status"] in statuses]


def for_member(member, statuses=T.OPEN):
    return [t for t in direct_tasks(statuses) if t["spec"]["target"]["member"] == member]


def written_for(member, program_id, activity_id=None):
    """Has this member already been written to for this program (or this activity)? Includes finished tasks:
    the cap is about how often a member is approached, not about what is still open."""
    out = []
    for t in T.all_tasks():
        if not is_direct(t):
            continue
        target = t["spec"]["target"]
        if target["member"] != member or target.get("program_id") != program_id:
            continue
        if activity_id is None or target.get("activity_id") == activity_id:
            out.append(t)
    return out


def needs_dm():
    """Should the loop open a 1:1 for a task? A queued one waits to be sent, a running one past its deadline
    waits for its report."""
    return any(t["status"] == "queued" or T.deadline_passed(t) for t in direct_tasks())


def check(member, program_id, activity_id=None, events_ui=None, ignore=None):
    """May 모카 write to this member first? Returns (context, None) or (None, why not).
    `events_ui` makes it the authoritative check: the attendee list is read from the app.
    `ignore` is a task id the counts leave out — the same check runs again just before that task's message is
    sent, and a task must not be counted as its own outstanding one.
    Context carries the 정모 and the program title, which the harness (not the model) puts in the message."""
    from chatbot.dm import has_consented
    from chatbot.memory_consent import shares
    from chatbot.profiles import profiles
    from harness import programs as P

    member = (member or "").strip()
    if not member:
        return None, "누구에게 보낼지(member) 적어야 합니다"
    if member not in profiles():
        return None, f"모르는 멤버입니다: {member}. member_search로 앱 이름을 확인하세요"
    p, problem = _program(program_id)
    if problem:
        return None, problem
    event_name, problem = event_of(program_id, activity_id)
    if problem:
        return None, problem
    if not has_consented(member):
        return None, (f"{member}님은 1:1 메시지 안내에 아직 동의하지 않았습니다. 동의 전에는 모카가 먼저 말을 걸 수 "
                      "없습니다. 모임 채팅에서 물어보세요")
    if opted_out(member):
        return None, f"{member}님은 1:1 운영 안내를 받지 않기로 했습니다. 모임 채팅이나 게시글로 알리세요"
    # the answer to this message comes back to 운영 — which is exactly the use the memory-scope question asks
    # about. Without that consent a 1:1 stays between the member and chat 모카.
    if not shares(member):
        return None, (f"{member}님은 1:1 내용을 모임 운영에 쓰는 것에 동의하지 않았습니다(또는 아직 답하지 않았습니다). "
                      "그 동의 없이는 모카가 먼저 1:1을 보낼 수 없습니다. 모임 채팅이나 게시글로 알리세요")

    roster = attending(events_ui, event_name) if events_ui is not None else None
    if roster is None:
        roster = signed_up(event_name)
        checked = "기록"
    else:
        checked = "앱"
    if member not in set(roster):
        return None, (f"{member}님은 정모 '{event_name}'에 신청하지 않았습니다 ({checked} 확인). 신청한 멤버에게만 "
                      "1:1로 먼저 말을 걸 수 있습니다")

    open_mine = [t for t in for_member(member)
                 if t["spec"]["target"].get("program_id") == program_id and t["id"] != ignore]
    if len(open_mine) >= MAX_OPEN_PER_MEMBER:
        return None, (f"{member}님께 보낸 안내의 답을 아직 기다리는 중입니다 ({open_mine[0]['id']}). 답을 받은 뒤에 "
                      "다시 보내세요")
    earlier = [t for t in written_for(member, program_id, activity_id) if t["id"] != ignore]
    if activity_id and len(earlier) >= MAX_PER_ACTIVITY:
        return None, f"{member}님께는 이 활동에 대해 이미 안내했습니다. 같은 활동으로 다시 보내지 않습니다"
    if len([t for t in direct_tasks() if t["id"] != ignore]) >= MAX_OPEN:
        return None, f"답을 기다리는 1:1 안내가 이미 {MAX_OPEN}개입니다. 먼저 온 답을 받은 뒤에 보내세요"
    return {"program": p, "program_title": P.show(p, p["title"]), "event": event_name,
            "activity_id": activity_id, "member": member}, None


def create(member, instruction, program_id, activity_id=None, hours=DEFAULT_HOURS, goal_id=None,
           created_by="admin", events_ui=None):
    """Queue a 1:1 안내 for one member. Returns (task, None) or (None, why the harness refused)."""
    instruction = (instruction or "").strip()
    if not instruction:
        return None, "instruction이 비어 있습니다"
    if len(instruction) > INSTRUCTION_LIMIT:
        return None, f"instruction은 {INSTRUCTION_LIMIT}자 이내여야 합니다"
    try:
        hours = int(hours)
    except (TypeError, ValueError):
        return None, "deadline_hours는 정수여야 합니다"
    if not T.DEADLINE_HOURS[0] <= hours <= T.DEADLINE_HOURS[1]:
        return None, f"deadline_hours는 {T.DEADLINE_HOURS[0]}~{T.DEADLINE_HOURS[1]} 사이여야 합니다"
    context, problem = check(member, program_id, activity_id, events_ui=events_ui)
    if problem:
        return None, problem
    import datetime as dt
    import secrets
    created = dt.datetime.now()
    task = {"id": f"t_{created:%Y%m%d_%H%M}_{secrets.token_hex(2)}",
            "spec": {"executor": dict(DM_MOCA),
                     "target": {"channel": "direct", "member": member, "program_id": program_id,
                                "activity_id": activity_id or None, "event": context["event"]},
                     "instruction": instruction},
            "status": "queued", "result": None, "created_by": created_by, "goal_id": goal_id,
            "created_at": created.strftime(T.FMT),
            "deadline": (created + dt.timedelta(hours=hours)).strftime(T.FMT), "run": {}}
    T.save(task)
    T._log("created", task, created_by=created_by, deadline=task["deadline"], goal_id=goal_id,
           member=member, program_id=program_id, activity_id=activity_id, channel="direct")
    return task, None


# --- 멤버가 보는 머리글: 모델이 아니라 하네스가 붙인다 -------------------------------------------------

def header(program_title):
    """Why this message arrived and how to stop it. The model writes the message; this line is not its to
    write, because a member deciding whether to trust it should not have to trust the model."""
    return (f"(「{program_title}」에 참가 신청하신 분께 보내는 안내예요. 이런 1:1 안내를 받지 않으려면 "
            f"'안내 그만'이라고 답해 주세요.)")


def block(program_id):
    """For the program agent's input: who has been written to already, so it doesn't write again."""
    mine = [t for t in T.all_tasks() if is_direct(t) and t["spec"]["target"].get("program_id") == program_id]
    if not mine:
        return "## 1:1로 안내한 사람\n(아직 없음)"
    lines = ["## 1:1로 안내한 사람 (신청한 멤버에게만 보낼 수 있다)"]
    for t in mine:
        target = t["spec"]["target"]
        state = {"queued": "보내는 중", "running": "답 기다림", "succeeded": "답 받음", "failed": "답 없음"}
        lines.append(f"- {target['member']}: {t['spec']['instruction'][:60]} [{state.get(t['status'], t['status'])}]"
                     + (f" (활동 {target['activity_id']})" if target.get("activity_id") else ""))
    return "\n".join(lines)
