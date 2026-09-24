"""별조각 (stardust): what members earn for taking part (documents/stardust.md).

The harness grants it, not 모카 — the model never decides who gets what, and never sees another member's
count. Everything is an append-only line in awards.jsonl; totals and caps are derived from it, so a wrong
rule can be fixed by changing the table rather than the data. No notice is sent when stardust is granted;
only a tier change is worth a message, and that message comes from the system, not from 모카.

Usage (from the moca/ directory):
    python -m harness.stardust                 # every member's total and tier
    python -m harness.stardust 홍길동           # one member, with the last awards
"""
import argparse
import datetime as dt
import json
import sys
import time

from chatbot.store import ROOT

DATA = ROOT / "data" / "stardust"
AWARDS = DATA / "awards.jsonl"
STATE = DATA / "state.json"

# amount: 한 번에 주는 별조각. cap: 그 기간에 이 종류로 받을 수 있는 별조각의 상한 (개수 기준).
RULES = {
    "dm":      {"amount": 1, "window": "day",  "cap": 1, "what": "모카에게 1:1 메시지 보내기"},
    "mention": {"amount": 1, "window": "day",  "cap": 3, "what": "모임 채팅에서 모카 부르기"},
    "post":    {"amount": 3, "window": "week", "cap": 3, "what": "게시글 쓰기"},
    "comment": {"amount": 1, "window": "day",  "cap": 1, "what": "게시글에 댓글 쓰기"},
    "like":    {"amount": 1, "window": "day",  "cap": 1, "what": "게시글에 좋아요 누르기"},
    "vote":    {"amount": 2, "window": "day",  "cap": 2, "what": "투표 참여"},
    "event":   {"amount": 4, "window": None,   "cap": None, "what": "정모 참여"},
}
NOT_YET = ("comment", "like")  # 아직 댓글·좋아요를 읽지 못한다 (documents/moca_capabilities.md)
TIERS = ((10, 1), (20, 2), (50, 3), (100, 4))  # 그 이하면 그 티어, 100 초과는 5


def tier(total):
    return next((t for limit, t in TIERS if total <= limit), 5)


def _load():
    if not AWARDS.exists():
        return []
    return [json.loads(line) for line in AWARDS.read_text(encoding="utf-8").splitlines() if line.strip()]


def _append(record):
    DATA.mkdir(parents=True, exist_ok=True)
    with open(AWARDS, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    DATA.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def _window_start(window, now=None):
    now = now or dt.datetime.now()
    if window == "day":
        return now.strftime("%Y-%m-%d")
    if window == "week":  # 월요일 시작
        return (now - dt.timedelta(days=now.weekday())).strftime("%Y-%m-%d")
    return ""


def earned(member, kind, window=None, awards=None):
    """How much this member already got for `kind` inside the current window."""
    since = _window_start(window) if window else ""
    return sum(a["amount"] for a in (awards if awards is not None else _load())
               if a["member"] == member and a["kind"] == kind and a["at"][:10] >= since)


def award(member, kind, ref=None, log=None):
    """Grant stardust if the rules allow it. Returns the amount granted (0 when capped or already paid).
    `ref` makes an award idempotent: the same post, vote or 정모 never pays twice."""
    rule = RULES.get(kind)
    if rule is None or not member:
        return 0
    awards = _load()
    if ref is not None and any(a["member"] == member and a["kind"] == kind and a.get("ref") == ref for a in awards):
        return 0
    if rule["cap"] is not None and earned(member, kind, rule["window"], awards) + rule["amount"] > rule["cap"]:
        return 0
    _append({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "member": member, "kind": kind,
             "amount": rule["amount"], "ref": ref})
    if log:
        log(f"  별조각 +{rule['amount']} → {member} ({rule['what']})")
    return rule["amount"]


def total(member, awards=None):
    return sum(a["amount"] for a in (awards if awards is not None else _load()) if a["member"] == member)


def members():
    awards = _load()
    return {m: total(m, awards) for m in dict.fromkeys(a["member"] for a in awards)}


def summary(member, awards=None):
    awards = awards if awards is not None else _load()
    mine = [a for a in awards if a["member"] == member]
    got = total(member, mine)
    return {"member": member, "total": got, "tier": tier(got),
            "next": next((limit + 1 for limit, t in TIERS if got <= limit), None),
            "recent": mine[-5:]}


# --- what the member sees -------------------------------------------------------------

def status_text(member):
    s = summary(member)
    line = f"지금 별조각 {s['total']}개, 티어 {s['tier']} 단계야."
    if s["next"]:
        line += f" {s['next'] - s['total']}개 더 모으면 티어 {s['tier'] + 1} 단계."
    return line


def help_text():
    lines = ["별조각은 모임 활동에 참여하면 프로그램이 자동으로 주는 표식이야. 받을 때 따로 알림은 가지 않아.", ""]
    for kind, rule in RULES.items():
        if kind in NOT_YET:
            continue
        cap = "" if rule["cap"] is None else (
            f" (하루 최대 {rule['cap']}개)" if rule["window"] == "day" else f" (한 주 최대 {rule['cap']}개)")
        lines.append(f"- {rule['what']}: {rule['amount']}개{cap}")
    lines += ["", "티어: 10개 이하 1단계, 20개 이하 2단계, 50개 이하 3단계, 100개 이하 4단계, 100개 초과 5단계.",
              "티어가 오르면 프로그램이 1:1로 알려줘.",
              "쓸 곳은 아직 없어 — 지금은 모으는 재미야. 나중에 투표에서 본인 의견의 가중치를 올리는 데 쓸 수 있게 준비 중이야.",
              "다른 사람의 별조각 개수는 아무도 볼 수 없어. 네 것만 /stardust로 볼 수 있어."]
    return "\n".join(lines)


def parse_command(text):
    """'/stardust' or '/stardust help' typed in a 1:1. Returns "show" | "help" | None."""
    word = (text or "").strip().lower().split()
    if not word or word[0] not in ("/stardust", "/별조각"):
        return None
    return "help" if len(word) > 1 and word[1] in ("help", "도움말") else "show"


# --- tier notices ---------------------------------------------------------------------

def tier_notices():
    """Members whose tier went up since the last notice: [(member, old_tier, new_tier, total)]."""
    state, out = load_state(), []
    for member, got in members().items():
        now, before = tier(got), state.get(member, {}).get("tier_notified", 1)
        if now > before:
            out.append((member, before, now, got))
    return out


def mark_notified(member, new_tier):
    state = load_state()
    state.setdefault(member, {}).update(tier_notified=new_tier, at=time.strftime("%Y-%m-%d %H:%M:%S"))
    save_state(state)


def notice_text(member, new_tier, got):
    """The system's own message — 모카 doesn't write this and doesn't send it."""
    from chatbot.profiles import call_name
    return (f"[알림] 축하해요! {call_name(member)}님이 별조각 {got}개를 모아 티어 {new_tier} 단계가 되었어요.\n"
            "별조각은 모임 활동에 참여하면 프로그램이 자동으로 드리는 표식이에요. "
            "'/stardust'로 내 별조각과 티어를, '/stardust help'로 지급 기준을 볼 수 있어요.")


# --- the daily sweep ------------------------------------------------------------------
# 채팅·1:1·게시글은 하네스가 그 일을 하는 김에 바로 준다. 투표와 정모는 앱 화면을 따로 열어 봐야 알 수 있어서
# 하루에 한 번만 훑는다 (documents/stardust.md).

SWEEP = DATA / "sweep.json"


def load_sweep():
    if SWEEP.exists():
        return json.loads(SWEEP.read_text(encoding="utf-8"))
    return {"votes": {}, "events": {}}


def save_sweep(state):
    DATA.mkdir(parents=True, exist_ok=True)
    SWEEP.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def sweep(events_ui, votes_ui, log, dry_run=False):
    """Award 투표 참여 and 정모 참여. Reads the app, so it runs once a day."""
    from chatbot.agent import ACCOUNT_NAME
    state = load_sweep()
    _sweep_votes(votes_ui, state, log, dry_run, ACCOUNT_NAME)
    _sweep_events(events_ui, state, log, dry_run, ACCOUNT_NAME)
    if not dry_run:
        save_sweep(state)


def _sweep_votes(votes_ui, state, log, dry_run, me):
    """Everyone who has voted gets 2, once per vote. An 익명투표 shows no names, so it pays nobody."""
    from admin.votes import is_closed, load_index
    for vote in load_index()["votes"]:
        title = vote["title"]
        if state["votes"].get(title) == "closed":
            continue  # 끝난 투표는 더 볼 필요가 없다
        detail = votes_ui.read(title)
        if detail is None:
            log(f"  투표 '{title}'를 읽지 못해 건너뜀")
            continue
        voters = [name for option in detail["options"] for name in option["voters"] if name != me]
        log(f"  투표 '{title}': 참여 {detail['participants']}명, 이름 확인 {len(set(voters))}명")
        for member in dict.fromkeys(voters):
            if not dry_run:
                award(member, "vote", ref=f"vote:{title}", log=log)
        if detail["closed"] or is_closed(vote):
            state["votes"][title] = "closed"


def _sweep_events(events_ui, state, log, dry_run, me):
    """정모 pays after it is over, to whoever was on the attendee list when it started. The list is
    snapshotted on every sweep, because a 정모 that has passed may not stay in the app's list.
    A program's sign-up 정모 is not a gathering — nobody meets at its date — so it pays nobody."""
    from harness.programs import containers
    from admin.events import load_index
    events = load_index()["events"]
    log(f"  정모 {len(events)}개 참석자 확인")
    for event in events:
        names = events_ui.participants(event["name"])
        if names is None:
            continue
        state["events"].setdefault(event["name"], {}).update(
            when=event["when"], participants=[n for n in names if n != me], seen_at=time.strftime("%Y-%m-%d %H:%M"))
    now, signup = time.strftime("%Y-%m-%d %H:%M"), containers()
    for name, record in state["events"].items():
        if record.get("paid") or not record.get("when") or record["when"] > now:
            continue
        if name in signup:  # 프로그램 참가 등록 정모: 모이는 자리가 아니라 명부다
            record["paid"] = True
            continue
        log(f"  정모 '{name}' 종료 — 참석자 {len(record['participants'])}명에게 별조각")
        if dry_run:
            continue
        for member in record["participants"]:
            award(member, "event", ref=f"event:{name}", log=log)
        record["paid"] = True


# --- what 모카 sees -------------------------------------------------------------------

def member_block(member):
    """For the 1:1 prompt: this member's own stardust. 모카 never sees anyone else's."""
    s = summary(member)
    return (f"\n\n## 별조각 (이 멤버의 것만 보여. 다른 멤버의 개수는 너도 알 수 없어)\n"
            f"- {member}님: 별조각 {s['total']}개, 티어 {s['tier']} 단계"
            + (f" (티어 {s['tier'] + 1}까지 {s['next'] - s['total']}개)" if s["next"] else "") + "\n"
            "- 별조각은 하네스가 규칙에 따라 자동으로 주는 거야. 네가 주거나 늘려줄 수는 없고, 약속하지도 마.\n"
            "- 멤버가 '/stardust'를 치면 본인 개수와 티어를, '/stardust help'를 치면 기준을 하네스가 보여줘.\n"
            "- 티어가 오르면 하네스가 1:1로 축하 메시지를 보내. 네 메시지가 아니야.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("member", nargs="?")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if not args.member:
        for member, got in sorted(members().items(), key=lambda kv: -kv[1]):
            print(f"{member}: {got}개 (티어 {tier(got)})")
        return
    s = summary(args.member)
    print(f"{s['member']}: {s['total']}개 (티어 {s['tier']})")
    for a in s["recent"]:
        print(f"  {a['at']} +{a['amount']} {RULES[a['kind']]['what']}" + (f" [{a['ref']}]" if a.get("ref") else ""))


if __name__ == "__main__":
    main()
