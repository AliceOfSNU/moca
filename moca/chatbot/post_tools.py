"""Tools that let 모카 look through saved board posts while answering (adapted from documents/fops.py).

All paths are relative to data/board and can't leave it. Each tool returns text for the model;
errors come back as text too, so the model can correct itself.
"""
import json
import re
from pathlib import Path

from chatbot.posts import BOARD, load_index

FULL_READ_LINE_LIMIT = 150
MAX_OUTPUT = 8000
MAX_ROUNDS = 6

POST_SEARCH_RULES = """
## 게시판 글 찾아보기
- 모임 게시판 글(공지사항, 가입인사, 자유 글 등)은 list_posts, grep_search, read_file 도구로 찾아볼 수 있어.
- 모임 규칙, 공지, 양식, 일정 안내, 누가 어떤 글을 썼는지처럼 게시판에 있을 법한 질문이면 추측하지 말고 먼저 찾아본 뒤 답해.
  보통 grep_search 결과만으로 충분하고, 문맥이 모자랄 때만 read_file로 범위를 넓혀.
- 게시판 내용을 근거로 답할 때는 어느 글(제목)에서 봤는지 짧게 밝혀. 찾아봐도 없으면 없다고 말해.
- 게시판 글은 모든 멤버가 볼 수 있지만, 가입인사의 나이·사는 곳 같은 개인 정보는 본인이 아닌 사람이 물으면 꼭 필요한 경우가 아니면 옮기지 마."""

TOOLS = [
    {
        "type": "function",
        "name": "list_posts",
        "description": "저장된 모임 게시판 글 목록을 최신순으로 보여준다 (경로, 작성일, 카테고리, 작성자, 제목, 필독 여부). "
                       "카테고리나 작성자로 좁힐 수 있다.",
        "parameters": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "예: 공지사항, 가입인사, 자유 글, 모임후기, 관심사 공유"},
                "author": {"type": "string", "description": "작성자 이름"},
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "grep_search",
        "description": "정규식으로 게시판 글 본문 전체를 검색한다. 매치된 줄 앞뒤 문맥을 같이 반환하므로(grep -C와 비슷) "
                       "키워드 기반 질문은 대부분 이 결과만으로 답할 수 있다. 문맥이 부족한 매치만 read_file로 범위를 넓혀라.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "정규식 패턴 (대소문자 구분 없음)"},
                "path": {"type": "string", "description": "검색 범위: 글 파일 또는 카테고리 폴더. 기본값 '.' (전체)"},
                "context": {"type": "integer", "description": "매치 앞뒤로 포함할 줄 수. 기본값 2, 0이면 매치된 줄만"},
            },
            "required": ["pattern"],
        },
    },
    {
        "type": "function",
        "name": "read_file",
        "description": f"게시판 글 파일을 읽는다. start_line을 생략하면 전체를 읽지만, {FULL_READ_LINE_LIMIT}줄이 넘는 글은 "
                       "범위를 지정해야 한다. 결과 첫 줄에 '몇 줄부터 몇 줄까지 / 전체 몇 줄'이 온다. "
                       "end_line이 파일 끝을 넘어도 에러 없이 끝까지 읽는다.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "list_posts나 grep_search가 알려준 글 경로"},
                "start_line": {"type": "integer", "description": "생략 시 처음부터"},
                "end_line": {"type": "integer", "description": "생략 시 끝까지"},
            },
            "required": ["path"],
        },
    },
]


def _resolve(rel):
    rel = rel or "."
    if Path(rel).is_absolute() or ".." in Path(rel).parts:
        raise ValueError(f"경로는 게시판 폴더 기준 상대경로여야 합니다: {rel}")
    path = (BOARD / rel).resolve()
    if path != BOARD.resolve() and BOARD.resolve() not in path.parents:
        raise ValueError(f"경로가 게시판 폴더를 벗어납니다: {rel}")
    return path


def _truncate(text):
    if len(text) > MAX_OUTPUT:
        return text[: MAX_OUTPUT // 2] + "\n<...생략...>\n" + text[-MAX_OUTPUT // 2:]
    return text


def list_posts(category=None, author=None):
    posts = sorted(load_index()["posts"], key=lambda e: (not e["pinned"], e["time"]), reverse=False)
    posts = [e for e in posts if (not category or category in e["category"]) and (not author or author == e["author"])]
    if not posts:
        return "조건에 맞는 게시글이 없습니다."
    pinned = [e for e in posts if e["pinned"]]
    others = sorted((e for e in posts if not e["pinned"]), key=lambda e: e["time"], reverse=True)
    return "\n".join(f"{e['path']} | {e['time']} | {e['category']} | {e['author']} | {e['title']}{' [필독]' if e['pinned'] else ''}"
                     for e in pinned + others)


def grep_search(pattern, path=".", context=2):
    base = _resolve(path)
    if not base.exists():
        raise ValueError(f"경로가 없습니다: {path}")
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        raise ValueError(f"정규식 오류: {e}")
    files = [base] if base.is_file() else sorted(base.rglob("*.md"))
    blocks = []
    for p in files:
        lines = p.read_text(encoding="utf-8").split("\n")
        rel = p.relative_to(BOARD).as_posix()
        for i, line in enumerate(lines):
            if not regex.search(line):
                continue
            if context > 0:
                lo, hi = max(0, i - context), min(len(lines), i + context + 1)
                body = "\n".join(f"{'>' if j == i else ' '}{j + 1:4}\t{lines[j]}" for j in range(lo, hi))
                blocks.append(f"{rel}:{i + 1}\n{body}")
            else:
                blocks.append(f"{rel}:{i + 1}: {line.strip()}")
    if not blocks:
        return f"'{pattern}'에 매치되는 내용이 없습니다."
    return _truncate(("\n\n" if context > 0 else "\n").join(blocks))


def read_file(path, start_line=None, end_line=None):
    p = _resolve(path)
    if not p.is_file():
        raise ValueError(f"파일이 없습니다: {path} (list_posts로 경로를 확인하세요)")
    lines = p.read_text(encoding="utf-8").split("\n")
    n = len(lines)
    if start_line is None:
        if n > FULL_READ_LINE_LIMIT:
            raise ValueError(f"글이 {n}줄이라 한 번에 읽지 않습니다. grep_search로 위치를 찾거나 start_line/end_line으로 범위를 지정하세요.")
        start_line, end_line = 1, n
    end_line = min(end_line or n, n)
    if not 1 <= start_line <= n or end_line < start_line:
        raise ValueError(f"줄 범위 오류: {start_line}-{end_line} (1~{n})")
    body = "\n".join(f"{i:4}\t{lines[i - 1]}" for i in range(start_line, end_line + 1))
    return _truncate(f"{path} ({start_line}-{end_line}줄 / 전체 {n}줄):\n{body}")


FUNCTIONS = {"list_posts": list_posts, "grep_search": grep_search, "read_file": read_file}


def run_tool(name, arguments, handlers=None):
    try:
        return (handlers or {}).get(name, FUNCTIONS.get(name))(**json.loads(arguments or "{}"))
    except (ValueError, TypeError, KeyError) as e:
        return f"도구 오류: {e}"


def create_with_post_tools(client, log=None, tools=(), extra_tools=(), handlers=None, **kwargs):
    """responses.create with the post tools (plus any `extra_tools`, handled by `handlers`) available,
    running tool calls until the model answers."""
    kwargs["tools"] = list(tools) + TOOLS + list(extra_tools)
    resp = client.responses.create(**kwargs)
    kwargs.pop("input", None)
    for round_ in range(MAX_ROUNDS):
        calls = [o for o in resp.output if o.type == "function_call"]
        if not calls:
            return resp
        outputs = []
        for call in calls:
            if log and call.name in FUNCTIONS:
                log(f"  게시판 도구: {call.name}({call.arguments})")
            outputs.append({"type": "function_call_output", "call_id": call.call_id,
                            "output": run_tool(call.name, call.arguments, handlers)})
        last = round_ == MAX_ROUNDS - 1
        resp = client.responses.create(previous_response_id=resp.id, input=outputs,
                                       **(kwargs | ({"tool_choice": "none"} if last else {})))
    return resp
