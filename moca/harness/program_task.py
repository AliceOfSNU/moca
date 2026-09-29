"""운영 모카가 프로그램 담당 모카에게 일을 맡기는 길 (documents/program_agent.md).

Until now the two agents never spoke. 운영 모카 wrote a program record; the harness noticed a program without an
agent and made one, deriving its brief from the record itself. Nobody authored that brief, nobody was told the
agent existed, and the only thing that ever came back was the program ending. When the program side got stuck it
filed a request to 로하 — using the developer as a message bus between two of 모카's own agents.

So a running task becomes the channel, the same shape as a Group Chat Task: 운영 모카 asks for one deliverable
and waits; the program agent answers by finishing the goal that the request created.

    운영 모카  ──[ Request: 무엇을 해내라 + 무엇으로 끝났다고 보는지 ]──▶  프로그램 모카
              ◀─[ Finish: 해냈다 / 여기까지 하고 멈췄다 + 한계 ]────────

The request *is* the goal. The harness creates it in the program agent's tree when the task starts, so a
deliverable is authored, timestamped and auditable, and the agent has nothing to do until one arrives — an agent
with no request has no goal, so the loop's focus() never picks it and it burns no rounds.

One request at a time per program: the agent's attention is the scarce thing, the way the 모임 chat is.

    {"id": "t_…", "spec": {"executor": {"type": "agent", "name": "program_moca"},
                           "target": {"channel": "program", "program_id": "p_…", "goal_id": "g_p_…_r1"},
                           "instruction": "…", "criteria": ["…"]},
     "status": …, "result": {"outcome", "summary", …}}

Later (not yet): Interrupt / Resume / Stop / Update downward, Pause upward. Each needs to say what it promises
about what the agent has already done in the 모임 — an opened 정모 and a sent 안내 cannot be taken back.
"""
import datetime as dt
import secrets

from harness import goals as G
from harness import programs as P
from harness import tasks as T

PROGRAM_MOCA = {"type": "agent", "name": "program_moca"}
MAX_OPEN_PER_PROGRAM = 1      # 담당 모카 하나가 한 번에 맡는 일 하나
INSTRUCTION_LIMIT = 300
MAX_CRITERIA = 4
DEFAULT_HOURS = 24


def is_program_task(task):
    spec = task["spec"]
    return spec["executor"] == PROGRAM_MOCA and spec.get("target", {}).get("channel") == "program"


def program_tasks(statuses=T.OPEN):
    return [t for t in T.all_tasks() if is_program_task(t) and t["status"] in statuses]


def open_for(program_id):
    return [t for t in program_tasks() if t["spec"]["target"]["program_id"] == program_id]


def for_goal(goal_id):
    """The request a goal came from, if any — how a finished goal finds the task waiting on it."""
    return next((t for t in program_tasks() if t["spec"]["target"].get("goal_id") == goal_id), None)


def _goal_id(program_id):
    return f"g_{program_id}_r{secrets.token_hex(2)}"


def check(program_id):
    """May 운영 모카 hand this program's agent a job? Returns (program, None) or (None, why not)."""
    p = next((x for x in P.load() if x["id"] == program_id), None)
    if p is None:
        return None, f"없는 프로그램입니다: {program_id}"
    if p["status"] not in P.OPEN_FOR_ACTIVITIES:
        return None, f"프로그램 {program_id}는 지금 '{P.STATUSES[p['status']]}' 상태입니다"
    if not p.get("agent"):
        return None, (f"프로그램 {program_id}에는 아직 담당 모카가 없습니다. 기획이 채워지면 하네스가 붙입니다")
    already = open_for(program_id)
    if len(already) >= MAX_OPEN_PER_PROGRAM:
        t = already[0]
        return None, (f"이 프로그램의 담당 모카는 지금 다른 일을 맡고 있습니다 ({t['id']}: "
                      f"{t['spec']['instruction'][:60]}). 그 결과를 받은 뒤에 다음 일을 맡기세요")
    return p, None


def create(program_id, instruction, criteria, hours=DEFAULT_HOURS, goal_id=None, created_by="admin", log=None):
    """Hand one deliverable to a program's agent. Creates the goal in its tree and the task 운영 모카 waits on.
    Returns (task, None) or (None, why the harness refused)."""
    instruction = (instruction or "").strip()
    criteria = [c.strip() for c in (criteria or []) if isinstance(c, str) and c.strip()][:MAX_CRITERIA]
    if not instruction:
        return None, "instruction이 비어 있습니다 (무엇을 해내야 하는지 한 문장)"
    if len(instruction) > INSTRUCTION_LIMIT:
        return None, f"instruction은 {INSTRUCTION_LIMIT}자 이내여야 합니다"
    if not criteria:
        return None, "completion_criteria를 하나 이상 적으세요 (무엇을 보면 끝났다고 할 수 있는지)"
    if any(len(c) > G.OBJECTIVE_LIMIT for c in criteria):
        return None, f"완료 기준 하나는 {G.OBJECTIVE_LIMIT}자 이내여야 합니다"
    try:
        hours = int(hours)
    except (TypeError, ValueError):
        return None, "deadline_hours는 정수여야 합니다"
    if not T.DEADLINE_HOURS[0] <= hours <= T.DEADLINE_HOURS[1]:
        return None, f"deadline_hours는 {T.DEADLINE_HOURS[0]}~{T.DEADLINE_HOURS[1]} 사이여야 합니다"
    p, problem = check(program_id)
    if problem:
        return None, problem

    created = dt.datetime.now()
    new_goal = _goal_id(program_id)
    goal = G.add(new_goal, instruction[:G.OBJECTIVE_LIMIT], criteria,
                 created_by=f"운영 모카(작업)" if goal_id is None else f"운영 모카(목표 {goal_id})",
                 program_id=program_id)
    task = {"id": f"t_{created:%Y%m%d_%H%M}_{secrets.token_hex(2)}",
            "spec": {"executor": dict(PROGRAM_MOCA),
                     "target": {"channel": "program", "program_id": program_id, "goal_id": new_goal},
                     "instruction": instruction, "criteria": criteria},
            "status": "running",   # the agent has it the moment its goal exists; there is nothing to "send"
            "result": None, "created_by": created_by, "goal_id": goal_id,
            "created_at": created.strftime(T.FMT),
            "deadline": (created + dt.timedelta(hours=hours)).strftime(T.FMT),
            "run": {"started_at": created.strftime(T.FMT)}}
    T.save(task)
    T._log("created", task, created_by=created_by, deadline=task["deadline"], goal_id=goal_id,
           program_id=program_id, agent_goal=new_goal, channel="program")
    if log:
        log(f"  프로그램 {program_id}의 담당 모카에게 일을 맡김: {new_goal} — {instruction}")
    return task, None


def finish_from_outcome(goal, status, outcome, log=None):
    """The agent finished a requested goal: hand the outcome back as the task's result. Returns the task or None.
    'achieved' is an answer; a goal closed without reaching its criteria is a partial one, and its limitations
    are what 운영 모카 most needs to read."""
    task = for_goal(goal["id"])
    if task is None:
        return None
    limits = outcome.get("limitations") or []
    summary = outcome["summary"] + ("" if not limits else " / 한계: " + "; ".join(limits))
    T.finish(task, {"outcome": "answer_available" if status == "achieved" else "partial_answer",
                    "summary": summary, "summary_subjects": [], "knowledge": []},
             how="early" if status == "achieved" else "given_up")
    if log:
        log(f"  맡긴 일 {task['id']} 끝: {'해냄' if status == 'achieved' else '여기까지'} — {outcome['summary'][:80]}")
    return task


def close_open(program_id, reason, log=None):
    """The program ended while its agent still had a job: the waiting side must not wait forever."""
    for task in open_for(program_id):
        T.finish(task, {"outcome": "no_shareable_answer", "summary": f"프로그램이 끝나 맡긴 일도 멈췄습니다: {reason}",
                        "summary_subjects": [], "knowledge": []}, how="program_over")
        if log:
            log(f"  맡긴 일 {task['id']}도 함께 멈춤 ({reason})")


def block(program_id=None):
    """For 운영 모카's input: which programs have an agent, and what it is doing. This is how 운영 모카 finds
    out an agent exists at all — the harness makes one without being asked."""
    lines = ["## 프로그램 담당 모카"]
    live = [p for p in P.load() if p["status"] in P.OPEN_FOR_ACTIVITIES]
    if not live:
        return "\n".join(lines + ["(진행 중인 프로그램이 없음)"])
    for p in live:
        title = P.show(p, p["title"])
        if not p.get("agent"):
            lines.append(f"- {p['id']} 「{title}」: 담당 모카 없음 (기획이 채워지면 하네스가 붙인다)")
            continue
        jobs = open_for(p["id"])
        if jobs:
            t = jobs[0]
            lines.append(f"- {p['id']} 「{title}」: 맡긴 일 진행 중 — {t['spec']['instruction']} "
                         f"(작업 {t['id']}, 마감 {t['deadline']})")
        else:
            lines.append(f"- {p['id']} 「{title}」: 담당 모카가 붙어 있고 지금 맡은 일이 없다. "
                         f"start_task의 program_moca로 한 가지를 맡길 수 있다")
    return "\n".join(lines)


class Tools:
    """운영 모카's side: ending a program. Which programs exist is its decision, and with the agent no longer
    holding a goal that stands for the whole program, nothing else could end one."""

    def __init__(self, log=None, dry_run=False):
        self.log = log
        self.dry_run = dry_run

    def finish_program(self, program_id=None, status=None, reason=None, **_):
        from admin import program_agent
        if status not in ("finished", "dropped"):
            return "finish_program 실패: status는 finished(목적을 이뤘다) 또는 dropped(그만둔다)여야 합니다"
        if not (reason or "").strip():
            return "finish_program 실패: reason을 적으세요 (왜 끝내는지)"
        if self.dry_run:
            return f"(dry-run) 프로그램 {program_id}를 {status}로 끝내는 요청을 확인했습니다"
        p, problem = program_agent.teardown(program_id, status, reason.strip(), self.log)
        if problem:
            return f"finish_program 실패: {problem}"
        return (f"finish_program 완료: 프로그램 {program_id}를 {P.STATUSES[status]}으로 끝냈습니다. "
                f"담당 모카의 목표와 기획 중이던 활동도 정리했습니다")

    def handlers(self):
        return {"finish_program": self.finish_program}
