"""Carrying out a 1:1 안내 작업 (harness/direct.py): 모카 writes to one member first, then reads their answer.

The shape follows chatbot/group_task.py — open, watch, report — with three differences that come from it
being one person rather than the room:

- the gate is re-checked right before sending. A member can cancel their 정모 신청 between the step that asked
  for the message and the round that sends it, and the whole point of this channel is that only people who
  signed up are written to;
- the harness prepends its own line (which program, how to stop these). The model writes the message; that
  line is not the model's to write;
- "안내 그만" from the member ends the channel for them immediately, recorded by the harness, before anything
  the model says about the reply.

A reply here is what one named member said in private, so the report keeps it attributed (unlike a group chat
task, where answers are anonymised): the gate already required that member's consent to their 1:1 being used
for 모임 운영, and a 준비물 안내 is useless if 모카 cannot tell who said they would present.
"""
import json

from chatbot.dm import format_line, member_dir, plain_text
from chatbot.store import ChatStore
from harness import direct
from harness import tasks as T

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["answered", "reason", "summary"],
          "properties": {"answered": {"type": "boolean"}, "reason": {"type": "string"},
                         "summary": {"type": "string"}}}

OPEN_PROMPT = """'{member}'님에게 1:1로 먼저 보낼 메시지를 써.

이 메시지의 목적: {instruction}

- 「{program}」 참가 신청을 한 분이야. 왜 연락했는지 첫 문장에서 분명히 해.
- 이 분에게만 해당하는 이야기를 해. 모두에게 같은 안내라면 1:1로 보낼 일이 아니야.
- 확인할 것이 있으면 하나만 물어. 답하기 쉽게, 부담 없이.
- 다른 멤버가 무엇을 했는지 말하지 마.
- 2~5문장. 메시지 텍스트만 출력해."""

CHECK_PROMPT = """'{member}'님에게 1:1로 보낸 안내의 답이 왔는지 판단해.

보낸 목적: {instruction}
보낸 메시지: {opener}

## 보낸 뒤 이 분이 한 말
{replies}

answered: 목적한 것이 확인됐으면 true. 아직이면 false.
reason: 한 문장.
summary: 운영 모카에게 전할 요약. 이 분이 실제로 한 말만, 1~3문장. 답이 없으면 빈 문자열."""


class DirectTasks:
    """Drives the DM agent through the queued and running 1:1 안내 작업."""

    def __init__(self, agent, chat, log, events_ui=None, dry_run=False):
        self.agent = agent
        self.chat = chat            # SomoimChat: navigation to a 1:1
        self.log = log
        self.events_ui = events_ui  # for re-reading the 정모 attendee list before sending
        self.dry_run = dry_run

    def turn(self):
        """Advance one 1:1 안내 작업. Returns True if a message was sent."""
        for task in direct.direct_tasks(("running",)):
            if self._progress(task):
                return True
        queued = next(iter(direct.direct_tasks(("queued",))), None)
        return self._open(queued) if queued else False

    # --- 보내기 -------------------------------------------------------------------------------------

    def _open(self, task):
        target = task["spec"]["target"]
        member = target["member"]
        context, problem = direct.check(member, target["program_id"], target.get("activity_id"),
                                        events_ui=self.events_ui, ignore=task["id"])
        if problem:  # something changed since the step asked for this
            self.log(f"1:1 안내 취소 ({member}): {problem}")
            T.finish(task, {"outcome": "no_shareable_answer", "summary": f"보내지 못했습니다: {problem}",
                            "summary_subjects": [], "knowledge": []}, how="send_failed")
            return False
        store = ChatStore(member_dir(member))
        text = open_text(self.agent, member, task["spec"]["instruction"], context["program_title"],
                         store.history(20))
        body = direct.header(context["program_title"]) + "\n\n" + text
        self.log(f"1:1 안내 → {member} (「{context['program_title']}」): {text}")
        if self.dry_run:
            return False
        from somoim.direct import DirectChat
        dm = DirectChat(self.chat, member, log=self.log)
        if not dm.open():
            self.log(f"  {member}님과의 1:1을 열지 못함 (다음 회차에 다시 시도)")
            T.start_failed(task)
            return False
        start_id = store.last_id()
        if not dm.send(body):
            self.log("  전송 실패 (다음 회차에 다시 시도)")
            T.start_failed(task)
            return False
        self.log("  전송 완료")
        T.start(task, start_id, body)
        return True

    # --- 답 기다리기 --------------------------------------------------------------------------------

    def _progress(self, task):
        target = task["spec"]["target"]
        member = target["member"]
        store = ChatStore(member_dir(member))
        window = [m for m in store.since(task["run"]["start_msg_id"]) if not m.get("mine")]
        fresh = direct.replies_since_check(task)   # what arrived since the last look
        stop = next((m for m in fresh if direct.asked_to_stop(m["text"])), None)
        if stop is not None:
            direct.set_optout(member, how="1:1 답장")
            self.log(f"{member}님이 1:1 운영 안내를 그만 받기로 했습니다 → 앞으로 보내지 않습니다")
            T.finish(task, {"outcome": "partial_answer" if len(window) > 1 else "no_shareable_answer",
                            "summary": f"{member}님은 1:1 운영 안내를 그만 받기로 했습니다. 앞으로 이 분께는 "
                                       "모임 채팅이나 게시글로만 알릴 수 있습니다.",
                            "summary_subjects": [member], "knowledge": []}, how="early")
            return False
        due = T.deadline_passed(task)
        if not fresh and not due:
            return False              # nothing new to read, and there is still time
        if not window:
            self.log(f"1:1 안내 마감 ({member}): 답 없음")
            T.finish(task, {"outcome": "no_shareable_answer",
                            "summary": f"{member}님께 1:1로 안내했지만 마감까지 답이 없었습니다.",
                            "summary_subjects": [member], "knowledge": []}, how="deadline")
            return False
        answered, reason, summary = check_reply(self.agent, member, task["spec"]["instruction"],
                                               task["run"]["opener"], window)
        self.log(f"1:1 안내 확인 ({member}): {'마무리' if answered else '계속 기다림'} ({reason})")
        if not self.dry_run:
            T.checked(task, window[-1]["id"])
        if not answered and not due:
            return False
        if self.dry_run:
            return False
        T.finish(task, {"outcome": "answer_available" if answered else "partial_answer",
                        "summary": summary or f"{member}님의 답을 받았습니다.",
                        "summary_subjects": [member], "knowledge": []},
                 how="early" if answered else "deadline")
        return False


def open_text(agent, member, instruction, program, history):
    """The message itself, in the 1:1 voice the DM agent already has."""
    from chatbot.dm import dm_prompt
    prompt = ""
    if history:
        prompt += ("## 지금까지의 1:1 대화 (오래된 순)\n"
                   + "\n".join(format_line(m) for m in history) + "\n\n")
    prompt += OPEN_PROMPT.format(member=member, instruction=instruction, program=program)
    return plain_text(agent._text(dm_prompt(member), prompt, search=False))


def check_reply(agent, member, instruction, opener, replies):
    """Is what this member said an answer to what was asked? Returns (answered, reason, summary)."""
    from chatbot.dm import dm_prompt
    prompt = CHECK_PROMPT.format(member=member, instruction=instruction, opener=opener,
                                 replies="\n".join(format_line(m) for m in replies))
    resp = agent.client.responses.create(
        model=agent.model, reasoning=agent.reasoning, instructions=dm_prompt(member), input=prompt,
        text={"format": {"type": "json_schema", "name": "dm_task_check", "strict": True, "schema": SCHEMA}})
    out = json.loads(resp.output_text)
    return bool(out["answered"]), out["reason"].strip(), plain_text(out["summary"]).strip()
