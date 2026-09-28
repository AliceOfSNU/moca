"""A 소모임 that lives in a file, for testing the loop without the real 모임.

Testing against real members is slow and irreversible: a chat question takes hours, a vote takes days, and a 정모
that goes out cannot be unsent. This layer replaces the app-facing classes (somoim/*) with ones that read and
write data/mock/app.json instead of driving the emulator. Everything above — the harness, the agents, tasks,
knowledge, programs — runs exactly as it does for real.

Run it with its own data root so a test never touches the real 모임's files:

    MOCA_DATA=data-mock python run.py --mock

The dashboard (--data ../moca/data-mock) is the other half: it shows this file and lets one person act as
several members — write in the chat, answer a 1:1, vote, sign up for a 정모 — and finish a running task at once
instead of waiting for its deadline.

What the fakes keep faithful, because the harness depends on it: the shapes the real screens return (정모 cards
with "2명 참석중 (2/60)", vote cards with "3명 참여 • 미참여"), 모카's own account name, and the message keys the
read position is built from. What they don't model: scrolling, keyboards, photos, notifications from the OS.
"""
import argparse
import contextlib
import datetime as dt
import json
import os
import pathlib
import shutil
import sys
import time

from chatbot.store import DATA_ROOT

STATE = DATA_ROOT / "mock" / "app.json"
PAGE = 15  # messages a screen holds, for the fakes that stand in for scrolling
DAYS = "월화수목금토일"


def _now():
    return dt.datetime.now()


def clock(at=None):
    at = at or _now()
    hour = at.hour % 12 or 12
    return f"{'오전' if at.hour < 12 else '오후'} {hour}:{at.minute:02d}"


def day_label(at=None):
    at = at or _now()
    return f"{at.year}년 {at.month}월 {at.day}일 {DAYS[at.weekday()]}요일"


def blank():
    return {"members": [], "chat": [], "dms": {}, "posts": [], "events": [], "votes": [], "delivered": 0}


_running = False   # set by install(): only a loop run beats the heartbeat, not the CLI


def load():
    if _running:
        # The real loop's heartbeat hangs off SomoimUI.on_read — every screen it reads (run.py). The mock reads
        # a file instead, so nothing beat, and the dashboard called a perfectly healthy mock run "stopped".
        # Reading the mock 모임's state is this world's equivalent of seeing the screen. presence throttles.
        from harness import presence
        presence.seen()
    if STATE.exists():
        return {**blank(), **json.loads(STATE.read_text(encoding="utf-8"))}
    return blank()


def save(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATE)


def _append(kind, item):
    state = load()
    state[kind].append(item)
    save(state)
    return item


def post_message(sender, text, at=None):
    """A member writes in the 모임 chat (the dashboard's test form calls this)."""
    at = at or _now()
    return _append("chat", {"sender": sender, "mine": False, "text": text, "time": clock(at),
                            "day": day_label(at), "at": at.strftime("%Y-%m-%d %H:%M:%S")})


def _parts(text, mention=None):
    """How the real app would break this send up: the input box holds MESSAGE_LIMIT units, and only the first
    part carries the @mention (somoim/chat.py). The harness recognises its own consent notice by its last
    part, so the split has to match."""
    from somoim.chat import MESSAGE_LIMIT, split_message, units
    parts = split_message(text, MESSAGE_LIMIT - (units(f"@{mention} ") if mention else 0))
    return [(f"@{mention} " if mention and i == 0 else "") + part for i, part in enumerate(parts)]


def whisper(member, text, at=None):
    """A 귓속말 소모임 itself posts in the 모임 chat, visible to 운영진 only: a sign-up, a cancellation, a 정모
    deleted, someone leaving. 모카 learns about a 정모 sign-up this way and no other — the attendee list alone
    changes silently — so a mock that skips these makes the loop blind until its next timer.
    Worded as the real app words them (data/chat/transcript.jsonl)."""
    at = at or _now()
    return _append("chat", {"sender": f"{member}(귓속말)", "mine": False, "text": f"(운영진에게만) {text}",
                            "time": clock(at), "day": day_label(at), "at": at.strftime("%Y-%m-%d %H:%M:%S")})


def attend(event_name, member, joining=True, at=None):
    """A member presses 참석 (or 취소) on a 정모. Returns (the event, None) or (None, why not)."""
    state = load()
    e = next((x for x in state["events"] if x["name"] == event_name), None)
    if e is None:
        return None, f"없는 정모입니다: {event_name}"
    before = list(e["joiners"])
    e["joiners"] = sorted(set(before) | {member}) if joining else [n for n in before if n != member]
    if e["joiners"] == before:
        return e, None  # nothing changed, so the app would say nothing
    save(state)
    whisper(member, f"{member}님께서 '{event_name}' 정모에 참석하셨습니다." if joining
                    else f"{member}님께서 '{event_name}' 정모 참석을 취소하셨습니다.", at=at)
    return next(x for x in load()["events"] if x["name"] == event_name), None


def post_dm(member, sender, text, at=None):
    at = at or _now()
    state = load()
    state["dms"].setdefault(member, []).append(
        {"sender": sender, "mine": sender == account_name(), "text": text, "time": clock(at),
         "day": day_label(at), "at": at.strftime("%Y-%m-%d %H:%M:%S")})
    save(state)


def account_name():
    from chatbot.agent import ACCOUNT_NAME
    return ACCOUNT_NAME


def seed(members=None, log=print):
    """Start a mock 모임. Members come from the profiles in this data root unless given."""
    state = load()
    if not state["members"]:
        if members is None:
            from chatbot.profiles import profiles
            members = [m for m in profiles() if m != account_name()]
        state["members"] = list(members)
        save(state)
        log(f"목업 모임 준비: 멤버 {len(state['members'])}명 ({', '.join(state['members'][:6])}…)")
    return state


# --- the fakes -------------------------------------------------------------------------------------

class MockDevice:
    width, height = 1080, 2400

    def __init__(self, *args, **kwargs):
        pass  # the real one takes scale=, serial= and so on; none of it means anything here

    def wake(self):
        return True

    @contextlib.contextmanager
    def adb_keyboard(self):
        yield

    def type_text(self, text):
        return True

    def clear_text(self):
        return True

    def key(self, code):
        return True


class MockChat:
    """The 모임 chat screen."""

    def __init__(self, dev=None, moim_names=None, log=print):
        self.dev = dev or MockDevice()
        self.ui = None
        self.log = log

    def open(self):
        return True

    def park(self):
        return True

    def preview(self):
        chat = load()["chat"]
        return (chat[-1]["text"], chat[-1]["time"]) if chat else None

    def scroll_to_bottom(self, max_swipes=30):
        """The real one returns (what is on screen, the span it scrolled); the mock has no screen, so it
        hands back the last messages and no span."""
        return [dict(m) for m in load()["chat"][-PAGE:]], None

    def newest(self):
        chat = load()["chat"]
        return chat[-1] if chat else None

    def read_since(self, anchor, backlog=30, max_pages=40):
        from somoim.chat import key
        chat = load()["chat"]
        if anchor:
            for i in range(len(chat) - 1, -1, -1):
                if key(chat[i]) in anchor:
                    return [dict(m) for m in chat[i + 1:]], True
        return [dict(m) for m in chat[-backlog:]], False

    def send(self, text, mention=None, dry_run=False):
        parts = _parts(text, mention)
        if dry_run:
            return "\n".join(parts)
        for part in parts:
            at = _now()
            _append("chat", {"sender": account_name(), "mine": True, "text": part, "time": clock(at),
                             "day": day_label(at), "at": at.strftime("%Y-%m-%d %H:%M:%S")})
        self.log(f"  (목업) 모임 채팅 전송: {parts[0][:60]}"
                 + (f" (+{len(parts) - 1}개로 나뉨)" if len(parts) > 1 else ""))
        return "\n".join(parts)


class MockDirect:
    """One 1:1 conversation."""

    def __init__(self, moim_chat, member, log=print):
        self.moim = moim_chat
        self.member = member
        self.dev = getattr(moim_chat, "dev", None) or MockDevice()
        self.log = log

    def open(self, open_profile=None):
        return True

    def scroll_to_bottom(self, max_swipes=30):
        return [dict(m) for m in load()["dms"].get(self.member, [])[-PAGE:]], None

    def newest(self):
        msgs = load()["dms"].get(self.member, [])
        return msgs[-1] if msgs else None

    def read_since(self, anchor, backlog=30, max_pages=40):
        from somoim.chat import key
        msgs = load()["dms"].get(self.member, [])
        if anchor:
            for i in range(len(msgs) - 1, -1, -1):
                if key(msgs[i]) in anchor:
                    return [dict(m) for m in msgs[i + 1:]], True
        return [dict(m) for m in msgs[-backlog:]], False

    def send(self, text, dry_run=False):
        parts = _parts(text)
        if dry_run:
            return "\n".join(parts)
        for part in parts:
            post_dm(self.member, account_name(), part)
        self.log(f"  (목업) 1:1 전송 → {self.member}: {parts[0][:60]}"
                 + (f" (+{len(parts) - 1}개로 나뉨)" if len(parts) > 1 else ""))
        return "\n".join(parts)


def inbox_rows(moim_chat):
    """The 1:1 inbox: one row per conversation, newest first. `preview` is the last message's text — the
    loop compares it with what it has stored to decide whether a conversation has something new."""
    rows = []
    for member, msgs in load()["dms"].items():
        last = msgs[-1] if msgs else None
        rows.append({"member": member, "time": last["time"] if last else None,
                     "preview": last["text"] if last else None, "node": None})
    rows.sort(key=lambda r: next((m["at"] for m in reversed(load()["dms"][r["member"]])), ""), reverse=True)
    return rows


class MockBoard:
    def __init__(self, moim_chat=None, log=print):
        self.log = log

    def open(self):
        return True

    def list_cards(self, category="전체", stop=None, max_pages=30):
        cards = [{"author": p["author"], "title": p["title"], "time": p["time"], "category": p["category"],
                  "preview": p["body"][:40], "pinned": p.get("pinned", False)}
                 for p in reversed(load()["posts"]) if category in (None, "전체", p["category"])]
        cards.sort(key=lambda c: not c["pinned"])  # pinned notices first, like the real board
        out = []
        for card in cards:
            if stop is not None and not card["pinned"] and stop(card):
                break
            out.append(card)
        return out

    list_posts = list_cards

    def read_post(self, card, category="전체"):
        posts = load()["posts"]
        if isinstance(card, dict):
            # a title does not identify a post — every 가입인사 shares one. The author and the posting time do,
            # which is what chatbot/posts.py matches on too.
            p = next((x for x in posts if x["title"] == card["title"] and x["author"] == card.get("author")
                      and x["time"] == card.get("time")), None)
            if p is None:
                p = next((x for x in posts if x["title"] == card["title"]
                          and x["author"] == card.get("author")), None)
        else:
            p = next((x for x in posts if x["title"] == card), None)
        if p is None:
            return None
        return {"title": p["title"], "author": p["author"], "role": p.get("role", ""), "time": p["time"],
                "category": p["category"], "body": p["body"]}

    def open_author_profile(self, post, category="전체"):
        """On the real board this taps the writer's name to reach their profile, and from there the 1:1
        screen. In the mock a 1:1 needs no screen, so there is nothing to open."""
        return True

    def write(self, category, title, body, dry_run=False):
        if dry_run:
            return True
        at = _now()
        _append("posts", {"category": category, "title": title, "body": body, "author": account_name(),
                          "time": f"{at.month}월 {at.day}일 {clock(at)}", "at": at.strftime("%Y-%m-%d %H:%M:%S")})
        self.log(f"  (목업) 게시글 작성: [{category}] {title}")
        return True


class MockEvents:
    def __init__(self, moim_chat=None, log=print):
        self.log = log
        self.dev = MockDevice()

    def open_home(self):
        return {}

    @staticmethod
    def _card(e):
        at = dt.datetime.strptime(e["when"], "%Y-%m-%d %H:%M")
        joined = len(e["joiners"])
        mine_in = account_name() in e["joiners"]
        return {"name": e["name"], "when": f"{at.month}.{at.day}({DAYS[at.weekday()]}) {at.hour}:{at.minute:02d}",
                "location": e["location"], "expense": str(e.get("expense", 0)),
                "d_day": f"D-{max((at - _now()).days, 0)}",
                "joiners": f"{joined}명 참석중 ({joined}/{e['capacity']})",
                "join_label": "참석취소" if mine_in else "참석"}

    def list_events(self, max_pages=8):
        return [self._card(e) for e in load()["events"] if not e.get("canceled")]

    def create(self, name, when, location, expense=0, capacity=20, notify=False, post_title=None):
        _append("events", {"name": name, "when": when.strftime("%Y-%m-%d %H:%M"), "location": location,
                           "expense": expense, "capacity": capacity, "post_title": post_title,
                           "joiners": [account_name()], "canceled": False,
                           "created_at": _now().strftime("%Y-%m-%d %H:%M:%S")})
        self.log(f"  (목업) 정모 개설: {name} ({when:%Y-%m-%d %H:%M}, {location})")
        return True

    def edit(self, name, new_name=None, location=None, expense=None, capacity=None):
        state = load()
        e = next((x for x in state["events"] if x["name"] == name), None)
        if e is None:
            return False
        if new_name:
            e["name"] = new_name
        for field, value in (("location", location), ("expense", expense), ("capacity", capacity)):
            if value is not None:
                e[field] = value
        save(state)
        return True

    def delete(self, name):
        state = load()
        e = next((x for x in state["events"] if x["name"] == name), None)
        if e is None:
            return False
        e["canceled"] = True
        save(state)
        self.log(f"  (목업) 정모 취소: {name}")
        return True

    def set_attendance(self, name, joining=True):
        e, problem = attend(name, account_name(), joining)
        return e is not None and problem is None

    def participants(self, name):
        e = next((x for x in load()["events"] if x["name"] == name), None)
        return None if e is None else list(e["joiners"])


class MockVotes:
    def __init__(self, moim_chat=None, log=print):
        self.log = log
        self.dev = MockDevice()

    def open_list(self):
        return True

    @staticmethod
    def _closed(v):
        if v.get("closed"):
            return True
        return bool(v.get("ends_at")) and v["ends_at"] <= _now().strftime("%Y-%m-%d %H:%M")

    def _card(self, v):
        when = "종료됨"
        if v.get("ends_at"):
            at = dt.datetime.strptime(v["ends_at"], "%Y-%m-%d %H:%M")
            when = (f"{at.year}.{at.month}.{at.day} ({DAYS[at.weekday()]}) {clock(at)} "
                    + ("종료됨" if self._closed(v) else "종료예정"))
        return {"author": v["author"], "posted": v["posted"], "title": v["title"],
                "status": f"{len(v['choices'])}명 참여 • 미참여", "when": when}

    def list_votes(self, max_pages=15):
        return [self._card(v) for v in reversed(load()["votes"])]

    def read(self, title):
        v = next((x for x in load()["votes"] if x["title"] == title), None)
        if v is None:
            return None
        card = self._card(v)
        picks = v["choices"]
        options = [{"name": o, "votes": sum(1 for c in picks.values() if o in c),
                    "voters": [] if v.get("anonymous") else sorted(m for m, c in picks.items() if o in c)}
                   for o in v["options"]]
        voted = set(picks)
        return {"title": v["title"], "author": v["author"], "posted": v["posted"], "when": card["when"],
                "left": "", "closed": self._closed(v), "options": options, "participants": len(voted),
                "selections": sum(len(c) for c in picks.values()),
                "not_voted": [m for m in load()["members"] if m not in voted]}

    def create(self, title, options, ends_at=None, multi=False, anonymous=False):
        at = _now()
        _append("votes", {"title": title, "options": list(options),
                          "ends_at": ends_at.strftime("%Y-%m-%d %H:%M") if ends_at else None,
                          "multi": bool(multi), "anonymous": bool(anonymous), "author": account_name(),
                          "posted": f"{at.month}월 {at.day}일 {clock(at)}", "choices": {}, "closed": False})
        self.log(f"  (목업) 투표 올림: {title} ({len(options)}개 선택지)")
        return True

    def share_to_chat(self, title):
        MockChat(log=self.log).send(f"[투표] {title} — 게시판에서 참여해 주세요")
        return True

    def close(self, title):
        state = load()
        v = next((x for x in state["votes"] if x["title"] == title), None)
        if v is None:
            return False
        v["closed"] = True
        save(state)
        return True

    def delete(self, title):
        state = load()
        state["votes"] = [x for x in state["votes"] if x["title"] != title]
        save(state)
        return True


class MockWatcher:
    """Stands in for 소모임's notifications: anything a member wrote since the last look becomes an event."""

    def __init__(self, dev=None, log=print):
        self.log = log
        self.pending = []

    def _collect(self):
        from chatbot.config import MOIM_NAMES
        state = load()
        chat = state["chat"]
        events = []
        for m in chat[state.get("delivered", 0):]:
            if not m["mine"]:
                events.append({"id": "101", "title": m["sender"], "text": m["text"],
                               "subText": MOIM_NAMES[0], "when": m["time"], "at": time.time()})
        for member, msgs in state.get("dms", {}).items():
            unseen = [m for m in msgs if not m["mine"] and not m.get("seen")]
            for m in unseen:
                events.append({"id": "104", "title": f"[DM] {member}", "text": m["text"],
                               "subText": "1:1메시지", "when": m["time"], "at": time.time()})
                m["seen"] = True
        state["delivered"] = len(chat)
        save(state)
        return events

    def wait(self, timeout):
        deadline = time.time() + max(timeout, 0)
        while True:
            if not self.pending:
                self.pending = self._collect()
            if self.pending:
                return self.pending.pop(0)
            if time.time() >= deadline:
                return None
            time.sleep(min(2, max(0.2, deadline - time.time())))

    def drain(self, before=None, where=None):
        self.pending = [e for e in self.pending if not ((before is None or e["at"] < before)
                                                        and (where is None or where(e)))]
        return []


def _real():
    """The app-facing objects to stand in for, paired with their fakes."""
    from cua.android import AndroidDevice
    from somoim.board import SomoimBoard
    from somoim.chat import SomoimChat
    from somoim.direct import DirectChat
    from somoim.direct import inbox_rows as real_inbox_rows
    from somoim.events import SomoimEvents
    from somoim.notifications import NotificationWatcher
    from somoim.votes import SomoimVotes
    return {"AndroidDevice": (AndroidDevice, MockDevice), "SomoimBoard": (SomoimBoard, MockBoard),
            "SomoimChat": (SomoimChat, MockChat), "DirectChat": (DirectChat, MockDirect),
            "SomoimEvents": (SomoimEvents, MockEvents), "SomoimVotes": (SomoimVotes, MockVotes),
            "NotificationWatcher": (NotificationWatcher, MockWatcher),
            "inbox_rows": (real_inbox_rows, inbox_rows)}


def install(log=print):
    """Point the loop's app classes at the mock. Call before any session runs.

    `from somoim.board import SomoimBoard` binds the class in the *importing* module's namespace, so patching
    the source module alone leaves every importer holding the real one. And run.py runs as __main__: `import
    run` there makes a second, unrelated copy of the module, so patching that copy changes nothing the loop
    uses — which is how a --mock run ended up reading and writing the real 모임. So patch by identity across
    every module already loaded, and refuse to run if any real object is still reachable.
    """
    import sys
    import time as _time
    global _running
    seed(log=log)
    # a mock run that has not been watched for days did not sleep for days — start the heartbeat here rather
    # than let the first beat report a nap that never happened
    from harness import presence
    state = presence.load()
    if state.get("last_seen") and _time.time() - state["last_seen"] > presence.GAP:
        state["last_seen"] = _time.time()
        presence.save(state)
    _running = True
    pairs = _real()
    for module in list(sys.modules.values()):
        ns = getattr(module, "__dict__", None)
        if ns is None or getattr(module, "__name__", "") == __name__:
            continue  # this module holds the fakes themselves
        for name, (real, fake) in pairs.items():
            if ns.get(name) is real:
                ns[name] = fake
    # the source modules too: a `from somoim.… import …` inside a function runs after this and would
    # otherwise hand back the real class
    for name, (real, fake) in pairs.items():
        source = sys.modules.get(getattr(real, "__module__", ""))
        if source is not None and getattr(source, name, None) is real:
            setattr(source, name, fake)
    left = [f"{m.__name__}.{name}" for m in list(sys.modules.values()) if getattr(m, "__dict__", None)
            for name, (real, _) in pairs.items()
            if m.__dict__.get(name) is real and getattr(m, "__name__", "") != __name__]
    if left:
        raise RuntimeError(f"목업 설치 실패: 진짜 앱 객체가 남아 있습니다 {left}")
    log(f"목업 소모임으로 실행합니다 ({STATE})")


def _card_time(iso):
    """'2026-09-17 19:38' -> '2026년 9월 17일 오후 7:38', the shape a board card shows and chatbot/posts.py
    parses back to exactly the same minute."""
    at = dt.datetime.strptime(iso, "%Y-%m-%d %H:%M")
    return f"{at.year}년 {at.month}월 {at.day}일 {clock(at)}"


def clone_real(real=None, log=print):
    """Fill the mock 모임 with the real one's 게시판 and 모임 채팅 as they stand now.

    A test 모카 that has never seen the group it is testing reasons about strangers. But copying only the
    *records* across would not work: the mock board would still be empty, so the next full sync would decide
    every saved post had been deleted and forget it, facts and all. So all three have to agree — what the mock
    screens show, what 모카 has saved, and where it stopped reading. Posts are rebuilt from the saved entries
    (same titles, same bodies, a card time that parses back to the same minute), so a sync finds nothing
    changed; the transcript and read position are copied, so the 290 messages already there don't arrive as
    new ones 모카 feels it must answer.

    1:1 대화, 정모, 투표 are left alone: the real ones belong to real people, and a test that opens a 정모 or
    answers a 1:1 should start from what the test itself set up.
    """
    from chatbot import posts as P
    real = pathlib.Path(real) if real else DATA_ROOT.parent / "data"
    if real.resolve() == DATA_ROOT.resolve():
        raise RuntimeError(f"목업 데이터 폴더와 같은 곳입니다: {real} — MOCA_DATA=data-mock 로 실행하세요")

    index = json.loads((real / "board" / "index.json").read_text(encoding="utf-8"))
    entries = sorted(index["posts"], key=lambda e: e["time"])
    posts = []
    for e in entries:
        body = (real / "board" / e["path"]).read_text(encoding="utf-8").split("\n\n", 2)[-1].strip()
        posts.append({"category": e["category"], "title": e["title"], "body": body, "author": e["author"],
                      "role": e.get("role") or "", "pinned": e.get("pinned", False),
                      "time": _card_time(e["time"]), "at": e["time"] + ":00"})

    chat = []
    lines = (real / "chat" / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
    for line in lines:
        if not line.strip():
            continue
        m = json.loads(line)
        chat.append({"sender": m["sender"], "mine": m.get("mine", False), "text": m["text"],
                     "time": m["time"], "day": m.get("day"), "at": m.get("read_at") or ""})

    state = load()
    state["posts"], state["chat"] = posts, chat
    save(state)

    # the records and the read position, so none of this looks new or deleted
    board = DATA_ROOT / "board"
    shutil.rmtree(board, ignore_errors=True)
    shutil.copytree(real / "board", board)
    (DATA_ROOT / "chat").mkdir(parents=True, exist_ok=True)
    for name in ("transcript.jsonl", "state.json"):
        shutil.copyfile(real / "chat" / name, DATA_ROOT / "chat" / name)
    log(f"진짜 모임에서 복제: 게시글 {len(posts)}개, 채팅 {len(chat)}개 ({real} → {DATA_ROOT})")
    return state


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="목업 소모임의 상태를 보거나, 진짜 모임의 게시판·채팅을 복제합니다")
    ap.add_argument("--clone-real", action="store_true",
                    help="진짜 모임(data/)의 게시글과 모임 채팅을 목업 모임으로 가져온다 (1:1·정모·투표는 그대로)")
    ap.add_argument("--real", default=None, help="복제해 올 데이터 폴더 (기본: data)")
    args = ap.parse_args()
    state = clone_real(args.real) if args.clone_real else seed()
    print(json.dumps({k: (len(v) if isinstance(v, (list, dict)) else v) for k, v in state.items()},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
