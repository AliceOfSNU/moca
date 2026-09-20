"""Ground-truth chat reader: scrolls the 소모임 chat with ADB and reads messages from the UI hierarchy.

Run with the 모임 chat tab open. Prints messages oldest -> newest as JSON.
"""
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "cua"))
from android import ADB  # noqa: E402

PKG = "com.friendscube.somoim:id/"


def adb(*args):
    return subprocess.run([ADB, *args], capture_output=True, check=True).stdout.decode("utf-8", "replace")


def bounds(n):
    x1, y1, x2, y2 = map(int, re.findall(r"\d+", n.get("bounds")))
    return x1, y1, x2, y2


def dump():
    adb("shell", "uiautomator", "dump", "/sdcard/ui.xml")
    return ET.fromstring(adb("exec-out", "cat", "/sdcard/ui.xml"))


def visible_messages(root):
    """Messages on screen, top -> bottom, as dicts {sender, text, time, mine}."""
    # configuration changes (e.g. font size) can leave stale copies of the list behind; the live one is last
    rv = [n for n in root.iter("node") if n.get("resource-id") == PKG + "main_recyclerview"][-1]
    out, last_sender = [], None
    for row in rv:  # each RecyclerView child is one row (message, date divider, ...)
        ids = {n.get("resource-id"): n for n in row.iter("node")}
        name, content, time = ids.get(PKG + "name_text"), ids.get(PKG + "content_text"), ids.get(PKG + "time_text")
        if content is None:
            continue
        mine = bounds(content)[0] > 300  # own bubbles are right-aligned
        if mine:
            sender = "(나)"
        elif name is not None:
            sender = last_sender = name.get("text")
        else:
            sender = last_sender or "?"  # grouped consecutive message without a name label
        out.append({"sender": sender, "text": content.get("text"), "time": time.get("text") if time is not None else None,
                    "mine": mine, "y": bounds(content)[1]})
    return out


def key(m):
    return (m["sender"], m["text"], m["time"])


def same(a, b):
    # rows cut off at the screen edge can lose their time label, so a missing time matches anything
    # likewise a grouped message whose sender label scrolled off comes back as "?"
    return ((a["sender"] == b["sender"] or "?" in (a["sender"], b["sender"])) and a["text"] == b["text"]
            and (a["time"] is None or b["time"] is None or a["time"] == b["time"]))


def pick(a, b):
    return {**a, "sender": a["sender"] if b["sender"] == "?" else b["sender"], "time": b["time"] or a["time"]}


def merge(acc, page):
    """Append the part of `page` that is new, using the longest suffix/prefix overlap."""
    for k in range(min(len(acc), len(page)), 0, -1):
        if all(same(a, b) for a, b in zip(acc[-k:], page[:k])):
            return acc[:-k] + [pick(a, b) for a, b in zip(acc[-k:], page[:k])] + page[k:]
    return acc + page


def swipe(y_from, y_to):
    adb("shell", "input", "swipe", "540", str(y_from), "540", str(y_to), "500")


def read_all():
    # scroll to the top until the screen stops changing
    prev = None
    while True:
        cur = [key(m) for m in visible_messages(dump())]
        if cur == prev:
            break
        prev = cur
        swipe(700, 2000)
    msgs, prev = [], None
    while True:
        page = visible_messages(dump())
        if [key(m) for m in page] == prev:
            break
        prev = [key(m) for m in page]
        msgs = merge(msgs, page)
        swipe(1900, 900)
    return [{k: v for k, v in m.items() if k != "y"} for m in msgs]


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(read_all(), ensure_ascii=False, indent=1))
