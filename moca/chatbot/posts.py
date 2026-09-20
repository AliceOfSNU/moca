"""Board memory: save every 게시판 post as a text file so 모카 can search and read them (documents/posts.md).

Files live under data/board/<category>/<date>_<author>_<title>.md, with data/board/index.json listing them.
No LLM is involved in saving; pictures are skipped.

Usage (from the moca/ directory; stop the main loop first, both use the same emulator):
    python -m chatbot.posts sync          # save posts that aren't saved yet
    python -m chatbot.posts sync --full   # also re-read changed posts and drop deleted ones
"""
import argparse
import datetime as dt
import json
import re
import sys
import time

from chatbot.store import ROOT
from somoim.board import titles_match

BOARD = ROOT / "data" / "board"
INDEX = BOARD / "index.json"


# --- dates -----------------------------------------------------------------------------

def parse_time(text, now=None):
    """'오늘 오후7:38', '9월 15일 오후12:07', '2025년 3월 2일 오전9:05', '5분 전' -> 'YYYY-MM-DD HH:MM' (None if unknown)."""
    if not text:
        return None
    now = now or dt.datetime.now()
    m = re.search(r"(\d+)\s*(분|시간) 전", text)
    if m or "방금" in text:
        delta = dt.timedelta(minutes=int(m.group(1))) if m and m.group(2) == "분" else dt.timedelta(hours=int(m.group(1))) if m else dt.timedelta()
        return (now - delta).strftime("%Y-%m-%d %H:%M")
    if "오늘" in text:
        day = now.date()
    elif "어제" in text:
        day = now.date() - dt.timedelta(days=1)
    else:
        m = re.search(r"(?:(\d{4})년\s*)?(\d{1,2})월\s*(\d{1,2})일", text)
        if not m:
            return None
        day = dt.date(int(m.group(1) or now.year), int(m.group(2)), int(m.group(3)))
        if not m.group(1) and day > now.date():  # a date "later this year" without a year is from last year
            day = day.replace(year=day.year - 1)
    m = re.search(r"(오전|오후)\s*(\d{1,2}):(\d{2})", text)
    hour, minute = (int(m.group(2)) % 12 + (12 if m.group(1) == "오후" else 0), int(m.group(3))) if m else (0, 0)
    return f"{day.isoformat()} {hour:02d}:{minute:02d}"


# --- the index -------------------------------------------------------------------------

def load_index():
    if INDEX.exists():
        return json.loads(INDEX.read_text(encoding="utf-8"))
    return {"posts": [], "last_full_sync": 0}


def save_index(index):
    BOARD.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")


def find_entry(index, card):
    """The saved post a board-list card refers to, or None.
    A post keeps its author and posting time when edited, so those identify it; the title only breaks ties
    between posts from the same author in the same minute. Pinned notices show only a title in the list."""
    if card["pinned"]:
        return next((e for e in index["posts"] if e["pinned"] and titles_match(card["title"], e["title"])), None)
    same = [e for e in index["posts"] if not e["pinned"] and e["author"] == card["author"]
            and e["time"] == parse_time(card["time"])]
    if len(same) > 1:
        same = [e for e in same if titles_match(card["title"], e["title"])] or same
    return same[0] if same else None


def title_changed(card, entry):
    return card["title"] != entry.get("listed_title")


def _squash(text):
    # all whitespace goes: the list preview shows a post's lines separated by spaces or breaks, and bodies
    # saved before board.body_text kept line breaks have the lines glued together
    return re.sub(r"\s+", "", text or "")


def looks_changed(card, entry, body):
    """Cheap edit check from the list card alone: title, category or the body preview no longer matches."""
    if title_changed(card, entry) or (card["category"] and card["category"] != entry["category"]):
        return True
    preview = _squash(card["preview"].rstrip().rstrip("…") if card["preview"] else "")[:40]
    return bool(preview) and not _squash(body).startswith(preview)


def stored_body(entry):
    """The body text saved for an index entry ("" if its file is gone)."""
    path = BOARD / entry["path"]
    return path.read_text(encoding="utf-8").split("\n\n", 2)[-1].strip() if path.exists() else ""


def same_as_saved(entry, card, post):
    """Would saving this post change anything? Pinned notices are re-read on every full sync because the list
    can't show their edits; this keeps an unchanged one from being rewritten and reported as 갱신."""
    body = post["body"] or "(본문 없음 — 사진만 있는 글일 수 있음)"
    return (entry["title"] == post["title"] and entry["listed_title"] == card["title"]
            and entry["pinned"] == card["pinned"] and stored_body(entry) == body.strip())


def _slug(text, limit=40):
    return re.sub(r"[^\w가-힣-]+", "_", text).strip("_")[:limit] or "untitled"


def save_post(index, card, post):
    """Write one post file and its index entry (replacing an older copy of the same post)."""
    when = parse_time(card["time"]) or parse_time(post["time"]) or time.strftime("%Y-%m-%d %H:%M")
    category = post["category"] or card["category"] or "기타"
    author = post["author"] or card["author"] or "알 수 없음"
    rel = f"{_slug(category)}/{when[:10]}_{when[11:13]}{when[14:16]}_{_slug(author, 20)}_{_slug(post['title'])}.md"
    old = find_entry(index, card)
    if old is None:
        # the post itself gives author and time, which survive what the list card can't match across:
        # a renamed pinned notice, or a post that was pinned/unpinned since it was saved
        same = [e for e in index["posts"] if e["author"] == author and e["time"] == when]
        same = [e for e in same if titles_match(post["title"], e["title"])] or same
        old = same[0] if len(same) == 1 else None
    if old:
        index["posts"].remove(old)
        if old["path"] != rel:
            (BOARD / old["path"]).unlink(missing_ok=True)
    lines = [f"# {post['title']}", "",
             f"- 작성자: {author}" + (f" ({post['role']})" if post["role"] else ""),
             f"- 작성일: {when}", f"- 카테고리: {category}",
             f"- 필독 공지: {'예' if card['pinned'] else '아니오'}", "", post["body"] or "(본문 없음 — 사진만 있는 글일 수 있음)", ""]
    path = BOARD / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    entry = {"path": rel, "title": post["title"], "author": author, "role": post["role"], "time": when,
             "category": category, "pinned": card["pinned"], "listed_title": card["title"], "listed_time": card["time"],
             "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    index["posts"].append(entry)
    save_index(index)
    return entry


# --- syncing ---------------------------------------------------------------------------

def sync_posts(board, log, full=False):
    """Save posts not saved yet. `full` also re-reads posts that look edited and drops deleted ones.
    Returns the entries that were newly saved or updated."""
    index = load_index()
    incremental = not full and index["posts"]
    # the board is newest first, so an incremental sync can stop at the first post already saved as-is
    def saved_as_is(card):
        entry = find_entry(index, card)
        return entry is not None and not title_changed(card, entry)

    cards = board.list_cards("전체", stop=saved_as_is if incremental else None)
    if cards is None:
        log("게시판 목록을 읽지 못함")
        return []
    changed = []
    for card in cards:
        entry = find_entry(index, card)
        if entry is not None:
            if not full:
                # only title edits are visible without opening the post; other edits wait for the full re-sync
                if card["pinned"] or not title_changed(card, entry):
                    continue
            elif not card["pinned"] and not looks_changed(card, entry, stored_body(entry)):
                continue  # pinned notices show no preview in the list, so a full sync always re-reads them
        post = board.read_post(card)
        if post is None:
            log(f"  게시글을 열지 못함: {card['title']}")
            continue
        if entry is not None and same_as_saved(entry, card, post):
            continue
        saved = save_post(index, card, post)
        changed.append(saved)
        log(f"  게시글 {'갱신' if entry else '저장'}: [{saved['category']}] {saved['title']} ({saved['author']}, {saved['time']})")
    if full:
        listed = [find_entry(index, c) for c in cards]
        for entry in [e for e in index["posts"] if not any(e is l for l in listed)]:
            index["posts"].remove(entry)
            if not any(e["path"] == entry["path"] for e in index["posts"]):  # never delete a file another entry uses
                (BOARD / entry["path"]).unlink(missing_ok=True)
            log(f"  삭제된 게시글 제거: {entry['title']}")
        index["last_full_sync"] = time.time()
        save_index(index)
    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["sync"])
    ap.add_argument("--full", action="store_true")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    from chatbot.config import MOIM_NAMES
    from cua.android import AndroidDevice
    from somoim.board import SomoimBoard
    from somoim.chat import SomoimChat
    log = lambda msg: print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)
    chat = SomoimChat(AndroidDevice(scale=0.5), MOIM_NAMES, log=log)
    t = time.time()
    changed = sync_posts(SomoimBoard(chat), log, full=args.full)
    log(f"완료: {len(changed)}개 저장/갱신, {time.time() - t:.0f}초")
    chat.park()


if __name__ == "__main__":
    main()
