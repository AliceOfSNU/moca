"""Knowledge from standing sources: what members said once and stands, and what the harness saw itself.

Until now knowledge had one writer — the reports of group-chat tasks. These feed it here:

- member notes (chatbot/members.py), only those the member allowed for 모임 운영. Consent is also checked
  again whenever knowledge is read (harness/knowledge.py: visible), so /memory off hides them at once and they
  are never shown as '한 멤버' instead — the 활용 범위 promise is "used for 운영 only with permission".
- the 가입인사 form (chatbot/profiles.py). A public post, so its facts name the member for everyone.
- observed facts (basis "observed"): when each member was first seen in the 모임 chat, how busy the chat was
  each finished day (quiet days included), and who signed up for each 정모. Who actually came to a 정모 is not
  among them — the app can't show it, so 모카 can't know it unless someone tells it.

Each derived record carries origin.key (the note or the intro field it came from). Syncing is idempotent: a
record is only added when its source is new or has changed, and readers see only the newest one per key.
Group-chat conversation is not a source yet — which of its statements should become knowledge is still open.

Usage (from the moca/ directory):
    python -m harness.sources            # sync once and say what was added
"""
import datetime as dt
import hashlib
import json
import re
import sys

from chatbot.members import KINDS, members_with_notes
from chatbot.profiles import _intros
from harness import knowledge

INTRO_FIELDS = {"job": "직업 또는 분야", "meet": "모이기 편한 곳", "lives": "사는 곳", "age": "나이",
                "message": "하고 싶은 말"}


def _digest(*parts):
    return hashlib.sha1(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:12]


def _put(existing, key, digest, statement, subjects, source_refs, origin, added, basis="reported"):
    """Add a record for `key` unless the newest one already says the same thing."""
    last = existing.get(key)
    if last and last["origin"].get("digest") == digest:
        return
    record = knowledge.add(statement, subjects, basis, source_refs, {**origin, "key": key, "digest": digest})
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


# --- observed: facts the harness saw itself, not something anyone said -------------------------------

WEEKDAYS = "월화수목금토일"


def _chat():
    """The 모임 chat as recorded, each message with its calendar date."""
    from chatbot.store import ChatStore
    out = []
    for m in ChatStore().history(100000):
        found = re.match(r"(\d{4})년 (\d{1,2})월 (\d{1,2})일", m.get("day") or "")
        if found:
            out.append((dt.date(*map(int, found.groups())), m))
    return out


def _label(day):
    return f"{day.month}월 {day.day}일({WEEKDAYS[day.weekday()]})"


def sync_members(existing, added):
    """When each member was first seen in the 모임 chat. The app makes a new member say hello there, so for
    anyone who joined after 모카 started reading, this is the day they joined."""
    seen = {}
    for day, m in _chat():
        who = m["sender"].replace("(귓속말)", "").strip()
        if not m["mine"] and who not in seen:
            seen[who] = (day, m)
    for who, (day, m) in seen.items():
        _put(existing, f"member:{who}:first_seen", _digest(str(day)),
             f"{{s0}}님을 모임 채팅에서 처음 본 날: {_label(day)}", [who], [m["id"]],
             {"channel": "membership"}, added, basis=knowledge.OBSERVED)


def sync_chat_stats(existing, added):
    """One record per finished day of the 모임 chat, quiet days included — silence is a fact too.
    The hello a new member is made to say, 모카's own messages and 귓속말 notices are not counted."""
    from chatbot.agent import answerable, is_call
    from chatbot.config import DEVELOPER
    chat = _chat()
    if not chat:
        return
    greeted, by_day = set(), {}
    for day, m in chat:
        stats = by_day.setdefault(day, {"messages": 0, "speakers": set(), "developer": 0, "calls": 0, "joined": 0})
        who = m["sender"].replace("(귓속말)", "").strip()
        if m["mine"]:
            continue
        if who not in greeted:  # 가입 첫인사
            greeted.add(who)
            stats["joined"] += 1
            continue
        if not answerable(m):
            continue
        stats["messages"] += 1
        stats["speakers"].add(who)
        stats["developer"] += who == DEVELOPER
        stats["calls"] += is_call(m)
    today, day = dt.date.today(), chat[0][0]
    while day < today:  # only finished days, so a record never changes once written
        s = by_day.get(day, {"messages": 0, "speakers": set(), "developer": 0, "calls": 0, "joined": 0})
        others = sorted(s["speakers"] - {DEVELOPER})
        text = (f"{_label(day)} 모임 채팅: 멤버 메시지 {s['messages']}개(가입 첫인사 제외), 말한 멤버 {len(s['speakers'])}명. "
                f"그중 {DEVELOPER} {s['developer']}개, 다른 멤버 {s['messages'] - s['developer']}개({len(others)}명). "
                f"모카를 부른 메시지 {s['calls']}개. "
                + (f"새로 첫인사한 멤버 {s['joined']}명." if day != chat[0][0] else
                   "모카가 채팅을 읽기 시작한 날이라, 이날 처음 보인 멤버가 이날 들어왔는지는 알 수 없다."))
        _put(existing, f"chat:{day}", _digest(text), text, [], [f"chat:{day}"],
             {"channel": "chat_stats"}, added, basis=knowledge.OBSERVED)
        day += dt.timedelta(days=1)


def sync_signups(existing, added):
    """Who signed up for each 정모, from the attendee list the daily sweep reads (harness/stardust.py).
    Names follow the usual rule — shown only for members who allowed 모임 운영 use — as vote choices do.
    Whether someone actually came can't be seen from the app, so there is nothing about attendance here."""
    from admin.events import load_index
    from harness.stardust import load_sweep
    listed = {e["name"]: e for e in load_index()["events"]}
    for name, snap in load_sweep()["events"].items():
        people = snap.get("participants", [])
        event = listed.get(name, {})
        cap = f" / 정원 {event['capacity']}명" if event.get("capacity") else ""
        origin = {"channel": "event", "event": name}
        _put(existing, f"event:{name}:signups", _digest(len(people), event.get("capacity")),
             f"정모 '{name}' ({snap.get('when')}): 참석 신청 {len(people)}명{cap} ({snap.get('seen_at')} 기준)",
             [], [f"event:{name}"], origin, added, basis=knowledge.OBSERVED)
        for who in people:
            _put(existing, f"event:{name}:{who}", _digest("signed_up"),
                 f"{{s0}}님이 정모 '{name}'에 참석 신청했다", [who], [f"event:{name}"], origin, added,
                 basis=knowledge.OBSERVED)
        if name not in listed:
            continue  # gone from the app (over or deleted): its last list stands
        prefix = f"event:{name}:"
        for key, record in list(existing.items()):
            who = key[len(prefix):] if key.startswith(prefix) else None
            if who and who != "signups" and who not in people and record["origin"].get("digest") == _digest("signed_up"):
                _put(existing, key, _digest("cancelled"), f"{{s0}}님이 정모 '{name}' 참석 신청을 취소했다",
                     [who], [f"event:{name}"], origin, added, basis=knowledge.OBSERVED)


def sync(log=None):
    """Bring derived knowledge up to date. Cheap (local files only), so it can run every round."""
    existing, added = knowledge.latest_by_key(), []
    sync_notes(existing, added)
    sync_intros(existing, added)
    sync_members(existing, added)
    sync_chat_stats(existing, added)
    sync_signups(existing, added)
    if added and log:
        by = {}
        for r in added:
            by[r["origin"]["channel"]] = by.get(r["origin"]["channel"], 0) + 1
        names = {"member_note": "멤버 메모", "intro": "자기소개", "membership": "처음 본 날", "chat_stats": "채팅 통계",
                 "event": "정모 신청"}
        log(f"지식 {len(added)}건 추가 (" + ", ".join(f"{names.get(c, c)} {n}건" for c, n in by.items()) + ")")
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
