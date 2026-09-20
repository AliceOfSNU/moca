"""운영 모카's goal loop: take steps towards the goals in data/goals/ until they are achieved or closed.

Design: documents/orient.md (goal / step / task / knowledge); prompt: documents/admin_goal_prompt.md.
Reading happens inside the step call (knowledge_search, board tools); acting or delegating is a task the
harness creates and runs. Each call returns one next_step:

- start_task  -> the harness checks it, creates the task (tool tasks run at once), and asks for the next step
                 in the same wake-up, telling 운영 모카 the new task id and any result;
- wait        -> the harness records the resume condition and ends the wake-up; when every task it names has
                 finished, the goal is woken again with the results (the tasks are then consumed);
- propose_goal_outcome -> accepted if no subgoal and no task of the goal is still open; the goal's status and
                 outcome are set, and the loop moves on (to the parent, which is its own separate check);
- modify_goals -> add subgoals / remove never-started ones in the current top-level tree (harness/goals.py:
                 modify, all or nothing). If the goal being worked on got new subgoals, the wake-up ends and the
                 first new subgoal is next; otherwise the next step is asked for.

Which goal a wake-up is about is the harness's choice (harness/goals.py: focus).

Usage (from the moca/ directory; stop the main loop first, both use the same emulator):
    python -m admin.goal_loop --dry-run    # one step for each goal that is due, nothing is executed or saved
    python -m admin.goal_loop              # a real round, as the main loop runs it
"""
import argparse
import datetime as dt
import json
import sys
import time

from admin.agent import EventTools
from admin.events import MAX_CREATES_PER_DAY, sync_events
from admin.votes import MAX_CREATES_PER_DAY as MAX_VOTES_PER_DAY
from admin.votes import MAX_OPEN as MAX_OPEN_VOTES
from admin.votes import VOTE_TOOLS, VoteTools, open_block, sync_votes
from harness.devmail import MAX_PER_DAY as MAX_DEV_REQUESTS
from harness.devmail import GoalTools as DevTools
from chatbot.agent import base_prompt
from chatbot.group_task import render_summary
from chatbot.post_tools import create_with_post_tools
from harness import goals as G
from harness import knowledge, tasks

MODEL = "gpt-6-astra"
MAX_STEPS = 4            # steps per wake-up
MAX_REJECTIONS = 2       # re-asks after a step the harness refused
MAX_FOCUS_SWITCHES = 8   # goals handled in one round (a subgoal finishing hands over to its parent)
MAX_MODIFY = 2           # modify_goals steps per wake-up (they don't count towards MAX_STEPS)
WRITE_TOOLS = ("create_event", "edit_event", "cancel_event", "set_attendance",
               "create_vote", "close_vote", "delete_vote", "ask_developer")
READ_TOOLS = ("list_events", "read_event", "list_votes", "read_vote")

EXECUTOR_CATALOG = f"""- {{type: agent, name: chat_moca}}  모임 채팅에서 멤버들에게 묻고 답을 모아 결과(요약 + 출처 있는 지식)로 돌려준다.
    spec: instruction("무엇을 알아낼지" 한 문장), deadline_hours(1~72, 기본 24). arguments_json은 null.
- {{type: tool, name: list_events}}      정모 목록.               arguments_json: {{}}
- {{type: tool, name: read_event}}       정모 하나의 상세.         arguments_json: {{"name": …}}
- {{type: tool, name: create_event}}     정모 만들기.             arguments_json: {{"name", "when": "YYYY-MM-DD HH:MM", "location", "capacity"?, "expense"?}}
- {{type: tool, name: edit_event}}       모카가 만든 정모 수정.     arguments_json: {{"name", "new_name"?, "location"?, "capacity"?, "expense"?}}
- {{type: tool, name: cancel_event}}     모카가 만든 정모 취소.     arguments_json: {{"name", "reason"}}
- {{type: tool, name: set_attendance}}   모카 자신의 참석/취소.    arguments_json: {{"name", "attending": true|false}}
- {{type: tool, name: list_votes}}       게시판 투표 목록.         arguments_json: {{}}
- {{type: tool, name: read_vote}}        투표 결과(항목별 득표·누가 골랐는지·미참여자). arguments_json: {{"title": …}}
- {{type: tool, name: create_vote}}      투표 올리기.             arguments_json: {{"title", "options": [...], "ends_at"?: "YYYY-MM-DD HH:MM", "multi"?, "anonymous"?}}
- {{type: tool, name: close_vote}}       모카가 올린 투표 종료.    arguments_json: {{"title": …}}
- {{type: tool, name: delete_vote}}      모카가 올린 투표 삭제.    arguments_json: {{"title", "reason"}}
- {{type: tool, name: ask_developer}}    개발자 로하에게 하네스 변경을 1:1로 요청. arguments_json: {{"text", "kind": "feature"|"limit"|"bug"|"question", "why"?}}"""

GOAL_RULES = f"""

## 지금 너의 역할: 운영 모카 (목표 루프)
너는 모임의 운영을 맡은 모카야. 멤버와 대화하는 모카와 같은 모카지만, 지금은 목표(goal)를 이루기 위해
다음에 할 일 하나를 정하는 자리에 있어. 멤버에게 직접 말을 걸 수는 없고, 1:1 대화 내용도 볼 수 없어.

## 어떻게 움직이는가
- 너는 한 번에 next_step 하나만 고른다. 하네스가 그걸 받아 실행하고, 필요하면 너를 다시 부른다.
- next_step의 종류는 넷뿐이다.
  - start_task: 작업을 하나 시작한다. 실행은 하네스가 하고, 새 작업의 id를 알려 준다.
    그 뒤 너는 바로 다음 step을 다시 고르게 된다.
  - wait: 기다린다. resume_when에 무엇을 기다리는지 적으면, 그 조건이 되었을 때 하네스가 결과와 함께 너를 깨운다.
  - propose_goal_outcome: 목표를 이뤘거나(achieved) 더 진행할 수 없어 닫자고(closed) 제안한다.
  - modify_goals: 목표 트리를 고친다. 하위 목표를 더하거나, 아직 시작하지 않은 하위 목표를 뺀다.
- 목표 상태(status)와 결과(outcome), 트리의 실제 변경은 하네스가 한다. 너는 제안만 한다.

## 읽기 도구 (이 호출 안에서 바로 쓴다)
- knowledge_search: 모임에 대해 쌓인 지식을 찾는다. 입력에는 지난 판단 이후 새로 생긴 지식만 보이니,
  그 전의 지식이 필요하면 직접 찾아. 결과의 이름도 운영 활용을 허락한 멤버만 보인다.
- list_posts, grep_search, read_file: 게시판 글 목록·검색·읽기.
- 읽기 도구는 아무것도 바꾸지 않는다. 판단에 필요한 만큼 쓰고, 마지막에 next_step 하나를 골라.

## 목표
- 지금 다룰 목표는 하네스가 정해서 [지금 다루는 목표]로 표시해 준다. 그 목표에 대해서만 step을 골라.
- goal_id에는 최상위 목표의 id를, 지금 다루는 목표가 하위 목표라면 subgoal_id에 그 id를 적어.
  최상위 목표 자체를 다룰 때는 subgoal_id를 null로.
- 목표의 completion_criteria가 기준이다. 기준에 없는 일을 벌이지 마.
- 상위 목표는 하위 목표가 끝났다고 저절로 이뤄지지 않는다. 상위 목표 자신의 기준을 따로 확인해.

## modify_goals
- 목표가 한 번에 하기엔 크면, 필요한 단계를 하위 목표로 나눠. 각 하위 목표에는 objective와 확인할 수 있는
  completion_criteria(1~{G.MAX_CRITERIA}개)를 적어. 하위 목표는 적은 순서대로 하나씩 진행된다.
- 한 step에 여러 변경을 goal_changes로 한꺼번에 낼 수 있고, 하나라도 규칙에 어긋나면 전부 거절된다.
  - add: {{"op": "add", "parent_id": 붙일 목표 id, "objective": …, "completion_criteria": […]}}. 새 id는 하네스가 정해 알려 준다.
  - remove: {{"op": "remove", "goal_id": …}}. 아직 아무것도 시작하지 않은 하위 목표만 뺄 수 있다.
    이미 진행된 목표는 지우지 말고 propose_goal_outcome(closed)로 닫아. 최상위 목표는 지울 수 없다.
  - 쓰지 않는 칸(add의 goal_id, remove의 parent_id·objective·completion_criteria)은 null로.
- 지금 목표 트리 안에서만 바꿀 수 있다. 깊이 {G.MAX_DEPTH}단계, 한 목표 아래 하위 목표 {G.MAX_CHILDREN}개,
  한 트리에 진행 중인 목표 {G.MAX_OPEN_PER_TREE}개까지.
- 지금 다루는 목표 아래에 하위 목표를 더하면, 하네스는 첫 번째 새 하위 목표부터 다루게 한다.
  지금 목표 자신은 그 하위 목표들이 끝난 뒤에 다시 판단한다.
- 쪼갤 필요가 없으면 쪼개지 마. 작업 하나로 끝날 일은 바로 start_task로 해.

## start_task
- 이미 같은 일을 하는 작업이 진행 중이면 새로 시작하지 말고 그 작업을 기다려.
- 이 목록에 있는 실행자만 쓸 수 있다. 없는 실행자나 인자를 지어내지 마.
{EXECUTOR_CATALOG}
- 모임 채팅에 묻는 작업(chat_moca)은 한 번에 하나, 하루 {tasks.MAX_GROUP_CHAT_PER_DAY}개까지다. 멤버들이 설문 받는 느낌이
  들지 않게 꼭 필요할 때만 쓰고, instruction에는 무엇을 알아낼지 한 문장으로 짧고 구체적으로 써.
  출처 정리, 동의 범위, 익명 처리, 결과 형식은 하네스와 채팅 모카가 알아서 하니 instruction에 쓰지 마.
- 정모 작업은 하네스 규칙을 따른다: 모카가 만든 정모만 수정·취소, 다른 멤버가 참석한 정모는 취소 불가,
  날짜·시간은 수정 불가, 하루 {MAX_CREATES_PER_DAY}개까지 생성. 거절되면 결과에 이유가 온다.
- 투표(create_vote)는 정해진 선택지 중 멤버들의 선호를 모을 때 쓴다. 답이 열려 있는 질문은 투표가 아니라
  chat_moca로 물어라. 투표는 게시판에 남아 멤버가 아무 때나 답할 수 있으니, 여러 날에 걸친 일정·장소
  정하기에 맞다. read_vote로 누가 아직 답하지 않았는지 볼 수 있으니, 채팅으로 다시 묻기 전에 먼저 확인해.
  하네스 규칙: 모카가 올린 투표만 종료·삭제, 누군가 답한 투표는 삭제 불가(종료만), 진행 중인 모카 투표
  {MAX_OPEN_VOTES}개·하루 {MAX_VOTES_PER_DAY}개까지.
- 네가 할 수 없는 일이 목표에 필요하면 포기하기 전에 ask_developer로 하네스에 무엇이 필요한지 적어 보내라.
  하루 {MAX_DEV_REQUESTS}건까지고, 답은 로하가 1:1로 보내온다. 멤버에게는 아직 없는 기능을 약속하지 마.
- 도구 작업(type: tool)은 바로 끝나고 결과가 다음 판단 때 보인다. 에이전트 작업(chat_moca)은 몇 시간이
  걸릴 수 있으니, 시작한 뒤에는 보통 그 작업을 wait한다.

## wait
- resume_when.type은 지금 task_terminal 하나뿐이다. task_ids에는 하네스가 알려 준 실제 작업 id만 적어.
  아직 없는 작업을 기다리게 하지 마.
- 기다릴 작업이 없는데 wait를 고르지 마. 할 일이 없으면 그 이유로 목표를 닫을지(closed) 생각해.

## propose_goal_outcome
- completion_criteria 하나하나에 대해 근거(작업 결과나 지식)가 있을 때만 achieved를 제안해.
- outcome.summary에는 무엇을 확인했는지, outcome.limitations에는 아직 모르거나 확정되지 않은 것을 빠짐없이 적어.
- 더 알아낼 방법이 없거나 목표 자체가 의미를 잃었으면 closed를 제안하고 이유를 적어.
- 진행 중인 작업이 있거나 하위 목표가 남아 있으면 하네스가 제안을 받아들이지 않는다.

## 근거와 개인정보
- 판단은 아래에 주어진 목표, 지난 step과 작업 결과, 읽기 도구로 찾은 지식과 게시판 글, 정모 목록에만 기대.
  모르는 것은 모른다고 적어. 결과를 기억에 의존해 말하지 말고, 필요하면 찾아서 확인해.
- 멤버 이름은 운영 활용을 허락한 멤버만 보인다. '한 멤버'로 보이는 사람이 누군지 추측하거나 알아내려 하지 마.
- reason은 이 step을 고른 근거를 한두 문장으로. 나중에 사람이 읽고 이해할 수 있게.

## 출력
정해진 JSON 형식 하나만 출력해.
kind에 해당하지 않는 필드(spec, resume_when, proposed_status, outcome, goal_changes)는 null로 둬."""

KNOWLEDGE_TOOL = {
    "type": "function", "name": "knowledge_search",
    "description": "모임에 대해 쌓인 지식(출처가 있는 관찰·보고·추론)을 찾는다. 새로운 순. 조건은 모두 선택.",
    "parameters": {"type": "object", "properties": {
        "query": {"type": "string", "description": "문장에 모두 들어 있어야 하는 낱말들 (띄어쓰기로 구분)"},
        "basis": {"type": "string", "enum": ["reported", "inferred"]},
        "since": {"type": "string", "description": "이 날짜(YYYY-MM-DD) 이후에 생긴 것만"},
        "subject": {"type": "string", "description": "이 멤버에 대한 것만 (운영 활용을 허락한 멤버만 찾을 수 있다)"},
        "limit": {"type": "integer", "description": "최대 개수, 기본 20"}},
        "required": []}}

_NULLABLE_STR = {"type": ["string", "null"]}
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["next_step"],
    "properties": {
        "next_step": {
            "type": "object", "additionalProperties": False,
            "required": ["kind", "goal_id", "subgoal_id", "reason", "spec", "resume_when", "proposed_status", "outcome",
                         "goal_changes"],
            "properties": {
                "kind": {"type": "string", "enum": ["start_task", "wait", "propose_goal_outcome", "modify_goals"]},
                "goal_id": {"type": "string"},
                "subgoal_id": _NULLABLE_STR,
                "reason": {"type": "string"},
                "spec": {"type": ["object", "null"], "additionalProperties": False,
                         "required": ["executor", "instruction", "deadline_hours", "arguments_json"],
                         "properties": {
                             "executor": {"type": "object", "additionalProperties": False, "required": ["type", "name"],
                                          "properties": {"type": {"type": "string", "enum": ["agent", "tool"]},
                                                         "name": {"type": "string"}}},
                             "instruction": _NULLABLE_STR,
                             "deadline_hours": {"type": ["integer", "null"]},
                             "arguments_json": _NULLABLE_STR}},
                "resume_when": {"type": ["object", "null"], "additionalProperties": False, "required": ["type", "task_ids"],
                                "properties": {"type": {"type": "string", "enum": ["task_terminal"]},
                                               "task_ids": {"type": "array", "items": {"type": "string"}}}},
                "proposed_status": {"type": ["string", "null"], "enum": ["achieved", "closed", None]},
                "outcome": {"type": ["object", "null"], "additionalProperties": False, "required": ["summary", "limitations"],
                            "properties": {"summary": {"type": "string"},
                                           "limitations": {"type": "array", "items": {"type": "string"}}}},
                "goal_changes": {"type": ["array", "null"], "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["op", "parent_id", "goal_id", "objective", "completion_criteria"],
                    "properties": {"op": {"type": "string", "enum": ["add", "remove"]},
                                   "parent_id": _NULLABLE_STR, "goal_id": _NULLABLE_STR, "objective": _NULLABLE_STR,
                                   "completion_criteria": {"type": ["array", "null"], "items": {"type": "string"}}}}}}}}}


def instructions():
    return base_prompt() + GOAL_RULES


def _knowledge_search(query=None, basis=None, since=None, subject=None, limit=20, **ignored):
    found = knowledge.search(query=query, basis=basis, since_date=since, subject=subject, limit=min(int(limit or 20), 50))
    return json.dumps(found, ensure_ascii=False) if found else "찾은 지식이 없습니다."


def _next_daily(hour):
    now = dt.datetime.now()
    tick = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    return (tick if tick > now else tick + dt.timedelta(days=1)).strftime(tasks.FMT)


class GoalLoop:
    def __init__(self, client, events_ui, log, dry_run=False, daily_hour=10, votes_ui=None):
        self.client = client
        self.events_ui = events_ui
        self.votes_ui = votes_ui
        self.log = log
        self.dry_run = dry_run
        self.daily_hour = daily_hour

    # --- rounds -------------------------------------------------------------------------

    def run(self, daily=False):
        """Wake every goal that is due, one after another. `daily`: the daily tick, which also lifts cooldowns."""
        if daily and not self.dry_run:
            for g in G.load():
                if g.get("cooldown_until"):
                    G.update(g["id"], cooldown_until=None)
        handled = []
        for _ in range(MAX_FOCUS_SWITCHES):
            due = [g for g in G.focus() if g["id"] not in handled]
            if not due:
                break
            goal = due[0]
            handled.append(goal["id"])
            self.wake(goal, daily)
            if self.dry_run:
                continue
        return handled

    def wake(self, goal, daily=False):
        """One wake-up of one goal: deliver finished task results, then take steps until it waits or finishes."""
        own_steps = G.steps_of(goal["id"])
        first = not own_steps
        kids = G.children(G.load(), goal)
        # its subgoals finished since this goal last acted (e.g. after it split itself with modify_goals)
        after_children = bool(kids) and (first or max(k.get("finished_at") or "" for k in kids) >= own_steps[-1]["at"])
        reason = self._collect_results(goal) or (
            "하위 목표가 모두 끝남 — 이제 이 목표 자신의 기준을 확인할 차례" if after_children else
            "첫 실행" if first else "매일 점검" if daily else "다시 판단할 차례")
        self.log(f"운영 모카 ▶ 목표 {goal['id']} ({goal['objective']}) — {reason}")
        this_wake, rejections, modifies = [], 0, 0
        from harness.presence import activity
        for step_no in range(MAX_STEPS + MAX_REJECTIONS + MAX_MODIFY):
            since = goal.get("last_step_at")
            called_at = tasks.now()
            if not self.dry_run:
                activity("goal", goal_id=goal["id"], phase="deciding", step=len(this_wake) + 1, reason=reason)
            step = self._decide(goal, reason, this_wake, since)
            if not self.dry_run:
                goal = G.update(goal["id"], last_step_at=called_at)
            verdict, note, ends = self._handle(goal, step)
            self.log(f"  step {step['kind']}: {step['reason']} → {verdict}{': ' + note if note else ''}")
            this_wake.append({"step": step, "verdict": verdict, "note": note})
            if not self.dry_run:
                activity("goal", goal_id=goal["id"], phase="handled", step=len(this_wake),
                         last={"kind": step["kind"], "verdict": verdict, "note": note})
            if self.dry_run or ends:
                return
            if verdict == "거절":
                rejections += 1
                if rejections > MAX_REJECTIONS:
                    break
            elif step["kind"] == "modify_goals":
                modifies += 1
                if modifies >= MAX_MODIFY:
                    break
            elif len([s for s in this_wake if s["verdict"] != "거절" and s["step"]["kind"] != "modify_goals"]) >= MAX_STEPS:
                break
        until = _next_daily(self.daily_hour)
        G.update(goal["id"], cooldown_until=until)
        G.log_step(goal["id"], {"kind": "cooldown", "until": until, "reason": "한 번 깨어났을 때 할 수 있는 step을 다 썼지만 기다리거나 끝내지 않음"})
        self.log(f"  기다림도 마무리도 없이 step을 다 씀 → {until}까지 쉬게 함")

    def _collect_results(self, goal):
        """If this goal's wait is over, hand over the results (and consume the tasks). Returns the wake reason."""
        wait = goal.get("wait")
        if not wait:
            return None
        lines = []
        for task_id in wait["task_ids"]:
            task = tasks.load(task_id)
            if task is None:
                lines.append(f"{task_id}: 결과를 찾을 수 없음")
                continue
            r = task["result"] or {}
            lines.append(f"{task_id} ({task['spec'].get('instruction')}): {r.get('outcome')} — {render_summary(r) if r else ''}")
            if not self.dry_run:
                G.log_step(goal["id"], {"kind": "task_result", "task_id": task_id, "status": task["status"],
                                        "outcome": r.get("outcome"), "summary": r.get("summary"),
                                        "summary_subjects": r.get("summary_subjects", []),
                                        "knowledge": [k["id"] for k in r.get("knowledge", [])]})
                tasks.consume(task)
        if not self.dry_run:
            G.update(goal["id"], wait=None)
        return "기다리던 작업이 끝남: " + "; ".join(lines)

    # --- the step call ------------------------------------------------------------------

    def _decide(self, goal, reason, this_wake, since):
        resp = create_with_post_tools(
            self.client, log=self.log, extra_tools=[KNOWLEDGE_TOOL], handlers={"knowledge_search": _knowledge_search},
            model=MODEL, instructions=instructions(), input=self._input(goal, reason, this_wake, since),
            text={"format": {"type": "json_schema", "name": "next_step", "strict": True, "schema": SCHEMA}})
        return json.loads(resp.output_text)["next_step"]

    def _input(self, goal, reason, this_wake, since):
        goals = G.load()
        new = knowledge.since(since) if since else []
        lines = [f"오늘은 {dt.datetime.now():%Y-%m-%d (%a) %H:%M}이야.", "", "## 목표", self._tree(goals, goal["id"]), "",
                 "## 이 목표에서 지난 step들 (오래된 순)", self._history(goal["id"]), "",
                 "## 깨어난 이유", reason, ""]
        if this_wake:
            lines += ["## 이번에 깨어나서 이미 한 일"]
            for s in this_wake:
                st = s["step"]
                lines.append(f"- {st['kind']} ({st['reason']}) → {s['verdict']}{': ' + s['note'] if s['note'] else ''}")
            lines.append("")
        open_ = tasks.group_chat_tasks()
        lines += ["## 진행 중인 작업"] + ([f"- {t['id']} [{t['status']}] {t['spec']['instruction']} (목표 {t.get('goal_id')}, 마감 {t['deadline']})"
                                     for t in open_] or ["(없음)"])
        lines += ["", "## 지난 판단 이후 새로 쌓인 지식"] + ([f"{knowledge.rendered_line(k)} [{k['id']}, 작업 {k['origin'].get('task')}]"
                                                    for k in new] or ["(없음)"])
        lines += [f"(저장된 지식은 모두 {len(knowledge.load_all())}건. 그 밖의 지식은 knowledge_search로 찾아.)", "",
                  "## 지금 잡혀 있는 정모", EventTools(self.events_ui, self.log).list_events(),
                  "", "## 진행 중인 투표", open_block(), "", "다음 step 하나를 골라."]
        return "\n".join(lines)

    @staticmethod
    def _tree(goals, focus_id):
        out = []

        def walk(g, depth):
            mark = "  ← [지금 다루는 목표]" if g["id"] == focus_id else ""
            out.append(f"{'  ' * depth}- {g['id']} [{g['status']}] {g['objective']}{mark}")
            out.extend(f"{'  ' * depth}    기준: {c}" for c in g["completion_criteria"])
            if g.get("outcome"):
                out.append(f"{'  ' * depth}    결과: {g['outcome']['summary']}")
                out.extend(f"{'  ' * depth}    한계: {x}" for x in g["outcome"].get("limitations", []))
            for c in G.children(goals, g):
                walk(c, depth + 1)

        focus_goal = G.get(goals, focus_id)
        walk(G.root_of(goals, focus_goal), 0)
        return "\n".join(out)

    @staticmethod
    def _history(goal_id):
        lines = []
        for s in G.steps_of(goal_id):
            if s["kind"] == "task_result":
                summary = render_summary({"summary": s.get("summary") or "", "summary_subjects": s.get("summary_subjects", [])})
                lines.append(f"- [{s['at']}] 작업 결과 {s['task_id']}: {s['outcome']} — {summary} (지식 {s['knowledge']})")
            elif s["kind"] == "cooldown":
                lines.append(f"- [{s['at']}] 쉬어 감: {s['reason']}")
            else:
                lines.append(f"- [{s['at']}] {s['kind']}: {s['reason']} → {s['verdict']}{': ' + s['note'] if s.get('note') else ''}")
        return "\n".join(lines) or "(아직 없음)"

    # --- the harness side of each step ----------------------------------------------------

    def _handle(self, goal, step):
        """Returns (verdict, note, ends the wake-up?). Every step is logged, refused ones too."""
        verdict, note, ends, extra = self._apply(goal, step)
        if not self.dry_run:
            G.log_step(goal["id"], {"kind": step["kind"], "reason": step["reason"], "verdict": verdict, "note": note,
                                    "spec": step.get("spec"), "resume_when": step.get("resume_when"),
                                    "proposed_status": step.get("proposed_status"), "outcome": step.get("outcome"), **extra})
        return verdict, note, ends

    def _apply(self, goal, step):
        goals = G.load()
        focus_id = goal["id"]
        root_id = G.root_of(goals, goal)["id"]
        expected_sub = focus_id if goal["parent_id"] else None
        if step["goal_id"] != root_id or (step.get("subgoal_id") or None) != expected_sub:
            return "거절", f"지금 다루는 목표는 goal_id={root_id}, subgoal_id={expected_sub}입니다", False, {}
        kind = step["kind"]
        if kind == "start_task":
            return self._start_task(goal, step.get("spec"))
        if kind == "modify_goals":
            done, problem = G.modify(focus_id, step.get("goal_changes") or [], dry_run=self.dry_run)
            if problem:
                return "거절", problem, False, {}
            summary = "; ".join(f"{d['goal_id']} 추가 (상위 {d['parent_id']}): {d['objective']}" if d["op"] == "add"
                                else f"{d['goal_id']} 삭제 ({', '.join(d['removed'])})" for d in done)
            got_children = any(d["op"] == "add" and d["parent_id"] == focus_id for d in done)
            # new subgoals under the goal being worked on: it can't act until they are done, so hand over
            return "변경", summary + (" → 첫 새 하위 목표부터 진행" if got_children else ""), got_children, {"changes": done}
        if kind == "wait":
            wait = step.get("resume_when") or {}
            open_ids = {t["id"] for t in tasks.group_chat_tasks()}
            ids = wait.get("task_ids") or []
            if not ids or not set(ids) <= open_ids:
                return "거절", f"기다릴 수 있는 작업은 진행 중인 작업뿐입니다: {sorted(open_ids) or '없음'}", False, {}
            if not self.dry_run:
                G.update(focus_id, wait={"type": "task_terminal", "task_ids": ids, "since": tasks.now()})
            return "대기", f"{ids}가 끝나면 다시 깨움", True, {}
        # propose_goal_outcome
        status, outcome = step.get("proposed_status"), step.get("outcome")
        if status not in G.FINISHED or not outcome or not outcome.get("summary"):
            return "거절", "proposed_status(achieved|closed)와 outcome.summary가 필요합니다", False, {}
        open_children = [c["id"] for c in G.children(goals, goal) if c["status"] not in G.FINISHED]
        own_tasks = [t["id"] for t in tasks.all_tasks() if t.get("goal_id") == focus_id and t["status"] in tasks.OPEN]
        if open_children or own_tasks:
            return "거절", f"아직 끝나지 않은 하위 목표 {open_children}나 작업 {own_tasks}이 있습니다", False, {}
        if not self.dry_run:
            G.update(focus_id, status=status, wait=None, finished_at=tasks.now(),
                     outcome={"summary": outcome["summary"], "limitations": outcome.get("limitations", [])})
        return "수락", f"목표 {focus_id}를 {status}로 마침", True, {}

    def _start_task(self, goal, spec):
        if not spec:
            return "거절", "start_task에는 spec이 필요합니다", False, {}
        executor = spec["executor"]
        if executor == tasks.CHAT_MOCA:
            if self.dry_run:
                return "실행 안 함(dry-run)", f"모임 채팅 작업: {spec.get('instruction')}", True, {}
            task, problem = tasks.create_group_chat_task(spec.get("instruction"), spec.get("deadline_hours") or tasks.DEFAULT_HOURS,
                                                         created_by=f"goal:{goal['id']}", goal_id=goal["id"])
            if problem:
                return "거절", problem, False, {}
            self.log(f"  모임 채팅 작업 생성: {task['id']} — {spec.get('instruction')} (마감 {task['deadline']})")
            return "시작", f"작업 {task['id']}를 채팅 모카에게 맡김 (마감 {task['deadline']})", False, {"task_id": task["id"]}
        name = executor.get("name")
        if executor.get("type") != "tool" or name not in WRITE_TOOLS + READ_TOOLS:
            return "거절", f"없는 실행자입니다: {executor}", False, {}
        try:
            arguments = json.loads(spec.get("arguments_json") or "{}")
            assert isinstance(arguments, dict)
        except (ValueError, AssertionError):
            return "거절", "arguments_json은 JSON 객체여야 합니다", False, {}
        agent = f"goal:{goal['id']}"
        if name == "ask_developer":
            tools = DevTools(self.log, agent=agent, dry_run=self.dry_run)
        elif name in VOTE_TOOLS:
            if self.votes_ui is None:
                return "거절", "투표 도구를 쓸 수 없습니다 (앱 연결 없음)", False, {}
            tools = VoteTools(self.votes_ui, self.log, agent=agent, dry_run=self.dry_run)
        else:
            tools = EventTools(self.events_ui, self.log, agent=agent, dry_run=self.dry_run)

        def run():
            try:
                text = getattr(tools, name)(**arguments)
            except TypeError as e:
                return False, f"인자가 맞지 않습니다: {e}"
            ok = (text.startswith(f"{name} 완료") or text.startswith("(dry-run)")) if name in WRITE_TOOLS \
                else "찾을 수 없습니다" not in text
            return ok, text

        if self.dry_run and name in WRITE_TOOLS:
            return "실행 안 함(dry-run)", f"{name} {arguments}", True, {}
        task_id, status, result = tasks.run_tool_task(goal["id"], name, arguments, run)
        return ("완료" if status == "succeeded" else "실패"), f"작업 {task_id}: {result[:400]}", False, {"task_id": task_id}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    from chatbot.config import MOIM_NAMES
    from cua.agent import openai_client
    from cua.android import AndroidDevice
    from somoim.chat import SomoimChat
    from somoim.events import SomoimEvents
    from somoim.votes import SomoimVotes
    log = lambda msg: print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)
    chat = SomoimChat(AndroidDevice(scale=0.5), MOIM_NAMES, log=log)
    events_ui = SomoimEvents(chat)
    votes_ui = SomoimVotes(chat)
    sync_events(events_ui, log)
    sync_votes(votes_ui, log)
    handled = GoalLoop(openai_client(), events_ui, log, dry_run=args.dry_run, votes_ui=votes_ui).run()
    log(f"다룬 목표: {handled or '없음'}")
    chat.park()


if __name__ == "__main__":
    main()
