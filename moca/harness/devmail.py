"""모카 → 개발자(로하) 1:1. The one exception to "모카 never writes first in a 1:1".

모카 can't change its own harness: a new command, a new tool, a higher limit is code 로하 writes. So it
may ask — but only 로하, and only through this queue. The model writes the request, the harness rate-limits
it and sends it from the DM session (run.py). Every other member still has to write first.
"""
import json
import time

from chatbot.config import DEVELOPER
from chatbot.store import ROOT

QUEUE = ROOT / "data" / "dm" / "dev_requests.jsonl"
MAX_PER_DAY = 3      # 개발자도 사람이다. 하루에 쏟아붓지 말고 중요한 것부터.
MAX_LEN = 600
KINDS = {"feature": "새 기능·도구 요청", "limit": "하네스 제한 조정 요청",
         "bug": "이상 동작 제보", "question": "하네스·구현에 대한 질문"}


def _load():
    if not QUEUE.exists():
        return []
    return [json.loads(line) for line in QUEUE.read_text(encoding="utf-8").splitlines() if line.strip()]


def _save(records):
    QUEUE.parent.mkdir(parents=True, exist_ok=True)
    QUEUE.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")


def queued():
    return [r for r in _load() if r["status"] == "queued"]


def sent_today():
    today = time.strftime("%Y-%m-%d")
    return sum(1 for r in _load() if r["status"] != "failed" and r["at"].startswith(today))


def request(text, kind="feature", why="", asked_by="chat"):
    """Queue one request. Returns what to tell the model."""
    text = (text or "").strip()
    if not text:
        return "보낼 내용이 비어 있습니다"
    if len(text) > MAX_LEN:
        return f"요청은 {MAX_LEN}자 이내여야 합니다 (지금 {len(text)}자)"
    if kind not in KINDS:
        return f"kind는 {list(KINDS)} 중 하나여야 합니다"
    if sent_today() >= MAX_PER_DAY:
        return (f"오늘은 이미 개발자에게 {MAX_PER_DAY}건을 보냈습니다. 급하지 않으면 내일 다시 정리해서 보내세요")
    records = _load()
    pending = [r for r in records if r["status"] == "queued"]
    if any(r["text"] == text for r in pending):
        return "같은 요청이 이미 전달 대기 중입니다"
    record = {"id": f"d{len(records) + 1}", "at": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind,
              "text": text, "why": (why or "").strip()[:MAX_LEN], "asked_by": asked_by, "status": "queued"}
    records.append(record)
    _save(records)
    return (f"{DEVELOPER}님에게 보낼 요청으로 저장했습니다 ({record['id']}). 다음 1:1 점검 때 전달되고, "
            "답은 로하가 1:1로 보내옵니다. 멤버에게 이미 전달된 것처럼 말하지 마세요.")


def mark(record_id, sent):
    records = _load()
    for r in records:
        if r["id"] == record_id:
            r["status"] = "sent" if sent else "failed"
            r["sent_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save(records)


def message(record):
    """How the request reads in the 1:1: 모카's own words, with the harness saying where it came from."""
    head = f"[모카 → 개발자] {KINDS[record['kind']]}"
    body = record["text"]
    why = f"\n\n이유: {record['why']}" if record["why"] else ""
    return f"{head}\n\n{body}{why}"


TOOL = {
    "type": "function",
    "name": "ask_developer",
    "description": f"하네스(너를 움직이는 프로그램)를 바꿔야 할 일을 개발자 {DEVELOPER}에게 1:1로 요청한다. "
                   "새 명령어·도구·기능, 제한 조정, 이상 동작 제보에 쓴다. 네가 지금 할 수 없는 일을 "
                   "멤버에게 약속하는 대신 여기로 보내라. 대화 상대에게 할 대답을 대신하지는 못한다.",
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": f"요청 내용. {MAX_LEN}자 이내, 무엇이 필요한지 구체적으로"},
            "kind": {"type": "string", "enum": list(KINDS),
                     "description": "feature=새 기능·도구, limit=제한 조정, bug=이상 동작, question=질문"},
            "why": {"type": "string", "description": "왜 필요한지 한두 문장 (어떤 대화·상황에서 나왔는지)"},
        },
        "required": ["text", "kind"],
    },
}


class DeveloperRequests:
    """Tool handler: the model asks, the harness queues."""

    def __init__(self, asked_by="chat", log=None):
        self.asked_by = asked_by
        self.log = log

    def __call__(self, text=None, kind="feature", why=""):
        reply = request(text, kind=kind, why=why, asked_by=self.asked_by)
        if self.log:
            self.log(f"  개발자 요청 ({kind}): {text} → {reply}")
        return reply


class GoalTools:
    """운영 모카's version: the goal loop runs it as a tool task (admin/goal_loop.py)."""

    def __init__(self, log, agent="admin", dry_run=False):
        self.log = log
        self.agent = agent
        self.dry_run = dry_run

    def ask_developer(self, text=None, kind="feature", why=""):
        if self.dry_run:
            self.log(f"  (실행 안 함) ask_developer {text!r}")
            return f"(dry-run) 개발자 요청을 확인했습니다: {text}"
        reply = request(text, kind=kind, why=why, asked_by=self.agent)
        queued = reply.startswith(DEVELOPER)
        self.log(f"  개발자 요청 ({kind}): {text} → {reply}")
        return f"ask_developer 완료: {reply}" if queued else f"보내지 않았습니다: {reply}"
