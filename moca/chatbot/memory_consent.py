"""활용 범위 동의: what 모카 may use its member notes for (documents/personal_intelligence.md 3).

That the conversation is stored at all was agreed to when the member entered the 1:1 (chatbot/dm.py).
This is the separate, lighter question the doc asks for: may those notes leave the member's own 1:1 and
help run the 모임 — activity recommendations for them, and 운영 모카's picture of what the 모임 wants.

It is asked as a conversation the morning after the member agreed to 1:1, not as a form. What the member
answers is read by a separate structured call, so the 모카 that is chatting doesn't get to decide what
counts as a yes; only an explicit yes turns sharing on, and the record is the harness's to write.
Members flip it themselves any time with /memory on|off, and turning it off immediately un-shares
everything already recorded (chatbot.members.set_moim_scope).

Usage (from the moca/ directory):
    python -m chatbot.memory_consent            # who has been asked, and what they answered
"""
import json
import sys
import time

from chatbot.members import MEMBERS, set_moim_scope
from chatbot.store import ROOT

STATE = MEMBERS / "scope_consent.json"
POINTS = ROOT / "documents" / "memory_scope_request.txt"
DECISIONS = ["동의", "거절", "불명확"]
MAX_ASK_ATTEMPTS = 3   # failures to deliver the question
MAX_UNCLEAR = 3        # replies that answered something else; after this 모카 stops waiting for an answer


def load():
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}


def save(state):
    MEMBERS.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def record(member):
    return load().get(member, {})


def shares(member):
    """May 모카's notes about this member be used for 모임 운영?"""
    return record(member).get("sharing") is True


def awaiting(member):
    """모카 asked and is still waiting for an answer."""
    return record(member).get("status") == "asked"


def due(member, consent, now=None):
    """Time to ask this member? The morning after they agreed to the 1:1 notice — never the same day,
    so the question doesn't land on top of the first conversation. Asked once; a send that failed is
    retried on later days. Members who agreed before this step existed are due straight away."""
    if consent.get("status") != "agreed":
        return False
    agreed = (consent.get("agreed_at") or "")[:10]
    today = time.strftime("%Y-%m-%d", now or time.localtime())
    if not agreed or agreed >= today:
        return False
    state = record(member)
    if state.get("status") == "failed":
        return (state.get("asked_at") or "")[:10] < today and state.get("attempts", 0) < MAX_ASK_ATTEMPTS
    return not state.get("status")


def mark_asked(member, sent=True):
    state = load()
    entry = state.get(member, {})
    entry.update(status="asked" if sent else "failed", asked_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                 attempts=entry.get("attempts", 0) + 1)
    state[member] = entry
    save(state)
    return entry


def set_sharing(member, on, how="대화"):
    """Write the member's decision and apply it to every note they already have.
    Returns how many notes changed."""
    state = load()
    entry = state.get(member, {})
    entry.update(status="allowed" if on else "declined", sharing=bool(on),
                 decided_at=time.strftime("%Y-%m-%d %H:%M:%S"), decided_by=how)
    state[member] = entry
    save(state)
    return set_moim_scope(member, bool(on))


def mark_unclear(member):
    """The member said something, but not an answer. After a few of these 모카 stops waiting."""
    state = load()
    entry = state.get(member, {})
    entry["unclear"] = entry.get("unclear", 0) + 1
    if entry["unclear"] >= MAX_UNCLEAR:
        entry["status"] = "no_answer"  # not shared; the member can still turn it on with /memory on
    state[member] = entry
    save(state)
    return entry


def ask_prompt(member):
    """What 모카 is told to write. The points are required content, not a script — it writes it its own way."""
    return (f"'{member}'님과 1:1 대화를 시작한 지 하루가 지났어. 이제 모카가 기억하는 것을 어디까지 써도 되는지 "
            f"물어볼 차례야. 아래 내용을 빠짐없이 담되, 네 말투로 자연스럽게 하나의 메시지로 써 줘.\n\n"
            + POINTS.read_text(encoding="utf-8").strip())


def classify(client, model, member, lines):
    """Read the member's reply to that question: 동의 / 거절 / 불명확, with a one-line reason.
    Deliberately a separate call — the 모카 that is talking to the member doesn't get to call it consent."""
    schema = {"type": "object", "additionalProperties": False, "required": ["decision", "reason"],
              "properties": {"decision": {"type": "string", "enum": DECISIONS},
                             "reason": {"type": "string", "description": "한 줄 근거 (기록용)"}}}
    instructions = (
        "너는 1:1 대화 기록을 읽고, 멤버가 '모카가 기억한 내용을 모임 운영에도 활용해도 되는지'라는 질문에 "
        "어떻게 답했는지만 판정한다. 대화에 답하지 말고 판정만 해라.\n"
        "- 동의: 멤버가 분명하게 허락했다 ('네', '좋아요', '그렇게 해주세요', '괜찮아요' 등).\n"
        "- 거절: 멤버가 원하지 않는다고 했거나, 개인용으로만 써 달라고 했다.\n"
        "- 불명확: 질문과 상관없는 이야기를 했거나, 되물었거나, 조건을 달았거나, 판단하기 애매하다.\n"
        "애매하면 '불명확'으로 판정한다. 허락은 분명할 때만 '동의'다.")
    prompt = (f"멤버: {member}\n\n## 최근 1:1 대화 (오래된 순, '모카(나)'는 모카가 보낸 메시지)\n"
              + "\n".join(lines) + "\n\n멤버의 마지막 답변을 판정해.")
    resp = client.responses.create(
        model=model, instructions=instructions, input=prompt,
        text={"format": {"type": "json_schema", "name": "scope_decision", "strict": True, "schema": schema}})
    data = json.loads(resp.output_text)
    return data["decision"], data["reason"]


def parse_command(text):
    """The member's own switch. Checked by the harness, never by the model: "show", "on", "off" or None."""
    t = (text or "").strip().lower().replace("／", "/").rstrip(".!")
    if t in ("/memory", "/기억", "memory", "/메모리"):
        return "show"
    if t in ("/memory on", "/기억 on", "/memory 켜기", "/기억 켜기", "/메모리 on"):
        return "on"
    if t in ("/memory off", "/기억 off", "/memory 끄기", "/기억 끄기", "/메모리 off"):
        return "off"
    return None


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    state = load()
    if not state:
        print("아직 활용 범위를 물어본 멤버가 없습니다.")
        return
    for member, entry in state.items():
        print(f"{member}: {entry.get('status')} (공유 {'O' if entry.get('sharing') else 'X'})"
              f" 질문 {entry.get('asked_at', '-')} 결정 {entry.get('decided_at', '-')}"
              f"{' by ' + entry['decided_by'] if entry.get('decided_by') else ''}")


if __name__ == "__main__":
    main()
