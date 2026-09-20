"""정모 memory and the harness rules around them.

The model asks; this module decides whether the app is touched at all. Rules kept deliberately strict:
모카 only edits or deletes 정모 it created itself, never one other members have already joined, and it can't
create more than a couple per day.
"""
import datetime as dt
import json
import re
import time

from chatbot.store import ROOT  # the shared data/ root
from somoim.events import CAPACITY_RANGE

EVENTS = ROOT / "data" / "events"
INDEX = EVENTS / "index.json"
ACTIONS = EVENTS / "actions.jsonl"
MAX_CREATES_PER_DAY = 3
NAME_LIMIT = 40


def load_index():
    if INDEX.exists():
        return json.loads(INDEX.read_text(encoding="utf-8"))
    return {"events": [], "last_sync": 0}


def save_index(index):
    EVENTS.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")


def log_action(agent, action, args, result, note=""):
    """Every app-touching 정모 action, and who asked for it."""
    EVENTS.mkdir(parents=True, exist_ok=True)
    with open(ACTIONS, "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "agent": agent, "action": action,
                            "args": args, "result": result, "note": note}, ensure_ascii=False) + "\n")


def parse_when(text, now=None):
    """'9.24(목) 20:00' -> ISO 'YYYY-MM-DD HH:MM'. The app shows no year, so a date far in the past means next year."""
    now = now or dt.datetime.now()
    m = re.match(r"\s*(\d{1,2})\.(\d{1,2}).*?(\d{1,2}):(\d{2})", text or "")
    if not m:
        return None
    month, day, hour, minute = (int(g) for g in m.groups())
    year = now.year + (1 if month < now.month - 6 else 0)
    try:
        return dt.datetime(year, month, day, hour, minute).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return None


def joiner_counts(text):
    """'2명 참석중 (2/60)' -> (2, 60)."""
    m = re.search(r"\((\d+)/(\d+)\)", text or "")
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def sync_events(events_ui, log, agent="harness"):
    """Refresh the 정모 list from the app. Keeps the "모카가 만든 정모" flag across syncs."""
    cards = events_ui.list_events()
    if cards is None:
        log("정모 목록을 읽지 못함")
        return None
    index = load_index()
    mine = {e["name"]: e.get("mine", False) for e in index["events"]}
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    index["events"] = []
    for card in cards:
        joined, capacity = joiner_counts(card["joiners"])
        index["events"].append({
            "name": card["name"], "when": parse_when(card["when"]), "when_text": card["when"],
            "location": card["location"], "expense": card["expense"], "d_day": card["d_day"],
            "joiners": joined, "capacity": capacity, "attending": card["join_label"] in ("취소", "참석취소"),
            "full": card["join_label"] == "빈자리 알림 받기", "mine": mine.get(card["name"], False), "synced_at": now})
    index["last_sync"] = time.time()
    save_index(index)
    log(f"정모 {len(index['events'])}개 확인" + (f" (모카가 만든 것 {sum(e['mine'] for e in index['events'])}개)" if index["events"] else ""))
    return index["events"]


def find_event(name):
    return next((e for e in load_index()["events"] if e["name"] == name), None)


def mark_mine(name):
    index = load_index()
    for e in index["events"]:
        if e["name"] == name:
            e["mine"] = True
    save_index(index)


def creates_today():
    if not ACTIONS.exists():
        return 0
    today = time.strftime("%Y-%m-%d")
    return sum(1 for line in ACTIONS.read_text(encoding="utf-8").splitlines()
               if line.strip() and (a := json.loads(line))["action"] == "create_event"
               and a["result"] == "ok" and a["at"].startswith(today))


# --- rules ---------------------------------------------------------------------------

def check_create(name, when, location, capacity, expense):
    if not name or len(name) > NAME_LIMIT:
        return f"정모 이름은 1~{NAME_LIMIT}자여야 합니다"
    if when <= dt.datetime.now() + dt.timedelta(minutes=30):
        return "정모 일시는 지금보다 30분 이상 뒤여야 합니다"
    if when > dt.datetime.now() + dt.timedelta(days=180):
        return "정모 일시는 180일 이내여야 합니다"
    if not location:
        return "정모 장소를 적어야 합니다"
    if not CAPACITY_RANGE[0] <= capacity <= CAPACITY_RANGE[1]:
        return f"정원은 {CAPACITY_RANGE[0]}~{CAPACITY_RANGE[1]}명이어야 합니다"
    if expense < 0:
        return "비용은 0원 이상이어야 합니다"
    if find_event(name):
        return f"'{name}' 정모가 이미 있습니다"
    if creates_today() >= MAX_CREATES_PER_DAY:
        return f"오늘은 이미 정모를 {MAX_CREATES_PER_DAY}개 만들었습니다. 더 만들려면 모임장에게 요청하세요"
    return None


def check_change(name, deleting=False):
    """Editing or deleting is only allowed for 모카's own 정모, and deleting only while nobody else has joined."""
    event = find_event(name)
    if event is None:
        return f"'{name}' 정모를 찾을 수 없습니다. list_events로 이름을 확인하세요"
    if not event["mine"]:
        return f"'{name}'은 모카가 만든 정모가 아니라 수정하거나 취소할 수 없습니다. 모임장에게 요청하세요"
    others = (event["joiners"] or 0) - (1 if event["attending"] else 0)
    if deleting and others > 0:
        return f"'{name}'에 이미 {others}명이 참석 신청했습니다. 취소는 모임장에게 요청하세요"
    return None
