"""Knowledge: sourced observations, reports and inferences the agents can build decisions on (documents/orient.md).

One record per line in data/knowledge/records.jsonl:

    {"id": "k_...", "statement": "{s0}는 주말 오후에 대체로 참여 가능하다고 밝혔다.", "subjects": ["멤버이름"],
     "basis": "reported" | "inferred" | "observed", "source_refs": ["m123"], "origin": {"task": "t_...", "channel": "group_chat"},
     "created_at": "..."}

Member names are stored as placeholders ({s0}, {s1}, …) and filled in only when the record is shown, following
each member's current 활용 범위 answer (chatbot/memory_consent.py): named if they allowed 모임 운영 use, "한 멤버"
otherwise. So /memory off takes effect on knowledge already recorded, not only on new records.

Usage (from the moca/ directory):
    python -m harness.knowledge            # every record as 운영 모카 would see it
"""
import json
import re
import secrets
import sys
import time

from harness.tasks import ROOT

KNOWLEDGE = ROOT / "data" / "knowledge"
RECORDS = KNOWLEDGE / "records.jsonl"
BASIS = ["reported", "inferred"]   # what a model may claim in a task report
OBSERVED = "observed"              # a fact the harness saw itself (counts, sign-ups, who joined) — never from a model
# what may ground a hypothesis: something a member said, or something the harness saw. 모카's own inferences
# may not, or 모카 would end up supporting its guesses with its guesses (documents/hypothesis.md)
GROUNDING = ("reported", OBSERVED)
BASIS_LABEL = {"reported": "멤버가 직접 말함", "inferred": "모카의 추론", OBSERVED: "하네스가 관찰한 사실"}
ANONYMOUS = "한 멤버"


def add(statement, subjects, basis, source_refs, origin):
    record = {"id": f"k_{time.strftime('%Y%m%d')}_{secrets.token_hex(3)}", "statement": statement,
              "subjects": list(subjects), "basis": basis, "source_refs": list(source_refs), "origin": origin,
              "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    KNOWLEDGE.mkdir(parents=True, exist_ok=True)
    with open(RECORDS, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


# Records derived from a standing source (a member note, an intro field) carry origin.key. The file stays
# append-only: when the source changes, a new record is added and only the newest one per key is shown.
# A record from a member note is shown only while that member allows 모임 운영 use — it is theirs to withdraw,
# and unlike chat answers it is never shown as '한 멤버' instead (chatbot/memory_consent.py).
PRIVATE_CHANNELS = ("member_note",)
# the 가입인사 post is public in the 모임 and written for 모카, so its facts carry the member's name for everyone
# (chatbot/profiles.py); every other channel shows names only for members who allowed 모임 운영 use
PUBLIC_CHANNELS = ("intro", "membership", "post")  # who joined and what a board post says are public too


def visible(records):
    """Hide what was superseded or withdrawn. Two ways a record is superseded: a newer record with the same
    origin.key (one fact per source), or a newer version of its origin.group (a set of facts drawn from one
    source together, like the facts of a board post — edit the post and the whole set is replaced). A record
    with origin.retracted is a tombstone: it hides its group and is never shown itself."""
    from chatbot.memory_consent import shares  # consent is read at display time, on purpose
    newest, version = {}, {}
    for r in records:
        o = r["origin"]
        if o.get("key"):
            newest[o["key"]] = r["id"]
        if o.get("group"):
            version[o["group"]] = o.get("version")
    return [r for r in records
            if not r["origin"].get("retracted")
            and (not r["origin"].get("key") or newest[r["origin"]["key"]] == r["id"])
            and (not r["origin"].get("group") or version[r["origin"]["group"]] == r["origin"].get("version"))
            and (r["origin"].get("channel") not in PRIVATE_CHANNELS or all(shares(s) for s in r["subjects"]))]


def latest_versions():
    """{origin.group: newest version} over everything stored, tombstones included."""
    out = {}
    for r in load_all(include_hidden=True):
        if r["origin"].get("group"):
            out[r["origin"]["group"]] = r["origin"].get("version")
    return out


def load_all(include_hidden=False):
    """Every record 운영 모카 may see now. `include_hidden` is for the harness's own bookkeeping only."""
    if not RECORDS.exists():
        return []
    records = [json.loads(line) for line in RECORDS.read_text(encoding="utf-8").splitlines() if line.strip()]
    return records if include_hidden else visible(records)


def latest_by_key():
    """{origin.key: newest record} over everything stored, hidden or not — so a source isn't re-added."""
    out = {}
    for r in load_all(include_hidden=True):
        if r["origin"].get("key"):
            out[r["origin"]["key"]] = r
    return out


def render(record):
    """The statement as 운영 모카 may see it right now."""
    from chatbot.memory_consent import shares  # consent is read at display time, on purpose
    from chatbot.profiles import call_name     # and so is the name they asked to be called
    public = record.get("origin", {}).get("channel") in PUBLIC_CHANNELS
    names = [call_name(n) if public or shares(n) else None for n in record["subjects"]]

    def fill(m):
        i = int(m.group(1))
        if i >= len(names):
            return m.group(0)
        # '{s0}님의' → '페피토님의' but '한 멤버의': the honorific belongs to a name, not to '한 멤버'
        word = ANONYMOUS if names[i] is None else names[i] + (m.group(2) or "")
        return word + (_particle(word, m.group(3)) if m.group(3) else "")

    # the particle after a name was written for whichever name the model saw, but the name shown now may be
    # another one ('한 멤버'), so it is chosen again: 로하는 / 김범진은 / 한 멤버는
    return re.sub(r"\{s(\d+)\}(님)?(은|는|이|가|을|를|과|와)?", fill, record["statement"])


PARTICLES = {"은": ("은", "는"), "는": ("은", "는"), "이": ("이", "가"), "가": ("이", "가"),
             "을": ("을", "를"), "를": ("을", "를"), "과": ("과", "와"), "와": ("과", "와")}


def _particle(word, written):
    """The form of `written` that fits `word`: after a final consonant the first, otherwise the second."""
    with_final, without = PARTICLES[written]
    last = (word or "")[-1:]
    if "가" <= last <= "힣":
        return with_final if (ord(last) - 0xAC00) % 28 else without
    return written  # not Hangul: keep what was written


def rendered_line(record):
    basis = BASIS_LABEL.get(record["basis"], record["basis"])
    when = f", {record['created_at'][:10]}" if record.get("created_at") else ""
    return f"- {render(record)} ({basis}{when})"


def recent(n=20):
    return load_all()[-n:]


def since(timestamp):
    """Records created after `timestamp` ("YYYY-MM-DD HH:MM:SS"), oldest first; all of them if it is None."""
    return [r for r in load_all() if timestamp is None or r["created_at"] > timestamp]


def search(query=None, basis=None, since_date=None, subject=None, limit=20):
    """Records matching every given filter, newest first, as 운영 모카 may see them.

    Everything is matched against the rendered text — members who haven't allowed 운영 use are already
    '한 멤버' there — so searching for their name finds nothing, and `subject` only matches members who
    allowed it. Source message ids are left out: reading the original message would give the name away."""
    from chatbot.memory_consent import shares
    found = []
    for r in reversed(load_all()):
        text = render(r)
        if query and not all(word in text for word in query.split()):
            continue
        if basis and r["basis"] != basis:
            continue
        if since_date and r["created_at"][:10] < since_date:
            continue
        if subject and not (subject in r["subjects"] and shares(subject)):
            continue
        found.append({"id": r["id"], "statement": text, "basis": r["basis"], "created_at": r["created_at"],
                      "task": r["origin"].get("task")})
        if len(found) >= limit:
            break
    return found


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    records = load_all()
    for r in records:
        print(f"{r['id']}  {rendered_line(r)[2:]}  근거 {r['source_refs']}  (작업 {r['origin'].get('task')})")
    if not records:
        print("아직 쌓인 지식이 없습니다.")


if __name__ == "__main__":
    main()
