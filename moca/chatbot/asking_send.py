"""먼저 묻기를 실제로 보낸다: 시간대가 되면 물어도 되는 멤버마다 질문을 하나 만들어(chatbot/asking.py),
하네스 규칙(harness/asks.py)을 보내기 직전에 한 번 더 확인하고 1:1로 보낸다.

루프에서는 run.py --asking으로만 켜진다 (기본은 꺼짐). 답을 지식으로 읽는 단계가 아직 없어서,
그 전에는 손으로만 — 로하에게 먼저 — 시험한다.

손으로 (루프를 먼저 멈춰라 — 같은 에뮬레이터를 쓴다. status와 draft는 앱을 건드리지 않는다):
    python -m chatbot.asking_send status [멤버…]       # 멤버마다 지금 물을 수 있는지, 최근 질문
    python -m chatbot.asking_send draft 로하            # 만들어 보고 규칙 판정만 (기록·발송 안 함)
    python -m chatbot.asking_send send 로하             # 지금 만들어 보낸다 (시간대만 무시, 나머지 규칙은 그대로)
    python -m chatbot.asking_send send 로하 --force     # 규칙도 무시 (시험용; 동의는 그래도 본다)
    python -m chatbot.asking_send round [--dry-run]     # 루프가 하는 것과 같은 한 회차 (지금 시간대)
"""
import argparse
import sys
import time

from chatbot import asking
from harness import asks


def _text(q):
    return asking.rendered(q) + "\n\n" + asks.FOOTER


def ask_member(chat, agent, member, slot, log, force=False, dry_run=False, draft_only=False):
    """Draft for one member, record it, and send it if it passes. Returns (status, question dict or None)."""
    from somoim.direct import DirectChat
    t0 = time.time()
    previous = [q["message"] for q in asks.recent(member, n=50)]
    # 모델에게는 시간대 이름을, 손으로 보낼 때는 지금 시각을 알려 준다 ("15:10 시간대")
    q = asking.draft(agent, member, slot if slot in asks.WINDOWS else time.strftime("%H:%M"))
    bad = asking.problems(q, previous)
    log(f"{member}님께 물을 질문 ({time.time() - t0:.0f}s): "
        + (f"안 물음 — {q['hold_reason']}" if not q["ask"] else f"[{q['axis']}] {q['message']}"
           + (f" / 걸림: {'; '.join(bad)}" if bad else "")))
    if draft_only:
        return ("held" if not q["ask"] else "rejected" if bad else "ready"), q
    if dry_run:
        log("  (기록·발송 안 함)")
        return "dry-run", q
    qid, status = asks.record_draft(member, slot, q, bad)
    if status != "ready":
        return status, q
    dm = DirectChat(chat, member, log=log)
    if not dm.open():
        log(f"  {member}님과의 1:1 대화를 열 수 없음")
        asks.mark_sent(qid, None, ok=False)
        return "failed", q
    why = None if force else asks.gate(member, slot=slot, check_slot=False)
    if why:
        log(f"  보내기 직전 막힘: {why}")
        asks.mark_blocked(qid, why)
        return "blocked", q
    text = _text(q)
    sent = dm.send(text)
    asks.mark_sent(qid, text, ok=bool(sent))
    log(f"  {'전송 완료' if sent else '전송 실패'} ({qid})")
    return ("sent" if sent else "failed"), q


def ask_round(chat, agent, log, now=None, dry_run=False):
    """What the loop runs in a window: every member who may be asked now. Returns [(member, status)]."""
    slot = asks.slot_at(now)
    if not slot:
        return []
    out = []
    for member in asks.due(now):
        status, _ = ask_member(chat, agent, member, slot, log, dry_run=dry_run)
        out.append((member, status))
    return out


def _status(members):
    from chatbot.dm import load_consent
    consents = load_consent()
    asks.expire()
    slot = asks.slot_at()
    print(f"지금 시간대: {slot or '없음'} (출근길 {asks.WINDOWS['출근길'][0]}–{asks.WINDOWS['출근길'][1]}, "
          f"퇴근길 {asks.WINDOWS['퇴근길'][0]}–{asks.WINDOWS['퇴근길'][1]})")
    for m in members or sorted(consents):
        why = asks.gate(m, slot=slot or asks.NOW_SLOT, consents=consents)
        print(f"\n{m}: {'물을 수 있음' if why is None else why}")
        for q in asks.questions(m)[-5:]:
            print(f"  {q['created_at'][5:16]} [{q['slot']}·{q['status']}] "
                  + (q["message"] or f"(안 물음: {q['hold_reason']})")[:70]
                  + (f" → 답: {q['answer'][:40]}" if q.get("answer") else ""))


def main():
    ap = argparse.ArgumentParser(description="먼저 묻기 — 만들고, 규칙을 보고, 보낸다")
    ap.add_argument("command", choices=["status", "draft", "send", "round"])
    ap.add_argument("members", nargs="*")
    ap.add_argument("--force", action="store_true", help="send: 규칙(횟수·대기·대화 중)을 무시 — 시험용")
    ap.add_argument("--dry-run", action="store_true", help="round: 만들어 보기만")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    log = lambda msg: print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)
    if args.command == "status":
        return _status(args.members)

    from chatbot.dm import DMAgent, load_consent
    from cua.agent import openai_client
    agent = DMAgent(openai_client())
    if args.command == "draft":
        if not args.members:
            ap.error("draft needs a member")
        for m in args.members:
            why = asks.gate(m, slot=asks.NOW_SLOT)
            log(f"{m}: 규칙 판정 — {'물을 수 있음' if why is None else why}")
            status, q = ask_member(None, agent, m, asks.slot_at() or asks.NOW_SLOT, log, draft_only=True)
            if q["ask"]:
                print("  ┌ 멤버가 받을 것")
                for line in _text(q).splitlines():
                    print(f"  │ {line}")
                print("  └")
                print(f"  알 수 있는 것: {q['reads_as']}\n  읽으면 안 되는 것: {q['does_not_mean']}")
        return

    from chatbot.config import MOIM_NAMES
    from cua.android import AndroidDevice
    from somoim.chat import SomoimChat
    chat = SomoimChat(AndroidDevice(scale=0.5), MOIM_NAMES, log=log)
    if args.command == "round":
        log(f"먼저 묻기 회차: {ask_round(chat, agent, log, dry_run=args.dry_run) or '물을 사람 없음'}")
    else:
        if not args.members:
            ap.error("send needs a member")
        consents = load_consent()
        for m in args.members:
            if (consents.get(m) or {}).get("status") != "agreed":
                log(f"{m}: 1:1 동의 전이라 보낼 수 없음 (--force로도 넘지 않는다)")
                continue
            why = None if args.force else asks.gate(m, slot=asks.NOW_SLOT, consents=consents)
            if why:
                log(f"{m}: 지금은 물을 수 없음 — {why} (--force로 시험할 수 있음)")
                continue
            status, _ = ask_member(chat, agent, m, asks.NOW_SLOT, log, force=args.force)
            log(f"{m}: {status}")
    chat.park()


if __name__ == "__main__":
    main()
