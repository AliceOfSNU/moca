"""정모 (gatherings) on the 모임 홈 tab: list, create, edit, delete, join, leave.

What the app allows, checked against 소모임 5.8.2:
- Only 운영진 see 수정 on a 정모 card; the edit screen can change name, location, map URL, cost and capacity.
- Date and time cannot be changed after creation ("날짜는 수정할 수 없습니다"), so moving a 정모 means
  deleting it and creating a new one.
- Cancelling is "정모 삭제하기" — the app deletes rather than marking cancelled.
- Creating optionally notifies every member (정모 공지) and can auto-create the 정모's board post.
"""
import datetime as dt
import hashlib
import pathlib
import time

from somoim.ui import bounds, first_id, rid

CAPACITY_RANGE = (1, 60)
ROOT = pathlib.Path(__file__).resolve().parent.parent
# 정모 creation requires a photo. The thumbnail 모카 puts on its 정모 is whatever image sits in
# data/events/ — drop a new one there to change it — with a plain default if that folder has none.
PHOTO_DIR = ROOT / "data" / "events"
FALLBACK_PHOTO = ROOT / "tools" / "event_default.png"
PHOTO_TYPES = (".png", ".jpg", ".jpeg", ".webp")


def event_photo():
    """The image to use as the 정모 thumbnail: the newest picture in data/events/, else the default."""
    pictures = [p for p in PHOTO_DIR.glob("*") if p.suffix.lower() in PHOTO_TYPES]
    return max(pictures, key=lambda p: p.stat().st_mtime) if pictures else FALLBACK_PHOTO


def device_photo(local):
    """Where that image lives on the device. The name carries the file's fingerprint, so replacing the
    picture in data/events/ pushes a new file instead of reusing the stale copy already on the phone."""
    stat = local.stat()
    mark = hashlib.sha1(f"{local.name}|{stat.st_size}|{int(stat.st_mtime)}".encode()).hexdigest()[:8]
    return f"/sdcard/Pictures/moca_event_{mark}{local.suffix.lower()}"


def _text(node):
    return (node.get("text") or "").strip() if node is not None else None


class SomoimEvents:
    def __init__(self, chat):
        self.chat = chat
        self.dev = chat.dev
        self.ui = chat.ui
        self.log = chat.log

    # --- reading -------------------------------------------------------------------

    def open_home(self):
        return self.chat.open_tab("홈")

    @staticmethod
    def _cards(root):
        """정모 cards on screen. A card starts at its D-day badge and ends at the join button."""
        cards, card = [], None
        for n in root.iter("node"):
            kind = rid(n)
            if kind == "cal_text2" and (_text(n) or "").startswith("D"):
                card = {"d_day": _text(n), "name": None, "when": None, "location": None,
                        "expense": None, "joiners": None, "join_label": None,
                        "name_node": None, "edit_node": None, "join_node": None}
            elif card is None:
                continue
            elif kind == "name_text" and card["name"] is None:
                card["name"], card["name_node"] = _text(n), n
            elif kind == "edit_text_layout":
                card["edit_node"] = n
            elif kind == "time_text" and card["when"] is None:
                card["when"] = _text(n)
            elif kind == "location_text":
                card["location"] = _text(n)
            elif kind == "expense_text":
                card["expense"] = _text(n)
            elif kind == "joiner_count_text":
                card["joiners"] = _text(n)
            elif kind == "join_text":
                card["join_label"], card["join_node"] = _text(n), n
                if card["name"]:
                    cards.append(card)
                card = None
        return cards

    def list_events(self, max_pages=8):
        """Upcoming 정모, as shown on the 모임 홈 tab. None if the tab can't be opened."""
        if self.open_home() is None:
            return None
        found, prev = {}, None
        for _ in range(max_pages):
            page = self._cards(self.ui.dump())
            names = [c["name"] for c in page]
            for c in page:
                found.setdefault(c["name"], {k: v for k, v in c.items() if not k.endswith("_node")})
            if names and names == prev:
                break
            prev = names
            self.ui.swipe(self.dev.width // 2, 1900, 800)
            time.sleep(1.2)
        return list(found.values())

    def _find_card(self, name, max_pages=8):
        """Scroll the 홈 tab until the named 정모 is on screen; returns the card (with nodes) or None."""
        if self.open_home() is None:
            return None
        prev = None
        for _ in range(max_pages):
            page = self._cards(self.ui.dump())
            for c in page:
                if c["name"] == name:
                    return c
            names = [c["name"] for c in page]
            if names and names == prev:
                return None
            prev = names
            self.ui.swipe(self.dev.width // 2, 1900, 800)
            time.sleep(1.2)
        return None

    # --- date and time pickers ------------------------------------------------------

    def _confirm(self, label="OK"):
        """Tap a dialog's confirm button (android:id/button1) if one is open."""
        root = self.ui.dump(windows=True)
        button = next((n for n in root.iter("node") if n.get("resource-id") == "android:id/button1"), None)
        if button is not None:
            self.ui.tap(button)
            time.sleep(1.5)
            return True
        return False

    def _pick_date(self, target):
        """Drive Android's date dialog to `target` (a date). The dialog opens on the selected date's month."""
        root = self.ui.dump(windows=True)
        header = next((n for n in root.iter("node") if n.get("resource-id") == "android:id/date_picker_header_date"), None)
        year = next((n for n in root.iter("node") if n.get("resource-id") == "android:id/date_picker_header_year"), None)
        if header is None:
            return False
        shown = dt.datetime.strptime(f"{_text(header)} {_text(year)}", "%a, %b %d %Y").date()
        months = (target.year - shown.year) * 12 + target.month - shown.month
        step = "android:id/next" if months > 0 else "android:id/prev"
        for _ in range(abs(months)):
            node = next((n for n in self.ui.dump(windows=True).iter("node") if n.get("resource-id") == step), None)
            if node is None:
                return False
            self.ui.tap(node)
            time.sleep(0.8)
        day = next((n for n in self.ui.dump(windows=True).iter("node")
                    if _text(n) == str(target.day) and n.get("clickable") == "true" and bounds(n)[1] > 900), None)
        if day is None:
            return False
        self.ui.tap(day)
        time.sleep(0.8)
        return self._confirm()

    def _time_dialog_open(self):
        return any(n.get("resource-id") == "android:id/am_label" for n in self.ui.dump(windows=True).iter("node"))

    def _by_id(self, node_id, windows=True):
        return next((n for n in self.ui.dump(windows=windows).iter("node") if n.get("resource-id") == node_id), None)

    def _pick_time(self, hour, minute):
        """Drive Android's clock dialog to `hour`:`minute` (24h in). The dial is awkward to tap reliably,
        so switch it to text input mode and type the numbers."""
        if self._by_id("android:id/input_hour") is None:
            toggle = self._by_id("android:id/toggle_mode")
            if toggle is None:
                return False
            self.ui.tap(toggle)
            time.sleep(1.5)
        with self.dev.adb_keyboard():
            for node_id, value in (("android:id/input_hour", hour % 12 or 12), ("android:id/input_minute", f"{minute:02d}")):
                node = self._by_id(node_id)
                if node is None:
                    return False
                self.ui.tap(node)
                time.sleep(0.6)
                self.dev.clear_text()
                self.dev.type_text(str(value))
                time.sleep(0.6)
        wanted = "PM" if hour >= 12 else "AM"
        for _ in range(3):
            label = self._by_id("android:id/text1")
            if label is None or _text(label) == wanted:
                break
            self.ui.tap(label)  # AM/PM selector: either toggles or opens a two-item list
            time.sleep(1.2)
            # the list items carry the same id as the spinner itself, so tell them apart by position
            option = next((n for n in self.ui.dump(windows=True).iter("node")
                           if _text(n) == wanted and bounds(n) != bounds(label)), None)
            if option is not None:
                self.ui.tap(option)
                time.sleep(1.0)
        return self._confirm()

    # --- writing --------------------------------------------------------------------

    def _scroll_to(self, field, tries=6):
        """Bring a form field into view (typing leaves the keyboard over the buttons). Returns the node or None."""
        for _ in range(tries):
            node = first_id(self.ui.dump(), field)
            if node is not None:
                return node
            self.ui.swipe(self.dev.width // 2, 1700, 900)
            time.sleep(1.0)
        return None

    def _fill(self, field, value):
        root = self.ui.dump()
        node = first_id(root, field)
        if node is None:
            return False
        self.ui.tap(node)
        time.sleep(0.8)
        with self.dev.adb_keyboard():
            self.dev.clear_text()
            self.dev.type_text(str(value))
        time.sleep(0.8)
        return True

    def _set_photo(self):
        """Pick 모카's 정모 thumbnail (event_photo). A 정모 can't be created without a photo: tapping
        정모 만들기 opens the system gallery instead. The picture is pushed to the device once, and the
        gallery shows it first because it is the most recently added one."""
        local = event_photo()
        remote = device_photo(local)
        # `ls` on a missing file exits 1, which adb() turns into an exception, so ask the shell instead
        if "MISSING" in self.dev.adb("shell", f"ls {remote} 2>/dev/null || echo MISSING"):
            self.log(f"정모 사진 준비: {local.name}")
            self.dev.adb("push", str(local), remote)
            self.dev.adb("shell", "am", "broadcast", "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
                         "-d", f"file://{remote}")
            time.sleep(2)
        picture = first_id(self.ui.dump(), "picture_layout2")
        if picture is None:
            return True  # already has a photo (e.g. restored draft)
        self.ui.tap(picture)
        root = self.ui.wait_for(lambda r: any((n.get("content-desc") or "").startswith("Photo taken") for n in r.iter("node")),
                                timeout=15, windows=True)
        if root is None:
            self.log("사진 선택 화면이 열리지 않음")
            return False
        self.ui.tap(next(n for n in root.iter("node") if (n.get("content-desc") or "").startswith("Photo taken")))
        crop = self.ui.wait_for(lambda r: any((n.get("content-desc") or n.get("text")) == "Crop" for n in r.iter("node")),
                                timeout=15, windows=True)
        if crop is not None:
            self.ui.tap(next(n for n in crop.iter("node") if (n.get("content-desc") or n.get("text")) == "Crop"))
        return self.ui.wait_for(lambda r: first_id(r, "name_edit") is not None, timeout=20) is not None

    def create(self, name, when, location, expense=0, capacity=20, notify=False):
        """Open 정모 만들기 and create one. `when` is a datetime. Returns True once it shows on 홈."""
        if self.open_home() is None:
            return False
        for _ in range(8):
            button = first_id(self.ui.dump(), "button2")
            if button is not None and _text(button) == "정모 만들기":
                break
            self.ui.swipe(self.dev.width // 2, 1900, 800)
            time.sleep(1.2)
        else:
            self.log("'정모 만들기' 버튼을 찾지 못함")
            return False
        self.ui.tap(button)
        if self.ui.wait_for(lambda r: first_id(r, "name_edit") is not None, timeout=10) is None:
            self.log("정모 개설 화면이 열리지 않음")
            return False

        if not self._set_photo():  # 소모임 opens the gallery instead of creating when there's no 정모 photo
            return False
        self._fill("name_edit", name)
        self.ui.tap(first_id(self.ui.dump(), "date_text"))
        time.sleep(1.5)
        if not self._pick_date(when.date()):
            self.log("날짜를 고르지 못함")
            return False
        if not self._time_dialog_open():  # the app usually opens the time picker itself once the date is set
            self.ui.tap(first_id(self.ui.dump(), "time_text"))
            time.sleep(1.5)
        if not self._pick_time(when.hour, when.minute):
            self.log("시간을 고르지 못함")
            return False
        self._fill("location_edit", location)
        self._fill("expense_edit", expense)
        self._fill("max_count_edit", capacity)
        if notify:
            box = first_id(self.ui.dump(), "check_box")
            if box is not None:
                self.ui.tap(box)
                time.sleep(0.5)

        root = self.ui.dump()
        filled = {f: _text(first_id(root, f)) for f in ("name_edit", "date_text", "time_text", "location_edit",
                                                        "expense_edit", "max_count_edit")}
        expected = (f"{when.month}월 {when.day}일", f"{'오후' if when.hour >= 12 else '오전'} {when.hour % 12 or 12}:{when.minute:02d}")
        if filled["name_edit"] != name or not (filled["date_text"] or "").startswith(expected[0]) or filled["time_text"] != expected[1]:
            self.log(f"입력 확인 실패: {filled} (기대 {expected})")
            return False
        self.log(f"  정모 입력 확인: {filled}")
        save = self._scroll_to("save_button")  # typing leaves the keyboard over the button
        if save is None:
            return False
        self.ui.tap(save)
        time.sleep(3)
        self._confirm()  # the app may ask to confirm
        return self._find_card(name) is not None

    def edit(self, name, new_name=None, location=None, expense=None, capacity=None):
        """Change a 정모's editable fields (date and time cannot be changed). Returns True on success."""
        card = self._find_card(name)
        if card is None or card["edit_node"] is None:
            self.log(f"'{name}' 정모의 수정 버튼을 찾지 못함 (운영진만 수정할 수 있음)")
            return False
        self.ui.tap(card["edit_node"])
        if self.ui.wait_for(lambda r: first_id(r, "save_button") is not None
                            and _text(first_id(r, "save_button")) == "수정하기", timeout=10) is None:
            self.log("정모 수정 화면이 열리지 않음")
            return False
        for field, value in (("name_edit", new_name), ("location_edit", location),
                             ("expense_edit", expense), ("max_count_edit", capacity)):
            if value is not None:
                self._fill(field, value)
        self.ui.tap(first_id(self.ui.dump(), "save_button"))
        time.sleep(3)
        self._confirm()
        return self._find_card(new_name or name) is not None

    def delete(self, name):
        """Delete (cancel) a 정모. Returns True once it's gone from 홈."""
        card = self._find_card(name)
        if card is None or card["edit_node"] is None:
            self.log(f"'{name}' 정모의 수정 버튼을 찾지 못함 (운영진만 삭제할 수 있음)")
            return False
        self.ui.tap(card["edit_node"])
        if self.ui.wait_for(lambda r: first_id(r, "delete_button") is not None, timeout=10) is None:
            self.log("정모 수정 화면이 열리지 않음")
            return False
        self.ui.tap(first_id(self.ui.dump(), "delete_button"))
        time.sleep(2)
        self._confirm()
        time.sleep(2)
        return self._find_card(name) is None

    def set_attendance(self, name, joining):
        """Tap 참석 / 참석취소 for 모카's own account. Returns True if the card ends up in the wanted state."""
        card = self._find_card(name)
        if card is None:
            self.log(f"'{name}' 정모를 찾지 못함")
            return False
        label = card["join_label"]
        joined_labels, want = ("취소", "참석취소"), ("참석",)
        if label in (want if joining else joined_labels):
            self.ui.tap(card["join_node"])
            time.sleep(2)
            self._confirm()
            time.sleep(2)
        elif label in (joined_labels if joining else want):
            self.log(f"  이미 {'참석' if joining else '미참석'} 상태입니다")
            return True
        else:
            self.log(f"  참석 버튼 상태가 '{label}'이라 누르지 않았습니다")
            return False
        card = self._find_card(name)
        return card is not None and (card["join_label"] in joined_labels) == joining
