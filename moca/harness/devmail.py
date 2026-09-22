"""모카 → 개발자(로하) 1:1. The one exception to "모카 never writes first in a 1:1".

모카 can't change its own harness: a new command, a new tool, a higher limit is code 로하 writes. So it
may ask — but only 로하, only through this queue, and only 운영 모카 (the goal loop's ask_developer): chat 모카
and 1:1 모카 don't file requests. The model writes the request, the harness rate-limits it and sends it from
the DM session (run.py), numbered: "#2". Every other member still has to write first.

로하 answers with `/issue 2 …` in the 1:1 (the number is required). The harness records the answer on the
request, confirms in one line, and the goal that asked wakes up — even while it waits on a task — with the
answer in its input ([개발자 요청]). Nothing of this goes through a model.
"""
import json
import re
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
    return (f"{DEVELOPER}님에게 보낼 요청으로 저장했습니다 (#{number(record)}). 다음 1:1 점검 때 전달되고, "
            "답이 오면 [개발자 요청]에 보이고 이 목표가 깨어납니다. 멤버에게 이미 전달된 것처럼 말하지 마세요.")


def number(record):
    return int(record["id"][1:])


def answer(n, text):
    """Record 로하's answer to request #n. Returns (record, None) or (None, why not)."""
    records = _load()
    r = next((x for x in records if number(x) == n), None)
    if r is None:
        return None, f"#{n} 요청은 없어요."
    if r["status"] == "queued":
        return None, f"#{n} 요청은 아직 보내지 않았어요."
    r.setdefault("answers", []).append({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "text": text})
    r["status"] = "answered"
    _save(records)
    return r, None


ISSUE = re.compile(r"^\s*/issue(?:\s+#?(\d+))?(?:\s+(.*))?\s*$", re.S)


def parse_command(text):
    """'/issue 2 고쳤어' -> (2, '고쳤어'); '/issue' or '/issue 고쳤어' -> (None, …); anything else -> None."""
    m = ISSUE.match(text or "")
    if not m:
        return None
    return (int(m.group(1)) if m.group(1) else None), (m.group(2) or "").strip()


def handle_command(n, text):
    """What the harness answers in 로하's 1:1. Returns (reply, the answered record or None)."""
    waiting = [f"#{number(r)}" for r in _load() if r["status"] == "sent"]
    usage = "형식: /issue 번호 답 (예: /issue 2 고쳤어)" + (f". 답을 기다리는 요청: {', '.join(waiting)}" if waiting else "")
    if n is None:
        return f"번호가 필요해요. {usage}", None
    if not text:
        return f"#{n}에 대한 답이 비어 있어요. {usage}", None
    record, problem = answer(n, text)
    if problem:
        return f"{problem} {usage}", None
    return f"#{n}에 대한 답으로 기록했어요. 운영 모카가 확인할게요.", record


def for_goal(goal_id):
    return [r for r in _load() if r.get("asked_by") == f"goal:{goal_id}"]


def new_answers(goal):
    """Answers to this goal's requests that came after its last step: they wake it."""
    since = goal.get("last_step_at") or ""
    return [(r, a) for r in for_goal(goal["id"]) for a in r.get("answers", []) if a["at"] > since]


def block(goal, limit=8):
    """For 운영 모카's input: its requests to 로하 and the answers, newest first."""
    recent = sorted(_load(), key=lambda r: r["at"], reverse=True)[:limit]
    if not recent:
        return "## 개발자 요청\n(없음)"
    since = goal.get("last_step_at") or ""
    status = {"queued": "보내기 전", "sent": "답 기다리는 중", "failed": "보내지 못함", "answered": "답 옴"}
    lines = ["## 개발자 요청 (로하에게 보낸 것, 최근 순)"]
    for r in recent:
        by = r.get("asked_by") or ""
        # before 2026-09-22 chat 모카 and 1:1 모카 could file requests too; those records keep their origin
        where = ("이 목표" if by == f"goal:{goal['id']}" else f"다른 목표 {by[5:]}" if by.startswith("goal:")
                 else "예전에 모임 채팅 모카가" if by == "group_chat" else "예전에 1:1 모카가" if by.startswith("dm:") else by)
        lines.append(f"- #{number(r)} [{status.get(r['status'], r['status'])}] ({where}, {r['at'][:16]}) {r['text'][:200]}")
        for a in r.get("answers", []):
            lines.append(f"  {'★ 새 답' if a['at'] > since else '답'} ({a['at'][:16]}): {a['text']}")
    return "\n".join(lines)


def mark(record_id, sent):
    records = _load()
    for r in records:
        if r["id"] == record_id:
            r["status"] = "sent" if sent else "failed"
            r["sent_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save(records)


def message(record):
    """How the request reads in the 1:1: 모카's own words, with the harness saying where it came from."""
    n = number(record)
    head = f"[모카 → 개발자] #{n} {KINDS[record['kind']]}"
    body = record["text"]
    why = f"\n\n이유: {record['why']}" if record["why"] else ""
    return f"{head}\n\n{body}{why}\n\n(답은 /issue {n} …)"


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
