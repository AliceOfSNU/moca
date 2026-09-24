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
import contextlib
import datetime as dt
import json
import os
import sys
import time

from chatbot.store import DATA_ROOT

STATE = DATA_ROOT / "mock" / "app.json"
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


def load():
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
        text = (f"@{mention} " if mention else "") + text
        if dry_run:
            return text
        at = _now()
        _append("chat", {"sender": account_name(), "mine": True, "text": text, "time": clock(at),
                         "day": day_label(at), "at": at.strftime("%Y-%m-%d %H:%M:%S")})
        self.log(f"  (목업) 모임 채팅 전송: {text[:60]}")
        return text


class MockDirect:
    """One 1:1 conversation."""

    def __init__(self, moim_chat, member, log=print):
        self.moim = moim_chat
        self.member = member
        self.dev = getattr(moim_chat, "dev", None) or MockDevice()
        self.log = log

    def open(self, open_profile=None):
        return True

    def read_since(self, anchor, backlog=30, max_pages=40):
        from somoim.chat import key
        msgs = load()["dms"].get(self.member, [])
        if anchor:
            for i in range(len(msgs) - 1, -1, -1):
                if key(msgs[i]) in anchor:
                    return [dict(m) for m in msgs[i + 1:]], True
        return [dict(m) for m in msgs[-backlog:]], False

    def send(self, text, dry_run=False):
        if dry_run:
            return text
        post_dm(self.member, account_name(), text)
        self.log(f"  (목업) 1:1 전송 → {self.member}: {text[:60]}")
        return text


def inbox_rows(moim_chat):
    """Who has a 1:1 open with 모카 (the mock never has unread badges)."""
    return [{"member": m, "node": None, "unread": False} for m in load()["dms"]]


class MockBoard:
    def __init__(self, moim_chat=None, log=print):
        self.log = log

    def open(self):
        return True

    def list_posts(self, category=None, pages=3):
        posts = load()["posts"]
        return [{"author": p["author"], "title": p["title"], "time": p["time"], "category": p["category"],
                 "preview": p["body"][:40], "pinned": p.get("pinned", False)}
                for p in reversed(posts) if category in (None, "전체", p["category"])]

    def read_post(self, card, category="전체"):
        title = card["title"] if isinstance(card, dict) else card
        p = next((x for x in load()["posts"] if x["title"] == title), None)
        if p is None:
            return None
        return {"title": p["title"], "author": p["author"], "role": p.get("role", ""), "time": p["time"],
                "category": p["category"], "body": p["body"]}

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
        state = load()
        e = next((x for x in state["events"] if x["name"] == name), None)
        if e is None:
            return False
        me = account_name()
        e["joiners"] = sorted(set(e["joiners"]) | {me}) if joining else [n for n in e["joiners"] if n != me]
        save(state)
        return True

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


def install(log=print):
    """Point the loop's app classes at the mock. Call before any session runs."""
    import chatbot.dm
    import chatbot.posts
    import run as run_module
    import admin.goal_loop
    seed(log=log)
    for module in (run_module, chatbot.dm, admin.goal_loop):
        for name, fake in (("SomoimBoard", MockBoard), ("SomoimEvents", MockEvents), ("SomoimVotes", MockVotes),
                           ("DirectChat", MockDirect), ("SomoimChat", MockChat), ("inbox_rows", inbox_rows),
                           ("NotificationWatcher", MockWatcher), ("AndroidDevice", MockDevice)):
            if hasattr(module, name):
                setattr(module, name, fake)
    log(f"목업 소모임으로 실행합니다 ({STATE})")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    state = seed()
    print(json.dumps({k: (len(v) if isinstance(v, (list, dict)) else v) for k, v in state.items()},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
