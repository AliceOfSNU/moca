"""투표 (polls) on the 게시판: create, list, read results, close and delete.

What the app allows, checked against 소모임 5.8.2:
- A vote is a board post of category 투표: a title, 2+ options, an optional end time, 복수선택 and 익명투표.
  There is no body text.
- Votes only show up under the 투표 filter, and that filter button sits off the right edge of the bar,
  so the bar has to be scrolled first.
- While a vote runs, the counts are only on the participants screen ("N명 참여"), which also lists who voted
  for what and, on its second tab, who hasn't voted at all. Once closed, the counts show on the vote itself.
- 투표 종료 (close) is on the vote; 삭제 (delete) is only in the long-press menu of the list card, together
  with 상위고정. The post menu (⋮) has neither.
"""
import datetime as dt
import time

from somoim.board import SomoimBoard, titles_match
from somoim.events import SomoimEvents  # the date and time dialogs are the ones 정모 uses
from somoim.ui import bounds, by_text, first_id, rid

MIN_OPTIONS, MAX_OPTIONS = 2, 10
TITLE_LIMIT, OPTION_LIMIT = 60, 20  # an option field holds 20 characters (measured 2026-09-22)


def _text(node):
    return (node.get("text") or "").strip() if node is not None else None


class SomoimVotes:
    def __init__(self, chat):
        self.chat = chat
        self.dev = chat.dev
        self.ui = chat.ui
        self.log = chat.log
        self.board = SomoimBoard(chat)
        self.pickers = SomoimEvents(chat)  # only for its date/time dialog handling

    # --- the list -----------------------------------------------------------------------

    def open_list(self):
        """게시판 → 투표. The filter bar scrolls; 투표 is the last chip and starts off-screen."""
        if not self.board.open():
            self.log("게시판을 열 수 없음")
            return False
        for _ in range(4):
            chips = [n for n in by_text(self.ui.dump(), lambda t: t == "투표") if rid(n) == "button"]
            if chips and bounds(chips[0])[2] - bounds(chips[0])[0] > 100:  # fully on screen, so tappable
                self.ui.tap(chips[0])
                time.sleep(2.5)
                return True
            self.dev.adb("shell", "input", "swipe", "900", "432", "300", "432", "400")
            time.sleep(1.2)
        self.log("'투표' 필터를 찾지 못함")
        return False

    @staticmethod
    def _cards(root):
        """Vote cards on screen: {"author", "posted", "title", "status", "when", "node"}."""
        cards, card = [], None
        for n in root.iter("node"):
            kind = rid(n)
            if kind == "name_text":
                card = {"author": _text(n), "posted": None, "title": None, "status": None, "when": None, "node": None}
            elif card is None:
                continue
            elif kind == "time_text":
                card["posted"] = _text(n)
            elif kind == "vote_text":
                card["title"], card["node"] = _text(n), n
            elif kind == "vote_text2":
                card["status"] = _text(n)           # "1명 참여 • 미참여"
            elif kind == "vote_text3":
                card["when"] = _text(n)             # "2026.9.22 (화) 오전 0:30 종료예정" / "종료됨"
                if card["title"]:
                    cards.append(card)
                card = None
        return cards

    def list_votes(self, max_pages=15):
        """Every vote, newest first, or None if the board couldn't be opened."""
        if not self.open_list():
            return None
        votes, prev = {}, None
        for _ in range(max_pages):
            page = self._cards(self.ui.dump())
            keys = [c["title"] for c in page]
            if keys == prev:
                break
            prev = keys
            for c in page:
                votes.setdefault(c["title"], {k: v for k, v in c.items() if k != "node"})
            self.ui.swipe(self.dev.width // 2, 1900, 900)
            time.sleep(1.2)
        return list(votes.values())

    def _find_card(self, title, max_pages=15):
        """Scroll the 투표 list until the vote is on screen. Returns its card (with node) or None."""
        if not self.open_list():
            return None
        prev = None
        for _ in range(max_pages):
            page = self._cards(self.ui.dump())
            for c in page:
                if titles_match(c["title"], title) or titles_match(title, c["title"]):
                    return c
            keys = [c["title"] for c in page]
            if keys == prev:
                return None
            prev = keys
            self.ui.swipe(self.dev.width // 2, 1900, 900)
            time.sleep(1.2)
        return None

    def open_vote(self, title):
        """Open one vote. Returns its card, or None if it isn't there."""
        card = self._find_card(title)
        if card is None:
            self.log(f"'{title}' 투표를 찾지 못함")
            return None
        self.ui.tap(card["node"])
        if self.ui.wait_for(lambda r: first_id(r, "vote_name_text") is not None, timeout=10) is None:
            self.log(f"'{title}' 투표 화면이 열리지 않음")
            return None
        return card

    # --- reading ------------------------------------------------------------------------

    def read(self, title):
        """The vote with its results: {"title", "author", "when", "left", "closed", "participants",
        "selections", "options": [{"name", "votes", "voters"}], "not_voted": [names]}.
        With 복수선택 one member can pick several options, so `selections` >= `participants`.
        `voters` stays empty for an 익명투표."""
        card = self.open_vote(title)
        if card is None:
            return None
        root = self.ui.dump()
        # the option names always live on the vote itself; the counts come from the participants screen
        vote = {"title": _text(first_id(root, "vote_name_text")), "author": card["author"], "posted": card["posted"],
                "when": card["when"], "left": _text(first_id(root, "vote_name_text2")),
                "closed": first_id(root, "close_vote_button") is None,
                "options": [{"name": _text(n), "votes": 0, "voters": []}
                            for n in root.iter("node") if rid(n) == "vote_itemname_text"],
                "participants": int("".join(c for c in (card["status"] or "") if c.isdigit()) or 0),
                "selections": 0, "not_voted": []}
        bottom = first_id(root, "vote_bottom_layout")
        if bottom is None:
            return vote
        self.ui.tap(bottom)
        time.sleep(2.5)
        counted, vote["selections"] = self._results(self.ui.dump())  # with 복수선택 one member can pick several
        by_name = {o["name"]: o for o in counted}
        for option in vote["options"]:
            option.update(by_name.get(option["name"], {}))
        tab = next((n for n in by_text(self.ui.dump(), lambda t: t == "미참여")), None)
        if tab is not None:
            self.ui.tap(tab)
            time.sleep(2)
            vote["not_voted"] = [_text(n) for n in self.ui.dump().iter("node") if rid(n) == "name_text"]
        self.ui.back()
        time.sleep(1.5)
        return vote

    @staticmethod
    def _results(root):
        """Parse the 항목별 tab: each option with its count and the members who chose it."""
        options, current, voted = [], None, 0
        for n in root.iter("node"):
            kind = rid(n)
            if kind == "item_name_text":
                current = {"name": (_text(n) or "").rstrip(":"), "votes": 0, "voters": []}
                options.append(current)
            elif current is None:
                continue
            elif kind == "item_voter_count":
                current["votes"] = int("".join(c for c in _text(n) if c.isdigit()) or 0)
                voted += current["votes"]
            elif kind == "name_text":
                current["voters"].append(_text(n))
        return options, voted

    # --- writing ------------------------------------------------------------------------

    def create(self, title, options, ends_at=None, multi=False, anonymous=False):
        """Post a new vote. `ends_at` is a datetime (the app defaults to two days out at 00:30)."""
        # ADBKeyBoard (no on-screen keys) is held for the whole form. Switching back to the normal keyboard
        # between steps pops it up over the focused field, and the scroll swipes that follow land on its keys
        # and type into the form instead of scrolling it.
        with self.dev.adb_keyboard():
            return self._create(title, options, ends_at, multi, anonymous)

    def _create(self, title, options, ends_at, multi, anonymous):
        title = (title or "").strip()
        options = [o.strip() for o in options if o and o.strip()]
        if not title or len(title) > TITLE_LIMIT:
            self.log(f"투표 제목은 1~{TITLE_LIMIT}자여야 함")
            return False
        if not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
            self.log(f"항목은 {MIN_OPTIONS}~{MAX_OPTIONS}개여야 함")
            return False
        if not self.board.open():
            return False
        self.ui.tap(first_id(self.ui.dump(), "fab"))
        time.sleep(2.5)
        category = next((n for n in self.ui.dump(windows=True).iter("node")
                         if n.get("resource-id") == "android:id/title" and _text(n) == "투표"), None)
        if category is None:
            self.log("'투표' 카테고리를 찾지 못함")
            self.ui.back()
            return False
        self.ui.tap(category)
        if self.ui.wait_for(lambda r: first_id(r, "vote_name_edit") is not None, timeout=10) is None:
            self.log("투표 작성 화면이 열리지 않음")
            return False

        with self.dev.adb_keyboard():
            self._type(first_id(self.ui.dump(), "vote_name_edit"), title)
            for i, option in enumerate(options):
                fields = [n for n in self.ui.dump().iter("node") if rid(n) == "item_name_edit"]
                while i >= len(fields):  # the form starts with three; "항목 추가" adds one at a time
                    plus = first_id(self.ui.dump(), "vote_item_name_plus_layout")
                    if plus is None:
                        self.log("'항목 추가'를 찾지 못함")
                        return False
                    self.ui.tap(plus)
                    time.sleep(1.2)
                    fields = [n for n in self.ui.dump().iter("node") if rid(n) == "item_name_edit"]
                self._type(fields[i], option)
        if ends_at and not self._set_end(ends_at):
            return False
        for flag, node_id in ((multi, "multi_select_check_button"), (anonymous, "use_anon_check_button")):
            if not flag:
                continue
            node = self._scroll_to(node_id)
            if node is None:
                self.log(f"{node_id}를 찾지 못함")
                return False
            self.ui.tap(node)
            time.sleep(0.8)

        done = next((n for n in self.ui.dump().iter("node") if "완료" in (n.get("text") or "")), None)
        if done is None:
            self.log("'완료' 버튼을 찾지 못함")
            return False
        self.ui.tap(done)
        time.sleep(3.5)
        found = self._find_card(title) is not None
        self.log(f"투표 만듦: {title}" if found else f"투표 게시 확인 실패: {title}")
        return found

    def _scroll_to(self, field, tries=6):
        """Bring a form field into view: typing and the date dialogs leave it scrolled away."""
        for _ in range(tries):
            node = first_id(self.ui.dump(), field)
            if node is not None:
                return node
            self.ui.swipe(self.dev.width // 2, 1700, 900)
            time.sleep(1.0)
        return None

    def _type(self, field, text):
        if field is None:
            return False
        self.ui.tap(field)
        time.sleep(0.7)
        self.dev.clear_text()
        self.dev.type_text(text[:OPTION_LIMIT])
        time.sleep(0.7)
        return True

    def _set_end(self, ends_at):
        """Set the end date and time through the same dialogs 정모 uses."""
        date_node = self._scroll_to("vote_endtime_text")
        if date_node is None:
            self.log("종료시간 칸을 찾지 못함")
            return False
        self.ui.tap(date_node)
        time.sleep(2)
        if not self.pickers._pick_date(ends_at.date()):
            self.log("종료 날짜를 고르지 못함")
            return False
        time.sleep(1.5)
        # the app opens the time dialog straight after the date; only tap the time field if it didn't
        if not self.pickers._time_dialog_open():
            time_node = self._scroll_to("vote_endtime_text2")
            if time_node is None:
                self.log("종료 시각 칸을 찾지 못함")
                return False
            self.ui.tap(time_node)
        time.sleep(2)  # the dialog animates in; typing into it too early fails
        if not self.pickers._pick_time(ends_at.hour, ends_at.minute):
            self.log("종료 시각을 고르지 못함")
            return False
        return True

    def close(self, title):
        """End a running vote early (투표 종료). Only the author sees the button."""
        if self.open_vote(title) is None:
            return False
        button = first_id(self.ui.dump(), "close_vote_button")
        if button is None:
            self.log(f"'{title}'은 이미 종료됐거나 종료 버튼이 없음")
            return False
        self.ui.tap(button)
        time.sleep(1.5)
        if not self._confirm("투표를 종료"):
            return False
        time.sleep(2.5)
        closed = "종료됨" in (_text(first_id(self.ui.dump(), "vote_name_text2")) or "")
        self.log(f"투표 종료: {title}" if closed else f"투표 종료 확인 실패: {title}")
        return closed

    def _long_press(self, title, item_text):
        """The list card's long-press menu (상위고정 / 모임 채팅방에 공유하기 / 삭제). True if the item was tapped."""
        card = self._find_card(title)
        if card is None:
            self.log(f"'{title}' 투표를 찾지 못함")
            return False
        x1, y1, x2, y2 = bounds(card["node"])
        self.dev.adb("shell", "input", "swipe", str((x1 + x2) // 2), str((y1 + y2) // 2),
                     str((x1 + x2) // 2), str((y1 + y2) // 2), "1200")
        time.sleep(2.5)
        item = next((n for n in self.ui.dump(windows=True).iter("node") if _text(n) == item_text), None)
        if item is None:
            self.log(f"'{item_text}' 메뉴를 찾지 못함")
            self.ui.back()
            return False
        self.ui.tap(item)
        time.sleep(1.5)
        return True

    def share_to_chat(self, title):
        """Share the vote to the 모임 채팅방 so everyone (채팅 모카 included) sees it there."""
        if not self._long_press(title, "모임 채팅방에 공유하기"):
            return False
        dialog = any(n.get("resource-id") == "android:id/button1" for n in self.ui.dump(windows=True).iter("node"))
        if dialog and not self._confirm():  # the app asks before sharing; older builds just share
            return False
        time.sleep(3)
        self.log(f"투표를 모임 채팅방에 공유: {title}")
        return True

    def delete(self, title):
        """Delete a vote post: long-press its card in the list, then 삭제."""
        if not self._long_press(title, "삭제"):
            return False
        if not self._confirm("삭제"):
            return False
        time.sleep(3)
        gone = self._find_card(title) is None
        self.log(f"투표 삭제: {title}" if gone else f"투표 삭제 확인 실패: {title}")
        return gone

    def _confirm(self, expect=""):
        """Tap 확인 on the app's yes/no dialog, checking the message is the expected one."""
        root = self.ui.dump(windows=True)
        message = next((n for n in root.iter("node") if n.get("resource-id") == "android:id/message"), None)
        if expect and expect not in (_text(message) or ""):
            self.log(f"예상과 다른 확인창: {_text(message)!r}")
            return False
        ok = next((n for n in root.iter("node") if n.get("resource-id") == "android:id/button1"), None)
        if ok is None:
            self.log("확인 버튼을 찾지 못함")
            return False
        self.ui.tap(ok)
        return True
