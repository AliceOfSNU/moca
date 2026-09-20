"""The harness around chat 모카 when it carries out 운영 모카's Group Chat Tasks (documents/orient.md).

The work is done by the same chat 모카 that answers the 모임 (ChatAgent in chatbot/agent.py, with its usual
system prompt); this module hands it the task, keeps the task's state and checks what it reports.
The loop calls GroupChatTasks.turn() after each chat read. A task goes:

1. queued  -> 모카 asks the 모임 once, in its own words, saying it's for planning. Everything recorded after
              that point is the task's window.
2. running -> after each new member message, 모카 judges whether it can wrap up early. The harness ends it at
              the deadline regardless.
3. report  -> 모카 writes its result from the window (messages with their ids). The harness checks it:
              every source must be a real message in the window, a "reported" statement must be backed by its
              subject's own message, and member names become placeholders that are filled in, or anonymised,
              by each member's 활용 범위 answer whenever the result is shown (harness/knowledge.py).
4. 모카 thanks the 모임 once if anyone answered.
"""
import re

from chatbot.agent import answerable, plain_text
from harness import knowledge
from harness.tasks import FOLLOW_UP_GAP, MAX_TASK_MESSAGES, messages_left
from harness.tasks import (checked, deadline_passed, finish, group_chat_tasks, may_follow_up,
                           running_group_chat_task, sent_message, start, start_failed)

OUTCOMES = ["answer_available", "partial_answer", "no_shareable_answer"]  # as ChatAgent.TASK_OUTCOMES
STATEMENT_LIMIT = 200
MAX_CANDIDATES = 12


def _num(msg_id):
    return int(msg_id[1:])


def templatize(text, names):
    """Replace member names in `text` with {s0}, {s1}, … Returns (template, subjects in placeholder order).
    Longer names first, so a name that contains another is not split."""
    subjects = []
    for name in sorted(set(names), key=len, reverse=True):
        if name and name in text:
            text = re.sub(re.escape(name) + r"(님)?", f"{{s{len(subjects)}}}", text)
            subjects.append(name)
    return text, subjects


def validate(raw, window):
    """Turn chat 모카's report into a TaskResult the harness can stand behind. Returns (result, dropped count)."""
    by_id = {m["id"]: m for m in window}
    senders = {m["sender"] for m in window if answerable(m)}
    kept, dropped = [], 0
    for c in raw.get("knowledge_candidates", [])[:MAX_CANDIDATES]:
        # "[m89]" and "m89" are the same message; whatever the spelling, it has to be in the window
        refs = [r for r in (re.sub(r"[\[\]\s]", "", r) for r in c.get("source_refs", [])) if r in by_id]
        statement = plain_text(c.get("statement", ""))[:STATEMENT_LIMIT]
        if not refs or not statement or c.get("basis") not in knowledge.BASIS:
            dropped += 1
            continue
        cited = {by_id[r]["sender"] for r in refs if answerable(by_id[r])}
        named = [s for s in c.get("subjects", []) if s in senders]
        if c["basis"] == "reported":
            named = [s for s in named if s in cited]  # "reported" means they said it themselves, in a cited message
            if not named:
                dropped += 1
                continue
        # any member mentioned in the statement becomes a placeholder too, subject or not
        template, subjects = templatize(statement, set(named) | {s for s in senders if s in statement})
        kept.append({"statement": template, "subjects": subjects, "basis": c["basis"], "source_refs": refs})
    outcome = raw.get("outcome") if raw.get("outcome") in OUTCOMES else "no_shareable_answer"
    if outcome == "answer_available" and not kept:
        outcome = "partial_answer"
    summary, summary_subjects = templatize(plain_text(raw.get("summary", ""))[:400], senders)
    return {"outcome": outcome, "summary": summary, "summary_subjects": summary_subjects, "knowledge": kept}, dropped


def render_summary(result):
    return knowledge.render({"statement": result["summary"], "subjects": result.get("summary_subjects", [])})


def task_context():
    """What chat 모카 should keep in mind while a task is running — appended to its chat prompts."""
    task = running_group_chat_task()
    if task is None:
        return ""
    return f"""

## 지금 모임 채팅에서 알아보는 중인 것 (모임 운영을 위해)
- 알아내야 할 것: {task['spec']['instruction']}
- 이 답을 얻어내는 건 네 몫이야. 필요한 게 있으면 네가 물어봐.
- 이미 이렇게 물어봤어: "{task['run'].get('opener', '')[:200]}"
- 마감: {task['deadline']}. 그때까지 모인 만큼만 운영에 전달돼.
- 네가 먼저 보낼 수 있는 메시지: {messages_left(task)}개 남음 (한 작업에 {MAX_TASK_MESSAGES}개까지, 최소 {FOLLOW_UP_GAP // 60}분 간격).
  한 번 보낼 때마다 작업이 앞으로 나아가야 해. 보탤 게 없으면 아끼고, 마감이 가까운데 답이 모자라면 아끼지 말고 써.
- 누가 말을 걸어서 답장할 때는 그 답장 안에 필요한 질문을 같이 담아도 돼. 답장은 이 한도에 들지 않아.
- 같은 질문을 그대로 반복하거나 재촉하지는 마. 내부 작업, 다른 에이전트, 작업 번호 이야기도 하지 마."""


class GroupChatTasks:
    """Drives one ChatAgent (chat 모카) through the current Group Chat Task."""

    def __init__(self, agent, store, chat, log, dry_run=False):
        self.agent = agent
        self.store = store
        self.chat = chat
        self.log = log
        self.dry_run = dry_run

    def turn(self):
        """Advance the Group Chat Task, if there is one. Returns True if a message was sent."""
        task = running_group_chat_task()
        if task is not None:
            return self._progress(task)
        queued = next(iter(group_chat_tasks(("queued",))), None)
        return self._open(queued) if queued else False

    def _open(self, task):
        text = self.agent.task_open(task["spec"]["instruction"], self.store.history(30))
        self.log(f"운영 작업 시작 → 모임 채팅 질문: {text}")
        if self.dry_run:
            self.chat.send(text, dry_run=True)
            return False
        start_id = self.store.last_id()
        if not self.chat.send(text):
            self.log("  전송 실패 (다음 회차에 다시 시도)")
            start_failed(task)
            return False
        self.log("  전송 완료")
        start(task, start_id, text)
        return True

    def _progress(self, task):
        window = self.store.since(task["run"]["start_msg_id"])
        new = [m for m in window if _num(m["id"]) > _num(task["run"]["checked_msg_id"]) and answerable(m)]
        if deadline_passed(task):
            return self._report(task, window, how="deadline")
        if not new:
            return False
        allowed = may_follow_up(task)
        done, reason, follow_up = self.agent.task_check(task["spec"]["instruction"], window,
                                                        task["run"]["started_at"], task["deadline"],
                                                        may_follow_up=allowed, left=messages_left(task))
        self.log(f"운영 작업 진행 확인: {'마무리' if done else '계속'} ({reason})")
        if not self.dry_run:
            checked(task, window[-1]["id"])
        if done:
            return self._report(task, window, how="early")
        if not follow_up or not allowed or self.dry_run:
            return False
        self.log(f"운영 작업 추가 메시지 ({task['run'].get('sent', 1)}/{MAX_TASK_MESSAGES}): {follow_up}")
        sent = self.chat.send(follow_up)
        self.log("  전송 완료" if sent else "  전송 실패")
        if sent:
            sent_message(task)
        return bool(sent)

    def _report(self, task, window, how):
        raw = self.agent.task_report(task["spec"]["instruction"], window)
        result, dropped = validate(raw, window)
        self.log(f"운영 작업 결과 ({'조기 마무리' if how == 'early' else '마감'}): {result['outcome']} — "
                 f"{render_summary(result)} (지식 {len(result['knowledge'])}건, 하네스가 뺀 것 {dropped}건)")
        if self.dry_run:
            return False
        origin = {"task": task["id"], "channel": "group_chat"}
        result["knowledge"] = [{"id": knowledge.add(k["statement"], k["subjects"], k["basis"], k["source_refs"],
                                                    origin)["id"], **k} for k in result["knowledge"]]
        for k in result["knowledge"]:
            self.log(f"  지식 {k['id']}: {knowledge.render(k)} ({k['basis']}, 근거 {k['source_refs']})")
        finish(task, result, how)
        if result["outcome"] == "no_shareable_answer" or not any(answerable(m) for m in window):
            # nothing was learned: thanking members for answers they never gave would be a lie
            return False
        text = self.agent.task_thanks(task["spec"]["instruction"], window)
        self.log(f"운영 작업 마무리 인사: {text}")
        sent = self.chat.send(text)
        self.log("  전송 완료" if sent else "  전송 실패")
        if sent:
            sent_message(task)
        return bool(sent)
