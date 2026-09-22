"""Goals and steps: what 운영 모카 is working towards, and what it decided along the way (documents/orient.md).

- data/goals/goals.json: every goal. A subgoal is a goal with parent_id. status/outcome are the harness's
  to change (on an accepted propose_goal_outcome); `wait` is the resume condition of a wait step.
- data/goals/steps.jsonl: one line per step 운영 모카 took (or tried), and one per task result it received.
  Tasks are deleted once read, so this is where the history of a goal lives.

Which goal a wake-up is about (the focus) is the harness's choice, not the model's:
- a goal can be the focus only if it is active, not waiting (or its wait is over), and all its subgoals are
  finished — so a parent comes after its subgoals;
- subgoals run one at a time, in creation order: the first unfinished child blocks its later siblings.

Usage (from the moca/ directory):
    python -m harness.goals list
    python -m harness.goals seed         # the example goals from documents/orient.md (g_first_study, g_find_time)
    python -m harness.goals steps g_…    # a goal's step history
"""
import argparse
import json
import sys
import time

from harness import tasks
from harness.tasks import ROOT

GOALS = ROOT / "data" / "goals"
GOALS_FILE = GOALS / "goals.json"
STEPS = GOALS / "steps.jsonl"
FINISHED = ("achieved", "closed")
# how far 운영 모카 may reshape a tree with modify_goals
MAX_DEPTH = 3             # top-level goal is depth 1
MAX_CHILDREN = 5          # subgoals under one goal
MAX_OPEN_PER_TREE = 10    # unfinished goals in one top-level tree
OBJECTIVE_LIMIT = 200
MAX_CRITERIA = 5

EXAMPLE_GOALS = [
    {"id": "g_first_study", "parent_id": None, "objective": "첫 자율스터디 정모를 개설한다",
     "completion_criteria": ["정모가 등록되었음을 확인했다"]},
    {"id": "g_find_time", "parent_id": "g_first_study", "objective": "첫 스터디의 공통 시간대 후보를 파악한다",
     "completion_criteria": ["공통 시간대 후보와 이를 뒷받침하는 멤버별 근거가 있다",
                             "확인된 범위와 아직 모르는 범위가 구분되어 있다"]},
]


def load():
    return json.loads(GOALS_FILE.read_text(encoding="utf-8")) if GOALS_FILE.exists() else []


def save(goals):
    GOALS.mkdir(parents=True, exist_ok=True)
    GOALS_FILE.write_text(json.dumps(goals, ensure_ascii=False, indent=1), encoding="utf-8")


def get(goals, goal_id):
    return next((g for g in goals if g["id"] == goal_id), None)


def update(goal_id, **fields):
    goals = load()
    goal = get(goals, goal_id)
    goal.update(fields)
    save(goals)
    return goal


def _record(goal_id, objective, completion_criteria, parent_id, created_by):
    return {"id": goal_id, "parent_id": parent_id, "objective": objective,
            "completion_criteria": list(completion_criteria), "status": "active", "outcome": None,
            "created_at": tasks.now(), "created_by": created_by, "wait": None, "last_step_at": None,
            "cooldown_until": None}


def add(goal_id, objective, completion_criteria, parent_id=None, created_by="모임장"):
    goals = load()
    if get(goals, goal_id):
        raise ValueError(f"{goal_id} 목표가 이미 있습니다")
    if parent_id and not get(goals, parent_id):
        raise ValueError(f"상위 목표 {parent_id}가 없습니다")
    goals.append(_record(goal_id, objective, completion_criteria, parent_id, created_by))
    save(goals)


def children(goals, goal):
    return [g for g in goals if g["parent_id"] == goal["id"]]


def root_of(goals, goal):
    while goal["parent_id"]:
        goal = get(goals, goal["parent_id"])
    return goal


def depth(goals, goal):
    d = 1
    while goal["parent_id"]:
        goal, d = get(goals, goal["parent_id"]), d + 1
    return d


def subtree(goals, goal):
    """The goal and everything below it."""
    out = [goal]
    for c in children(goals, goal):
        out += subtree(goals, c)
    return out


def started(goal_id):
    """Has anything happened under this goal — a step taken or a task created for it?"""
    if steps_of(goal_id):
        return True
    if any(t.get("goal_id") == goal_id for t in tasks.all_tasks()):
        return True
    return tasks.LOG.exists() and f'"goal_id": "{goal_id}"' in tasks.LOG.read_text(encoding="utf-8")


def _new_id(goals, parent_id):
    n = 1
    while get(goals, f"{parent_id}_{n}"):
        n += 1
    return f"{parent_id}_{n}"


def modify(focus_id, changes, dry_run=False):
    """Apply 운영 모카's modify_goals step: add subgoals / remove not-yet-started ones, all or nothing.

    Allowed only inside the top-level tree of the goal being worked on:
    - add: under an active goal of that tree, within MAX_DEPTH / MAX_CHILDREN / MAX_OPEN_PER_TREE; the harness
      picks the id (parent id + number). New subgoals go after existing siblings, and siblings run in that order.
    - remove: a subgoal (never the top-level goal, never the goal being worked on) where nothing has started in
      it or below it. Anything that has started is ended with propose_goal_outcome(closed) instead, so its
      history stays. Removing a goal removes its (unstarted) subgoals too.
    Returns (list of what happened, None) or (None, why the whole change was refused)."""
    goals = load()
    focus_goal = get(goals, focus_id)
    root = root_of(goals, focus_goal)
    tree = {g["id"] for g in subtree(goals, root)}
    if not changes:
        return None, "goal_changes가 비어 있습니다"
    work = [dict(g) for g in goals]
    done = []
    for c in changes:
        op = c.get("op")
        if op == "add":
            parent = get(work, c.get("parent_id"))
            objective = (c.get("objective") or "").strip()
            criteria = [x.strip() for x in (c.get("completion_criteria") or []) if x and x.strip()]
            if parent is None or parent["id"] not in tree:
                return None, f"상위 목표 {c.get('parent_id')}는 지금 목표 트리({root['id']}) 안에 있어야 합니다"
            if parent["status"] != "active":
                return None, f"{parent['id']}는 이미 끝난 목표라 하위 목표를 붙일 수 없습니다"
            if depth(work, parent) + 1 > MAX_DEPTH:
                return None, f"목표 깊이는 {MAX_DEPTH}단계까지입니다"
            if len(children(work, parent)) >= MAX_CHILDREN:
                return None, f"{parent['id']}에는 하위 목표를 {MAX_CHILDREN}개까지만 둘 수 있습니다"
            if sum(1 for g in work if g["id"] in tree and g["status"] not in FINISHED) >= MAX_OPEN_PER_TREE:
                return None, f"한 목표 트리에 진행 중인 목표는 {MAX_OPEN_PER_TREE}개까지입니다"
            if not objective or len(objective) > OBJECTIVE_LIMIT:
                return None, f"objective는 1~{OBJECTIVE_LIMIT}자여야 합니다"
            if not 1 <= len(criteria) <= MAX_CRITERIA:
                return None, f"completion_criteria는 1~{MAX_CRITERIA}개여야 합니다"
            new = _record(_new_id(work, parent["id"]), objective, criteria, parent["id"], "운영 모카")
            work.append(new)
            tree.add(new["id"])
            done.append({"op": "add", "goal_id": new["id"], "parent_id": parent["id"], "objective": objective})
        elif op == "remove":
            target = get(work, c.get("goal_id"))
            if target is None or target["id"] not in tree:
                return None, f"{c.get('goal_id')}는 지금 목표 트리 안에 없습니다"
            if target["parent_id"] is None:
                return None, "최상위 목표는 지울 수 없습니다 (모임장이 정한 목표)"
            if target["id"] == focus_id or target["id"] in {g["id"] for g in _ancestors(work, focus_goal)}:
                return None, "지금 다루는 목표와 그 상위 목표는 지울 수 없습니다"
            doomed = subtree(work, target)
            if any(g["status"] in FINISHED or started(g["id"]) for g in doomed):
                return None, f"{target['id']}(또는 그 하위 목표)는 이미 진행된 적이 있어 지울 수 없습니다. propose_goal_outcome(closed)로 닫아"
            gone = {g["id"] for g in doomed}
            work = [g for g in work if g["id"] not in gone]
            tree -= gone
            done.append({"op": "remove", "goal_id": target["id"], "removed": sorted(gone)})
        else:
            return None, f"op는 add나 remove여야 합니다: {op}"
    if not dry_run:
        save(work)
    return done, None


def _ancestors(goals, goal):
    out = []
    while goal["parent_id"]:
        goal = get(goals, goal["parent_id"])
        out.append(goal)
    return out


def wait_over(goal):
    """Is the goal's wait satisfied? task_terminal: every task it names has finished (or is already gone)."""
    wait = goal.get("wait")
    if not wait:
        return True
    for task_id in wait["task_ids"]:
        task = tasks.load(task_id)
        if task is not None and task["status"] not in tasks.DONE:
            return False
    return True


def focus(goals=None, now=None):
    """The goals that may take a step now, one per top-level goal. See the module docstring for the rule."""
    goals = load() if goals is None else goals
    now = now or tasks.now()

    def pick(goal):
        if goal["status"] != "active":
            return None
        open_children = [c for c in children(goals, goal) if c["status"] not in FINISHED]
        if open_children:
            return pick(open_children[0])  # one subgoal at a time, in creation order
        from harness import devmail
        if devmail.new_answers(goal):
            return goal  # 로하 answered a request this goal made: it wakes even while waiting or resting
        if goal.get("cooldown_until") and now < goal["cooldown_until"]:
            return None
        return goal if wait_over(goal) else None

    picked = [pick(g) for g in goals if g["parent_id"] is None]
    return [g for g in picked if g is not None]


def log_step(goal_id, record):
    GOALS.mkdir(parents=True, exist_ok=True)
    with open(STEPS, "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": tasks.now(), "goal_id": goal_id, **record}, ensure_ascii=False) + "\n")


def steps_of(goal_id):
    if not STEPS.exists():
        return []
    return [s for s in (json.loads(l) for l in STEPS.read_text(encoding="utf-8").splitlines() if l.strip())
            if s["goal_id"] == goal_id]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["list", "seed", "steps"])
    ap.add_argument("goal", nargs="?")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if args.command == "seed":
        for g in EXAMPLE_GOALS:
            try:
                add(g["id"], g["objective"], g["completion_criteria"], g["parent_id"])
                print(f"추가: {g['id']}")
            except ValueError as e:
                print(e)
    elif args.command == "steps":
        for s in steps_of(args.goal):
            print(json.dumps(s, ensure_ascii=False))
    else:
        goals = load()
        focused = {g["id"] for g in focus(goals)}
        for g in goals:
            depth = 0
            p = g
            while p["parent_id"]:
                p, depth = get(goals, p["parent_id"]), depth + 1
            wait = f" 대기: {g['wait']['task_ids']}" if g.get("wait") else ""
            print(f"{'  ' * depth}{g['id']} [{g['status']}]{' ← 다음 차례' if g['id'] in focused else ''}{wait}  {g['objective']}")
        if not goals:
            print("목표가 없습니다.")


if __name__ == "__main__":
    main()
