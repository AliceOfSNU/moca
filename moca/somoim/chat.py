"""The 모임 chat screen: open it, read messages newer than the last read position, send a message.

Messages are dicts:
    {"sender": "로하", "mine": False, "time": "오후 6:42", "day": "2026년 9월 17일 목요일" | None,
     "text": "...", "reply_to": {"name": "MOCA에게 답장", "text": "..."} (optional),
     "photo": True (optional), "_crop": PNG bytes of a fully visible photo (optional, in-memory only)}
"""
import io
import time

from PIL import Image

from somoim.ui import ID, PKG, SomoimUI, bounds, by_id, by_text, first_id, rid

MINE = "(나)"
# the chat input box stops accepting text at 500 UTF-16 code units (an emoji like 📁 counts as 2)
MESSAGE_LIMIT = 500


def units(text):
    return len(text.encode("utf-16-le")) // 2


def split_message(text, limit=MESSAGE_LIMIT):
    """Split text into messages that fit the input box: at blank lines, then line breaks, then mid-line."""
    text = text.strip()
    if units(text) <= limit:
        return [text]
    parts, current = [], ""
    for sep, pieces in (("\n\n", text.split("\n\n")),):
        for piece in pieces:
            candidate = f"{current}{sep}{piece}" if current else piece
            if units(candidate) <= limit:
                current = candidate
                continue
            if current:
                parts.append(current)
            if units(piece) <= limit:
                current = piece
                continue
            # one paragraph is too long by itself: fall back to lines, then characters
            current = ""
            for line in piece.split("\n"):
                candidate = f"{current}\n{line}" if current else line
                if units(candidate) <= limit:
                    current = candidate
                    continue
                if current:
                    parts.append(current)
                current = ""
                for ch in line:
                    if units(current + ch) > limit:
                        parts.append(current)
                        current = ""
                    current += ch
    if current:
        parts.append(current)
    return [p.strip() for p in parts if p.strip()]
UNKNOWN = "?"
POPUP_TITLE = "Popup Window"


# --- message identity & merging -------------------------------------------------

def key(m):
    return {"sender": m["sender"], "text": m["text"], "time": m["time"], "photo": bool(m.get("photo"))}


def same(a, b):
    """Same message? Rows cut off at the screen edge can lose their time or sender label, so those match anything."""
    return (a["text"] == b["text"] and bool(a.get("photo")) == bool(b.get("photo"))
            and (a["sender"] == b["sender"] or UNKNOWN in (a["sender"], b["sender"]))
            and (a["time"] is None or b["time"] is None or a["time"] == b["time"]))


def combine(a, b):
    """Merge two sightings of the same message, keeping whichever has each detail."""
    out = {**a, **{k: v for k, v in b.items() if v not in (None, "")}}
    out["sender"] = a["sender"] if b["sender"] == UNKNOWN else b["sender"]
    return out


def merge(older, newer):
    """Join two overlapping lists (oldest -> newest) on their longest suffix/prefix overlap.
    Returns (merged, overlapped)."""
    for k in range(min(len(older), len(newer)), 0, -1):
        if all(same(a, b) for a, b in zip(older[-k:], newer[:k])):
            return older[:-k] + [combine(a, b) for a, b in zip(older[-k:], newer[:k])] + newer[k:], True
    return older + newer, False


def find_anchor(msgs, anchor):
    """Index in `msgs` of the last message read previously, or None.
    `anchor` holds keys of the last few messages read (oldest -> newest). The newest anchor message is
    preferred, with the message before it as context; older anchor messages cover the newest one being deleted."""
    for s in range(len(anchor)):
        target = anchor[len(anchor) - 1 - s]
        prev = anchor[len(anchor) - 2 - s] if len(anchor) - 2 - s >= 0 else None
        for i in range(len(msgs) - 1, -1, -1):
            if same(msgs[i], target) and (prev is None or i == 0 or same(msgs[i - 1], prev)):
                return i
    return None


# --- the chat screen -------------------------------------------------------------

class ChatScreen:
    """Reading and sending on any 소모임 chat screen (the 모임 chat and 1:1 messages share the same layout).
    Subclasses provide `open()` and `is_open(root)`."""

    def __init__(self, dev, log=print):
        self.dev = dev
        self.ui = SomoimUI(dev)
        self.log = log

    # reading ---------------------------------------------------------------------

    def _list(self, root):
        # configuration changes can leave stale copies of the list behind; the live one is last
        return by_id(root, "main_recyclerview")[-1]

    def _page(self):
        """Messages currently on screen, top -> bottom."""
        root = self.ui.dump()
        rv = self._list(root)
        top, bottom = bounds(rv)[1], bounds(rv)[3]
        msgs, last_sender, need_shot = [], None, False
        for row in rv:
            nodes = {}
            for n in row.iter("node"):
                nodes.setdefault(rid(n), n)
            content, photo = nodes.get("content_text"), nodes.get("photos_container_layout")
            if content is None and photo is None:
                continue  # divider-only or a row whose body is off screen
            # (Elements without children are falsy, so no `a or b` here)
            main = next(n for n in (nodes.get("bubble_container"), content, photo) if n is not None)
            mine = bounds(main)[0] > self.dev.width * 0.25  # own bubbles are right-aligned
            name = nodes.get("name_text")
            if mine:
                sender = MINE
            elif name is not None:
                sender = last_sender = name.get("text")
            else:
                sender = last_sender or UNKNOWN  # grouped consecutive message without a name label
            time_node, day = nodes.get("time_text"), nodes.get("day_text")
            m = {"sender": sender, "mine": mine, "time": time_node.get("text") if time_node is not None else None,
                 "day": day.get("text") if day is not None else None,
                 "text": content.get("text") if content is not None else ""}
            if nodes.get("reply_target_name3") is not None:
                target = nodes.get("reply_target_message3")
                m["reply_to"] = {"name": nodes["reply_target_name3"].get("text"),
                                 "text": target.get("text") if target is not None else ""}
            if photo is not None:
                m["photo"] = True
                pb = bounds(nodes["picture_image1"] if nodes.get("picture_image1") is not None else photo)
                if pb[1] > top and pb[3] < bottom:
                    m["_photo_bounds"] = pb
                    need_shot = True
            msgs.append(m)
        if need_shot:
            png = self.dev.adb("exec-out", "screencap", "-p", binary=True)
            img = Image.open(io.BytesIO(png)).convert("RGB")
            for m in msgs:
                if "_photo_bounds" in m:
                    buf = io.BytesIO()
                    img.crop(m.pop("_photo_bounds")).save(buf, "PNG")
                    m["_crop"] = buf.getvalue()
        return msgs, (top, bottom)

    def _scroll(self, span, older):
        top, bottom = span
        a, b = top + int((bottom - top) * 0.3), top + int((bottom - top) * 0.7)  # 40% of the list per step
        self.ui.swipe(self.dev.width // 2, a if older else b, b if older else a)
        time.sleep(0.8)

    def scroll_to_bottom(self, max_swipes=30):
        prev = None
        for _ in range(max_swipes):
            page, span = self._page()
            keys = [key(m) for m in page]
            if keys == prev:
                return page, span
            prev = keys
            self._scroll(span, older=False)
        return page, span

    def newest(self):
        """Newest message on screen after scrolling to the bottom (cheap change check)."""
        page, _ = self.scroll_to_bottom()
        return page[-1] if page else None

    def read_since(self, anchor, backlog=30, max_pages=40):
        """Scroll back from the newest message to the previous read position.
        Returns (messages after the anchor oldest -> newest, anchor_found).
        Without an anchor (first run), returns up to `backlog` most recent messages."""
        acc, span = self.scroll_to_bottom()
        for _ in range(max_pages):
            if anchor:
                i = find_anchor(acc, anchor)
                if i is not None:
                    return self._fill(acc[i + 1:], acc[: i + 1]), True
            elif len(acc) >= backlog:
                break
            self._scroll(span, older=True)
            page, span = self._page()
            merged, overlapped = merge(page, acc)
            at_top = [key(m) for m in merged] == [key(m) for m in acc]
            acc = merged  # even at the top: the last page can still add details such as the date divider
            if at_top:
                break
            if not overlapped:
                self.log("  경고: 스크롤 전후 화면이 겹치지 않음 (메시지 누락 가능)")
        return self._fill(acc[-backlog:] if not anchor else acc, []), False

    def _fill(self, msgs, before):
        """Resolve grouped senders and carry the date divider forward."""
        prev_sender = next((m["sender"] for m in reversed(before) if not m["mine"]), None)
        day = next((m["day"] for m in reversed(before) if m.get("day")), None)
        for m in msgs:
            if m["sender"] == UNKNOWN and prev_sender:
                m["sender"] = prev_sender
            if not m["mine"]:
                prev_sender = m["sender"]
            day = m.get("day") or day
            m["day"] = day
        return msgs

    # sending ---------------------------------------------------------------------

    def _popup(self, root):
        return next((w for w in root.iter("window") if w.get("title") == POPUP_TITLE), None)

    def _input(self, root):
        return first_id(root, "inputtext_edittext")

    def _mention(self, name):
        """Type '@' and pick `name` from the member list. Returns True if a real tag was inserted."""
        self.dev.type_text("@")
        time.sleep(1.5)
        self.dev.type_text(name)
        for _ in range(3):
            time.sleep(1.5)
            popup = self._popup(self.ui.dump(windows=True))
            hits = [n for n in popup.iter("node") if rid(n) == "name_text" and n.get("text") == name] if popup is not None else []
            if hits:
                self.ui.tap(hits[0], dx=150)
                time.sleep(1.5)
                return True
        return False

    def send(self, text, mention=None, dry_run=False):
        """Send `text` (optionally tagging `mention` first), split into several messages if it's longer than one
        message can be. Returns the text as it should appear in the chat, or None if any part could not be verified.
        `dry_run` fills and checks the input box, then clears it."""
        parts = split_message(text, MESSAGE_LIMIT - (units(f"@{mention} ") if mention else 0))
        sent = []
        for i, part in enumerate(parts):
            result = self._send_one(part, mention if i == 0 else None, dry_run)
            if result is None:
                return None
            sent.append(result)
        return "\n".join(sent)

    def _send_one(self, text, mention=None, dry_run=False):
        before = self._page()[0]  # newest messages before this send (checked against after sending)
        root = self.ui.dump()
        box = self._input(root)
        if box is None:
            return None
        self.ui.tap(box)
        time.sleep(1)
        prefix = ""
        with self.dev.adb_keyboard():  # one keyboard for the whole sequence, or the member list closes
            self.dev.clear_text()
            if mention:
                if self._mention(mention):
                    prefix = f"@{mention} "
                else:
                    self.log(f"  멘션 목록에서 '{mention}'을 찾지 못해 멘션 없이 보냅니다")
                    self.dev.clear_text()
            self.dev.type_text(text)
            time.sleep(1)
            root = self.ui.dump(windows=True)
            if self._popup(root) is not None:
                self.ui.back()  # close the member list if it is still open
                time.sleep(1)
                root = self.ui.dump(windows=True)
            expected = prefix + text
            box = self._input(root)
            if box is None or box.get("text").strip() != expected.strip():
                self.log(f"  입력 확인 실패: {box.get('text') if box is not None else None!r}")
                self.dev.clear_text()
                return None
            if dry_run:
                self.log(f"  (전송 직전 확인만) 입력칸: {expected!r}")
                self.dev.clear_text()
                return expected
            # tap 전송 while ADBKeyBoard is still the input method: it shows no on-screen keyboard, so the screen
            # is laid out exactly as in `root`. Once this block ends the normal keyboard comes back and pushes the
            # input bar up, and a tap at the old position lands on its Enter key (a newline, nothing sent).
            self.ui.tap(first_id(root, "inputtext_sendbutton"))
        time.sleep(2.5)
        after, _ = self.scroll_to_bottom()
        # the other person may answer within seconds, so our message need not be the newest one — only newer
        # than what was there before (an identical older message, e.g. a repeated reminder, doesn't count)
        start = next((i + 1 for i in range(len(after) - 1, -1, -1) if before and same(after[i], before[-1])), None)
        fresh = after[start:] if start is not None else after[-5:]
        if any(m["mine"] and m["text"].strip() == expected.strip() for m in fresh):
            return expected
        self.log("  전송 확인 실패: 최신 메시지가 보낸 내용과 다름")
        return None


class SomoimChat(ChatScreen):
    """The 모임 group chat."""

    def __init__(self, dev, moim_names, log=print):
        super().__init__(dev, log)
        # all names the 모임 has had: after a rename, some screens keep showing the old one for a while
        self.moim_names = [moim_names] if isinstance(moim_names, str) else list(moim_names)

    # navigation ------------------------------------------------------------------

    def _title_matches(self, text):
        strip = lambda s: s.replace("️", "")  # emoji variation selectors differ between screens
        text = strip(text).rstrip("…").rstrip(".")  # long titles are ellipsized
        return len(text) >= 8 and any(strip(name).startswith(text) for name in self.moim_names)

    def is_open(self, root):
        # a 1:1 message screen also shows the 모임 name (as its header row), but has no 홈/게시판/사진첩/채팅 tabs
        return (first_id(root, "inputtext_edittext") is not None and bool(by_id(root, "main_recyclerview"))
                and any(rid(n) == "text" for n in by_text(root, lambda t: t == "채팅"))
                and any(self._title_matches(n.get("text")) for n in by_text(root, lambda t: True)))

    def open_tab(self, name):
        """Open one of the 모임's tabs (홈/게시판/사진첩/채팅). Returns the UI root once it's shown."""
        root = self.ui.dump()
        # the 모임's own tab row, not just its name somewhere on screen (the app's 홈 lists 정모 chats by 모임 name)
        tabs = {n.get("text") for n in root.iter("node") if rid(n) == "text"} & {"홈", "게시판", "사진첩", "채팅"}
        on_moim = len(tabs) >= 3 and any(self._title_matches(n.get("text")) for n in by_text(root, lambda t: True))
        tab = [n for n in by_text(root, lambda t: t == name) if rid(n) == "text"] if on_moim else []
        if not tab:  # not on one of this 모임's tabs yet: go through the chat tab, reachable from anywhere
            if not self.open():
                return None
            tab = [n for n in by_text(self.ui.dump(), lambda t: t == name) if rid(n) == "text"]
        if not tab:
            return None
        self.ui.tap(tab[0])
        time.sleep(2.5)
        return self.ui.dump()

    def moim_rows(self, root):
        """Nodes labelled with the 모임 name that actually open the 모임.

        The name is `groupname_text` in recommendation lists and `name_text` in the 가입한 모임 list. It also
        labels the cards of the "참여중인 정모 채팅" carousel — 소모임 gives every 정모 its own chat room for
        that event's attendees, and those cards open the room, not the 모임, so skip the carousel. They only
        showed up once 모카 started attending 정모, which is why navigation broke then.
        """
        parents = {child: node for node in root.iter("node") for child in node}
        def in_carousel(node):
            while node is not None:
                if rid(node) == "horizontal_recyclerview":
                    return True
                node = parents.get(node)
            return False
        on_my_moim_list = bool(by_text(root, lambda t: t == "가입한 모임"))
        return [n for n in by_text(root, self._title_matches)
                if (rid(n) == "groupname_text" or (rid(n) == "name_text" and on_my_moim_list))
                and not in_carousel(n)]

    def open(self):
        """Navigate to the 모임 chat using the UI tree. Returns True on success."""
        self.dev.wake()
        for _ in range(8):
            root = self.ui.dump()
            if self.is_open(root):
                return True
            if self.ui.foreground_package(root) != PKG:
                self.log("  소모임 앱 실행")
                self.ui.launch()
                time.sleep(6)
                continue
            chat_tab = [n for n in by_text(root, lambda t: t == "채팅") if rid(n) == "text"]
            my_tab = [n for n in by_text(root, lambda t: t == "내모임") if rid(n) == "tab_text"]
            moim = self.moim_rows(root)
            if chat_tab and any(self._title_matches(n.get("text")) for n in by_text(root, lambda t: True)):
                self.ui.tap(chat_tab[0])
            elif moim:
                self.ui.tap(moim[0])
            elif my_tab:
                self.ui.tap(my_tab[0])
            else:
                self.ui.back()
            time.sleep(2.5)
        return False

    def park(self):
        """Leave the chat for the 내모임 list. While the chat is open, 소모임 posts no notifications for it."""
        for _ in range(6):
            root = self.ui.dump()
            if self.ui.foreground_package(root) != PKG:
                self.ui.launch()
                time.sleep(6)
                continue
            my_tab = [n for n in by_text(root, lambda t: t == "내모임") if rid(n) == "tab_text"]
            if my_tab and by_text(root, lambda t: t == "가입한 모임"):
                return True
            if my_tab:
                self.ui.tap(my_tab[0])
            else:
                self.ui.back()
            time.sleep(2.5)
        return False

    def preview(self):
        """(text, time) of the latest chat message as previewed in the 내모임 list, or None if not on that screen."""
        root = self.ui.dump()
        nodes = list(root.iter("node"))
        rows = self.moim_rows(root)
        for i, n in enumerate(nodes):
            if n in rows and rid(n) == "name_text":
                row = {}
                for m in nodes[i + 1:]:
                    if rid(m) == "name_text":
                        break  # next 모임 row
                    row.setdefault(rid(m), m.get("text"))
                return row.get("content_text"), row.get("time_text")
        return None
