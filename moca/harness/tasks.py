"""Tasks: the work a step starts, created and run by the harness (documents/orient.md).

A task lives only from creation until 운영 모카 has read its result, but the loop restarts often, so a live
task is kept as data/tasks/<id>.json. When it is consumed the file goes away and one line stays in
data/tasks/log.jsonl. Only one kind exists so far — the Group Chat Task, carried out by chat 모카
(chatbot/group_task.py):

    {"id": "t_...", "spec": {"executor": {"type": "agent", "name": "chat_moca"},
                             "target": {"channel": "group_chat"}, "instruction": "..."},
     "status": "queued" | "running" | "succeeded" | "failed", "result": null | {...},
     "created_by": "admin", "created_at": "...", "deadline": "...", "run": {...harness-owned progress...}}

Usage (from the moca/ directory):
    python -m harness.tasks list
    python -m harness.tasks new "자율스터디에 참여하기 편한 대략적인 시간대를 확인하라." --hours 24
    python -m harness.tasks show t_...
"""
import argparse
import datetime as dt
import json
import pathlib
import secrets
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
TASKS = ROOT / "data" / "tasks"
LOG = TASKS / "log.jsonl"

OPEN = ("queued", "running")
DONE = ("succeeded", "failed")
CHAT_MOCA = {"type": "agent", "name": "chat_moca"}
DEADLINE_HOURS = (1, 72)
DEFAULT_HOURS = 24
MAX_OPEN_GROUP_CHAT = 1      # one question at a time in the 모임 chat
MAX_GROUP_CHAT_PER_DAY = 4   # members shouldn't feel surveyed
MAX_TASK_MESSAGES = 6        # messages 모카 may send the 모임 for one task (opener + follow-ups + thanks)
FOLLOW_UP_GAP = 20 * 60      # seconds between a task's messages: stops bursts, the budget does the limiting
INSTRUCTION_LIMIT = 300
MAX_START_ATTEMPTS = 3
FMT = "%Y-%m-%d %H:%M:%S"


def now():
    return time.strftime(FMT)


def _path(task_id):
    return TASKS / f"{task_id}.json"


def _log(event, task, **extra):
    TASKS.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": now(), "event": event, "task": task["id"], "status": task["status"],
                            "instruction": task["spec"].get("instruction"), **extra}, ensure_ascii=False) + "\n")


def save(task):
    TASKS.mkdir(parents=True, exist_ok=True)
    _path(task["id"]).write_text(json.dumps(task, ensure_ascii=False, indent=1), encoding="utf-8")


def load(task_id):
    path = _path(task_id)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def all_tasks():
    if not TASKS.exists():
        return []
    tasks = [json.loads(p.read_text(encoding="utf-8")) for p in TASKS.glob("t_*.json")]
    return sorted(tasks, key=lambda t: t["created_at"])


def is_group_chat(task):
    spec = task["spec"]
    return spec["executor"] == CHAT_MOCA and spec.get("target", {}).get("channel") == "group_chat"


def group_chat_tasks(statuses=OPEN):
    return [t for t in all_tasks() if is_group_chat(t) and t["status"] in statuses]


def running_group_chat_task():
    return next((t for t in group_chat_tasks(("running",))), None)


def deadline_passed(task):
    return time.strftime(FMT) >= task["deadline"]


def _created_today():
    if not LOG.exists():
        return 0
    today = time.strftime("%Y-%m-%d")
    return sum(1 for line in LOG.read_text(encoding="utf-8").splitlines()
               if line.strip() and (e := json.loads(line))["event"] == "created" and e["at"].startswith(today))


def create_group_chat_task(instruction, hours=DEFAULT_HOURS, created_by="admin", goal_id=None):
    """Queue a Group Chat Task for chat 모카. Returns (task, None) or (None, why the harness refused)."""
    instruction = (instruction or "").strip()
    if not instruction:
        return None, "instruction이 비어 있습니다"
    if len(instruction) > INSTRUCTION_LIMIT:
        return None, f"instruction은 {INSTRUCTION_LIMIT}자 이내여야 합니다"
    try:
        hours = int(hours)
    except (TypeError, ValueError):
        return None, "deadline_hours는 정수여야 합니다"
    if not DEADLINE_HOURS[0] <= hours <= DEADLINE_HOURS[1]:
        return None, f"deadline_hours는 {DEADLINE_HOURS[0]}~{DEADLINE_HOURS[1]} 사이여야 합니다"
    if len(group_chat_tasks()) >= MAX_OPEN_GROUP_CHAT:
        return None, "이미 모임 채팅에서 진행 중인 작업이 있습니다. 그 결과를 받은 뒤에 새로 요청하세요"
    if _created_today() >= MAX_GROUP_CHAT_PER_DAY:
        return None, f"모임 채팅 작업은 하루 {MAX_GROUP_CHAT_PER_DAY}개까지입니다"
    created = dt.datetime.now()
    task = {"id": f"t_{created:%Y%m%d_%H%M}_{secrets.token_hex(2)}",
            "spec": {"executor": dict(CHAT_MOCA), "target": {"channel": "group_chat"}, "instruction": instruction},
            "status": "queued", "result": None, "created_by": created_by, "goal_id": goal_id,
            "created_at": created.strftime(FMT), "deadline": (created + dt.timedelta(hours=hours)).strftime(FMT),
            "run": {}}
    save(task)
    _log("created", task, created_by=created_by, deadline=task["deadline"], goal_id=goal_id)
    return task, None


VOTE_WATCHER = {"type": "watcher", "name": "vote"}


def is_vote(task):
    return task["spec"]["executor"] == VOTE_WATCHER


def vote_tasks(statuses=OPEN):
    return [t for t in all_tasks() if is_vote(t) and t["status"] in statuses]


def open_tasks():
    """Everything 운영 모카 may still be waiting on."""
    return group_chat_tasks() + vote_tasks()


def vote_due():
    """Open vote watchers whose vote should be over by now — the harness has results to collect."""
    return [t for t in vote_tasks(("running",)) if deadline_passed(t)]


def create_vote_task(title, deadline, goal_id=None, created_by="harness"):
    """Watch a vote 모카 just posted. Unlike a tool task this one stays open, so a goal can wait on it:
    when the vote closes, the harness finishes it with the tally (admin/votes.py: finish_vote_tasks)."""
    created = dt.datetime.now()
    task = {"id": f"t_{created:%Y%m%d_%H%M}_{secrets.token_hex(2)}",
            "spec": {"executor": dict(VOTE_WATCHER), "target": {"vote": title},
                     "instruction": f"투표 '{title}'의 결과를 기다린다"},
            "status": "running", "result": None, "created_by": created_by, "goal_id": goal_id,
            "created_at": created.strftime(FMT), "deadline": deadline, "run": {"started_at": created.strftime(FMT)}}
    save(task)
    _log("created", task, created_by=created_by, deadline=deadline, goal_id=goal_id, vote=title)
    return task


def run_tool_task(goal_id, name, arguments, run):
    """A tool task runs at once, so it never needs a file: create, run, log, done.
    `run()` returns (ok, result text). Returns (task_id, status, result text)."""
    task = {"id": f"t_{time.strftime('%Y%m%d_%H%M')}_{secrets.token_hex(2)}",
            "spec": {"executor": {"type": "tool", "name": name}, "arguments": arguments}, "status": "running"}
    ok, result = run()
    task["status"] = "succeeded" if ok else "failed"
    _log("tool", task, goal_id=goal_id, tool=name, arguments=arguments, result=result[:300])
    return task["id"], task["status"], result


def start(task, start_msg_id, opener):
    """Chat 모카 asked in the chat: messages after `start_msg_id` belong to this task."""
    task["status"] = "running"
    task["run"].update(started_at=now(), start_msg_id=start_msg_id, checked_msg_id=start_msg_id, opener=opener,
                       sent=1, last_message_at=now())
    save(task)
    _log("started", task)


def start_failed(task):
    """The opening message could not be sent. Give up after a few tries instead of retrying forever."""
    task["run"]["start_attempts"] = task["run"].get("start_attempts", 0) + 1
    if task["run"]["start_attempts"] >= MAX_START_ATTEMPTS:
        finish(task, {"outcome": "no_shareable_answer", "summary": "모임 채팅에 질문을 보내지 못했다.",
                      "knowledge": []}, how="send_failed")
    else:
        save(task)


def sent_message(task):
    """Count a message 모카 sent the 모임 for this task, and remember when."""
    task["run"]["sent"] = task["run"].get("sent", 0) + 1
    task["run"]["last_message_at"] = now()
    save(task)


def messages_left(task):
    """Proactive messages 모카 may still send the 모임 for this task, keeping one back for the thanks."""
    return max(0, MAX_TASK_MESSAGES - 1 - task["run"].get("sent", 1))


def may_follow_up(task):
    """Is there budget and enough quiet time for another message? One message stays reserved for the thanks."""
    if not messages_left(task):
        return False
    last = task["run"].get("last_message_at") or task["run"].get("started_at")
    return not last or (dt.datetime.now() - dt.datetime.strptime(last, FMT)).total_seconds() >= FOLLOW_UP_GAP


def checked(task, msg_id):
    task["run"]["checked_msg_id"] = msg_id
    save(task)


def finish(task, result, how):
    """`how`: "early" (chat 모카 judged it answered), "deadline" or "send_failed"."""
    task["status"] = "failed" if result["outcome"] == "no_shareable_answer" else "succeeded"
    task["result"] = result
    task["run"].update(finished_at=now(), finished_by=how)
    save(task)
    _log("finished", task, outcome=result["outcome"], finished_by=how)


def finished_group_chat_tasks():
    return group_chat_tasks(DONE)


def consume(task):
    """운영 모카 has read the result: the task is over, only the log line stays."""
    result = task["result"] or {}
    _log("consumed", task, outcome=result.get("outcome"), summary=result.get("summary"),
         summary_subjects=result.get("summary_subjects", []), knowledge=[k["id"] for k in result.get("knowledge", [])])
    _path(task["id"]).unlink(missing_ok=True)


def needs_chat():
    """Should the loop open the 모임 chat for a task? A queued one waits for its opening question,
    a running one past its deadline waits for its report."""
    return any(t["status"] == "queued" or deadline_passed(t) for t in group_chat_tasks())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["list", "new", "show"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--hours", type=int, default=DEFAULT_HOURS)
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if args.command == "new":
        task, problem = create_group_chat_task(args.arg, args.hours, created_by="모임장(수동)")
        print(problem or f"만듦: {task['id']} (마감 {task['deadline']})")
    elif args.command == "show":
        print(json.dumps(load(args.arg), ensure_ascii=False, indent=1))
    else:
        for t in all_tasks():
            print(f"{t['id']}  {t['status']:9}  마감 {t['deadline']}  {t['spec'].get('instruction')}")
        if not all_tasks():
            print("진행 중이거나 결과를 기다리는 작업이 없습니다.")


if __name__ == "__main__":
    main()
