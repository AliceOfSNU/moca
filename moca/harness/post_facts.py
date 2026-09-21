"""Knowledge from board posts: the facts worth keeping, so 모카 doesn't have to search the board every time.

Which posts: every category except 가입인사 (its fields are read by chatbot/profiles.py) and 투표 (the vote watcher
records results); never 모카's own posts, so 모카 is never its own source; and not posts titled [테스트].
What 모카 can do is not taken from posts at all — posts go stale or promise ahead, and documents/moca_capabilities.md
is the source of truth for that.

How: one model call per post, only when the post is new or its text changed — the harness keeps a digest per
post. The model proposes up to MAX_FACTS one-sentence facts; the harness checks them (length, names only from the
roster), stores names as placeholders, and records each as `reported`, citing the post (`post:<path>`).

The facts of one post are one versioned set (origin.group / origin.version, harness/knowledge.py: visible):
an edited post gets a fresh extraction that replaces the whole set, and a deleted post gets a tombstone that
withdraws it. Board posts are public in the 모임, so their facts name people for everyone, like intros.

When: right after a board sync saves or updates a post (run.py: post_session) — new posts arrive by
notification, edits and deletions with the full re-sync every few hours.

Usage (from the moca/ directory):
    python -m harness.post_facts            # extract what's missing or stale, and print it
"""
import hashlib
import json
import sys

from chatbot.agent import ACCOUNT_NAME, MODEL
from chatbot.posts import load_index, stored_body
from harness import knowledge

SKIP_CATEGORIES = ("가입인사", "투표")
MAX_FACTS = 10
STATEMENT_LIMIT = 200
BODY_LIMIT = 12000   # the longest post today is ~5,000자
DELETED = "deleted"

INSTRUCTIONS = """이 모임은 AI 에이전트 모카가 운영하는 소모임이야. 운영이란 정모와 활동을 기획하고, 멤버들이 무엇을
원하고 무엇을 할 수 있는지 파악하고, 모임의 규칙과 방향을 지키는 일이야.

아래는 모임 게시판의 글 하나야. 운영 모카가 모임을 꾸려 갈 때 두고두고 참고할 사실만 한 문장씩 뽑아 줘.

뽑을 것
- 모임의 규칙, 공지, 이용 방법
- 모임의 방향과 목표, 모카에게 기대하는 역할
- 예정된 활동·변경·이벤트와 그 조건. 앞으로의 일은 '예정'이라고 분명히 적어.
  아직 없는 기능을 이미 있는 것처럼 쓰지 마.
- 글쓴이나 멤버가 밝힌 관심사, 할 수 있는 것, 원하는 것

빼는 것
- 운영과 상관없는 내용. 예를 들어 누가 최신 AI 소식을 다룬 기사를 공유했다면, 기사 속 사실들은 운영에
  쓸모가 없어. 남길 것은 '글쓴이가 그 주제에 관심이 있다'는 것 정도야.
- 모카의 기능이 지금 무엇을 할 수 있는지, 개발 진행 상황이나 로드맵. 그건 기능 문서가 기준이야.
  (모카가 모임에서 맡기를 기대하는 역할은 남겨.)
- 점검·재시작처럼 곧 의미가 없어지는 일시적인 안내
- 인사말, 감탄, 꾸밈말, 글에 없는 추측
- 건강·정치·종교·재정 같은 민감한 내용, 누군가의 성격에 대한 평가
- 같은 사실의 반복

쓰는 법
- 한 항목에 한 문장, 200자 이내. 글에 적힌 대로, 부풀리지 마.
- 누군가에 대한 사실이면 문장에 그 사람의 앱 이름을 그대로 쓰고 about에도 넣어. 모임 전체에 대한 것이면
  about은 비워. 멤버의 앱 이름: {roster}
- 많아야 {max_facts}개, 중요한 것부터. 뽑을 게 없으면 빈 배열."""

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["facts"],
          "properties": {"facts": {"type": "array", "items": {
              "type": "object", "additionalProperties": False, "required": ["statement", "about"],
              "properties": {"statement": {"type": "string"},
                             "about": {"type": "array", "items": {"type": "string"}}}}}}}


def eligible(entry):
    return (entry["category"] not in SKIP_CATEGORIES and entry["author"] != ACCOUNT_NAME
            and not entry["title"].lstrip().startswith("[테스트]"))


def _group(entry_or_path):
    path = entry_or_path if isinstance(entry_or_path, str) else entry_or_path["path"]
    return f"post:{path}"


def _digest(entry, body):
    return hashlib.sha1(json.dumps([entry["title"], body], ensure_ascii=False).encode()).hexdigest()[:12]


def extract(client, entry, body, roster):
    """The model's facts for one post, checked by the harness. Returns [(template, subjects)]."""
    from chatbot.group_task import templatize
    from chatbot.agent import plain_text
    prompt = (f"게시판 [{entry['category']}] 「{entry['title']}」 — 글쓴이 {entry['author']}, {entry['time']}\n\n"
              f"{body[:BODY_LIMIT]}")
    resp = client.responses.create(
        model=MODEL, reasoning={"effort": "medium"},
        instructions=INSTRUCTIONS.format(roster=", ".join(sorted(roster)), max_facts=MAX_FACTS), input=prompt,
        text={"format": {"type": "json_schema", "name": "post_facts", "strict": True, "schema": SCHEMA}})
    facts = []
    for f in json.loads(resp.output_text)["facts"][:MAX_FACTS]:
        statement = plain_text(f["statement"])[:STATEMENT_LIMIT].strip()
        if not statement:
            continue
        # names only from the roster, whether the model tagged them or just wrote them
        names = {n for n in f["about"] if n in roster} | {n for n in roster if len(n) > 1 and n in statement}
        facts.append(templatize(statement, names))
    return facts


def sync(client, log=None, dry_run=False):
    """Extract facts for posts that are new or changed, withdraw facts of deleted posts. Returns records added."""
    from chatbot.profiles import profiles
    versions, added = knowledge.latest_versions(), []
    posts = [e for e in load_index()["posts"] if eligible(e)]
    roster = set(profiles()) | {e["author"] for e in posts}
    for entry in posts:
        body = stored_body(entry)
        digest = _digest(entry, body)
        if versions.get(_group(entry)) == digest:
            continue  # already extracted from exactly this text
        facts = extract(client, entry, body, roster)
        if log:
            log(f"게시글 「{entry['title']}」에서 사실 {len(facts)}건")
        if dry_run:
            added += [{"statement": t, "subjects": s, "origin": {"title": entry["title"]}} for t, s in facts]
            continue
        origin = {"channel": "post", "group": _group(entry), "version": digest, "post": entry["path"],
                  "title": entry["title"], "category": entry["category"]}
        for template, subjects in facts:
            added.append(knowledge.add(template, subjects, "reported", [_group(entry)], origin))
        if not facts:  # nothing worth keeping: mark this text as done, and hide any older facts
            knowledge.add("", [], "reported", [], {**origin, "retracted": True})
    live = {_group(e) for e in posts}
    for group, version in versions.items():
        if group.startswith("post:") and group not in live and version != DELETED and not dry_run:
            knowledge.add("", [], "reported", [], {"channel": "post", "group": group, "version": DELETED,
                                                   "retracted": True})
            if log:
                log(f"사라진 게시글의 사실을 거둠: {group}")
    return added


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    from cua.agent import openai_client
    added = sync(openai_client(), log=print, dry_run="--dry-run" in sys.argv)
    for r in added:
        print(f"  「{r['origin']['title']}」 {knowledge.render({**r, 'origin': {'channel': 'post'}})}")


if __name__ == "__main__":
    main()
