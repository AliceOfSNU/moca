"""Watch for 소모임 activity: notification events (event log) and live updates of an open chat screen.
Prints one line per event. Run from moca/: python -m experiments.notif_listener
"""
import re
import subprocess
import sys
import threading
import time

from chatbot.config import MOIM_NAMES
from cua.android import ADB, AndroidDevice
from somoim.chat import SomoimChat
from somoim.ui import PKG

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
T0 = time.time()


def say(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def somoim_notifications():
    out = subprocess.run([ADB, "shell", "dumpsys", "notification", "--noredact"], capture_output=True).stdout.decode("utf-8", "replace")
    found = []
    for block in re.split(r"\n\s*NotificationRecord\(", out)[1:]:
        if f"pkg={PKG}" not in block.split("\n", 1)[0]:
            continue
        fields = {}
        for name in ("title", "text", "bigText", "subText"):
            m = re.search(rf"android\.{name}=\w+ \((.*?)\)\s*$", block, re.MULTILINE)
            if m:
                fields[name] = m.group(1)
        head = block.split("\n", 1)[0]
        nid = re.search(r"id=(\d+)", head)
        found.append((nid.group(1) if nid else "?", fields))
    return found


def watch_events():
    proc = subprocess.Popen([ADB, "logcat", "-b", "events", "-T", "1", "-v", "brief"], stdout=subprocess.PIPE)
    for raw in proc.stdout:
        line = raw.decode("utf-8", "replace")
        if PKG not in line or not re.search(r"notification_(enqueue|canceled|cancel_all)", line):
            continue
        kind = re.search(r"notification_\w+", line).group(0)
        if kind == "notification_enqueue":
            flags = "GROUP_SUMMARY" if "GROUP_SUMMARY" in line else ""
            time.sleep(0.5)
            say(f"[알림 도착] {flags} " + " | ".join(f"id={i} {f}" for i, f in somoim_notifications()))
        else:
            say(f"[알림 제거] {kind}")


def watch_chat():
    dev = AndroidDevice(scale=0.5)
    chat = SomoimChat(dev, MOIM_NAMES, log=lambda m: None)
    last, was_open = None, None
    while True:
        try:
            root = chat.ui.dump()
            is_open = chat.is_open(root)
            if is_open != was_open:
                say(f"[화면] 채팅 화면 {'열림' if is_open else '아님'}")
                was_open = is_open
            if is_open:
                page, _ = chat._page()  # no scrolling: does the list update by itself?
                newest = (page[-1]["sender"], page[-1]["text"], page[-1]["time"]) if page else None
                if last is not None and newest != last:
                    say(f"[채팅 화면 갱신] 최신 메시지: {newest}")
                last = newest
        except Exception as e:
            say(f"[chat watch error] {e}")
        time.sleep(4)


threading.Thread(target=watch_events, daemon=True).start()
say("listening")
watch_chat()
