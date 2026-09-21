"""게시글 쓰기 for 운영 모카, and the rules around it.

Until now only an operator could publish a post (chatbot/post.py, with a confirmation prompt). 모카 needs
one of its own because every 정모 it opens must have a post describing it: 모카 writes the post first, then
creates the 정모 with 기존 게시글 연동 pointing at it (admin/events.py, somoim/events.py).

The model writes title and body; the harness checks them, publishes, and re-reads the board so the new post
is in the index right away — create_event looks the post up there.
"""
import json
import re
import time

from chatbot.agent import ACCOUNT_NAME
from chatbot.posts import load_index, sync_posts
from chatbot.store import ROOT

BOARD = ROOT / "data" / "board"
ACTIONS = BOARD / "actions.jsonl"
CATEGORY = "모임후기"       # 정모 글을 한곳에 모으려고 고른 칸. 후기만 쓰는 곳은 아니다
MAX_PER_DAY = 2
TITLE_LIMIT, BODY_LIMIT = 40, 2000


def log_action(agent, action, args, result, note=""):
    BOARD.mkdir(parents=True, exist_ok=True)
    with open(ACTIONS, "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "agent": agent, "action": action,
                            "args": args, "result": result, "note": note}, ensure_ascii=False) + "\n")


def written_today():
    if not ACTIONS.exists():
        return 0
    today = time.strftime("%Y-%m-%d")
    return sum(1 for line in ACTIONS.read_text(encoding="utf-8").splitlines()
               if line.strip() and (a := json.loads(line))["action"] == "write_post"
               and a["result"] == "ok" and a["at"].startswith(today)
               and not a["agent"].startswith("test"))


def _key(title):
    return re.sub(r"\s+", "", title or "")


def find_post(title, author=ACCOUNT_NAME):
    """A post 모카 published, by title. The 정모 link picker matches on the title shown in the app."""
    return next((p for p in load_index()["posts"]
                 if _key(p["title"]) == _key(title) and (author is None or p["author"] == author)), None)


def check_write(title, body):
    title, body = (title or "").strip(), (body or "").strip()
    if not title or len(title) > TITLE_LIMIT:
        return f"제목은 1~{TITLE_LIMIT}자여야 합니다"
    if not body:
        return "본문이 비어 있습니다"
    if len(body) > BODY_LIMIT:
        return f"본문은 {BODY_LIMIT}자 이내여야 합니다 (지금 {len(body)}자)"
    if find_post(title, author=None):
        return f"'{title}'과 같은 제목의 글이 이미 있습니다. 연동하려면 그 글을 쓰세요"
    if written_today() >= MAX_PER_DAY:
        return f"오늘은 이미 글을 {MAX_PER_DAY}개 썼습니다. 더 쓰려면 로하에게 부탁하세요"
    return None


class PostTools:
    """Writing a board post. The board is re-read right after, so the post can be linked immediately."""

    def __init__(self, board_ui, log, agent="admin", dry_run=False):
        self.board = board_ui
        self.log = log
        self.agent = agent
        self.dry_run = dry_run

    def write_post(self, title=None, body=None):
        title, body = (title or "").strip(), (body or "").strip()
        problem = check_write(title, body)
        if problem:
            return f"쓰지 않았습니다: {problem}"
        if self.dry_run:
            self.log(f"  (실행 안 함) write_post {title!r} ({len(body)}자)")
            return f"(dry-run) 게시글 작성 요청을 확인했습니다: {title}"
        ok = self.board.write(CATEGORY, title, body)
        log_action(self.agent, "write_post", {"title": title, "category": CATEGORY, "length": len(body)},
                   "ok" if ok else "failed")
        if not ok:
            return "write_post 실패: 앱에서 글을 올리지 못했습니다"
        sync_posts(self.board, self.log)  # 방금 쓴 글을 목록에 반영해야 정모에 연동할 수 있다
        found = find_post(title)
        self.log(f"  게시글 작성: [{CATEGORY}] {title}")
        return (f"write_post 완료: '{title}'을(를) {CATEGORY}에 올렸습니다."
                + ("" if found else " (목록에서 다시 찾지 못했으니 정모에 연동하기 전에 확인하세요)"))

    def tools(self):
        return [{"type": "function", "name": "write_post",
                 "description": f"모임 게시판({CATEGORY})에 모카 이름으로 글을 올린다. 정모를 열기 전에 "
                                "무엇을 하는 자리인지 설명하는 글을 먼저 쓰는 데 쓴다. 하루 "
                                f"{MAX_PER_DAY}개까지.",
                 "parameters": {"type": "object", "properties": {
                     "title": {"type": "string", "description": f"제목 ({TITLE_LIMIT}자 이내)"},
                     "body": {"type": "string",
                              "description": f"본문 ({BODY_LIMIT}자 이내). 게시판은 마크다운을 렌더링하지 않으니 "
                                             "'#'과 '**'는 쓰지 말고 줄바꿈과 '- ' 목록 정도만 써라"}},
                     "required": ["title", "body"]}}]

    def handlers(self):
        return {"write_post": self.write_post}
