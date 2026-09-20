"""Thin ADB wrapper that turns computer-use actions into Android input events."""
import base64
import contextlib
import io
import os
import subprocess
import time

from PIL import Image

ADB = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk", "platform-tools", "adb.exe")
ADB_IME = "com.android.adbkeyboard/.AdbIME"

# computer-use key names -> Android keycodes
KEYCODES = {
    "ENTER": 66, "RETURN": 66, "BACK": 4, "ESC": 4, "ESCAPE": 4, "HOME": 3,
    "BACKSPACE": 67, "DELETE": 112, "TAB": 61, "SPACE": 62,
    "UP": 19, "DOWN": 20, "LEFT": 21, "RIGHT": 22,
    "ARROWUP": 19, "ARROWDOWN": 20, "ARROWLEFT": 21, "ARROWRIGHT": 22,
    "PAGEUP": 92, "PAGEDOWN": 93, "APP_SWITCH": 187,
    "CTRL": 113, "CONTROL": 113, "SHIFT": 59, "ALT": 57,
    "A": 29, "C": 31, "V": 50, "X": 52, "Z": 54,
}


class AndroidDevice:
    def __init__(self, serial=None, scale=0.5):
        self.serial = serial
        self.scale = scale  # screenshot downscale factor sent to the model
        w, h = self.adb("shell", "wm", "size").split(":")[-1].strip().split("x")
        self.width, self.height = int(w), int(h)

    def adb(self, *args, binary=False):
        cmd = [ADB] + (["-s", self.serial] if self.serial else []) + list(args)
        out = subprocess.run(cmd, capture_output=True, check=True, timeout=60).stdout
        return out if binary else out.decode("utf-8", "replace")

    # --- observation ---------------------------------------------------
    def screenshot_b64(self):
        png = self.adb("exec-out", "screencap", "-p", binary=True)
        img = Image.open(io.BytesIO(png)).convert("RGB")
        if self.scale != 1:
            img = img.resize((round(self.width * self.scale), round(self.height * self.scale)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return base64.b64encode(buf.getvalue()).decode()

    def _pt(self, x, y):
        # model coordinates are in screenshot space; map back to device pixels
        return (min(max(round(x / self.scale), 0), self.width - 1),
                min(max(round(y / self.scale), 0), self.height - 1))

    # --- actions -------------------------------------------------------
    def tap(self, x, y):
        self.adb("shell", "input", "tap", *map(str, self._pt(x, y)))

    def swipe(self, x1, y1, x2, y2, ms=300):
        a, b = self._pt(x1, y1), self._pt(x2, y2)
        self.adb("shell", "input", "swipe", str(a[0]), str(a[1]), str(b[0]), str(b[1]), str(ms))

    def key(self, keycode):
        self.adb("shell", "input", "keyevent", str(keycode))

    @contextlib.contextmanager
    def adb_keyboard(self):
        """Keep ADBKeyBoard active for a sequence of inputs. Switching keyboards between inputs
        moves focus and closes popups such as the @mention member list."""
        prev = self.adb("shell", "settings", "get", "secure", "default_input_method").strip()
        if prev != ADB_IME:
            self.adb("shell", "ime", "set", ADB_IME)
            time.sleep(0.5)
        self._ime_held = True
        try:
            yield
        finally:
            self._ime_held = False
            if prev and prev != ADB_IME:
                time.sleep(0.3)
                self.adb("shell", "ime", "set", prev)

    def _with_adb_ime(self, *broadcast):
        # Unicode (Korean) input goes through ADBKeyBoard; stock `input text` is ASCII-only.
        # switch IME only for the input so the normal keyboard stays usable for humans
        if getattr(self, "_ime_held", False):
            self.adb("shell", "am", "broadcast", *broadcast)
            return
        with self.adb_keyboard():
            self.adb("shell", "am", "broadcast", *broadcast)

    def type_text(self, text):
        b64 = base64.b64encode(text.encode("utf-8")).decode()
        self._with_adb_ime("-a", "ADB_INPUT_B64", "--es", "msg", b64)

    def clear_text(self):
        self._with_adb_ime("-a", "ADB_CLEAR_TEXT")

    def wake(self):
        self.adb("shell", "input", "keyevent", "224")  # KEYCODE_WAKEUP

    def execute(self, action):
        """Run one computer-use action (dict). Returns a short log string."""
        t = action["type"]
        if t in ("click", "double_click"):
            if action.get("button") == "back":
                self.key(4)
                return "back"
            self.tap(action["x"], action["y"])
            if t == "double_click":
                self.tap(action["x"], action["y"])
            return f"{t} ({action['x']},{action['y']})"
        if t == "scroll":
            x, y = action["x"], action["y"]
            dx, dy = action.get("scroll_x", 0), action.get("scroll_y", 0)
            # content scrolls down => finger moves up; cap the swipe to the screen
            lim = 0.6 * self.height * self.scale
            dy = max(-lim, min(lim, dy))
            dx = max(-lim, min(lim, dx))
            self.swipe(x, y, x - dx, y - dy, 400)
            return f"scroll ({x},{y}) by ({dx},{dy})"
        if t == "drag":
            p = action["path"]
            self.swipe(p[0]["x"], p[0]["y"], p[-1]["x"], p[-1]["y"], 600)
            return f"drag {p[0]} -> {p[-1]}"
        if t == "type":
            self.type_text(action["text"])
            return f"type {action['text']!r}"
        if t == "keypress":
            keys = [k.upper() for k in action["keys"]]
            codes = [KEYCODES[k] for k in keys if k in KEYCODES]
            if len(codes) > 1:
                self.adb("shell", "input", "keycombination", *map(str, codes))
            elif codes:
                self.key(codes[0])
            return f"keypress {keys}"
        if t == "wait":
            time.sleep(2)
            return "wait"
        if t in ("move", "screenshot"):
            return t
        return f"unsupported action {t}"
