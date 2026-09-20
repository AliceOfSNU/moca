"""Read and tap the 소모임 app through Android's UI hierarchy (uiautomator), without screenshots."""
import re
import subprocess
import time
import xml.etree.ElementTree as ET

PKG = "com.friendscube.somoim"
ID = PKG + ":id/"
DUMP_PATH = "/sdcard/moca_ui.xml"


def bounds(node):
    return tuple(map(int, re.findall(r"\d+", node.get("bounds"))))


def rid(node):
    return (node.get("resource-id") or "").replace(ID, "")


def by_id(root, name):
    return [n for n in root.iter("node") if n.get("resource-id") == ID + name]


def first_id(root, name):
    found = by_id(root, name)
    return found[0] if found else None


def by_text(root, pred):
    return [n for n in root.iter("node") if n.get("text") and pred(n.get("text"))]


class SomoimUI:
    # called after every successful read of the screen; the main loop uses it as 모카's heartbeat
    # (harness/presence.py), so a dead emulator counts as asleep even while the loop itself keeps running
    on_read = None

    def __init__(self, dev):
        self.dev = dev  # cua.android.AndroidDevice

    def dump(self, windows=False):
        """UI tree of the foreground app. `windows=True` also includes popup windows (e.g. the @mention list)."""
        args = ["shell", "uiautomator", "dump"] + (["--windows"] if windows else []) + [DUMP_PATH]
        for attempt in range(4):
            try:
                self.dev.adb(*args)
                root = ET.fromstring(self.dev.adb("exec-out", "cat", DUMP_PATH))
                if SomoimUI.on_read:
                    SomoimUI.on_read()
                return root
            except subprocess.TimeoutExpired:
                # uiautomator can hang (the emulator stalls under memory pressure); clear it and try again
                try:
                    self.dev.adb("shell", "pkill -f uiautomator || true")
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    pass
                time.sleep(2 + attempt)
            except (subprocess.CalledProcessError, ET.ParseError):
                # uiautomator fails while the screen is animating; retry shortly
                time.sleep(1 + attempt)
        raise RuntimeError("uiautomator dump failed")

    def wait_for(self, predicate, timeout=10, interval=1.5, windows=False):
        """Dump repeatedly until `predicate(root)` holds. Returns that root, or None after `timeout` seconds."""
        deadline = time.time() + timeout
        while True:
            root = self.dump(windows=windows)
            if predicate(root):
                return root
            if time.time() >= deadline:
                return None
            time.sleep(interval)

    def foreground_package(self, root):
        node = root.find("node")
        return node.get("package") if node is not None else None

    def tap(self, node, dx=0, dy=0):
        x1, y1, x2, y2 = bounds(node)
        self.dev.adb("shell", "input", "tap", str((x1 + x2) // 2 + dx), str((y1 + y2) // 2 + dy))

    def swipe(self, x, y1, y2, ms=700):
        self.dev.adb("shell", "input", "swipe", str(x), str(y1), str(x), str(y2), str(ms))

    def back(self):
        self.dev.key(4)

    def launch(self):
        self.dev.adb("shell", "monkey", "-p", PKG, "-c", "android.intent.category.LAUNCHER", "1")
