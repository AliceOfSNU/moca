"""The `propose_member_data` tool: 모카 proposes what to remember, the harness decides whose it is.

The model never chooses the member in a 1:1 (it's the person it's talking to) and can only name someone
who actually spoke in the group chat messages it is answering. Sharing beyond personal use is not the
model's to grant either: it follows the member's own answer to the 활용 범위 question (chatbot/memory_consent.py).
"""
from chatbot.members import KINDS, MAX_NOTES_PER_REPLY, STAGES, add_note
from chatbot.memory_consent import shares

RECORDING_RULES = """
## 멤버에 대해 기억하기
- 대화에서 멤버가 스스로 밝힌 다음 내용이 나오면 propose_member_data로 기록해:
  궁금한 질문, 해보고 싶은 활동, 나눌 수 있는 경험, 참여 조건(가능한 시간·장소·형식).
- 멤버가 직접 말한 것만 기록해. 네 추측, 성격 평가, 멤버들 사이의 관계는 기록하지 마.
- 건강, 정치·종교 성향, 재정 상태, 직장 불만처럼 민감한 내용은 기록하지 마.
- stage는 멤버가 실제로 약속한 만큼만 적어. 대부분은 '관심 있음'이야.
  '해보고 싶다', '관심 있다', '재밌겠다'는 전부 '관심 있음'.
  '참여 의사'는 구체적인 활동에 가겠다고 말했을 때, '주최 의사'는 자기가 열겠다고 말했을 때만.
- 이미 기억하고 있는 내용은 다시 제안하지 마. 새로 알게 된 것이나 달라진 것만.
- 기록은 조용히 해. 기록했다고 매번 알릴 필요는 없어."""

TOOL = {
    "type": "function",
    "name": "propose_member_data",
    "description": "멤버가 대화에서 직접 밝힌 내용을 모카의 기억에 남긴다. 운영과 맞춤 추천에 쓰인다. "
                   "누구의 기록인지와 활용 범위는 프로그램이 정하므로 네가 정할 수 없다.",
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": list(KINDS),
                     "description": "question=궁금한 질문, activity=해보고 싶은 활동, experience=나눌 수 있는 경험, availability=참여 조건"},
            "summary": {"type": "string", "description": "한 문장으로. 예: 'AI로 음악을 직접 만들어보고 싶어함'"},
            "stage": {"type": "string", "enum": STAGES, "description": "멤버가 말한 만큼만. 기본은 '관심 있음'"},
            "format": {"type": "string", "description": "선호하는 형식이 드러났다면. 예: '설명보다 함께 실습'"},
            "quote": {"type": "string", "description": "근거가 되는 멤버의 말 한 토막 (짧게)"},
            "member": {"type": "string", "description": "모임 채팅에서만 필요하다. 방금 그 말을 한 멤버의 이름"},
        },
        "required": ["kind", "summary"],
    },
}


class MemberNotes:
    """Handler bound to one conversation. `member` for a 1:1, `allowed` for group chat senders."""

    def __init__(self, member=None, allowed=(), context="dm", log=None):
        self.member = member
        self.allowed = set(allowed)
        self.context = context
        self.log = log
        self.stored = []

    def __call__(self, kind=None, summary=None, stage=None, format=None, quote=None, member=None, **ignored):
        if len(self.stored) >= MAX_NOTES_PER_REPLY:
            return f"이번 답장에서는 이미 {MAX_NOTES_PER_REPLY}개를 기록했습니다. 다음 기회에 기록하세요."
        who = self.member
        if who is None:  # group chat: only about someone who actually spoke here
            if member not in self.allowed:
                return f"'{member}'는 방금 대화한 멤버가 아닙니다. 기록할 수 있는 멤버: {sorted(self.allowed)}"
            who = member
        try:
            note, what = add_note(who, kind=kind, summary=summary, stage=stage, format=format,
                                  quote=quote, context=self.context, share=shares(who))
        except ValueError as e:
            return f"기록하지 못했습니다: {e}"
        self.stored.append(note)
        if self.log:
            self.log(f"  기억 {'갱신' if '갱신' in what else '추가'} [{who}] {note['summary']}"
                     f" ({note['stage']}{', ' + note['format'] if note['format'] else ''})")
        return f"{what}: [{KINDS[note['kind']]}] {note['summary']}"
