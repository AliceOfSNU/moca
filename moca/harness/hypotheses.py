"""Hypotheses: what 모카 believes about its 모임 but can't prove (documents/hypothesis.md).

Everything 모카 infers about what members want is a guess. A hypothesis makes the guess explicit, says what it
rests on, and says how it could be checked — so it can later be supported or dropped by evidence instead of
quietly hardening into "what everyone knows".

Creating a hypothesis is here; linking evidence and moving the status is harness/evidence.py, which reviews every
live hypothesis at the start of each 운영 round (a new hypothesis's first review covers all visible knowledge).

One record per hypothesis in data/hypotheses/hypotheses.json:

    {"id": "h_20260921_3fa1", "kind": "need",
     "claim": "{s0}님과 {s1}님은 자기가 만든 AI 결과물을 모임에서 보여 주고 싶어 한다.",
     "members": ["정재용", "하루"],                 # who it is about; also the {s…} placeholders in `claim`
     "events": [], "votes": [],                     # 정모 / 투표 it is about, if any
     "grounds": {"knowledge": ["k_…"], "reasoning": "왜 이렇게 보는지"},
     "test": "무엇을 보면 뒷받침되거나 약해지는지",
     "status": "open", "confidence": null,          # see STATUSES / CONFIDENCE
     "evidence": [],                                # stage 2
     "goal_id": "g_…", "created_at": "…", "updated_at": "…"}

Names follow the knowledge rule (harness/knowledge.py): stored as placeholders, shown by name only for members
who allowed 모임 운영 use, "한 멤버" otherwise. Only 운영 모카 creates or sees hypotheses: chat 모카 never
holds an unverified guess about the person it is talking to.

What can ground a hypothesis now, and count as evidence in stage 2: knowledge that is either `reported` (a member
said it — chat tasks, consented notes, intros) or `observed` (the harness saw it — first seen in the chat, daily
chat stats, 정모 sign-ups, vote results). Never `inferred`: 모카 would be propping up its guesses with its guesses.
Who actually came to a 정모 isn't known — the app doesn't show it.

Usage (from the moca/ directory):
    python -m harness.hypotheses            # every hypothesis, as 운영 모카 sees it
"""
import difflib
import json
import re
import secrets
import sys
import time

from harness.tasks import ROOT

DATA = ROOT / "data" / "hypotheses"
FILE = DATA / "hypotheses.json"

KINDS = {"need": "원하는 것", "behavior": "행동 패턴", "relationship": "멤버 사이의 관계",
         "commitment": "참여 의지", "mechanism": "모임 운영 방식의 효과"}
# from refuted to confirmed; `open` is where every hypothesis starts
STATUSES = {"refuted": "폐기", "weakened": "약해짐", "open": "검증 전", "supported": "뒷받침됨",
            "confirmed": "충분히 뒷받침됨"}
CONFIDENCE = {"low": "낮음", "medium": "보통", "high": "높음"}  # how sure 모카 is of the status, not of the claim
LIMITS = {"claim": 200, "reasoning": 400, "test": 250}
MAX_ACTIVE = 15          # 폐기되지 않은 가설. 늘리기 전에 검증하거나 정리하라는 뜻
SIMILAR = 0.72           # 이 이상 비슷하면 같은 가설로 본다
SHOWN = 10               # 운영 모카 프롬프트에 보여 줄 가설 수


def load():
    if FILE.exists():
        return json.loads(FILE.read_text(encoding="utf-8"))
    return []


def save(records):
    DATA.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")


def active(records=None):
    return [h for h in (records if records is not None else load()) if h["status"] != "refuted"]


def render(h):
    from harness import knowledge  # the same consent rule as knowledge, read at display time
    return knowledge.render({"statement": h["claim"], "subjects": h["members"]})


def _squash(text):
    return re.sub(r"[\s.,!?~…'\"]+", "", text or "")


def _similar(a, b):
    a, b = _squash(a), _squash(b)
    return bool(a and b) and difflib.SequenceMatcher(None, a, b).ratio() >= SIMILAR


def create(claim, kind, members=None, events=None, votes=None, knowledge_ids=None, reasoning=None, test=None,
           goal_id=None, created_by="admin"):
    """Store a hypothesis if it passes the rules. Returns (record, None) or (None, why it was refused)."""
    from chatbot.group_task import templatize
    from chatbot.profiles import profiles
    from harness import knowledge
    claim, reasoning, test = ((v or "").strip() for v in (claim, reasoning, test))
    members, events, votes, knowledge_ids = ([x for x in (v or []) if x] for v in (members, events, votes, knowledge_ids))

    if not claim:
        return None, "claim이 비어 있습니다"
    if kind not in KINDS:
        return None, f"kind는 {list(KINDS)} 중 하나여야 합니다"
    for field, value in (("claim", claim), ("reasoning", reasoning), ("test", test)):
        if len(value) > LIMITS[field]:
            return None, f"{field}는 {LIMITS[field]}자 이내여야 합니다"
    if not reasoning:
        return None, "reasoning이 비어 있습니다. 왜 이렇게 보는지 적으세요"
    if not test:
        return None, "test가 비어 있습니다. 무엇을 보면 뒷받침되거나 약해지는지 적으세요 (검증할 수 없으면 가설이 아닙니다)"

    roster = profiles()
    unknown = [m for m in members if m not in roster]
    if unknown:
        return None, f"모르는 멤버입니다: {unknown}. member_search로 앱 이름을 확인하세요"
    known = {k["id"]: k for k in knowledge.load_all()}
    missing = [k for k in knowledge_ids if k not in known]
    if missing:
        return None, f"없는 지식 id입니다: {missing}. knowledge_search로 확인하세요"
    inferred = [k for k in knowledge_ids if known[k]["basis"] not in knowledge.GROUNDING]
    if inferred:
        return None, (f"모카의 추론(inferred)은 근거가 될 수 없습니다: {inferred}. 멤버가 직접 말한 것(reported)이나 "
                      "하네스가 관찰한 사실(observed)을 근거로 쓰세요")
    if kind != "mechanism" and not knowledge_ids:
        return None, ("멤버에 대한 가설은 근거가 되는 지식이 하나 이상 있어야 합니다 (knowledge_ids). "
                      "근거가 아직 없다면 먼저 알아보세요")
    from admin.events import load_index as event_index
    from admin.votes import load_index as vote_index
    unknown = [e for e in events if e not in {x["name"] for x in event_index()["events"]}]
    unknown += [v for v in votes if v not in {x["title"] for x in vote_index()["votes"]}]
    if unknown:
        return None, f"없는 정모나 투표입니다: {unknown}"

    records = load()
    live = active(records)
    if len(live) >= MAX_ACTIVE:
        return None, f"폐기되지 않은 가설이 이미 {len(live)}개입니다. 새로 세우기 전에 기존 가설을 검증하거나 정리하세요"
    # any roster name in the claim becomes a placeholder too, tagged or not
    named = set(members) | {n for n in roster if n and len(n) > 1 and n in claim}
    template, subjects = templatize(claim, named)
    twin = next((h for h in live if _similar(h["claim"], template) and set(h["members"]) == set(subjects)), None)
    if twin:
        return None, f"비슷한 가설이 이미 있습니다: {twin['id']} \"{render(twin)}\""

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    record = {"id": f"h_{time.strftime('%Y%m%d')}_{secrets.token_hex(2)}", "kind": kind, "claim": template,
              "members": subjects, "events": events, "votes": votes,
              "grounds": {"knowledge": knowledge_ids, "reasoning": reasoning}, "test": test,
              "status": "open", "confidence": None, "evidence": [],
              "goal_id": goal_id, "created_by": created_by, "created_at": now, "updated_at": now}
    records.append(record)
    save(records)
    return record, None


def line(h, tally=None):
    about = []
    if h["events"]:
        about.append("정모 " + ", ".join(h["events"]))
    if h["votes"]:
        about.append("투표 " + ", ".join(h["votes"]))
    conf = f", 확신 {CONFIDENCE[h['confidence']]}" if h.get("confidence") else ""
    # a hypothesis seeded from the dashboard rests on 로하's own observation, not on anything 모카 recorded
    who = "로하가 세움 · " if h["grounds"].get("source") == "developer" else ""
    reasoning = h["grounds"].get("reasoning") or ""
    out = (f"- {h['id']} [{KINDS[h['kind']]} · {STATUSES[h['status']]}{conf}] {render(h)}"
           + (f" ({'; '.join(about)})" if about else "")
           + f"\n  {who}세운 근거: 지식 {len(h['grounds']['knowledge'])}건 — {reasoning[:120]}"
           + f"\n  확인 방법: {h['test']}")
    if tally is not None:
        out += f"\n  검토된 증거: 지지 {tally['nS']}출처({tally['S']}점) · 약화 {tally['nW']}출처({tally['W']}점)"
        last = (h.get("history") or [None])[-1]
        if last:
            out += f" — 최근 {last['at'][:10]}: {last['reason'][:120]}"
    return out


def block():
    """For 운영 모카's input: the hypotheses still in play, newest first."""
    live = sorted(active(), key=lambda h: h["updated_at"], reverse=True)
    if not live:
        return "## 모카의 가설\n(아직 없음)"
    from harness import evidence, knowledge
    visible = {k["id"]: k for k in knowledge.load_all()}
    lines = ["## 모카의 가설 (폐기되지 않은 것, 최근 순)"] + [line(h, evidence.tally(h, visible)) for h in live[:SHOWN]]
    if len(live) > SHOWN:
        lines.append(f"(그 밖에 {len(live) - SHOWN}개 더 있음)")
    return "\n".join(lines)


TOOL = {
    "type": "function", "name": "propose_hypothesis",
    "description": "모임에 대한 가설을 하나 세운다. 멤버들이 원하는 것, 행동 패턴, 관계, 참여 의지, 운영 방식의 효과에 대한 "
                   "추측을 근거와 확인 방법과 함께 기록한다. 하네스가 규칙을 확인하고 저장한다.",
    "parameters": {"type": "object", "properties": {
        "claim": {"type": "string", "description": f"가설 한 문장 ({LIMITS['claim']}자 이내). 구체적이고 확인할 수 있게"},
        "kind": {"type": "string", "enum": list(KINDS),
                 "description": "need=원하는 것, behavior=행동 패턴, relationship=관계, commitment=참여 의지, "
                                "mechanism=운영 방식의 효과 (예: 리더보드를 더하면 활동이 늘 것이다)"},
        "members": {"type": "array", "items": {"type": "string"},
                    "description": "가설의 대상인 멤버의 앱 이름. 모임 전체에 대한 가설이면 비워라"},
        "events": {"type": "array", "items": {"type": "string"}, "description": "대상 정모 이름 (있으면)"},
        "votes": {"type": "array", "items": {"type": "string"}, "description": "대상 투표 제목 (있으면)"},
        "knowledge_ids": {"type": "array", "items": {"type": "string"},
                          "description": "근거가 되는 지식 id (k_…). mechanism이 아니면 하나 이상 필요. "
                                         "멤버가 직접 말한 것(reported)이나 관찰한 사실(observed)만 — 추론(inferred)은 안 된다"},
        "reasoning": {"type": "string", "description": f"근거에서 이 가설로 가는 추론 ({LIMITS['reasoning']}자 이내)"},
        "test": {"type": "string",
                 "description": f"무엇을 보면 뒷받침되고 무엇을 보면 약해지는지 ({LIMITS['test']}자 이내)"}},
        "required": ["claim", "kind", "reasoning", "test"]},
}


class Proposals:
    """Tool handler for the goal loop: the model proposes, the harness decides."""

    def __init__(self, goal_id=None, log=None, dry_run=False):
        self.goal_id = goal_id
        self.log = log
        self.dry_run = dry_run

    def __call__(self, claim=None, kind=None, members=None, events=None, votes=None, knowledge_ids=None,
                 reasoning=None, test=None, **_):
        if self.dry_run:
            return f"(dry-run) 가설 요청을 확인했습니다: {claim}"
        record, problem = create(claim, kind, members, events, votes, knowledge_ids, reasoning, test,
                                 goal_id=self.goal_id, created_by=f"goal:{self.goal_id}" if self.goal_id else "admin")
        if problem:
            if self.log:
                self.log(f"  가설 거절: {problem}")
            return f"가설을 세우지 않았습니다: {problem}"
        if self.log:
            self.log(f"  가설 {record['id']}: {render(record)}")
        return f"가설 {record['id']}를 세웠습니다 (상태: 검증 전). 근거가 쌓이면 상태가 바뀝니다."


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    records = load()
    print("\n".join(line(h) + f"\n  추론: {h['grounds']['reasoning']}" for h in records) or "(가설 없음)")


if __name__ == "__main__":
    main()
