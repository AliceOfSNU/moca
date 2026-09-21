"""Knowledge from standing sources: what 모카 remembers about members, and what they wrote in their intro.

Until now knowledge had one writer — the reports of group-chat tasks. Two more feed it here:

- member notes (chatbot/members.py), only those the member allowed for 모임 운영. Consent is also checked
  again whenever knowledge is read (harness/knowledge.py: visible), so /memory off hides them at once and they
  are never shown as '한 멤버' instead — the 활용 범위 promise is "used for 운영 only with permission".
- the 가입인사 form (chatbot/profiles.py). A public post, so it follows the usual rule for names.

Each derived record carries origin.key (the note or the intro field it came from). Syncing is idempotent: a
record is only added when its source is new or has changed, and readers see only the newest one per key.
Group-chat conversation is not a source yet — which of its statements should become knowledge is still open.

Usage (from the moca/ directory):
    python -m harness.sources            # sync once and say what was added
"""
import hashlib
import json
import sys

from chatbot.members import KINDS, members_with_notes
from chatbot.profiles import _intros
from harness import knowledge

INTRO_FIELDS = {"job": "직업 또는 분야", "meet": "모이기 편한 곳", "lives": "사는 곳", "age": "나이",
                "message": "하고 싶은 말"}


def _digest(*parts):
    return hashlib.sha1(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:12]


def _put(existing, key, digest, statement, subjects, source_refs, origin, added):
    """Add a record for `key` unless the newest one already says the same thing."""
    last = existing.get(key)
    if last and last["origin"].get("digest") == digest:
        return
    record = knowledge.add(statement, subjects, "reported", source_refs, {**origin, "key": key, "digest": digest})
    existing[key] = record
    added.append(record)


def sync_notes(existing, added):
    """Member notes the member allowed for 모임 운영 → knowledge. Unconsented notes are never copied."""
    for member, notes in members_with_notes():
        for note in notes:
            if not note["scope"].get("moim"):
                continue
            label = KINDS.get(note["kind"], note["kind"])
            detail = f" (형식: {note['format']})" if note.get("format") else ""
            statement = f"{{s0}}님의 {label}: {note['summary']}{detail} — {note['stage']}"
            _put(existing, f"note:{member}:{note['id']}", _digest(note["summary"], note.get("format"), note["stage"]),
                 statement, [member], [f"note:{note['id']}"],
                 {"channel": "member_note", "context": note["source"]["context"]}, added)


def sync_intros(existing, added):
    """Every field of the 가입인사 form → one knowledge record each. A free-form intro becomes one record."""
    for author, intro in _intros().items():
        ref = [f"post:{intro['post']}"]
        origin = {"channel": "intro", "post": intro["post"]}
        fields = [(f, intro[f]) for f in INTRO_FIELDS if intro.get(f)]
        if not fields:
            if intro.get("raw"):
                _put(existing, f"intro:{author}:raw", _digest(intro["raw"]),
                     f"{{s0}}님의 자기소개: \"{intro['raw'][:200]}\"", [author], ref, origin, added)
            continue
        for field, value in fields:
            text = f"\"{value}\"" if field == "message" else value
            _put(existing, f"intro:{author}:{field}", _digest(value),
                 f"{{s0}}님의 자기소개 — {INTRO_FIELDS[field]}: {text}", [author], ref, origin, added)


def sync(log=None):
    """Bring derived knowledge up to date. Cheap (local files only), so it can run every round."""
    existing, added = knowledge.latest_by_key(), []
    sync_notes(existing, added)
    sync_intros(existing, added)
    if added and log:
        log(f"지식 {len(added)}건 추가 (멤버 메모 {sum(r['origin']['channel'] == 'member_note' for r in added)}건, "
            f"자기소개 {sum(r['origin']['channel'] == 'intro' for r in added)}건)")
    return added


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    added = sync(log=print)
    for r in added:
        print(f"  {r['id']}  {knowledge.render(r)}")
    if not added:
        print("새로 추가할 지식이 없습니다.")


if __name__ == "__main__":
    main()
