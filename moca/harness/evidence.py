"""Evidence review: link knowledge to hypotheses and move their status (documents/hypothesis.md, stage 2).

At the start of each 운영 round the harness asks a reviewer (the same model 운영 모카 uses) to look at each live
hypothesis against knowledge it hasn't been checked against yet — for a hypothesis that was never reviewed, all
visible knowledge (the backfill). The reviewer picks the records that bear on the claim, says which way and how
strongly, and proposes a status. The harness decides what sticks:

- evidence is a pointer to a knowledge record, never free text: {ref, direction, weight, note, at};
- only `reported` or `observed` knowledge counts — never 모카's own inferences — and never the records the
  hypothesis was created from (its grounds), or it would support itself;
- records from the same source (one post, one vote, one 정모's sign-ups, one member's intro or notes, one chat
  task answer, one day of chat) count as one piece of evidence, so "five members said it" and "one member
  said it five ways" stay different;
- a proposed status is kept only if the evidence clears its bar (THRESHOLDS); otherwise the harness steps it
  down to the strongest status the evidence does support, and the history says so.

Evidence is judged against knowledge as it is shown now: a record hidden since (consent withdrawn, post edited
or turned 비밀글) stops counting, and a status the remaining evidence can't carry is stepped down.

Usage (from the moca/ directory):
    python -m harness.evidence --dry-run     # review everything due and print what would change
    python -m harness.evidence               # review for real
"""
import json
import sys
import time

from harness import hypotheses as H
from harness import knowledge

MODEL_EFFORT = "high"
WEIGHT = {"weak": 1, "moderate": 2, "strong": 3}
ORDER = ["refuted", "weakened", "open", "supported", "confirmed"]
MAX_CANDIDATES = 200   # knowledge records shown to the reviewer in one call; the backfill today is ~120

# starting numbers, to be tuned after real reviews. S/W = support/weaken score over independent sources
# (each source counts its strongest link), nS/nW = number of independent sources on each side.
THRESHOLDS = """\
- 뒷받침됨(supported): 지지 점수 2 이상이고 약화 점수보다 크다
- 충분히 뒷받침됨(confirmed): 지지하는 독립 출처 3개 이상, 지지 점수 6 이상, 그중 하네스가 관찰한 사실(observed)이 하나 이상, 약화 점수는 지지 점수의 3분의 1 이하
- 약해짐(weakened): 약화 점수 2 이상이고 지지 점수 이상
- 폐기(refuted): 약화하는 독립 출처 2개 이상, 약화 점수 4 이상, 지지 점수의 2배 이상
- 점수: weak 1, moderate 2, strong 3. 같은 출처에서 나온 근거는 가장 센 하나만 센다."""


def allowed(status, tally):
    s, w, ns, nw, observed = tally["S"], tally["W"], tally["nS"], tally["nW"], tally["observed_support"]
    return {"open": True,
            "supported": s >= 2 and s > w,
            "confirmed": ns >= 3 and s >= 6 and observed and w * 3 <= s,
            "weakened": w >= 2 and w >= s,
            "refuted": nw >= 2 and w >= 4 and w >= 2 * s}[status]


def settle(proposed, tally):
    """The proposed status if the evidence carries it, else the next one toward 'open' that it does."""
    i, mid = ORDER.index(proposed), ORDER.index("open")
    step = -1 if i > mid else 1
    while i != mid and not allowed(ORDER[i], tally):
        i += step
    return ORDER[i]


def settle_confidence(proposed, tally):
    """High confidence needs three independent sources, medium two; otherwise low."""
    sources = tally["nS"] + tally["nW"]
    cap = "high" if sources >= 3 else "medium" if sources >= 2 else "low"
    order = ["low", "medium", "high"]
    return order[min(order.index(proposed or "low"), order.index(cap))]


def source(k):
    """What makes two knowledge records the same piece of evidence."""
    o, subj = k["origin"], tuple(k["subjects"])
    ch = o.get("channel")
    if ch == "post":
        return ("post", o.get("group"))
    if ch in ("intro", "member_note", "membership"):
        return (ch, subj)
    if ch == "vote":
        return ("vote", tuple(k["source_refs"]), subj)
    if ch in ("event", "chat_stats"):
        return (ch, o.get("key"))
    if o.get("task"):
        return ("task", o["task"], subj)
    return ("record", k["id"])


def tally(h, visible):
    """Score a hypothesis's evidence as the knowledge stands now."""
    best = {}  # (direction, source) -> (weight, basis)
    for e in h.get("evidence", []):
        k = visible.get(e["ref"])
        if k is None:
            continue  # hidden since, or gone: it no longer counts
        key = (e["direction"], source(k))
        if WEIGHT[e["weight"]] > best.get(key, (0, None))[0]:
            best[key] = (WEIGHT[e["weight"]], k["basis"])
    side = lambda d: [v for (dd, _), v in best.items() if dd == d]
    sup, wk = side("supports"), side("weakens")
    return {"S": sum(w for w, _ in sup), "W": sum(w for w, _ in wk), "nS": len(sup), "nW": len(wk),
            "observed_support": any(b == knowledge.OBSERVED for _, b in sup)}


INSTRUCTIONS = """너는 모카의 가설을 근거와 맞춰 보는 검토자야. 가설 하나와, 이 가설과 아직 맞춰 보지 않은 지식을 줄게.

할 일
1. 지식 하나하나가 이 가설을 뒷받침하는지(supports), 약하게 만드는지(weakens) 판단해서, 관련 있는 것만 골라.
   가설의 확인 방법(test)을 기준으로 봐. 억지로 연결하지 말고, 상관없는 지식은 고르지 마.
2. weight: strong = 가설을 직접 확인하거나 반박하는 발언·관찰, moderate = 분명히 관련 있지만 간접적인 것,
   weak = 약한 정황.
3. 같은 출처(같은 글, 같은 투표, 같은 사람의 자기소개나 메모, 같은 날의 채팅 통계)에서 나온 여러 지식은
   하나의 근거로 세어지니, 그중 가장 잘 맞는 것 하나만 골라도 돼.
4. 참여나 행동에 대한 가설이라면, 멤버가 말한 것(reported)보다 하네스가 관찰한 사실(observed)이 더 센 근거야.
   말로 원한다고 한 것과 실제로 한 일이 다를 수 있어.
5. note에는 이 지식이 왜 그쪽 근거인지 한 문장으로.
6. 마지막으로, 이미 연결된 근거와 이번에 고른 근거를 모두 보고 가설의 상태와 확신을 제안해.
   상태는 근거가 이 기준을 넘을 때만 올라가거나 내려가고, 넘지 못하면 하네스가 한 단계씩 되돌린다:
{thresholds}
   확신(confidence)은 이 상태 판단을 얼마나 믿는지야: 독립 출처가 셋 이상이어야 high, 둘이면 medium까지.
   reason에는 상태를 그렇게 본 이유를 한두 문장으로."""

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["links", "status", "confidence", "reason"],
          "properties": {
              "links": {"type": "array", "items": {
                  "type": "object", "additionalProperties": False,
                  "required": ["knowledge_id", "direction", "weight", "note"],
                  "properties": {"knowledge_id": {"type": "string"},
                                 "direction": {"type": "string", "enum": ["supports", "weakens"]},
                                 "weight": {"type": "string", "enum": list(WEIGHT)},
                                 "note": {"type": "string"}}}},
              "status": {"type": "string", "enum": ORDER},
              "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
              "reason": {"type": "string"}}}


def _hypothesis_text(h, visible):
    lines = [f"가설 {h['id']} [{H.KINDS[h['kind']]}] {H.render(h)}",
             f"확인 방법: {h['test']}",
             f"세울 때의 추론: {h['grounds'].get('reasoning', '')}",
             f"지금 상태: {H.STATUSES[h['status']]}" + (f", 확신 {H.CONFIDENCE[h['confidence']]}" if h.get("confidence") else "")]
    linked = [(e, visible[e["ref"]]) for e in h.get("evidence", []) if e["ref"] in visible]
    lines.append("이미 연결된 근거:" if linked else "이미 연결된 근거: (없음)")
    lines += [f"- [{e['direction']} · {e['weight']}] {knowledge.render(k)} — {e['note']}" for e, k in linked]
    return "\n".join(lines)


def _candidates(h, visible):
    """Knowledge this hypothesis hasn't been checked against: everything visible the first time, afterwards
    only what arrived since. Never its own grounds, never 모카's inferences, never what's already linked."""
    since = h.get("reviewed_until")
    skip = set(h["grounds"].get("knowledge", [])) | {e["ref"] for e in h.get("evidence", [])}
    out = [k for k in visible.values()
           if k["basis"] in knowledge.GROUNDING and k["id"] not in skip and (since is None or k["created_at"] > since)]
    if len(out) > MAX_CANDIDATES:
        # past the cap, records about the hypothesis's own members, 정모 and votes come first, then the newest
        tags = set(h["members"]) | set(h.get("events", [])) | set(h.get("votes", []))
        about = lambda k: bool(tags & (set(k["subjects"]) | {k["origin"].get("event"), k["origin"].get("title")}))
        out = sorted(out, key=lambda k: (about(k), k["created_at"]), reverse=True)[:MAX_CANDIDATES]
    return out


def _source_label(k):
    o = k["origin"]
    return {"post": f"게시글 「{o.get('title')}」", "intro": "자기소개", "member_note": "멤버 메모",
            "membership": "모임 채팅 관찰", "chat_stats": "채팅 통계", "event": "정모 참석자 명단",
            "vote": "투표 결과"}.get(o.get("channel"), f"작업 {o.get('task')}")


def review_one(client, h, visible, log=None):
    """One reviewer call for one hypothesis. Returns (links, proposal) or (None, None) when nothing is due."""
    from chatbot.agent import MODEL
    cands = _candidates(h, visible)
    if not cands:
        return None, None, cands
    listing = "\n".join(f"- {k['id']} ({knowledge.BASIS_LABEL[k['basis']]}, {_source_label(k)}, {k['created_at'][:10]}) "
                        f"{knowledge.render(k)}" for k in cands)
    resp = client.responses.create(
        model=MODEL, reasoning={"effort": MODEL_EFFORT}, instructions=INSTRUCTIONS.format(thresholds=THRESHOLDS),
        input=f"{_hypothesis_text(h, visible)}\n\n## 아직 맞춰 보지 않은 지식 ({len(cands)}건)\n{listing}",
        text={"format": {"type": "json_schema", "name": "evidence_review", "strict": True, "schema": SCHEMA}})
    out = json.loads(resp.output_text)
    ids = {k["id"] for k in cands}
    links, seen = [], set()
    for link in out["links"]:
        if link["knowledge_id"] in ids and link["knowledge_id"] not in seen:  # only what it was shown, once
            seen.add(link["knowledge_id"])
            links.append(link)
    return links, out, cands


def review(client, log=None, dry_run=False):
    """Review every live hypothesis that has knowledge it hasn't been checked against, and re-check statuses
    against the evidence as it stands. Returns ([(hypothesis id, old status, new status)], the hypotheses).
    With `dry_run` nothing is saved; the returned hypotheses show what would have been."""
    records = H.load()
    visible = {k["id"]: k for k in knowledge.load_all()}
    changes, now = [], time.strftime("%Y-%m-%d %H:%M:%S")
    for h in records:
        if h["status"] == "refuted":
            continue
        h.setdefault("evidence", [])
        links, proposal, cands = review_one(client, h, visible, log)
        before = (h["status"], h.get("confidence"))
        for link in links or []:
            h["evidence"].append({"ref": link["knowledge_id"], "direction": link["direction"],
                                  "weight": link["weight"], "note": link["note"].strip()[:200], "at": now})
        t = tally(h, visible)
        proposed = proposal["status"] if proposal else h["status"]
        status = settle(proposed, t)
        confidence = settle_confidence(proposal["confidence"] if proposal else h.get("confidence"), t) \
            if (proposal or h.get("confidence")) else None
        if cands:
            h["reviewed_until"] = max(k["created_at"] for k in cands)
        if (status, confidence) != before or links:
            note = proposal["reason"].strip()[:300] if proposal else "보이지 않게 된 근거를 빼고 다시 셈"
            if status != proposed:
                note += f" (하네스: 제안한 '{H.STATUSES[proposed]}'은 근거 기준을 넘지 못해 '{H.STATUSES[status]}'로 둠)"
            h.setdefault("history", []).append({"at": now, "from": before[0], "to": status, "confidence": confidence,
                                                "added": len(links or []), "tally": t, "reason": note})
            h["status"], h["confidence"], h["updated_at"] = status, confidence, now
        if log and (links or status != before[0]):
            log(f"가설 {h['id']} 검토: 근거 {len(links or [])}건 추가, {H.STATUSES[before[0]]} → {H.STATUSES[status]}"
                f"{' (확신 ' + H.CONFIDENCE[confidence] + ')' if confidence else ''}"
                f" [지지 {t['S']}점·{t['nS']}출처, 약화 {t['W']}점·{t['nW']}출처]")
        if status != before[0]:
            changes.append((h["id"], before[0], status))
    if not dry_run:
        H.save(records)
    return changes, records


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    from cua.agent import openai_client
    _, records = review(openai_client(), log=print, dry_run="--dry-run" in sys.argv)
    visible = {k["id"]: k for k in knowledge.load_all()}
    for h in records:
        print(H.line(h))
        for e in h.get("evidence", []):
            k = visible.get(e["ref"])
            print(f"    [{e['direction']}/{e['weight']}] {knowledge.render(k) if k else '(보이지 않음)'} — {e['note']}")
        for c in h.get("history", [])[-1:]:
            print(f"    ▶ {c['from']} → {c['to']} ({c['confidence']}) {c['tally']} — {c['reason']}")


if __name__ == "__main__":
    main()
