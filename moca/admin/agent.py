"""운영 모카: the admin side of 모카, separate from the 모카 that talks to members.

It plans and runs 정모 through tools. It never sees raw 1:1 conversations — what reaches it from the chat
side is only what members allowed for 모임 운영 (documents/personal_intelligence.md), and it cannot send
messages itself; reaching a member is the chat 모카's job (not built yet).

Usage (from the moca/ directory; stop the main loop first, both use the same emulator):
    python -m admin.agent                      # one round by hand (run.py does this daily on its own)
    python -m admin.agent "정모 하나 만들어줘"     # a one-off instruction from the 모임장
    python -m admin.agent --dry-run "..."      # decide and report, touch nothing
"""
import argparse
import datetime as dt
import json
import sys
import time

from chatbot.agent import ROOT, base_prompt
from chatbot.group_task import render_summary
from admin.events import (FORMATS, MAX_CREATES_PER_DAY, MODES, check_change, check_create, check_plan,
                           describe_plan, drop_plan, find_event, get_plan, load_index, log_action, mark_mine, remember_created,
                           rename_plan, set_plan, sync_events)
from admin.posts import find_post
from chatbot.members import KINDS, members_with_notes
from chatbot.post_tools import POST_SEARCH_RULES, create_with_post_tools
from chatbot.store import ChatStore
from cua.agent import openai_client
from harness import knowledge, programs
from harness.tasks import (DEADLINE_HOURS, DEFAULT_HOURS, MAX_GROUP_CHAT_PER_DAY, consume, create_group_chat_task,
                           finished_group_chat_tasks, group_chat_tasks)
from cua.android import AndroidDevice

MODEL = "gpt-6-astra"
STATE = ROOT / "data" / "events" / "admin_state.json"

ADMIN_RULES = f"""

## 지금 너의 역할: 운영 모카
- 너는 모임의 정모(정기모임)를 계획하고 관리하는 역할을 맡고 있어. 멤버와 대화하는 모카와 같은 '모카'지만,
  지금 이 자리에서는 멤버에게 말을 걸 수 없고, 1:1 대화 내용도 볼 수 없어.
- 도구로 할 수 있는 일: 정모 목록·상세 확인, 정모 만들기, 정모 수정(이름·장소·비용·정원), 정모 취소(삭제),
  모카 자신의 참석/참석취소, 게시판 글 찾아보기, 모임 채팅에서 알아봐 달라고 채팅 모카에게 맡기기(ask_group_chat).
- 할 수 없는 일: 멤버에게 직접 메시지 보내기, 멤버 대신 참석 신청하기, 다른 사람이 만든 정모 수정·취소,
  날짜나 시간 수정(소모임 앱이 막아 둠 — 옮기려면 취소하고 새로 만들어야 해).

## 모임 채팅에 물어보기 (ask_group_chat)
- 정모나 활동을 정하는 데 멤버들의 생각이 필요하면, 채팅 모카에게 모임 채팅에서 알아봐 달라고 맡길 수 있어.
  채팅 모카가 자기 말투로 묻고, 답을 모아 결과를 돌려줘. 결과는 다음 점검 때 '끝난 작업'으로 받아.
- 한 번에 하나만, 하루 {MAX_GROUP_CHAT_PER_DAY}개까지. 멤버들이 설문 받는 느낌이 들지 않게 꼭 필요할 때만 써.
- instruction에는 무엇을 알아낼지 구체적으로 한 문장으로 써. (예: "다음 정모에 참여하기 편한 요일과 시간대를 확인하라.")
- 결과의 멤버 이름은 운영 활용을 허락한 멤버만 보여. 나머지는 '한 멤버'로 보이니 누군지 추측하지 마.

## 판단 기준
- 확실한 이유가 있을 때만 움직여. 멤버들이 원한다는 근거(모임 운영에 활용해도 된다고 허락된 관심사, 게시판 글,
  모임장의 요청)가 없으면 정모를 새로 만들지 마.
- 하루에 만들 수 있는 정모는 {MAX_CREATES_PER_DAY}개까지야.
- 정모를 만들 때는 이름, 일시, 장소, 정원을 분명히 정해. 온라인이면 장소에 그렇게 적어.
- 이미 있는 정모와 겹치거나 비슷하면 새로 만들지 말고 그대로 둬.
- 할 일이 없으면 아무것도 하지 않는 게 맞아. 그럴 때는 왜 그런지만 짧게 보고해."""


def load_state():
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}


def save_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def shared_member_interests():
    """What the chat side has passed on for 모임 운영 — only notes of members who answered the 활용 범위
    question with a yes (chatbot/memory_consent.py). A member who has not been asked, said no, or later
    typed /memory off contributes nothing here, and an empty list is that boundary working, not a bug."""
    lines = []
    for member, notes in members_with_notes():
        allowed = [n for n in notes if n["scope"].get("moim")]
        lines += [f"- {member}: [{KINDS[n['kind']]}] {n['summary']} ({n['stage']})" for n in allowed]
    return "\n".join(lines) or "(아직 운영에 활용해도 된다고 허락된 멤버 정보가 없음)"


PLAN_FIELDS = {
    "purpose": {"type": "string", "description": "왜 여는지 한두 문장 (120자 이내). 어떤 목표·멤버 요청에서 나왔는지"},
    "mode": {"type": "string", "enum": list(MODES), "description": "offline / online / hybrid"},
    "topic": {"type": "string", "description": "주제 한 줄 (60자 이내)"},
    "format": {"type": "string", "enum": list(FORMATS),
               "description": "talk=발표, discussion=그룹 토론, workshop=같이 실습, cowork=각자 할 일·자율 스터디, social=친목"},
    "format_note": {"type": "string", "description": "진행 방식 메모 (300자 이내). 누가 이야기하는지, 순서, 준비물 등"},
}


class EventTools:
    """정모 tools. Every write goes through admin.events rules first, then the app, then the log."""

    def __init__(self, events_ui, log, agent="admin", dry_run=False):
        self.ui = events_ui
        self.log = log
        self.agent = agent
        self.dry_run = dry_run
        self.done = []

    def _run(self, action, args, do):
        if self.dry_run:
            self.log(f"  (실행 안 함) {action} {args}")
            return f"(dry-run) {action} 요청을 확인했습니다: {args}"
        ok = do()
        log_action(self.agent, action, args, "ok" if ok else "failed")
        self.done.append((action, args, ok))
        return f"{action} 완료: {args}" if ok else f"{action} 실패: 앱에서 처리하지 못했습니다"

    # reading -------------------------------------------------------------------
    def list_events(self):
        events = load_index()["events"]
        if not events:
            return "예정된 정모가 없습니다."
        return "\n".join(
            f"{e['name']} | {e['when_text']} | {e['location']} | {e['joiners']}/{e['capacity']}명"
            f" | 비용 {e['expense']} | {'모카가 만듦' if e['mine'] else '다른 운영진이 만듦'}"
            f"{' | 모카 참석중' if e['attending'] else ''}{' | 정원 참' if e['full'] else ''}"
            + (f" | {describe_plan(plan, short=True)}" if (plan := get_plan(e["name"])) else "") for e in events)

    def read_event(self, name=None):
        event = find_event(name)
        if event is None:
            return f"'{name}' 정모를 찾을 수 없습니다. list_events로 이름을 확인하세요."
        return json.dumps({**event, "plan": get_plan(name)}, ensure_ascii=False)

    # writing -------------------------------------------------------------------
    def create_event(self, name=None, when=None, location=None, capacity=20, expense=0, post_title=None,
                     purpose=None, mode=None, topic=None, format=None, format_note=None, program_id=None, stage=None):
        try:
            at = dt.datetime.strptime(when, "%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            return "when은 'YYYY-MM-DD HH:MM' 형식이어야 합니다"
        capacity, expense = int(capacity), int(expense)
        plan = {"purpose": purpose, "mode": mode, "topic": topic, "format": format, "format_note": format_note}
        problem = check_create(name, at, location, capacity, expense) or check_plan(plan, location)
        if not problem:  # every 정모 is an activity of a running program with a plan (harness/programs.py)
            _, problem = programs.check_activity(program_id, stage, mode)
        if not problem and not (post_title or "").strip():
            problem = "정모에는 연동할 게시글이 있어야 합니다. write_post로 안내 글을 먼저 쓰고 그 제목을 post_title로 주세요"
        if not problem and find_post(post_title) is None:
            problem = (f"'{post_title}' 게시글을 찾을 수 없습니다. 모카가 쓴 글만 연동할 수 있으니 "
                       "write_post로 먼저 쓰고, 제목을 그대로 주세요")
        if problem:
            return f"만들지 않았습니다: {problem}"
        args = {"name": name, "when": when, "location": location, "capacity": capacity, "expense": expense,
                "post_title": post_title}
        result = self._run("create_event", args,
                           lambda: self.ui.create(name, at, location, expense, capacity, post_title=post_title))
        if result.startswith("create_event 완료"):
            remember_created(name, at, location, capacity, expense)  # 바로 수정·취소할 수 있게 목록에 넣는다
            goal = self.agent[5:] if self.agent.startswith("goal:") else None
            set_plan(name, {**{k: (v or "").strip() or None for k, v in plan.items()}, "post_title": post_title,
                            "program_id": program_id, "stage": stage,
                            "goal_id": goal, "created_by": self.agent, "created_at": time.strftime("%Y-%m-%d %H:%M:%S")})
            act = programs.add_activity(program_id, name, when, stage, post_title, goal, self.agent)
            which = f"{act['session']}회차" if "session" in act else f"{act['stage']}단계"
            result += f" — 프로그램 {program_id}의 {which} 활동으로 기록했습니다"
        return result

    def edit_event(self, name=None, new_name=None, location=None, capacity=None, expense=None,
                   purpose=None, mode=None, topic=None, format=None, format_note=None):
        problem = check_change(name)
        if problem:
            return f"수정하지 않았습니다: {problem}"
        plan_edit = {k: v for k, v in (("purpose", purpose), ("mode", mode), ("topic", topic), ("format", format),
                                       ("format_note", format_note)) if v is not None}
        app_edit = any(v is not None for v in (new_name, location, capacity, expense))
        if not app_edit and not plan_edit:
            return "바꿀 내용을 하나 이상 지정하세요"
        current = get_plan(name) or {}
        merged = {**current, **plan_edit}
        problem = check_plan(merged, location if location is not None else (find_event(name) or {}).get("location"),
                             partial=not current)
        if problem:
            return f"수정하지 않았습니다: {problem}"
        result = f"edit_event 완료: {plan_edit}"
        if app_edit:
            args = {k: v for k, v in (("name", name), ("new_name", new_name), ("location", location),
                                      ("capacity", capacity), ("expense", expense)) if v is not None}
            result = self._run("edit_event", args,
                               lambda: self.ui.edit(name, new_name=new_name, location=location,
                                                    expense=expense, capacity=capacity))
            if not result.startswith("edit_event 완료"):
                return result
        elif self.dry_run:
            return f"(dry-run) 정모 계획 수정: {plan_edit}"
        if plan_edit:
            set_plan(name, {**merged, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")})
            log_action(self.agent, "edit_plan", {"name": name, **plan_edit}, "ok")
        if new_name:
            rename_plan(name, new_name)
            programs.rename_activity(name, new_name)
        return result

    def cancel_event(self, name=None, reason=None):
        problem = check_change(name, deleting=True)
        if problem:
            return f"취소하지 않았습니다: {problem}"
        result = self._run("cancel_event", {"name": name, "reason": reason}, lambda: self.ui.delete(name))
        if result.startswith("cancel_event 완료"):
            drop_plan(name)
            programs.cancel_activity(name, reason)
        return result

    def set_attendance(self, name=None, attending=True):
        if find_event(name) is None:
            return f"'{name}' 정모를 찾을 수 없습니다."
        return self._run("set_attendance", {"name": name, "attending": bool(attending)},
                         lambda: self.ui.set_attendance(name, joining=bool(attending)))

    def tools(self):
        when_help = "'YYYY-MM-DD HH:MM' (24시간)"
        return [
            {"type": "function", "name": "list_events", "description": "예정된 정모 목록을 본다.",
             "parameters": {"type": "object", "properties": {}, "required": []}},
            {"type": "function", "name": "read_event", "description": "정모 하나의 자세한 정보를 본다.",
             "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
            {"type": "function", "name": "create_event",
             "description": "정모를 새로 만든다. 모든 정모는 프로그램의 활동이라, 기획(템플릿·스케치)이 채워진 "
                            "프로그램의 program_id가 필요하다. 모카가 자동으로 참석자가 된다. "
                            "앱의 정모에는 이름·일시·장소·비용·정원밖에 없어서, 왜 열고 어떻게 진행하는지는 "
                            "계획(purpose·mode·topic·format)으로 함께 남긴다. 계획은 아직 멤버에게 보이지 않는다.",
             "parameters": {"type": "object", "properties": {
                 "name": {"type": "string", "description": "정모 이름 (40자 이내)"},
                 "when": {"type": "string", "description": when_help},
                 "location": {"type": "string", "description": "장소. 온라인이면 '온라인(Google Meet)'처럼"},
                 "capacity": {"type": "integer", "description": "정원 1~60, 기본 20"},
                 "expense": {"type": "integer", "description": "비용(원), 기본 0"},
                 "post_title": {"type": "string",
                                "description": "이 정모를 설명하는, 모카가 write_post로 먼저 쓴 게시글의 제목. "
                                               "그 글이 정모 게시글로 연동된다. 정모마다 하나씩 반드시 있어야 한다"},
                 "program_id": {"type": "string", "description": "이 정모가 활동으로 속하는 프로그램 id (p_…)"},
                 "stage": {"type": "integer", "description": "단계형 프로그램일 때만: 스케치의 몇 단계인지"},
                 **PLAN_FIELDS},
                 "required": ["name", "when", "location", "post_title", "purpose", "mode", "topic", "format",
                              "program_id"]}},
            {"type": "function", "name": "edit_event",
             "description": "모카가 만든 정모의 이름·장소·정원·비용이나 계획을 바꾼다. 날짜와 시간은 앱에서 바꿀 수 없다. "
                            "계획만 바꾸면 앱은 건드리지 않는다.",
             "parameters": {"type": "object", "properties": {
                 "name": {"type": "string"}, "new_name": {"type": "string"}, "location": {"type": "string"},
                 "capacity": {"type": "integer"}, "expense": {"type": "integer"}, **PLAN_FIELDS},
                 "required": ["name"]}},
            {"type": "function", "name": "cancel_event",
             "description": "모카가 만든 정모를 취소(삭제)한다. 다른 멤버가 이미 참석 신청했으면 할 수 없다.",
             "parameters": {"type": "object", "properties": {
                 "name": {"type": "string"}, "reason": {"type": "string", "description": "취소 이유 (기록용)"}},
                 "required": ["name", "reason"]}},
            {"type": "function", "name": "set_attendance",
             "description": "모카 자신의 참석 여부를 바꾼다. 멤버를 대신 신청할 수는 없다.",
             "parameters": {"type": "object", "properties": {
                 "name": {"type": "string"}, "attending": {"type": "boolean"}}, "required": ["name", "attending"]}},
        ]

    def handlers(self):
        return {"list_events": self.list_events, "read_event": self.read_event, "create_event": self.create_event,
                "edit_event": self.edit_event, "cancel_event": self.cancel_event, "set_attendance": self.set_attendance}


class TaskTools:
    """Delegation: 운영 모카 starts a task, the harness creates and runs it (harness/tasks.py)."""

    def __init__(self, log, dry_run=False):
        self.log = log
        self.dry_run = dry_run
        self.done = []

    def ask_group_chat(self, instruction=None, deadline_hours=DEFAULT_HOURS):
        if self.dry_run:
            self.log(f"  (실행 안 함) ask_group_chat {instruction!r} {deadline_hours}h")
            return f"(dry-run) 모임 채팅 작업 요청을 확인했습니다: {instruction}"
        task, problem = create_group_chat_task(instruction, deadline_hours, created_by="admin")
        if problem:
            return f"작업을 만들지 않았습니다: {problem}"
        self.log(f"  모임 채팅 작업 생성: {task['id']} — {instruction} (마감 {task['deadline']})")
        self.done.append(("ask_group_chat", {"instruction": instruction}, True))
        return f"채팅 모카에게 맡겼습니다 ({task['id']}, 마감 {task['deadline']}). 결과는 다음 점검 때 받습니다."

    def tools(self):
        return [{"type": "function", "name": "ask_group_chat",
                 "description": "채팅 모카에게 모임 채팅에서 멤버들에게 물어 알아봐 달라고 맡긴다. 결과는 나중에 돌아온다.",
                 "parameters": {"type": "object", "properties": {
                     "instruction": {"type": "string", "description": "무엇을 알아낼지 한 문장"},
                     "deadline_hours": {"type": "integer",
                                        "description": f"결과를 기다릴 최대 시간 {DEADLINE_HOURS[0]}~{DEADLINE_HOURS[1]}, 기본 {DEFAULT_HOURS}"}},
                     "required": ["instruction"]}}]

    def handlers(self):
        return {"ask_group_chat": self.ask_group_chat}


def task_sections():
    """Open tasks, finished results (to be marked read after this round) and recent knowledge, for the prompt."""
    open_ = group_chat_tasks()
    finished = finished_group_chat_tasks()
    lines = ["## 진행 중인 모임 채팅 작업"]
    lines += [f"- {t['spec']['instruction']} ({'질문 대기' if t['status'] == 'queued' else '답 모으는 중'}, 마감 {t['deadline']})"
              for t in open_] or ["(없음)"]
    lines += ["", "## 끝난 모임 채팅 작업 (이번에 처음 보는 결과)"]
    for t in finished:
        r = t["result"]
        lines.append(f"- 요청: {t['spec']['instruction']}")
        lines.append(f"  결과: {r['outcome']} — {render_summary(r)}")
        lines += [f"  {knowledge.rendered_line(k)}" for k in r.get("knowledge", [])]
    if not finished:
        lines.append("(없음)")
    lines += ["", "## 모임에 대해 쌓인 지식 (최근)"]
    lines += [knowledge.rendered_line(k) for k in knowledge.recent(20)] or ["(아직 없음)"]
    return "\n".join(lines), finished


def admin_prompt():
    return base_prompt() + ADMIN_RULES + POST_SEARCH_RULES


def run_admin(client, events_ui, log, request=None, dry_run=False, context=20):
    """One round of 운영 모카. `request` is an instruction from the 모임장; without it, the daily round."""
    sync_events(events_ui, log)
    tools = EventTools(events_ui, log, dry_run=dry_run)
    task_tools = TaskTools(log, dry_run=dry_run)
    tasks_text, finished = task_sections()
    recent = ChatStore().history(context)
    chat_digest = "\n".join(f"- {m['sender']}: {m['text'][:80]}" for m in recent if not m["mine"]) or "(최근 대화 없음)"
    prompt = f"""오늘은 {dt.datetime.now():%Y-%m-%d (%a) %H:%M}이야.

## 지금 잡혀 있는 정모
{tools.list_events()}

## 멤버들이 운영에 활용해도 된다고 허락한 정보
{shared_member_interests()}

## 최근 모임 채팅에서 오간 이야기 (참고용, 1:1 대화는 포함되지 않음)
{chat_digest}

{tasks_text}

{"## 모임장의 요청" + chr(10) + request if request else "## 할 일" + chr(10) + "오늘 정모와 관련해 손볼 것이 있는지 살펴봐. 없으면 아무것도 하지 않아도 돼."}

필요한 도구를 쓰고, 마지막에 무엇을 했는지(또는 왜 아무것도 하지 않았는지) 두세 문장으로 보고해."""
    resp = create_with_post_tools(client, log=log, extra_tools=tools.tools() + task_tools.tools(),
                                  handlers=tools.handlers() | task_tools.handlers(),
                                  model=MODEL, instructions=admin_prompt(), input=prompt)
    report = resp.output_text.strip()
    log(f"운영 모카: {report}")
    if not dry_run:
        for task in finished:  # 운영 모카 has now read these results
            consume(task)
        state = load_state()
        state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_state(state)
    return report, tools.done + task_tools.done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("request", nargs="?", help="모임장의 지시 (없으면 하루 한 번 도는 점검)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    from chatbot.config import MOIM_NAMES
    from somoim.chat import SomoimChat
    from somoim.events import SomoimEvents
    log = lambda msg: print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)
    chat = SomoimChat(AndroidDevice(scale=0.5), MOIM_NAMES, log=log)
    run_admin(openai_client(), SomoimEvents(chat), log, request=args.request, dry_run=args.dry_run)
    chat.park()


if __name__ == "__main__":
    main()
