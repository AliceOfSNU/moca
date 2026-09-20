"""Have 모카 write a board post on an operator's request.

Usage (from the moca/ directory; stop the chat loop first, both use the same emulator):
    python -m chatbot.post --category 가입인사 "요청 내용"             # draft, show, ask before posting
    python -m chatbot.post --category 가입인사 --dry-run "요청 내용"   # fill the form, verify, discard
    python -m chatbot.post --draft data/posts/xxx.json --yes           # post a saved draft as-is
"""
import argparse
import json
import sys
import time

from chatbot.agent import ChatAgent
from chatbot.config import MOIM_NAMES
from chatbot.store import ROOT
from cua.agent import openai_client
from cua.android import AndroidDevice
from somoim.board import CATEGORIES, SomoimBoard
from somoim.chat import SomoimChat

POSTS = ROOT / "data" / "posts"


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("request", nargs="?", help="what the post should be about")
    ap.add_argument("--category", default="자유 글", choices=CATEGORIES)
    ap.add_argument("--draft", help="post a saved draft file instead of writing a new one")
    ap.add_argument("--dry-run", action="store_true", help="fill the form and verify, but do not publish")
    ap.add_argument("--yes", action="store_true", help="publish without asking")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    if args.draft:
        post = json.loads(open(args.draft, encoding="utf-8").read())
    else:
        if not args.request:
            ap.error("request or --draft is required")
        post = ChatAgent(openai_client()).write_post(args.request) | {"category": args.category, "request": args.request}
        POSTS.mkdir(parents=True, exist_ok=True)
        path = POSTS / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
        path.write_text(json.dumps(post, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"초안 저장: {path}")

    print(f"\n[{post['category']}] {post['title']}\n{'-' * 40}\n{post['body']}\n{'-' * 40}")
    if not args.dry_run and not args.yes:
        if not sys.stdin.isatty() or input("게시할까요? [y/N] ").strip().lower() != "y":
            log("게시하지 않음 (--draft 파일로 나중에 게시 가능)")
            return

    board = SomoimBoard(SomoimChat(AndroidDevice(scale=0.5), MOIM_NAMES, log=log))
    ok = board.write(post["category"], post["title"], post["body"], dry_run=args.dry_run)
    log(("입력 확인 완료" if args.dry_run else "게시 완료") if ok else "실패")


if __name__ == "__main__":
    main()
