"""What 모카 remembers about each member (documents/personal_intelligence.md, items 1-2).

Kept apart from the board (public content) and the chat logs (raw conversation): these are short,
sourced notes about what a member wants from the 모임. 모카 proposes them through the
`propose_member_data` tool; the harness decides who they belong to and what they may be used for.

Usage (from the moca/ directory):
    python -m chatbot.members list            # every member with notes
    python -m chatbot.members show 홍길동      # one member's notes
"""
import argparse
import difflib
import hashlib
import json
import re
import sys
import time

from chatbot.store import ROOT

MEMBERS = ROOT / "data" / "members"

# what 모카 may record for now (personal_intelligence.md: start with these, no personality or relationships)
KINDS = {
    "question": "궁금한 질문",
    "activity": "해보고 싶은 활동",
    "experience": "나눌 수 있는 경험",
    "availability": "참여 조건",
}
# how far the member has actually committed; the harness never upgrades this on its own
STAGES = ["관심 있음", "참여 의사", "주최 의사"]
MAX_NOTES_PER_REPLY = 3
MAX_LEN = 200


def member_dir(member):
    safe = re.sub(r"[^\w가-힣-]", "_", member)[:30]
    return MEMBERS / f"{safe}-{hashlib.sha1(member.encode()).hexdigest()[:8]}"


def notes_file(member):
    return member_dir(member) / "notes.jsonl"


def load_notes(member):
    path = notes_file(member)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def save_notes(member, notes):
    member_dir(member).mkdir(parents=True, exist_ok=True)
    notes_file(member).write_text("".join(json.dumps(n, ensure_ascii=False) + "\n" for n in notes), encoding="utf-8")


def members_with_notes():
    if not MEMBERS.exists():
        return []
    out = []
    for path in sorted(MEMBERS.glob("*/notes.jsonl")):
        notes = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        if notes:
            out.append((notes[0]["member"], notes))
    return out


def _squash(text):
    return re.sub(r"[\s.,!?~…]+", "", text or "").lower()


def _same_note(a_summary, b_summary):
    """Same thing said twice? Wording varies between turns ('AI로 음악을 직접 만들어보고 싶어함' vs
    'AI 음악 만들기를 해보고 싶어함'), so compare loosely rather than exactly."""
    a, b = _squash(a_summary), _squash(b_summary)
    if not a or not b:
        return False
    return a in b or b in a or difflib.SequenceMatcher(None, a, b).ratio() >= 0.6


def set_moim_scope(member, allowed):
    """Turn 모임 운영 use on or off for every note this member has, the old ones included
    (personal_intelligence.md 3: consent covers what was already recorded, and turning it off un-shares it).
    The member's own 1:1 use is never touched. Returns how many notes changed."""
    notes, now, changed = load_notes(member), time.strftime("%Y-%m-%d %H:%M:%S"), 0
    for note in notes:
        if note["scope"].get("moim") != bool(allowed):
            note["scope"]["moim"] = bool(allowed)
            note["scope"]["decided_at"] = now
            changed += 1
    if changed:
        save_notes(member, notes)
    return changed


def add_note(member, kind, summary, stage=None, format=None, quote=None, context="dm", share=False):
    """Store one note about `member`. Returns (record, what happened) — the caller reports this to the model.
    The member, scope and timestamps are the harness's to set; the model only proposes content."""
    if kind not in KINDS:
        raise ValueError(f"kind는 {list(KINDS)} 중 하나여야 합니다")
    summary = (summary or "").strip()[:MAX_LEN]
    if not summary:
        raise ValueError("summary가 비어 있습니다")
    if stage and stage not in STAGES:
        raise ValueError(f"stage는 {STAGES} 중 하나여야 합니다")

    notes = load_notes(member)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for note in notes:
        if note["kind"] == kind and _same_note(note["summary"], summary):
            note["updated_at"] = now
            note["seen"] = note.get("seen", 1) + 1
            if stage and STAGES.index(stage) > STAGES.index(note.get("stage") or STAGES[0]):
                note["stage"] = stage  # the member committed further than before
            note["scope"]["moim"] = bool(share)
            save_notes(member, notes)
            return note, "이미 비슷한 기록이 있어 갱신했습니다"
    note = {"id": f"n{len(notes) + 1}", "member": member, "kind": kind, "summary": summary,
            "format": (format or "").strip()[:MAX_LEN] or None, "stage": stage or STAGES[0],
            "source": {"context": context, "at": now, "quote": (quote or "").strip()[:MAX_LEN] or None},
            # 공동 활동 기획에 쓰려면 멤버의 동의가 필요하다 (chatbot/memory_consent.py)
            "scope": {"personal": True, "moim": bool(share)},
            "created_at": now, "updated_at": now, "seen": 1}
    notes.append(note)
    save_notes(member, notes)
    return note, "새로 기록했습니다"


def format_note(note):
    bits = [f"- [{KINDS[note['kind']]}] {note['summary']}"]
    if note.get("format"):
        bits.append(f"(형식: {note['format']})")
    bits.append(f"({note['stage']}, {note['created_at'][:10]})")
    return " ".join(bits)


def notes_block(member, sharing=None):
    """The member's notes, for their own 1:1 prompt. Empty string when there are none.
    `sharing` (their 모임 운영 활용 setting) is the harness's to pass in — see chatbot/memory_consent.py."""
    notes = load_notes(member)
    if not notes:
        return ""
    state = "" if sharing is None else (
        f"\n(모임 운영 활용: {'켜짐' if sharing else '꺼짐'}. 멤버가 물으면 이대로 알려 주고, '/memory'로 확인, "
        "'/memory on' 또는 '/memory off'로 바꿀 수 있다고 안내해.)")
    return (f"\n## 모카가 '{member}'님에 대해 기억하고 있는 것 (이 멤버와의 대화에서 모은 것)\n"
            + "\n".join(format_note(n) for n in notes) + state
            + "\n(이 기록은 이 멤버와의 대화를 돕는 데만 써. 다른 멤버나 모임 채팅에 옮기지 마. "
              "멤버가 무엇을 기억하냐고 물으면 전체를 그대로 옮기지 말고 짧게 요약해 주고, "
              "전체 기록이 필요하면 로하에게 요청하라고 안내해.)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["list", "show"])
    ap.add_argument("member", nargs="?")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if args.command == "list":
        for member, notes in members_with_notes():
            print(f"{member}: {len(notes)}개 (최근 {max(n['updated_at'] for n in notes)})")
        return
    for note in load_notes(args.member):
        print(format_note(note))
        source = note["source"]
        print(f"    출처: {source['context']} {source['at']}" + (f" — \"{source['quote']}\"" if source.get("quote") else ""))
        print(f"    활용 범위: 개인 맞춤 {'O' if note['scope']['personal'] else 'X'} / 모임 운영 {'O' if note['scope']['moim'] else 'X'}")


if __name__ == "__main__":
    main()
