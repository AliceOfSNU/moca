"""The 모임 게시판: list, read and write posts through the UI tree."""
import time

from somoim.ui import ID, by_id, by_text, first_id, rid

# labels in the "게시글 카테고리" dialog shown after tapping 작성
CATEGORIES = ("자유 글", "관심사 공유", "모임후기", "가입인사", "공지사항(전체알림)", "투표")


def _normalize(text):
    return "\n".join(line.rstrip() for line in (text or "").replace("\r\n", "\n").strip().split("\n"))


def titles_match(listed, full):
    """A board list can cut a long title short without an ellipsis ("…가입" for "…가입안내")."""
    listed, full = (listed or "").rstrip("…").strip(), (full or "").strip()
    return bool(listed) and bool(full) and (full.startswith(listed) or listed.startswith(full))


def body_text(root):
    """Text of an open post's body, or None if it hasn't rendered yet. Bodies come in two shapes:
    an `editor` node holding the whole text, or an `editor`/`rich_editor` container whose children are
    the text pieces (Image nodes for pictures are skipped). Pieces are usually one line each — a filled-in
    가입인사 form arrives as "이름: …", "별칭: …" — with or without "\\n" views between them, so they are
    joined line by line; gluing them together once saved "이름: 홍길동별칭: 길동…"."""
    container = next((n for n in root.iter("node")
                      if n.get("resource-id") in ("editor", ID + "rich_editor") and (n.get("text") or len(n))), None)
    if container is None:
        return None
    if container.get("text"):
        text = container.get("text")
    else:
        pieces = (n.get("text") or "" for n in container.iter("node")
                  if n is not container and not n.get("class", "").endswith("Image"))
        text = "\n".join(p.strip("\n") for p in pieces if p.strip("\n"))
    return text.replace("\xa0", " ").strip() or None


class SomoimBoard:
    def __init__(self, chat):
        self.chat = chat  # SomoimChat: shares navigation to the 모임
        self.dev = chat.dev
        self.ui = chat.ui
        self.log = chat.log

    def open(self):
        """Navigate to the 모임's 게시판 tab."""
        for attempt in range(2):
            if attempt:  # e.g. started from a post or a dialog: start over from 내모임
                self.chat.park()
            if self.chat.open_tab("게시판") is None:
                continue
            self._scroll_to_top()  # the 작성 button hides while the list is scrolled down
            # the board list can take several seconds to load after switching tabs
            if self.ui.wait_for(lambda root: first_id(root, "fab") is not None) is not None:
                return True
        return False

    # listing -------------------------------------------------------------------------

    def _filter(self, category):
        """Tap a category filter (전체/공지/모임후기/가입인사/자유/관심사) and wait for its cards."""
        self._scroll_to_top()  # the filter bar scrolls away with the list
        button = [n for n in by_text(self.ui.dump(), lambda t: t == category) if rid(n) == "button"]
        if not button:
            return False
        self.ui.tap(button[0])
        time.sleep(1)
        self.ui.wait_for(lambda root: any(rid(n) in ("category_text", "nocontent_text") for n in root.iter("node")), timeout=8)
        return True

    def _scroll_to_top(self):
        # the board keeps its scroll position between visits
        prev = None
        for _ in range(15):
            titles = [n.get("text") for n in by_id(self.ui.dump(), "title_text")]
            if titles == prev:
                return
            prev = titles
            self.ui.swipe(self.dev.width // 2, 700, 2000, 300)
            time.sleep(0.8)

    @staticmethod
    def _cards(root):
        """Post cards on screen, top -> bottom. Each: {"pinned", "author", "time", "title", "category", "preview"}
        plus "title_node"/"face_node" for tapping. Pinned [필독] notices show only their title."""
        cards, card = [], None
        blank = lambda pinned, face=None: {"pinned": pinned, "author": None, "time": None, "title": None,
                                           "category": None, "preview": None, "title_node": None, "face_node": face}
        for n in root.iter("node"):
            kind = rid(n)
            if kind == "notice_text":
                card = blank(True)
            elif kind == "face_button":
                card = blank(False, n)
            elif card is None:
                continue
            elif kind == "name_text":
                card["author"] = n.get("text")
            elif kind == "time_text":
                card["time"] = n.get("text")
            elif kind == "content_text":
                card["preview"] = n.get("text")
            elif kind == "title_text":
                card["title"], card["title_node"] = n.get("text"), n
                if card["pinned"]:
                    cards.append(card)
                    card = blank(True)  # consecutive notices share one [필독] label per row
            elif kind == "category_text" and not card["pinned"]:
                card["category"] = n.get("text")
                if card["title"] and card["author"]:
                    cards.append(card)
                card = None
        return cards

    @staticmethod
    def card_key(card):
        return (card["pinned"], card["author"], card["time"], card["title"])

    def list_cards(self, category="전체", stop=None, max_pages=30):
        """All post cards, pinned notices first, then newest first, scrolling to the end of the board.
        `stop(card)` returning True for a (non-pinned) card ends the scan early."""
        if not self.open() or not self._filter(category):
            return None
        self._scroll_to_top()
        cards, prev = {}, None
        for _ in range(max_pages):
            page = self._cards(self.ui.dump())
            keys = [self.card_key(c) for c in page]
            if keys == prev:
                break
            prev = keys
            for c in page:
                cards.setdefault(self.card_key(c), {k: v for k, v in c.items() if not k.endswith("_node")})
                if stop and not c["pinned"] and stop(c):
                    return list(cards.values())
            self.ui.swipe(self.dev.width // 2, 1900, 900)
            time.sleep(1.2)
        return list(cards.values())

    def list_posts(self, category=None, pages=3):
        """Non-pinned posts, newest first: [{"author", "title", "time", "category", ...}]."""
        cards = self.list_cards(category or "전체", max_pages=pages)
        return None if cards is None else [c for c in cards if not c["pinned"]]

    def _find_card(self, card, category):
        """Scroll the board until `card` is on screen. Returns the on-screen card (with nodes) or None."""
        if not self.open() or not self._filter(category):
            return None
        self._scroll_to_top()
        prev = None
        for _ in range(30):
            page = self._cards(self.ui.dump())
            for c in page:
                if (c["pinned"] == card["pinned"] and titles_match(c["title"], card["title"])
                        and (card["pinned"] or (c["author"] == card["author"] and (not card.get("time") or c["time"] == card["time"])))):
                    return c
            keys = [self.card_key(c) for c in page]
            if keys == prev:
                return None
            prev = keys
            self.ui.swipe(self.dev.width // 2, 1900, 900)
            time.sleep(1.2)
        return None

    def open_author_profile(self, post, category="전체"):
        """Open the profile of `post`'s author by tapping the profile picture on its card
        (tapping the name opens the post instead)."""
        card = self._find_card({"pinned": False, **post}, category)
        if card is None or card["face_node"] is None:
            return False
        self.ui.tap(card["face_node"])
        time.sleep(3)
        return any(rid(m) == "title_text" and m.get("text") == post["author"] for m in self.ui.dump().iter("node"))

    # reading -------------------------------------------------------------------------

    def read_post(self, card, category="전체"):
        """Open a post from its card: {"title", "author", "role", "time", "category", "body"} (images skipped).
        Returns None if it can't be found or opened. Goes back to the board afterwards."""
        found = self._find_card(card, category)
        if found is None:
            return None
        self.ui.tap(found["title_node"])
        root = self.ui.wait_for(lambda r: any(n.get("text") == "게시글" for n in r.iter("node"))
                                and first_id(r, "title_text") is not None, timeout=10)
        if root is None:
            return None
        # the body is a WebView whose text arrives a moment after the header; image-only posts never get text
        root = self.ui.wait_for(body_text, timeout=8) or self.ui.dump()
        field = lambda name: first_id(root, name).get("text") if first_id(root, name) is not None else None
        post = {"title": field("title_text"), "author": field("name_text"), "role": field("admin_text"),
                "time": field("time_text"), "category": field("category_text"), "body": body_text(root) or ""}
        self.ui.back()
        time.sleep(2)
        return post

    # writing -------------------------------------------------------------------------

    def _editor(self, root):
        # the body is a contenteditable inside a WebView; its node id has no package prefix
        return next((n for n in root.iter("node") if n.get("resource-id") == "editor"), None)

    def _form(self):
        """(root, title box, body editor) of the write screen, scrolling back to the top if a long body
        pushed the title out of view. Boxes are None if this isn't the write screen."""
        for _ in range(6):
            root = self.ui.dump()
            title_box, editor = first_id(root, "title_edit"), self._editor(root)
            if title_box is not None or editor is None:
                return root, title_box, editor
            self.ui.swipe(self.dev.width // 2, 500, 2000, 300)
            time.sleep(0.8)
        return root, None, editor

    def _clear_form(self):
        """Empty both fields. The app autosaves drafts and restores them the next time 작성 is opened."""
        with self.dev.adb_keyboard():
            _, _, editor = self._form()
            if editor is not None:
                self.ui.tap(editor)
                time.sleep(0.8)
                self.dev.clear_text()
            _, title_box, _ = self._form()
            if title_box is not None:
                self.ui.tap(title_box)
                time.sleep(0.8)
                self.dev.clear_text()
        time.sleep(1)

    def _discard(self):
        self._clear_form()
        up = next((n for n in self.ui.dump().iter("node") if n.get("content-desc") == "Navigate up"), None)
        if up is not None:
            self.ui.tap(up)
            time.sleep(2)

    def write(self, category, title, body, dry_run=False):
        """Fill in and submit a post. `dry_run` fills and verifies everything, then discards the draft.
        Returns True when the post was published (or, for a dry run, filled correctly)."""
        if category not in CATEGORIES:
            raise ValueError(f"unknown category {category!r}; one of {CATEGORIES}")
        if not self.open():
            self.log("게시판을 열 수 없음")
            return False
        self.ui.tap(first_id(self.ui.dump(), "fab"))
        time.sleep(2.5)
        option = next((n for n in self.ui.dump(windows=True).iter("node")
                       if n.get("resource-id") == "android:id/title" and n.get("text") == category), None)
        if option is None:
            self.log(f"카테고리 '{category}'를 찾을 수 없음")
            self.ui.back()
            return False
        self.ui.tap(option)
        time.sleep(2.5)

        root = self._fill(title, body, category)
        if root is None:
            self._discard()
            return False
        if dry_run:
            self.log("(게시 직전 확인만) 제목·본문·카테고리 입력 확인 완료, 초안 폐기")
            self._discard()
            return True
        self._tap_done(root)
        for _ in range(5):
            time.sleep(2.5)
            if title in [n.get("text") for n in by_id(self.ui.dump(), "title_text")]:
                return True
        self.log("게시 확인 실패: 게시판에서 새 글 제목을 찾지 못함")
        return False

    def edit(self, card, title, body):
        """Replace the title and body of a post 모카 wrote (only the author gets the 편집 button).
        The category is left as it is. Returns True once the post shows the new text."""
        root = self.ui.dump()
        if first_id(root, "title_edit") is None or first_id(root, "select_category_text") is None:  # not already editing
            found = self._find_card(card, "전체")
            if found is None:
                self.log(f"수정할 글을 찾지 못함: {card['title']}")
                return False
            self.ui.tap(found["title_node"])
            is_edit_button = lambda n: n.get("content-desc") == "편집"
            root = self.ui.wait_for(lambda r: any(is_edit_button(n) for n in r.iter("node")), timeout=10)
            if root is None:
                self.log("편집 버튼이 없음 (모카가 쓴 글만 수정할 수 있음)")
                return False
            self.ui.tap(next(n for n in root.iter("node") if is_edit_button(n)))
            time.sleep(2)
            option = next((n for n in self.ui.dump(windows=True).iter("node") if n.get("text") == "게시글 수정"), None)
            if option is None:
                self.log("'게시글 수정' 메뉴가 없음")
                return False
            self.ui.tap(option)
            if self.ui.wait_for(lambda r: first_id(r, "title_edit") is not None, timeout=10) is None:
                self.log("수정 화면이 열리지 않음")
                return False
        root = self._fill(title, body)
        if root is None:
            self.ui.back()  # leave without 완료, so the post keeps its current text
            return False
        self._tap_done(root)
        # the app returns to the post; its body renders a moment after the title
        root = self.ui.wait_for(lambda r: any(rid(n) == "title_text" and n.get("text") == title for n in r.iter("node"))
                                and body_text(r) is not None, timeout=15)
        if root is None:
            self.log("수정 확인 실패: 수정된 글이 보이지 않음")
            return False
        if _normalize(body_text(root)) != _normalize(body):
            self.log("수정 확인 실패: 게시글 본문이 입력한 내용과 다름")
            return False
        return True

    def _fill(self, title, body, category=None):
        """Type `title` and `body` into the open write/edit form and check what landed.
        Returns the form's UI root, or None (with the difference logged)."""
        _, title_box, editor = self._form()
        if title_box is None or editor is None:
            self.log("글쓰기 화면을 찾을 수 없음")
            return None
        self._clear_form()  # a restored draft or the post being edited would otherwise stay in the fields
        with self.dev.adb_keyboard():
            _, title_box, _ = self._form()
            self.ui.tap(title_box)
            time.sleep(0.8)
            self.dev.type_text(title)
            _, _, editor = self._form()
            self.ui.tap(editor)
            time.sleep(0.8)
            self.dev.type_text(body)
        time.sleep(1.5)

        root, title_box, editor = self._form()
        filled_title = title_box.get("text") if title_box is not None else None
        filled_body = editor.get("text") if editor is not None else None
        category_label = first_id(root, "category_text")
        category_ok = category is None or (category_label is not None and category_label.get("text") in category)
        if filled_title != title or _normalize(filled_body) != _normalize(body) or not category_ok:
            a, b = _normalize(filled_body), _normalize(body)
            i = next((k for k in range(min(len(a), len(b))) if a[k] != b[k]), min(len(a), len(b)))
            self.log(f"입력 확인 실패: 제목={filled_title!r} (기대 {title!r}), 카테고리={category_label.get('text') if category_label is not None else None!r}, "
                     f"본문 길이 {len(a)}/{len(b)}, 첫 차이 위치 {i}: 입력={a[i-20:i+30]!r} 기대={b[i-20:i+30]!r}")
            return None
        return root

    def _tap_done(self, root):
        self.ui.tap(next(n for n in root.iter("node") if (n.get("text") or "").strip() == "완료"))
