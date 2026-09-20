"""파일 조작 순수 함수 모음. LLM이나 에이전트 루프를 전혀 모른다 — 나중에 웹 UI가
같은 함수들을 그대로 import해서 쓸 수 있도록 독립적으로 유지한다.

모든 함수는 DOC_ROOT 기준 상대경로 문자열만 받는다. 에러는 예외(ValueError)로
던지고, 호출부(tools.py)가 사람이 읽을 문자열로 변환한다."""

import re
import shutil
from pathlib import Path

from . import config


class FullReadTooLargeError(ValueError):
    """Raised by read_file when an unranged (start_line=None) read would dump a
    document past config.FULL_READ_LINE_LIMIT. tools.py catches this specifically
    (not just as a generic ValueError) to enrich the message with the document's
    heading map, so the model gets exactly what it needs to pick a scoped range
    in the same round trip instead of guessing blind."""

    def __init__(self, rel_path: str, line_count: int, limit: int):
        self.rel_path = rel_path
        self.line_count = line_count
        self.limit = limit
        super().__init__(
            f"문서가 {line_count}줄이라(전체 읽기 제한 {limit}줄) 한 번에 읽지 않습니다. "
            f"outline으로 헤딩 구조를 먼저 확인하거나 grep_search로 원하는 부분을 찾은 뒤 "
            f"read_file(path, start_line, end_line)으로 필요한 범위만 읽으세요. "
            f"정말 전체가 필요하면 read_file(path, start_line=1, end_line={line_count})처럼 명시적으로 요청하세요."
        )


def to_rel(path: Path) -> str:
    return path.relative_to(config.DOC_ROOT).as_posix()


def resolve_path(rel: str) -> Path:
    """상대경로를 DOC_ROOT 기준 절대경로로 바꾸고, 탈출/예약 디렉토리 접근을 막는다."""
    rel = rel or "."
    p = Path(rel)
    if p.is_absolute():
        raise ValueError(f"경로는 프로젝트 루트 기준 상대경로여야 합니다 (절대경로 금지): {rel}")

    resolved = (config.DOC_ROOT / p).resolve()
    try:
        rel_parts = resolved.relative_to(config.DOC_ROOT).parts
    except ValueError:
        raise ValueError(f"경로가 프로젝트 루트를 벗어납니다: {rel}")

    if rel_parts and rel_parts[0] in config.RESERVED_DIRS:
        raise ValueError(f"예약된 디렉토리에는 접근할 수 없습니다: {rel}")

    return resolved


def _maybe_truncate(content: str, max_length: int = 8000) -> str:
    if len(content) > max_length:
        return content[: max_length // 2] + "\n<...생략...>\n" + content[-max_length // 2 :]
    return content


def _format_lines(content: str, start: int = 1) -> str:
    lines = content.split("\n")
    return "\n".join(f"{i + start:6}\t{line}" for i, line in enumerate(lines))


# 들여쓰기 없이 시작하는 YAML 최상위 키. 정식 파서를 안 쓰는 이유는 두 가지다 —
# 작성 중이라 아직 문법이 깨진 파일에서도 구조를 보여줘야 하고, 파서는 줄 번호를
# 돌려주지 않는데 여기서 정작 필요한 게 줄 번호다.
_YAML_TOP_KEY_RE = re.compile(r"^([A-Za-z_][\w\-.]*)\s*:")


def line_count(rel_path: str) -> int:
    path = resolve_path(rel_path)
    if not path.is_file():
        raise ValueError(f"파일이 없습니다: {rel_path}")
    return len(path.read_text(encoding="utf-8").split("\n"))


def yaml_top_level_keys(rel_path: str) -> list[tuple[int, str]]:
    """YAML 최상위 키를 (줄 번호, 키)로. .md의 헤딩 목차에 해당하는 구조 지도다."""
    path = resolve_path(rel_path)
    if not path.is_file():
        raise ValueError(f"파일이 없습니다: {rel_path}")
    out = []
    for i, line in enumerate(path.read_text(encoding="utf-8").split("\n"), start=1):
        m = _YAML_TOP_KEY_RE.match(line)
        if m:
            out.append((i, m.group(1)))
    return out


def read_file(rel_path: str, start_line: int | None = None, end_line: int | None = None) -> str:
    path = resolve_path(rel_path)
    if not path.exists():
        raise ValueError(f"파일이 없습니다: {rel_path}")
    if path.is_dir():
        raise ValueError(f"{rel_path}는 디렉토리입니다. list_dir를 사용하세요.")

    content = path.read_text(encoding="utf-8")
    lines = content.split("\n")
    n = len(lines)

    if start_line is None:
        if n > config.FULL_READ_LINE_LIMIT:
            raise FullReadTooLargeError(rel_path, n, config.FULL_READ_LINE_LIMIT)
        body = _format_lines(_maybe_truncate(content), 1)
        header = f"{rel_path} (전체 {n}줄)"
    else:
        end_line = end_line if end_line is not None else n
        if start_line < 1 or start_line > n:
            raise ValueError(f"start_line 범위 초과: {start_line} (1~{n})")
        if end_line < start_line:
            raise ValueError(f"end_line({end_line})이 start_line({start_line})보다 작습니다.")
        # 파일 끝을 넘는 end_line은 에러가 아니라 그냥 끝까지 읽은 것으로 친다. 호출하는
        # 쪽은 파일이 몇 줄인지 미리 알 방법이 없어서 넉넉히 찍어 보내는 게 정상이고,
        # 여기서 에러를 내면 "줄 수를 알아내려고 일부러 실패시키는" 왕복만 늘어난다.
        clamped = end_line > n
        end_line = min(end_line, n)
        body = _format_lines("\n".join(lines[start_line - 1 : end_line]), start_line)
        header = f"{rel_path} ({start_line}-{end_line}줄 / 전체 {n}줄)"
        if clamped:
            header += " — 요청한 end_line이 파일 끝을 넘어서 끝까지만 읽었다"

    return f"{header}:\n{body}"


def list_dir(rel_path: str = ".") -> str:
    path = resolve_path(rel_path)
    if not path.exists():
        raise ValueError(f"경로가 없습니다: {rel_path}")
    if not path.is_dir():
        raise ValueError(f"{rel_path}는 파일입니다. read_file을 사용하세요.")

    entries = []
    for child in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if child.name in config.RESERVED_DIRS:
            continue
        entries.append(child.name + ("/" if child.is_dir() else ""))

    if not entries:
        return f"{rel_path} (비어 있음)"
    return "\n".join(entries)


def tree(rel_path: str = ".") -> list[dict]:
    """웹 UI 사이드바용 재귀 파일 트리. list_dir와 달리 사람이 읽는 텍스트가 아니라
    {"name","path","type","children"} 딕셔너리를 반환한다 (JSON으로 그대로 응답 가능)."""
    base = resolve_path(rel_path)
    if not base.exists() or not base.is_dir():
        raise ValueError(f"디렉토리가 아니거나 없습니다: {rel_path}")

    def walk(dir_path: Path) -> list[dict]:
        nodes = []
        for child in sorted(dir_path.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            if child.name in config.RESERVED_DIRS:
                continue
            rel = to_rel(child)
            if child.is_dir():
                nodes.append({"name": child.name, "path": rel, "type": "dir", "children": walk(child)})
            else:
                nodes.append({"name": child.name, "path": rel, "type": "file"})
        return nodes

    return walk(base)


def read_raw(rel_path: str) -> str:
    """줄번호 없는 순수 파일 내용. LLM용 read_file과 달리 에디터에 그대로 로드할 값."""
    path = resolve_path(rel_path)
    if not path.exists():
        raise ValueError(f"파일이 없습니다: {rel_path}")
    if path.is_dir():
        raise ValueError(f"{rel_path}는 디렉토리입니다.")
    return path.read_text(encoding="utf-8")


def write_raw(rel_path: str, content: str) -> str:
    """존재 여부와 무관하게 통째로 덮어쓴다 (에디터 저장용 — create_file과 달리
    '이미 존재하면 에러' 제약이 없다). 새 파일도 만들 수 있어 부모 디렉토리를 준비한다."""
    path = resolve_path(rel_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"저장됨: {rel_path}"


def create_file(rel_path: str, content: str) -> str:
    path = resolve_path(rel_path)
    if path.exists():
        raise ValueError(f"이미 존재하는 파일입니다: {rel_path} (수정하려면 str_replace/insert_text 사용)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"생성됨: {rel_path}"


def str_replace(rel_path: str, old_str: str, new_str: str = "") -> str:
    path = resolve_path(rel_path)
    if not path.exists():
        raise ValueError(f"파일이 없습니다: {rel_path}")

    content = path.read_text(encoding="utf-8").expandtabs()
    old = old_str.expandtabs()
    new = (new_str or "").expandtabs()

    occurrences = content.count(old)
    if occurrences == 0:
        raise ValueError(f"old_str이 파일에 그대로 나타나지 않습니다: {rel_path}")
    if occurrences > 1:
        hit_lines = [i + 1 for i, line in enumerate(content.split("\n")) if old in line]
        raise ValueError(f"old_str이 여러 곳({hit_lines})에 있어 고유하지 않습니다. 앞뒤 맥락을 더 포함하세요.")

    new_content = content.replace(old, new)
    path.write_text(new_content, encoding="utf-8")

    replaced_at = content.split(old)[0].count("\n")
    start = max(0, replaced_at - 2)
    end = replaced_at + 2 + new.count("\n")
    snippet = _format_lines("\n".join(new_content.split("\n")[start : end + 1]), start + 1)
    return f"수정됨: {rel_path}\n{snippet}"


def insert_text(rel_path: str, after_line: int, new_str: str) -> str:
    path = resolve_path(rel_path)
    if not path.exists():
        raise ValueError(f"파일이 없습니다: {rel_path}")

    content = path.read_text(encoding="utf-8").expandtabs()
    lines = content.split("\n")
    n = len(lines)
    if after_line < 0 or after_line > n:
        raise ValueError(f"after_line 범위 오류: {after_line} (0~{n})")

    insert_lines = new_str.expandtabs().split("\n")
    new_lines = lines[:after_line] + insert_lines + lines[after_line:]
    path.write_text("\n".join(new_lines), encoding="utf-8")

    start = max(0, after_line - 2)
    end = after_line + len(insert_lines) + 2
    snippet = _format_lines("\n".join(new_lines[start:end]), start + 1)
    return f"삽입됨: {rel_path}\n{snippet}"


def replace_lines(rel_path: str, start_line: int, end_line: int, new_str: str = "") -> str:
    """start_line~end_line(1부터 시작, 둘 다 포함) 구간을 통째로 new_str로 바꾼다.
    outline/read_file이 돌려주는 줄 번호와 짝지어 쓰면, 절 하나를 통째로 갈아끼울 때
    str_replace처럼 원래 텍스트를 old_str로 그대로 옮겨 적을 필요가 없다. new_str을
    생략하면 그 구간을 그냥 삭제하는 효과."""
    path = resolve_path(rel_path)
    if not path.exists():
        raise ValueError(f"파일이 없습니다: {rel_path}")

    content = path.read_text(encoding="utf-8").expandtabs()
    lines = content.split("\n")
    n = len(lines)
    if start_line < 1 or start_line > n:
        raise ValueError(f"start_line 범위 초과: {start_line} (1~{n})")
    if end_line < start_line or end_line > n:
        raise ValueError(f"end_line 범위 오류: {end_line} (start_line~{n})")

    new_lines = (new_str or "").expandtabs().split("\n")
    result_lines = lines[: start_line - 1] + new_lines + lines[end_line:]
    path.write_text("\n".join(result_lines), encoding="utf-8")

    start = max(0, start_line - 1 - 2)
    end = start_line - 1 + len(new_lines) + 2
    snippet = _format_lines("\n".join(result_lines[start:end]), start + 1)
    return f"교체됨: {rel_path}\n{snippet}"


def mkdir(rel_path: str) -> str:
    path = resolve_path(rel_path)
    path.mkdir(parents=True, exist_ok=True)
    return f"생성됨: {rel_path}/"


# copy_file/move_file/delete_file은 의도적으로 파일만 다룬다 — 디렉토리 통째 복사/이동/
# 삭제는 지원하지 않는다 (실수로 문서 폴더 전체가 날아가는 사고를 도구 차원에서 막아둠).
# 두 도구 모두 dest_path를 "디렉토리+파일명이 합쳐진 최종 경로"로 요구하고, mv처럼
# "디렉토리를 주면 그 안으로 원래 이름 그대로 들어간다" 같은 편의 동작은 없다 — 상대경로
# 하나만 보고 정확히 뭐가 될지 알 수 있는 쪽이 LLM이 잘못 추측하기 어렵다.


def copy_file(rel_path: str, dest_path: str) -> str:
    src = resolve_path(rel_path)
    dst = resolve_path(dest_path)
    if not src.exists():
        raise ValueError(f"파일이 없습니다: {rel_path}")
    if src.is_dir():
        raise ValueError(f"{rel_path}는 디렉토리입니다. copy_file은 파일만 지원합니다.")
    if dst.exists():
        raise ValueError(f"이미 존재하는 경로입니다: {dest_path}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return f"복사됨: {rel_path} -> {dest_path}"


def move_file(rel_path: str, dest_path: str) -> str:
    """같은 디렉토리 안에서 이름만 바꾸는 것도, 다른 디렉토리로 옮기는 것도 이 함수
    하나로 처리한다 (mv와 동일한 방식)."""
    src = resolve_path(rel_path)
    dst = resolve_path(dest_path)
    if not src.exists():
        raise ValueError(f"파일이 없습니다: {rel_path}")
    if src.is_dir():
        raise ValueError(f"{rel_path}는 디렉토리입니다. move_file은 파일만 지원합니다.")
    if dst.exists():
        raise ValueError(f"이미 존재하는 경로입니다: {dest_path}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return f"이동됨: {rel_path} -> {dest_path}"


def delete_file(rel_path: str) -> str:
    path = resolve_path(rel_path)
    if not path.exists():
        raise ValueError(f"파일이 없습니다: {rel_path}")
    if path.is_dir():
        raise ValueError(f"{rel_path}는 디렉토리입니다. delete_file은 파일만 지원합니다.")
    path.unlink()
    return f"삭제됨: {rel_path}"


def glob_search(pattern: str) -> str:
    # Unlike every other tool here, this one never routed `pattern` through
    # resolve_path (it's a glob pattern, not a path) — so it had two separate ways
    # to walk rglob() straight out of DOC_ROOT, both confirmed empirically, not just
    # in theory:
    #   1) a '..' segment in the pattern itself, leading ("../secret.txt") or buried
    #      mid-pattern ("sub/../../secret.txt").
    #   2) rglob() following a symlink that lives under DOC_ROOT but points outside
    #      it — no '..' anywhere in the pattern needed for this one, so the check
    #      below for (1) alone does NOT catch it.
    # (1) is rejected outright up front, since nothing here has a legitimate reason
    # to reference '..'. (2) is caught per-match by resolving each hit the same way
    # resolve_path() does for every other tool (.resolve() follows symlinks to their
    # real location) and re-checking containment against DOC_ROOT — a pattern-string
    # check alone can never catch this class, since the pattern contains no clue
    # that a matched entry happens to be a symlink pointing elsewhere.
    if ".." in Path(pattern).parts:
        raise ValueError(f"글롭 패턴에 '..'을 쓸 수 없습니다: {pattern}")

    matches = []
    for p in config.DOC_ROOT.rglob(pattern):
        resolved = p.resolve()
        try:
            rel = resolved.relative_to(config.DOC_ROOT)
        except ValueError:
            continue  # e.g. a symlink under DOC_ROOT pointing outside it
        if rel.parts and rel.parts[0] in config.RESERVED_DIRS:
            continue
        matches.append(rel.as_posix() + ("/" if resolved.is_dir() else ""))

    if not matches:
        return f"'{pattern}' 패턴에 매치되는 파일이 없습니다."
    return "\n".join(sorted(matches))


def grep_search(pattern: str, rel_path: str = ".", context: int = 2) -> str:
    """context>0 returns a few lines of surrounding text per match (like `grep -C`) so
    a single search is often enough to answer from directly, without a follow-up
    read_file call just to see what's around the matched line."""
    base = resolve_path(rel_path)
    if not base.exists():
        raise ValueError(f"경로가 없습니다: {rel_path}")
    try:
        regex = re.compile(pattern)
    except re.error as e:
        raise ValueError(f"정규식 오류: {e}")

    targets = [base] if base.is_file() else list(base.rglob("*"))
    blocks = []
    for p in targets:
        if p.is_dir():
            continue
        rel = p.relative_to(config.DOC_ROOT)
        if rel.parts and rel.parts[0] in config.RESERVED_DIRS:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        file_lines = text.split("\n")
        for i, line in enumerate(file_lines):
            if not regex.search(line):
                continue
            lineno = i + 1
            if context > 0:
                start = max(0, i - context)
                end = min(len(file_lines), i + context + 1)
                body = "\n".join(
                    f"{'>' if j == i else ' '}{j + 1:6}\t{file_lines[j]}" for j in range(start, end)
                )
                blocks.append(f"{rel.as_posix()}:{lineno}\n{body}")
            else:
                blocks.append(f"{rel.as_posix()}:{lineno}: {line.strip()}")

    if not blocks:
        return f"'{pattern}'에 매치되는 내용이 없습니다."
    return _maybe_truncate(("\n\n" if context > 0 else "\n").join(blocks))
