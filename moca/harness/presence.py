"""When 모카 was actually awake, so it can be told when it was asleep and for how long.

모카 only sees chat logs, so after a stop it would carry on as if nothing happened ("나 지금 여기 있어") while
members had been talking without it for hours. The harness keeps the facts instead:

- heartbeat: `seen()` runs after every successful read of the app's screen (somoim.ui.SomoimUI.on_read, set by
  run.py). Only a working device counts, so a dead emulator is sleep even if the loop process kept running.
- sleep: a gap of more than GAP between two heartbeats is recorded as {"from", "to"} when the next read works.
- `status_block()` turns that into a prompt section: facts only, no guessed cause.

Usage (from the moca/ directory):
    python -m harness.presence        # recent sleep periods
"""
import datetime as dt
import json
import sys
import time

from harness.tasks import ROOT

STATE = ROOT / "data" / "runtime" / "presence.json"
STATUS = ROOT / "data" / "runtime" / "status.json"   # what the loop is doing right now (read by the dashboard)
GAP = 10 * 60              # the loop reads the screen at least every ~2 minutes; a full board sync can take ~8
WRITE_EVERY = 30           # heartbeats are frequent; the file only needs to be roughly current
KEEP = 10                  # sleep periods kept
JUST_WOKE = 3 * 3600       # how long after waking 모카 is reminded to catch up
_written = 0.0


def load():
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {"last_seen": None, "sleeps": []}


def save(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def seen(now=None, log=None):
    """Heartbeat. Returns the sleep period if this call ended one."""
    global _written
    now = now or time.time()
    if now - _written < WRITE_EVERY:
        return None
    state = load()
    slept = None
    last = state.get("last_seen")
    if last and now - last > GAP:
        slept = {"from": last, "to": now}
        state["sleeps"] = (state.get("sleeps", []) + [slept])[-KEEP:]
        if log:
            log(f"모카가 깨어남: {_when(last)}부터 {_when(now)}까지 {_duration(now - last)} 동안 잠들어 있었음")
    state["last_seen"] = now
    save(state)
    _written = now
    return slept


def activity(kind, **detail):
    """Record what the loop is doing right now — "idle", "chat", "dm", "post", "memory" or "goal" — for the
    dashboard (a separate program that only reads data/). Written on every change, so it is always current."""
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps({"activity": kind, "since": time.strftime("%Y-%m-%d %H:%M:%S"), "detail": detail},
                              ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATUS)


def _when(ts, today=None):
    t = dt.datetime.fromtimestamp(ts)
    today = today or dt.date.today()
    day = "오늘" if t.date() == today else "어제" if t.date() == today - dt.timedelta(days=1) else f"{t.month}월 {t.day}일"
    return f"{day} {'오전' if t.hour < 12 else '오후'} {t.hour % 12 or 12}:{t.minute:02d}"


def _duration(seconds):
    minutes = int(seconds // 60)
    days, rest = divmod(minutes, 24 * 60)
    hours, minutes = divmod(rest, 60)
    parts = [f"{days}일" if days else "", f"{hours}시간" if hours else "", f"{minutes}분" if minutes and not days else ""]
    return "약 " + (" ".join(p for p in parts if p) or "1분")


def status_block(now=None):
    """Prompt section with the facts about 모카's recent sleep. Empty if it has never slept."""
    now = now or time.time()
    sleeps = load().get("sleeps", [])
    if not sleeps:
        return ""
    latest = sleeps[-1]
    lines = ["", "", "## 모카 상태 (프로그램이 알려 주는 사실)"]
    if now - latest["to"] < JUST_WOKE:
        lines += [
            f"- 너는 {_when(latest['from'])}부터 {_when(latest['to'])}까지 {_duration(latest['to'] - latest['from'])} 동안 "
            "잠들어 있었어(모카 프로그램이 멈춰 있었어). 왜 멈췄는지는 몰라.",
            "- 그동안 모임 채팅과 1:1에 온 메시지는 깨어난 뒤에 한꺼번에 읽은 거야. 메시지 시각을 보고 잠든 사이에 온 것을 구분해.",
            "- 그 사이 새로 온 멤버, 답을 못 받은 질문, 너를 찾은 말이 있으면 챙겨 주고, 그럴 때는 잠깐 자고 있었다고 가볍게 말해도 돼.",
            "- 잠든 사이 아무 일도 없었으면 깼다고 따로 알리지 마.",
        ]
    lines.append("- 멤버가 물으면 잠든 시간은 아래 기록 그대로 알려 주고, 이유는 지어내지 마.")
    lines.append("- 최근 잠든 기록: " + "; ".join(
        f"{_when(s['from'])} ~ {_when(s['to'])} ({_duration(s['to'] - s['from'])})" for s in sleeps[-3:]))
    return "\n".join(lines)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    state = load()
    print(f"마지막으로 깨어 있던 때: {_when(state['last_seen']) if state.get('last_seen') else '-'}")
    for s in state.get("sleeps", []):
        print(f"  {_when(s['from'])} ~ {_when(s['to'])}  ({_duration(s['to'] - s['from'])})")


if __name__ == "__main__":
    main()
