"""Wake-up signal from 소모임 push notifications.

Android writes a `notification_enqueue` line to the event log whenever an app posts a notification.
We stream that log over ADB and, for 소모임, read the notification's title/text from `dumpsys notification`.
What each kind of notification looks like is documented in run.classify.
"""
import queue
import re
import subprocess
import threading
import time

from somoim.ui import PKG



def current_notifications(dev):
    """소모임 notifications currently posted: [{"id", "title", "text", "subText"}]."""
    out = dev.adb("shell", "dumpsys", "notification", "--noredact")
    found = []
    for block in re.split(r"\n\s*NotificationRecord\(", out)[1:]:
        head = block.split("\n", 1)[0]
        if f"pkg={PKG}" not in head:
            continue
        when = re.search(r"when=(\d+)", block)
        n = {"id": (re.search(r"id=(\d+)", head) or [None, None])[1], "when": when.group(1) if when else None}
        for name in ("title", "text", "subText"):
            # values can span lines (e.g. "모임에 게시글을 작성했습니다.\n"), so match up to the next extra
            m = re.search(rf"android\.{name}=\w+ \((.*?)\)\s*(?=\n\s+(?:android\.|\}}))", block, re.DOTALL)
            n[name] = m.group(1).strip() if m else None
        if n["title"] or n["text"]:  # group summaries carry no content
            found.append(n)
    return found


class NotificationWatcher:
    """Background thread that puts 소모임 notifications on `self.events` as they are posted."""

    def __init__(self, dev, log=print):
        self.dev = dev
        self.log = log
        self.events = queue.Queue()
        # (id, when): notifications stay posted, and every new post re-lists them all. Ones already posted at
        # startup are covered by the startup cycle, which does every job once.
        self._seen = {(n["id"], n["when"]) for n in current_notifications(dev)}
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        from cua.android import ADB
        while True:
            try:
                proc = subprocess.Popen([ADB, "logcat", "-b", "events", "-T", "1"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                for raw in proc.stdout:
                    line = raw.decode("utf-8", "replace")
                    if "notification_enqueue" in line and PKG in line and "GROUP_SUMMARY" not in line:
                        time.sleep(0.5)  # let the record settle before reading its content
                        for n in current_notifications(self.dev):
                            if (n["id"], n["when"]) not in self._seen:
                                self._seen.add((n["id"], n["when"]))
                                self.events.put(n | {"at": time.time()})
                self.log("알림 로그 스트림이 끊김. 재연결합니다")
            except Exception as e:
                self.log(f"알림 감시 오류: {e}")
            time.sleep(5)

    def drain(self, before=None, where=None):
        """Take queued events posted before `before` (default: all) that match `where` (default: any),
        e.g. ones a read has already covered. Other events stay queued."""
        drained, keep = [], []
        while True:
            try:
                e = self.events.get_nowait()
            except queue.Empty:
                break
            taken = (before is None or e["at"] < before) and (where is None or where(e))
            (drained if taken else keep).append(e)
        for e in keep:
            self.events.put(e)
        return drained

    def wait(self, timeout):
        """Block until a notification arrives or `timeout` seconds pass. Returns the event or None."""
        try:
            return self.events.get(timeout=max(timeout, 0))
        except queue.Empty:
            return None
