"""Local chat memory: the transcript of everything read and the last read position."""
import json
import pathlib
import time

from somoim.chat import key

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "chat"
ANCHOR_SIZE = 5
ANSWERED_SIZE = 200


class ChatStore:
    def __init__(self, path=DATA):
        self.path = pathlib.Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.state_file = self.path / "state.json"
        self.transcript_file = self.path / "transcript.jsonl"
        self.state = {"anchor": [], "day": None, "answered": []}
        if self.state_file.exists():
            self.state.update(json.loads(self.state_file.read_text(encoding="utf-8")))

    @property
    def anchor(self):
        return self.state["anchor"]

    def _save(self):
        self.state_file.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")

    def _lines(self):
        return self.transcript_file.read_text(encoding="utf-8").splitlines() if self.transcript_file.exists() else []

    def record(self, msgs, anchor=None):
        """Append newly read messages and move the read position past them (or past `anchor`, when some of what
        was read is deliberately not kept — a /secret message still has to move the position, or it would be
        read again every time and the 내모임 preview check would keep asking for a re-read).
        Each message gets an id, "m<line number>": the transcript is append-only, so the id never changes and
        knowledge records can cite the exact message they came from (harness/knowledge.py)."""
        first = len(self._lines()) + 1
        with open(self.transcript_file, "a", encoding="utf-8") as f:
            for i, m in enumerate(msgs):
                m["id"] = f"m{first + i}"
                # the date divider is only on the first message of a day; carry it across reads
                m["day"] = self.state["day"] = m.get("day") or self.state["day"]
                f.write(json.dumps({k: v for k, v in m.items() if not k.startswith("_")} | {"read_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                                   ensure_ascii=False) + "\n")
        self.state["anchor"] = (self.anchor + [key(m) for m in (msgs if anchor is None else anchor)])[-ANCHOR_SIZE:]
        self._save()

    def remember(self, key, value):
        """Keep a small piece of loop state (e.g. when 모카 last joined in unprompted)."""
        self.state[key] = value
        self._save()

    def start_at(self, m):
        """Move the read position to `m` without recording it or anything before it."""
        self.state["anchor"] = [key(m)]
        self._save()

    def answered(self, m):
        return key(m) in self.state["answered"]

    def mark_answered(self, msgs):
        # guards against answering twice if the read position is ever lost and old messages are re-read
        self.state["answered"] = (self.state["answered"] + [key(m) for m in msgs])[-ANSWERED_SIZE:]
        self._save()

    def _messages(self, lines, first):
        # lines saved before ids existed get theirs from their position, which is the same rule
        return [json.loads(l) | {"id": f"m{first + i}"} for i, l in enumerate(lines)]

    def count_today(self, mine=False):
        """How many messages this conversation saw today, from the member (mine=False) or from 모카."""
        today = time.strftime("%Y-%m-%d")
        return sum(1 for m in self._messages(self._lines(), 1)
                   if bool(m.get("mine")) is mine and (m.get("read_at") or "").startswith(today))

    def history(self, n):
        lines = self._lines()
        recent = lines[-n:]
        return self._messages(recent, len(lines) - len(recent) + 1)

    def last_id(self):
        """Id of the newest recorded message ("m0" when there is none yet)."""
        return f"m{len(self._lines())}"

    def since(self, msg_id):
        """Every message recorded after `msg_id`, oldest first."""
        start = int(msg_id[1:])
        return self._messages(self._lines()[start:], start + 1)
