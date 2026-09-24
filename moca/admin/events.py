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
# the app keeps 20 UTF-16 units of the 정모 이름 and 장소 and silently drops the rest (measured 2026-09-24;
# an emoji costs 2). 40 was a guess, and names longer than 20 were being cut without anyone noticing.
NAME_LIMIT = LOCATION_LIMIT = 20


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


# --- plans: what the app has no field for -----------------------------------------------
# 소모임's 정모 form is only name / time / place / cost / capacity. Why a 정모 exists and how it runs is kept
# here, keyed by 정모 name, for the 정모 모카 opens. Separate from index.json because sync_events rebuilds that
# from the app. Members don't see this yet; a later tool will link a 정모 to a board post that describes it.

PLANS = EVENTS / "plans.json"
MODES = {"offline": "오프라인", "online": "온라인", "hybrid": "온·오프라인 함께"}
FORMATS = {"talk": "발표 (한두 사람이 이야기하고 나머지는 듣기)", "discussion": "그룹 토론",
           "workshop": "같이 실습", "cowork": "각자 할 일·자율 스터디", "social": "친목"}
PLAN_LIMITS = {"purpose": 120, "topic": 60, "format_note": 300}


def load_plans():
    if PLANS.exists():
        return json.loads(PLANS.read_text(encoding="utf-8"))
    return {}


def save_plans(plans):
    EVENTS.mkdir(parents=True, exist_ok=True)
    PLANS.write_text(json.dumps(plans, ensure_ascii=False, indent=1), encoding="utf-8")


def get_plan(name):
    return load_plans().get(name)


def set_plan(name, plan):
    plans = load_plans()
    plans[name] = plan
    save_plans(plans)


def rename_plan(old, new):
    plans = load_plans()
    if old in plans:
        plans[new] = plans.pop(old)
        save_plans(plans)


def drop_plan(name):
    plans = load_plans()
    if plans.pop(name, None) is not None:
        save_plans(plans)


def check_plan(plan, location, partial=False):
    """None if the plan is usable. With `partial`, only the fields given are checked (an edit)."""
    for field in ("purpose", "topic", "mode", "format"):
        if not partial and not (plan.get(field) or "").strip():
            return f"{field}를 적어야 합니다"
    for field, limit in PLAN_LIMITS.items():
        if len(plan.get(field) or "") > limit:
            return f"{field}는 {limit}자 이내여야 합니다"
    if plan.get("mode") and plan["mode"] not in MODES:
        return f"mode는 {list(MODES)} 중 하나여야 합니다"
    if plan.get("format") and plan["format"] not in FORMATS:
        return f"format은 {list(FORMATS)} 중 하나여야 합니다"
    online = "온라인" in (location or "")
    if plan.get("mode") == "online" and location is not None and not online:
        return "온라인 정모면 장소를 '온라인(Google Meet)'처럼 적어야 합니다"
    if plan.get("mode") == "offline" and online:
        return "장소가 온라인인데 mode가 offline입니다"
    return None


def describe_plan(plan, short=False):
    if not plan:
        return ""
    if plan.get("kind") == "program_signup":  # not a gathering: the program's sign-up 정모
        return f"{plan['purpose']} · 이 날짜에 모이는 자리가 아니라, 프로그램에 참가 신청하는 곳"
    head = f"{MODES.get(plan['mode'], plan['mode'])} · {FORMATS.get(plan['format'], plan['format'])} · 주제: {plan['topic']}"
    if short:
        return head
    note = f"\n  진행: {plan['format_note']}" if plan.get("format_note") else ""
    return f"{head}\n  목적: {plan['purpose']}{note}"


def plans_block():
    """For 채팅 모카 and 1:1 모카: what the 정모 모카 opened are for, so it can answer "이번 정모 뭐 해?"."""
    plans, events = load_plans(), {e["name"]: e for e in load_index()["events"]}
    upcoming = [(name, plan, events[name]) for name, plan in plans.items() if name in events]
    if not upcoming:
        return ""
    lines = ["\n\n## 네가(운영 모카로) 연 정모의 계획 (앱의 정모 카드에는 안 보이는 내용이야)"]
    for name, plan, event in upcoming:
        lines.append(f"- '{name}' — {event['when_text']}, {event['location']}, {event['joiners']}/{event['capacity']}명\n"
                     f"  {describe_plan(plan)}")
    lines.append("- 멤버가 물으면 이 계획대로 알려 줘. 계획에 없는 내용은 지어내지 말고, 정해지지 않았다고 말해.")
    lines.append("- '참가 등록'이라고 적힌 정모는 그 날짜에 모이는 자리가 아니라 프로그램 참가 신청이야. "
                 "실제 모임은 회차마다 따로 열린다고 알려 줘.")
    return "\n".join(lines)


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
    if not cards and index["events"]:
        # an empty read is almost always a screen that had not loaded yet; keeping the old list stops the
        # index (and the knowledge built from it) from flapping between full and empty
        log("정모 목록이 비어 보여 이전 목록을 유지함")
        return index["events"]
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


def remember_created(name, when, location, capacity=None, expense=0):
    """Put a 정모 모카 just made into the index at once. Without this it only appears at the next sync, and
    `mine` never gets set — so the harness would refuse to let 모카 edit or cancel the 정모 it just created.
    The next sync_events replaces these fields with what the app shows and keeps `mine`."""
    index = load_index()
    index["events"] = [e for e in index["events"] if e["name"] != name]
    index["events"].append({
        "name": name, "when": when.strftime("%Y-%m-%d %H:%M"),
        "when_text": f"{when.month}.{when.day}({'월화수목금토일'[when.weekday()]}) {when.hour}:{when.minute:02d}",
        "location": location, "expense": str(expense), "d_day": "", "joiners": 1, "capacity": capacity,
        "attending": True, "full": False, "mine": True, "synced_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    save_index(index)


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
               and a["result"] == "ok" and a["at"].startswith(today)
               and not a["agent"].startswith("test"))  # 개발 중 테스트는 모카의 하루치를 쓰지 않는다


# --- rules ---------------------------------------------------------------------------

def check_create(name, when, location, capacity, expense):
    from somoim.chat import units
    if not name or units(name) > NAME_LIMIT:
        return f"정모 이름은 1~{NAME_LIMIT}자여야 합니다 (앱이 그만큼만 받습니다. 이모지는 2자)"
    if location and units(location) > LOCATION_LIMIT:
        return f"정모 장소는 {LOCATION_LIMIT}자 이내여야 합니다 (앱이 그만큼만 받습니다)"
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
        return f"오늘은 이미 정모를 {MAX_CREATES_PER_DAY}개 만들었습니다. 더 만들려면 로하에게 부탁하세요"
    return None


def check_change(name, deleting=False):
    """Editing or deleting is only allowed for 모카's own 정모, and deleting only while nobody else has joined."""
    event = find_event(name)
    if event is None:
        return f"'{name}' 정모를 찾을 수 없습니다. list_events로 이름을 확인하세요"
    if not event["mine"]:
        return f"'{name}'은 모카가 만든 정모가 아니라 수정하거나 취소할 수 없습니다. 로하에게 부탁하세요"
    others = (event["joiners"] or 0) - (1 if event["attending"] else 0)
    if deleting and others > 0:
        return f"'{name}'에 이미 {others}명이 참석 신청했습니다. 취소는 로하에게 부탁하세요"
    return None
