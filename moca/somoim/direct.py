"""1:1 messages: find a member through the 모임 member list and open a direct conversation.

Only the 모임장 and 운영진 can start a 1:1 conversation; 모카's account is 운영진.
"""
import time

from somoim.chat import ChatScreen
from somoim.ui import bounds, by_text, first_id, rid


def open_inbox(moim_chat):
    """내모임 → the 1:1 메시지 inbox (the icon next to the notification bell). Returns the UI root or None."""
    ui = moim_chat.ui
    if not moim_chat.park():
        return None
    button = first_id(ui.dump(), "privatechat_notification_btn_layout")
    if button is None:
        return None
    ui.tap(button)
    time.sleep(3)
    root = ui.dump()
    return root if any(n.get("text") == "1:1 메시지" for n in root.iter("node")) else None


def inbox_rows(moim_chat):
    """Conversations in the 1:1 inbox, newest first: [{"member", "time", "preview", "node"}], or None if it can't be opened."""
    root = open_inbox(moim_chat)
    if root is None:
        return None
    rows, row = [], None
    for n in root.iter("node"):
        kind = rid(n)
        if kind == "name_text":
            row = {"member": n.get("text"), "time": None, "preview": None, "node": n}
            rows.append(row)
        elif row is not None and kind == "time_text":
            row["time"] = n.get("text")
        elif row is not None and kind == "content_text":
            row["preview"] = n.get("text")
    return rows


class DirectChat(ChatScreen):
    def __init__(self, moim_chat, member, log=print):
        super().__init__(moim_chat.dev, log)
        self.moim = moim_chat  # SomoimChat: navigation to the 모임
        self.member = member

    def is_open(self, root):
        # the toolbar title is the member's name; the 모임 chat has tabs, a 1:1 screen does not
        toolbar_title = [n for n in root.iter("node") if n.get("text") == self.member and not rid(n) and bounds(n)[3] <= 210]
        return (bool(toolbar_title) and first_id(root, "inputtext_edittext") is not None
                and not any(rid(n) == "text" for n in by_text(root, lambda t: t == "채팅")))

    def open(self, open_profile=None):
        """Open the conversation: from the 1:1 inbox if it already exists, otherwise through the member's profile.
        `open_profile` is a quicker way to reach the profile (e.g. from their post); the default searches the member list."""
        if self.is_open(self.ui.dump()):
            return True
        row = next((r for r in inbox_rows(self.moim) or [] if r["member"] == self.member), None)
        if row is not None:
            self.ui.tap(row["node"])
            time.sleep(3)
            if self.is_open(self.ui.dump()):
                return True
        if not (open_profile or self.open_profile)():
            return False
        plane = first_id(self.ui.dump(), "plus_btn_layout")  # the paper-plane icon on a member profile
        if plane is None:
            self.log(f"  {self.member} 프로필에 1:1 메시지 버튼이 없음")
            return False
        self.ui.tap(plane)
        time.sleep(3)
        return self.is_open(self.ui.dump())

    def open_profile(self):
        """모임 홈 → 모임 멤버 검색 → the member's profile."""
        root = self.moim.open_tab("홈")
        if root is None:
            return False
        for _ in range(6):
            box = first_id(root, "search_searchedit")
            if box is not None:
                break
            self.ui.swipe(self.dev.width // 2, 1800, 900)
            time.sleep(1.2)
            root = self.ui.dump()
        else:
            self.log("  모임 멤버 검색창을 찾지 못함")
            return False
        self.ui.tap(box)
        time.sleep(1.5)
        with self.dev.adb_keyboard():
            self.dev.clear_text()
            self.dev.type_text(self.member)
        time.sleep(2.5)
        root = self.ui.dump()
        hits = [n for n in root.iter("node") if rid(n) == "name_text" and n.get("text") == self.member]
        if len(hits) != 1:
            self.log(f"  멤버 검색 결과가 {len(hits)}명: '{self.member}'")  # 0 = not a member, 2+ = duplicate names
            return False
        self.ui.tap(hits[0])
        time.sleep(3)
        root = self.ui.dump()
        return any(rid(n) == "title_text" and n.get("text") == self.member for n in root.iter("node"))
