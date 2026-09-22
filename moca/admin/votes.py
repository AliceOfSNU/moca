"""투표 tools for 운영 모카, and the harness rules around them.

Same split as the 정모 tools (admin/events.py): the model asks, this module decides whether the app is
touched at all. A vote is cheap to make and expensive to undo — members' answers disappear with it — so
모카 only closes or deletes votes it posted itself, never deletes one people have already answered, and
can't flood the 게시판 with them.
"""
import datetime as dt
import json
import time

from chatbot.agent import ACCOUNT_NAME
from chatbot.store import ROOT
from somoim.votes import MAX_OPTIONS, MIN_OPTIONS, OPTION_LIMIT, TITLE_LIMIT

VOTES = ROOT / "data" / "votes"
INDEX = VOTES / "index.json"
ACTIONS = VOTES / "actions.jsonl"
VOTE_TOOLS = ("list_votes", "read_vote", "create_vote", "close_vote", "delete_vote")
MAX_CREATES_PER_DAY = 2
MAX_OPEN = 3            # 투표가 여러 개 열려 있으면 멤버들이 어느 것에 답해야 할지 모른다
END_RANGE_HOURS = (2, 24 * 30)


def log_action(agent, action, args, result, note=""):
    """Every app-touching 투표 action, and who asked for it."""
    VOTES.mkdir(parents=True, exist_ok=True)
    with open(ACTIONS, "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "agent": agent, "action": action,
                            "args": args, "result": result, "note": note}, ensure_ascii=False) + "\n")


def load_index():
    if INDEX.exists():
        return json.loads(INDEX.read_text(encoding="utf-8"))
    return {"votes": [], "last_sync": 0}


def sync_votes(votes_ui, log, agent="harness"):
    """Walk the 투표 board once and cache what's there, so the prompt can show open votes
    without opening the app again for every decision."""
    votes = votes_ui.list_votes()
    if votes is None:
        log("투표 목록을 열지 못해 이전 기록을 그대로 씁니다")
        return load_index()["votes"]
    VOTES.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps({"votes": votes, "last_sync": time.time(), "by": agent},
                                ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"투표 {len(votes)}개 확인 (진행 중 {sum(1 for v in votes if not is_closed(v))}개)")
    return votes


def open_block():
    """The running votes, for 운영 모카's prompt."""
    running = [v for v in load_index()["votes"] if not is_closed(v)]
    if not running:
        return "(진행 중인 투표 없음)"
    return "\n".join(describe(v) + (" | 모카가 올림" if is_mine(v) else "") for v in running)


def remember_created(title, ends_at=None):
    """Put a just-created vote into the cache at once, so 채팅 모카 knows about it before the next sync."""
    index = load_index()
    index["votes"] = [v for v in index["votes"] if v["title"] != title]
    index["votes"].insert(0, {"title": title, "author": ACCOUNT_NAME, "status": "0명 참여",
                              "posted": time.strftime("%Y-%m-%d %H:%M"),
                              "when": f"{ends_at:%Y-%m-%d %H:%M} 종료예정" if ends_at else ""})
    VOTES.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")


def my_votes():
    """모카's running votes, with the options it asked for (the list cards don't carry them)."""
    running = {v["title"]: v for v in load_index()["votes"] if is_mine(v) and not is_closed(v)}
    out = []
    for line in (ACTIONS.read_text(encoding="utf-8").splitlines() if ACTIONS.exists() else []):
        if not line.strip():
            continue
        a = json.loads(line)
        if a["action"] == "create_vote" and a["result"] == "ok" and a["args"]["title"] in running:
            out.append({**running.pop(a["args"]["title"]), "options": a["args"]["options"]})
    return out + [{**v, "options": []} for v in running.values()]


def chat_block():
    """For 채팅 모카's prompt: the votes its other hat opened. The card in the 모임 채팅방 came from
    the harness sharing the post, not from a message 채팅 모카 typed, so say so."""
    mine = my_votes()
    if not mine:
        return ""
    lines = ["\n\n## 네가(운영 모카로) 게시판에 올린 투표 — 모임 채팅방에도 공유해 뒀어"]
    for v in mine:
        options = f" (항목: {', '.join(v['options'])})" if v["options"] else ""
        lines.append(f"- '{v['title']}'{options} — {v.get('when') or '마감 미정'}, {v.get('status') or '참여 없음'}")
    lines.append("- 채팅방의 투표 카드는 하네스가 게시글을 공유한 것이지 네가 쓴 메시지가 아니야. "
                 "멤버가 물으면 네가 올린 투표라고 답하고, 게시판 투표 탭이나 채팅방의 카드에서 참여할 수 있다고 안내해. "
                 "결과와 마감 처리는 운영 모카가 맡으니 네가 임의로 약속하지는 마.")
    return "\n".join(lines)


def creates_today():
    if not ACTIONS.exists():
        return 0
    today = time.strftime("%Y-%m-%d")
    return sum(1 for line in ACTIONS.read_text(encoding="utf-8").splitlines()
               if line.strip() and (a := json.loads(line))["action"] == "create_vote"
               and a["result"] == "ok" and a["at"].startswith(today)
               and not a["agent"].startswith("test"))  # 개발 중 테스트는 모카의 하루치를 쓰지 않는다


def is_mine(vote):
    """소모임 shows the author on every card, so ownership doesn't need a local index."""
    return (vote.get("author") or "").strip() == ACCOUNT_NAME


def is_closed(vote):
    """List cards say '종료됨' in the when line; read() reports it directly."""
    if "closed" in vote:
        return bool(vote["closed"])
    return "종료" in (vote.get("when") or "") and "예정" not in (vote.get("when") or "")


def shown_names(names):
    """Who 운영 모카 may be told about by name. The rest are counted, not named — same rule as
    the knowledge store, read at display time (chatbot/memory_consent.py)."""
    from chatbot.memory_consent import shares
    named = [n for n in names if n and (n == ACCOUNT_NAME or shares(n))]
    rest = len([n for n in names if n]) - len(named)
    return named + ([f"외 {rest}명"] if rest else [])


def describe(vote):
    bits = [vote["title"], f"{vote['author']} 올림"]
    if vote.get("status"):
        bits.append(vote["status"])
    if vote.get("when"):
        bits.append(vote["when"])
    return " | ".join(b for b in bits if b)


# --- rules ---------------------------------------------------------------------------

def check_create(title, options, ends_at, open_votes):
    if not title or len(title) > TITLE_LIMIT:
        return f"투표 제목은 1~{TITLE_LIMIT}자여야 합니다"
    if not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
        return f"항목은 {MIN_OPTIONS}~{MAX_OPTIONS}개여야 합니다"
    long = [f"{o} ({len(o)}자)" for o in options if len(o) > OPTION_LIMIT]
    if long or not all(options):
        return (f"항목은 각각 1~{OPTION_LIMIT}자여야 합니다 (앱의 항목 칸이 {OPTION_LIMIT}자까지만 받습니다)"
                + (f". 너무 긴 항목: {', '.join(long)}" if long else ""))
    if len(set(options)) != len(options):
        return "같은 항목을 두 번 넣을 수 없습니다"
    if ends_at is not None:
        hours = (ends_at - dt.datetime.now()).total_seconds() / 3600
        if not END_RANGE_HOURS[0] <= hours <= END_RANGE_HOURS[1]:
            return (f"마감은 지금부터 {END_RANGE_HOURS[0]}시간 뒤 ~ {END_RANGE_HOURS[1] // 24}일 이내여야 합니다"
                    " (멤버들이 답할 시간은 주되, 잊힐 만큼 길지 않게)")
    if any(v["title"] == title for v in open_votes):
        return f"'{title}' 투표가 이미 있습니다"
    mine_open = [v for v in open_votes if is_mine(v) and not is_closed(v)]
    if len(mine_open) >= MAX_OPEN:
        return (f"모카가 연 투표가 이미 {len(mine_open)}개 진행 중입니다 ({', '.join(v['title'] for v in mine_open)}). "
                "먼저 끝내거나 종료하세요")
    if creates_today() >= MAX_CREATES_PER_DAY:
        return f"오늘은 이미 투표를 {MAX_CREATES_PER_DAY}개 만들었습니다. 더 만들려면 로하에게 부탁하세요"
    return None


def check_change(vote, title, deleting=False):
    """Closing or deleting is only for 모카's own votes; deleting only while nobody has answered."""
    if vote is None:
        return f"'{title}' 투표를 찾을 수 없습니다. list_votes로 제목을 확인하세요"
    if not is_mine(vote):
        return f"'{title}'은 모카가 올린 투표가 아니라 종료하거나 삭제할 수 없습니다. 로하에게 부탁하세요"
    if deleting:
        answered = vote.get("participants")
        if answered is None:
            answered = int("".join(c for c in (vote.get("status") or "") if c.isdigit()) or 0)
        if answered > 0:
            return (f"'{title}'에 이미 {answered}명이 답했습니다. 지우면 그 답이 사라지니 "
                    "close_vote로 종료만 하세요")
    elif is_closed(vote):
        return f"'{title}'은 이미 종료된 투표입니다"
    return None


def _eul(word):
    """'을' or '를', by whether the last syllable has a final consonant. Korean 조사 must match the word,
    and an option name is whatever 모카 typed."""
    last = (word or "")[-1:]
    if "가" <= last <= "힣":
        return "을" if (ord(last) - 0xAC00) % 28 else "를"
    return "을"


def vote_result(vote, title):
    """Turn a closed vote into a task result, in the shape a Group Chat Task result has
    (chatbot/group_task.py: validate). Voters are stored as subjects and shown by each member's
    현재 활용 범위 at display time (harness/knowledge.py), like every other record."""
    counted = [o for o in vote["options"] if o["votes"]]
    tally = ", ".join(f"{o['name']} {o['votes']}표" for o in vote["options"])
    summary = f"투표 '{title}' 종료: {tally}. 참여 {vote['participants']}명."
    if not counted:
        return {"outcome": "no_shareable_answer", "summary": summary + " 아무도 답하지 않았다.",
                "summary_subjects": [], "knowledge": []}
    records = [{"statement": f"투표 '{title}' 결과: {tally} (참여 {vote['participants']}명).",
                "subjects": [], "basis": "observed", "source_refs": [f"vote:{title}"]}]
    for option in counted:
        for name in option["voters"]:  # 익명투표면 voters가 비어 있어 집계만 남는다
            # '{s0}님이' 로 쓰는 이유: 이름이 가려지면 '한 멤버'로 바뀌어서, 이름 뒤 조사를 미리 정할 수 없다
            records.append({"statement": f"{{s0}}님이 투표 '{title}'에서 "
                                         f"'{option['name']}'{_eul(option['name'])} 골랐다.",
                            "subjects": [name], "basis": "observed", "source_refs": [f"vote:{title}"]})
    return {"outcome": "answer_available" if len(records) > 1 else "partial_answer", "summary": summary,
            "summary_subjects": [], "knowledge": records}


def finish_vote_tasks(votes_ui, log, dry_run=False):
    """Close out vote watchers whose vote is over: read the results once, record them, finish the task.
    Runs before 운영 모카's round, so a goal waiting on a vote wakes up with the tally in hand."""
    from harness import knowledge, tasks
    index = {v["title"]: v for v in load_index()["votes"]}
    for task in tasks.vote_tasks(("running",)):
        title = task["spec"]["target"]["vote"]
        card = index.get(title)
        if not tasks.deadline_passed(task) and not (card and is_closed(card)):
            continue  # 아직 진행 중
        vote = votes_ui.read(title)
        if vote is None:
            log(f"투표 '{title}'를 읽지 못해 결과 정리를 미룸")
            continue
        if not vote["closed"] and not tasks.deadline_passed(task):
            continue
        result = vote_result(vote, title)
        log(f"투표 작업 {task['id']} 마감: {result['summary']}")
        if dry_run:
            continue
        origin = {"task": task["id"], "channel": "vote"}
        result["knowledge"] = [{"id": knowledge.add(k["statement"], k["subjects"], k["basis"],
                                                    k["source_refs"], origin)["id"], **k}
                               for k in result["knowledge"]]
        for k in result["knowledge"]:
            log(f"  지식 {k['id']}: {knowledge.render(k)}")
        tasks.finish(task, result, how="vote_closed")


class VoteTools:
    """투표 tools. Every write goes through the rules above, then the app, then the log."""

    def __init__(self, votes_ui, log, agent="admin", dry_run=False):
        self.ui = votes_ui
        self.log = log
        self.agent = agent
        self.dry_run = dry_run
        self.done = []

    def _run(self, action, args, do):
        if self.dry_run:
            self.log(f"  (실행 안 함) {action} {args}")
            return f"(dry-run) {action} 요청을 확인했습니다: {args}"
        ok = do()
        log_action(self.agent, action, args, "ok" if ok else "failed")
        self.done.append((action, args, ok))
        return f"{action} 완료: {args}" if ok else f"{action} 실패: 앱에서 처리하지 못했습니다"

    def _list(self):
        votes = self.ui.list_votes()
        return [] if votes is None else votes

    def _find(self, title):
        return next((v for v in self._list() if v["title"] == title), None)

    # reading -------------------------------------------------------------------
    def list_votes(self):
        votes = self._list()
        if not votes:
            return "게시판에 투표가 없습니다."
        return "\n".join(describe(v) + (" | 모카가 올림" if is_mine(v) else "") for v in votes)

    def read_vote(self, title=None):
        """The results, and — while it runs — who hasn't answered yet.
        The counts are whole; the names follow the same 활용 범위 rule as knowledge (harness/knowledge.py),
        so 운영 모카 only sees the members who allowed 모임 운영 use."""
        vote = self.ui.read(title)
        if vote is None:
            return f"'{title}' 투표를 찾을 수 없습니다. list_votes로 제목을 확인하세요."
        for option in vote["options"]:
            option["voters"] = shown_names(option["voters"])
        vote["not_voted"] = shown_names(vote["not_voted"])
        return json.dumps(vote, ensure_ascii=False)

    # writing -------------------------------------------------------------------
    def create_vote(self, title=None, options=None, ends_at=None, multi=False, anonymous=False):
        options = [str(o).strip() for o in (options or []) if str(o).strip()]
        at = None
        if ends_at:
            try:
                at = dt.datetime.strptime(ends_at, "%Y-%m-%d %H:%M")
            except ValueError:
                return "ends_at은 'YYYY-MM-DD HH:MM' 형식이어야 합니다"
        problem = check_create((title or "").strip(), options, at, self._list())
        if problem:
            return f"만들지 않았습니다: {problem}"
        args = {"title": title, "options": options, "ends_at": ends_at,
                "multi": bool(multi), "anonymous": bool(anonymous)}
        result = self._run("create_vote", args,
                           lambda: self.ui.create(title, options, ends_at=at,
                                                  multi=bool(multi), anonymous=bool(anonymous)))
        if not result.startswith("create_vote 완료"):
            return result
        # 게시판에만 두면 아무도 보지 않는다. 채팅방에 공유해야 멤버도, 채팅 모카도 투표를 안다.
        remember_created(title, at)
        shared = self.ui.share_to_chat(title)
        log_action(self.agent, "share_vote", {"title": title}, "ok" if shared else "failed")
        return result + (" (모임 채팅방에도 공유함)" if shared
                         else " (채팅방 공유는 실패했습니다. 투표 자체는 게시판에 올라가 있습니다)")

    def close_vote(self, title=None):
        problem = check_change(self._find(title), title)
        if problem:
            return f"종료하지 않았습니다: {problem}"
        return self._run("close_vote", {"title": title}, lambda: self.ui.close(title))

    def delete_vote(self, title=None, reason=None):
        problem = check_change(self._find(title), title, deleting=True)
        if problem:
            return f"삭제하지 않았습니다: {problem}"
        return self._run("delete_vote", {"title": title, "reason": reason}, lambda: self.ui.delete(title))

    def tools(self):
        return [
            {"type": "function", "name": "list_votes",
             "description": "게시판의 투표 목록을 본다 (제목, 올린 사람, 참여 인원, 마감).",
             "parameters": {"type": "object", "properties": {}, "required": []}},
            {"type": "function", "name": "read_vote",
             "description": "투표 하나의 결과를 본다. 항목별 득표수와 누가 무엇을 골랐는지, 아직 답하지 않은 멤버까지 나온다.",
             "parameters": {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}},
            {"type": "function", "name": "create_vote",
             "description": ("게시판에 투표를 올린다. 여러 선택지 중 멤버들의 선호를 모을 때 쓴다. "
                             "자유롭게 답해야 하는 질문은 투표 대신 채팅으로 물어라."),
             "parameters": {"type": "object", "properties": {
                 "title": {"type": "string", "description": f"투표 제목 ({TITLE_LIMIT}자 이내). 무엇을 정하는지 분명하게"},
                 "options": {"type": "array", "items": {"type": "string"},
                             "description": f"선택지 {MIN_OPTIONS}~{MAX_OPTIONS}개, 각 {OPTION_LIMIT}자 이내"},
                 "ends_at": {"type": "string",
                             "description": f"마감 'YYYY-MM-DD HH:MM'. 지금부터 {END_RANGE_HOURS[0]}시간~"
                                            f"{END_RANGE_HOURS[1] // 24}일 사이. 비우면 앱 기본값(이틀 뒤)"},
                 "multi": {"type": "boolean", "description": "복수선택 허용 (기본 false)"},
                 "anonymous": {"type": "boolean",
                               "description": "익명투표 (기본 false). 익명이면 누가 무엇을 골랐는지 모카도 볼 수 없다"}},
                 "required": ["title", "options"]}},
            {"type": "function", "name": "close_vote",
             "description": "모카가 올린 투표를 마감 전에 종료한다. 종료하면 결과가 투표에 그대로 보인다.",
             "parameters": {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}},
            {"type": "function", "name": "delete_vote",
             "description": "모카가 올린 투표를 지운다. 아무도 답하지 않았을 때만 할 수 있다.",
             "parameters": {"type": "object", "properties": {
                 "title": {"type": "string"}, "reason": {"type": "string", "description": "지우는 이유 (기록용)"}},
                 "required": ["title", "reason"]}},
        ]

    def handlers(self):
        return {"list_votes": self.list_votes, "read_vote": self.read_vote, "create_vote": self.create_vote,
                "close_vote": self.close_vote, "delete_vote": self.delete_vote}
