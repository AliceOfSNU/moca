"""먼저 묻기 — 멤버에게 보낼 질문 하나를 만든다 (documents/asking.md).

따로 떨어진 에이전트가 아니다. 1:1 모카와 같은 규칙(dm_prompt)과 같은 대화 기록을 보고, 질문을 어떻게
골라야 하는지에 대한 지침만 더 받는다. 하네스가 하루 두 번(출근길·퇴근길) 이것을 불러 질문을 만들고,
모카의 말투로 보낸다. 답을 받아 오는 일은 그 뒤 1:1 모카에게 맡기는 작업이다 — 아직 없다.

질문의 문장은 모델이 쓰지만, **선택지의 번호는 하네스가 붙인다.** 멤버는 "2"라고만 답할 수 있어야 하고,
답을 받아 오는 쪽은 그 "2"가 무엇이었는지 정확히 알아야 한다. 번호를 모델이 문장 속에 섞어 쓰게 두면
그 대응이 흔들린다.

모델은 **묻지 않기로** 할 수도 있다(ask=false). 지금 이 사람에게 할 만한 질문이 없으면 보내지 않는 것이
지침이고(asking.md), 그 선택지가 스키마에 없으면 모델은 늘 무언가를 만들어 낸다.

이 파일은 만들기만 한다. 언제 누구에게 보낼지는 harness/asks.py, 실제로 보내는 것은 chatbot/asking_send.py.

    python -m chatbot.asking 김범진 최윤서             # 멤버마다 질문 하나씩
    python -m chatbot.asking 로하 --n 3 --slot 퇴근길  # 같은 멤버로 세 번 — 얼마나 흔들리는지 본다
"""
import argparse
import difflib
import json
import pathlib
import re
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
GUIDE = ROOT / "documents" / "asking.md"
HISTORY = 60

AXES = ("참여 조건과 활동 선호",
        "AI 사용 밀도", "새로운 AI 탐색", "업무 속 AI", "일상 속 AI", "AI 이슈 탐구",
        "관심 분야와 적용하고 싶은 곳",
        "모임에서 기대하는 경험")
SLOTS = ("출근길", "퇴근길")
LIMITS = {"message": 300, "choice": 40}
MIN_CHOICES, MAX_CHOICES = 2, 4
# 1:1 안내문이 멤버에게 보내지 말라고 한 것들. 모카가 거꾸로 묻는 일은 없어야 한다
SENSITIVE = ("주민", "연락처", "전화번호", "계좌", "카드번호", "비밀번호", "주소", "건강", "병원", "진단", "연봉", "월급")

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["ask", "hold_reason", "axis", "target", "why_this_person", "message", "choices", "multi",
                 "reads_as", "does_not_mean"],
    "properties": {
        "ask": {"type": "boolean"},
        "hold_reason": {"type": "string"},
        "axis": {"type": "string", "enum": list(AXES) + [""]},
        "target": {"type": "string"},
        "why_this_person": {"type": "string"},
        "message": {"type": "string"},
        "choices": {"type": "array", "items": {"type": "string"}},
        "multi": {"type": "boolean"},
        "reads_as": {"type": "string"},
        "does_not_mean": {"type": "string"},
    },
}

TASK = """
## 지금 할 일: 먼저 물을 질문 하나 만들기

지금은 대화에 답하는 게 아니야. '{member}'님에게 모카가 먼저 보낼 질문 하나를 만들거나, 지금은 묻지 않기로
정하는 거야. 아래 '먼저 묻기' 지침을 그대로 따라.

위 기능 문서에는 모카가 먼저 1:1을 보내는 경우가 둘뿐이라고 적혀 있지만, 이 질문은 하네스가 따로 정한
규칙(하루 횟수, 시간대, 그만 받기 안내)에 따라 보내는 것이라 그 제한과 별개야. 묻는 것 자체를 망설이지 마.
이 작업에서는 도구를 쓸 수 없어 — 위에 적힌 도구 안내는 대화할 때의 것이야.

{guide}

## 내보내는 모양

- ask: 지금 이 사람에게 물을 만한 질문이 있으면 true. 없으면 false — 지침의 네 가지 점검 중 하나라도 답이
  안 나오면 false다. 억지로 만들지 마.
- hold_reason: ask가 false일 때 왜 지금은 안 묻는지 한 문장. true면 빈 문자열.
- axis: 탐색 방향(AI 활용 경험이면 다섯 세부 축 중 하나). 안 물으면 빈 문자열.
- target: 구체적으로 무엇을 확인하려는지. "AI 수준" 같은 말 말고 "파일을 첨부해 물어본 경험"처럼.
- why_this_person: 왜 이 사람에게, 왜 지금 이걸 묻는지. 이미 아는 것과 겹치지 않는다는 것을 여기서 밝혀.
- message: 실제로 보낼 질문 문장. 모카의 말투로, 선택지는 넣지 마 — 번호와 선택지는 하네스가 붙인다.
  질문은 하나만. 1~3문장. '여러 개 골라도 돼'도 쓰지 마 — multi가 true면 하네스가 붙인다.
  날짜·요일·시간은 아래 기록에 적힌 그대로만 써. 기억으로 짐작해 요일을 붙이지 마 — 틀리면 멤버에게
  틀린 사실을 말하는 거야. 확실하지 않으면 날짜 없이 써.
- choices: 선택지 {lo}~{hi}개. 출퇴근길에 폰으로 읽는다 — 많을수록 고르기 어렵다.
  짧게(각 {clen}자 이내). 번호를 붙이지 마. 모르거나 해당 없을 수 있는
  질문이면 '모르겠어', '안 써봤어', '기타' 같은 빠져나갈 자리를 넣어.
- multi: 여러 개 골라도 되는 질문이면 true.
- reads_as: 답으로 알 수 있는 것.
- does_not_mean: 이 답으로 추론하면 안 되는 것 (지침의 '답을 어디까지 읽을 것인가').
"""


WEEKDAYS = "월화수목금토일"


def today():
    """'2026년 10월 5일 월요일' — 서버 로케일에 따라 (Mon)이 되던 것을 한국어로 고정한다."""
    t = time.localtime()
    return f"{t.tm_year}년 {t.tm_mon}월 {t.tm_mday}일 {WEEKDAYS[t.tm_wday]}요일"


def known_block(member):
    """이 멤버 한 사람에 대해 모임에서 이미 알려진 것. 지금 보이는 기록만, 그리고 멤버가 직접 말했거나
    하네스가 본 것만 — 모카의 추론(inferred)은 넣지 않는다. 짐작을 아는 것으로 착각해 묻지 않게 된다.

    다른 멤버가 함께 적힌 기록도 넣지 않는다. 그 사람에 대한 것이 이 1:1로 새어 나간다.
    1:1에서 모은 노트(member_note)는 dm_prompt가 이미 보여 주므로 여기서는 뺀다."""
    from chatbot.profiles import call_name
    from harness import knowledge as K
    name = call_name(member) or member
    lines = []
    for r in K.load_all():
        if r["subjects"] != [member] or r["basis"] not in ("reported", "observed"):
            continue
        if r["origin"].get("channel") == "member_note":
            continue
        # 이 멤버 자신의 사실을 자기 1:1에 보여 주는 것이라 동의 규칙과 상관없이 이름으로 쓴다.
        # 은/는 · 이/가는 render가 이름에 맞춰 다시 고른다
        text = K.render(r, names=[name])
        lines.append(f"- {text} ({K.BASIS_LABEL[r['basis']]}, {r['created_at'][:10]})")
    if not lines:
        return "## 모임에서 이미 알려진 것\n(아직 없음)"
    return "## 모임에서 이미 알려진 것 (가입인사, 모임 채팅, 정모 신청 등)\n" + "\n".join(lines)


def asked_block(member):
    """이 멤버에게 실제로 보낸 질문과 그 답. 같은 걸 또 묻지 않게."""
    from harness import asks
    sent = asks.recent(member)
    if not sent:
        return "## 이 멤버에게 최근 먼저 물은 질문\n(아직 없음)"
    lines = []
    for q in sent:
        opts = " / ".join(f"{i}) {c}" for i, c in enumerate(q["choices"], 1))
        got = (f"답: {q['answer']}" if q["status"] == "answered"
               else "답 없음" if q["status"] == "expired" else "답 기다리는 중")
        lines.append(f"- [{q['sent_at'][:10]} · {q['axis']}] {q['message']} ({opts}) — {got}")
    return ("## 이 멤버에게 최근 먼저 물은 질문 (같은 것을 다시 묻지 마. 답이 없었던 질문을 바로 다시 내밀지도 마)\n"
            + "\n".join(lines))


def build(member, slot):
    from chatbot.dm import dm_prompt, format_line, member_dir
    from chatbot.store import ChatStore
    instructions = dm_prompt(member) + TASK.format(
        member=member, guide=GUIDE.read_text(encoding="utf-8"), lo=MIN_CHOICES, hi=MAX_CHOICES,
        clen=LIMITS["choice"])
    history = ChatStore(member_dir(member)).history(HISTORY)
    convo = ("## 1:1 대화 기록 (오래된 순, 최근 %d개)\n" % HISTORY
             + ("\n".join(format_line(m) for m in history) if history else "(아직 없음)"))
    asked = asked_block(member)
    now = f"## 지금\n{today()} {slot} 시간대"
    return instructions, "\n\n".join([known_block(member), convo, asked, now])


def draft(agent, member, slot="출근길"):
    instructions, prompt = build(member, slot)
    resp = agent.client.responses.create(
        model=agent.model, reasoning=agent.reasoning, instructions=instructions, input=prompt,
        text={"format": {"type": "json_schema", "name": "asking", "strict": True, "schema": SCHEMA}})
    return json.loads(resp.output_text)


def problems(q, previous=()):
    """하네스가 보내기 전에 거르는 것. 모델이 지침을 읽었다고 지켰다는 보장은 없다.
    `previous`: 이 멤버에게 이미 보낸 질문 문장들 — 거의 같은 질문은 거른다."""
    if not q["ask"]:
        return [] if q["hold_reason"].strip() else ["묻지 않는다면서 이유가 없음"]
    out = []
    if q["axis"] not in AXES:
        out.append(f"탐색 방향이 없음: {q['axis']!r}")
    n = len(q["choices"])
    if not MIN_CHOICES <= n <= MAX_CHOICES:
        out.append(f"선택지 {n}개 ({MIN_CHOICES}~{MAX_CHOICES}개여야 함)")
    if len({c.strip() for c in q["choices"]}) != n:
        out.append("같은 선택지가 두 번")
    for c in q["choices"]:
        if not c.strip():
            out.append("빈 선택지")
        elif len(c) > LIMITS["choice"]:
            out.append(f"선택지가 김 ({len(c)}자): {c[:20]}…")
        elif re.match(r"\s*\(?\d+[).]", c):
            out.append(f"선택지에 번호를 붙였음: {c[:20]}")
    msg = q["message"].strip()
    if not msg:
        out.append("질문 문장이 없음")
    if len(msg) > LIMITS["message"]:
        out.append(f"질문이 김 ({len(msg)}자)")
    if msg.count("?") > 1:
        out.append(f"물음표가 {msg.count('?')}개 — 질문이 둘일 수 있음")
    if re.search(r"\(?\d\)", msg):
        out.append("질문 문장 안에 번호 선택지가 섞임")
    same = next((p for p in previous if p and difflib.SequenceMatcher(None, msg, p).ratio() >= 0.8), None)
    if same:
        out.append(f"전에 보낸 질문과 거의 같음: {same[:30]}…")
    hits = [w for w in SENSITIVE if w in msg + " ".join(q["choices"])]
    if hits:
        out.append(f"민감할 수 있는 말: {hits}")
    return out


def rendered(q):
    """멤버가 실제로 받을 모양. 번호는 하네스가 붙인다."""
    lines = [q["message"].strip()]
    lines += [f"{i}) {c.strip()}" for i, c in enumerate(q["choices"], 1)]
    if q["multi"]:
        lines.append("(여러 개 골라도 돼)")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="먼저 물을 질문을 만들어 보기만 한다 (보내지 않음)")
    ap.add_argument("members", nargs="+")
    ap.add_argument("--n", type=int, default=1, help="멤버마다 몇 번 만들어 볼지")
    ap.add_argument("--slot", choices=SLOTS, default="출근길")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    from chatbot.dm import DMAgent
    from cua.agent import openai_client
    agent = DMAgent(openai_client())
    for member in args.members:
        for i in range(args.n):
            t0 = time.time()
            q = draft(agent, member, args.slot)
            bad = problems(q)
            head = f"━━ {member}" + (f" #{i + 1}" if args.n > 1 else "") + f"  ({time.time() - t0:.0f}s)"
            print(head)
            if not q["ask"]:
                print(f"  [묻지 않음] {q['hold_reason']}")
            else:
                print(f"  방향   {q['axis']}")
                print(f"  확인   {q['target']}")
                print(f"  왜     {q['why_this_person']}")
                print("  ┌ 멤버가 받는 것")
                for line in rendered(q).splitlines():
                    print(f"  │ {line}")
                print("  └")
                print(f"  알 수 있는 것   {q['reads_as']}")
                print(f"  읽으면 안 되는 것 {q['does_not_mean']}")
            print(f"  하네스 검사: {'통과' if not bad else ' / '.join(bad)}")
            print()


if __name__ == "__main__":
    main()
