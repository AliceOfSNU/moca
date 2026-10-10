"""먼저 묻기의 하네스 쪽: 언제, 누구에게 물어도 되는지와 보낸 질문의 기록 (documents/moca_asks.md 4단계).

무엇을 물을지는 모델이 정한다 (chatbot/asking.py). 여기는 그 앞뒤의 규칙만 — 모델이 지침을 읽었다고 지켰다는
보장은 없으니, 횟수·시간·동의는 하네스가 센다.

언제 (로하가 정함, 2026-10-11)
- 출근길 07:30–09:30, 퇴근길 18:00–20:00, 매일. 시간대마다 한 번만 만든다 — 모델이 '지금은 안 물음'이라고
  해도 그 시간대는 쓴 것이다 (같은 시간대에 다시 만들면 비용만 들고, 결국 무언가를 묻게 된다).
- 하루 두 번은 상한이지 할당량이 아니다.

누구에게
- 1:1에 동의했고, 1:1을 그만두지 않았고, 기억을 모임 운영에 써도 된다고 했고(/memory on), /ask off가 아닌 멤버.
- 아직 답이 없는 질문이 있으면 새로 묻지 않는다. 답이 없는 질문은 2일 뒤 끝난 것으로 본다 — 다시 묻거나
  재촉하지 않는다. 연달아 두 번 답이 없으면 7일 쉰다.
- 1:1 대화가 한창이면(마지막 메시지가 한 시간 안) 묻지 않는다. 이어지는 대화에 엉뚱한 질문을 끼워 넣지 않게.

보내기 직전에 다시 본다 (시간대 확인만 빼고). 대화창을 여는 사이에 멤버가 답했거나 /ask off를 했을 수 있다.

답: 질문을 보낸 뒤 멤버가 처음 한 말을 그대로 붙여 둔다 (mark_answered). 무엇을 뜻하는지 읽어 지식으로 남기는
일은 다음 단계다. 지금은 '답이 왔다'는 사실과 원문만.

    python -m chatbot.asking_send status          # 멤버마다 지금 물을 수 있는지, 최근 질문
"""
import datetime as dt
import json
import secrets

from harness import db

WINDOWS = {"출근길": ("07:30", "09:30"), "퇴근길": ("18:00", "20:00")}
NOW_SLOT = "지금"                 # 손으로 바로 보낸 것 (시간대 밖)
DAILY_CAP = 2
EXPIRE_AFTER = dt.timedelta(days=2)
PAUSE_AFTER, PAUSE_FOR = 2, dt.timedelta(days=7)
QUIET_FOR = dt.timedelta(minutes=60)
FMT = "%Y-%m-%d %H:%M:%S"
FOOTER = "(이런 질문을 그만 받고 싶으면 /ask off)"
FIELDS = ("axis", "target", "why", "message", "reads_as", "does_not_mean", "hold_reason")


def _now(now=None):
    return now or dt.datetime.now()


def _ts(t):
    return t.strftime(FMT)


def slot_at(now=None):
    """Which sending window `now` is in, or None."""
    hm = _now(now).strftime("%H:%M")
    return next((s for s, (a, b) in WINDOWS.items() if a <= hm < b), None)


# --- 기록 -------------------------------------------------------------------------------------------

def _row(r):
    d = dict(r)
    d["choices"], d["problems"], d["multi"] = json.loads(d["choices"]), json.loads(d["problems"]), bool(d["multi"])
    return d


def questions(member=None, con=None):
    con = con or db.connect()
    sql, args = "SELECT * FROM questions", ()
    if member:
        sql, args = sql + " WHERE member = ?", (member,)
    return [_row(r) for r in con.execute(sql + " ORDER BY created_at, rowid", args)]


def get(qid, con=None):
    r = (con or db.connect()).execute("SELECT * FROM questions WHERE id = ?", (qid,)).fetchone()
    return _row(r) if r else None


def record_draft(member, slot, q, problems, now=None):
    """Keep every draft, sent or not. status: held (모델이 안 묻기로), rejected (하네스가 거름), ready (보낼 것)."""
    now = _now(now)
    status = "held" if not q.get("ask") else "rejected" if problems else "ready"
    qid = f"q_{now:%Y%m%d}_{secrets.token_hex(3)}"
    with db.connect() as con:
        con.execute(
            "INSERT INTO questions (id, member, slot, kind, status, axis, target, why, message, choices, multi, "
            "reads_as, does_not_mean, hold_reason, problems, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (qid, member, slot, "explore", status, q.get("axis") or None, q.get("target") or None,
             q.get("why_this_person") or None, q.get("message") or None, json.dumps(q.get("choices") or [], ensure_ascii=False),
             int(bool(q.get("multi"))), q.get("reads_as") or None, q.get("does_not_mean") or None,
             q.get("hold_reason") or None, json.dumps(problems, ensure_ascii=False), _ts(now)))
    return qid, status


def mark_sent(qid, text, ok, now=None):
    now = _now(now)
    with db.connect() as con:
        if ok:
            con.execute("UPDATE questions SET status='sent', sent_text=?, sent_at=?, expires_at=? WHERE id=?",
                        (text, _ts(now), _ts(now + EXPIRE_AFTER), qid))
        else:
            con.execute("UPDATE questions SET status='failed' WHERE id=?", (qid,))


def mark_blocked(qid, why):
    """보내기 직전 다시 본 규칙에 걸렸다. 만든 질문은 남기되 보내지 않는다."""
    with db.connect() as con:
        q = get(qid, con)
        con.execute("UPDATE questions SET status='blocked', problems=? WHERE id=?",
                    (json.dumps((q["problems"] if q else []) + [f"보내기 직전: {why}"], ensure_ascii=False), qid))


def expire(now=None):
    """2일 지나도록 답이 없는 질문은 끝난 것으로. 재촉하지 않는다. Returns how many."""
    with db.connect() as con:
        cur = con.execute("UPDATE questions SET status='expired' WHERE status='sent' AND expires_at <= ?",
                          (_ts(_now(now)),))
        return cur.rowcount


def open_question(member, now=None):
    """보냈고 아직 답이 없고 끝나지도 않은 질문. 한 번에 하나뿐이다."""
    r = db.connect().execute(
        "SELECT * FROM questions WHERE member=? AND status='sent' AND expires_at > ? ORDER BY sent_at DESC LIMIT 1",
        (member, _ts(_now(now)))).fetchone()
    return _row(r) if r else None


def mark_answered(qid, text, now=None):
    with db.connect() as con:
        con.execute("UPDATE questions SET status='answered', answer=?, answered_at=? WHERE id=? AND status='sent'",
                    (text, _ts(_now(now)), qid))


def recent(member, n=10):
    """이 멤버에게 실제로 보낸 최근 질문들 — 질문을 만들 때 겹치지 않게 보여 준다."""
    sent = [q for q in questions(member) if q["sent_at"]]
    return sent[-n:]


# --- /ask off|on ------------------------------------------------------------------------------------

def enabled(member, con=None):
    r = (con or db.connect()).execute("SELECT enabled FROM ask_prefs WHERE member=?", (member,)).fetchone()
    return True if r is None else bool(r["enabled"])


def set_enabled(member, on, now=None):
    with db.connect() as con:
        con.execute("INSERT INTO ask_prefs (member, enabled, changed_at) VALUES (?,?,?) "
                    "ON CONFLICT(member) DO UPDATE SET enabled=excluded.enabled, changed_at=excluded.changed_at",
                    (member, int(bool(on)), _ts(_now(now))))


def parse_command(text):
    """'/ask' → show, '/ask on' → on, '/ask off' → off, else None. 하네스가 읽는다, 모델이 아니라."""
    t = (text or "").strip().lower().replace("／", "/").rstrip(".!")
    return {"/ask": "show", "/ask on": "on", "/ask off": "off"}.get(" ".join(t.split()))


# --- 규칙 -------------------------------------------------------------------------------------------

def _last_talk(member):
    """1:1에서 마지막으로 메시지를 본 때 (모카가 읽어 둔 시각)."""
    from chatbot.dm import member_dir
    from chatbot.store import ChatStore
    last = None
    for m in ChatStore(member_dir(member)).history(5):
        at = m.get("read_at")
        if at and (last is None or at > last):
            last = at
    return dt.datetime.strptime(last, FMT) if last else None


def left_moim(member):
    """모임을 떠났는가: 모임 채팅의 '(운영진에게만) …님께서 모임을 탈퇴하셨습니다'가 그 사람이 모임 채팅에서 마지막으로
    한 말보다 나중이면. 다시 가입한다는 알림은 없어서, 돌아와 말을 하면 돌아온 것으로 본다."""
    from chatbot.store import ChatStore
    left = said = ""
    for m in ChatStore().history(100000):
        at = m.get("read_at") or ""
        if f"{member}님께서 모임을 탈퇴하셨습니다" in (m.get("text") or ""):
            left = max(left, at)
        elif m.get("sender") == member:
            said = max(said, at)
    return bool(left) and left > said


def gate(member, now=None, slot=None, check_slot=True, consents=None):
    """May 모카 ask this member first, right now? Returns None if yes, else the reason (Korean, for the log).
    `slot`: the window being filled (None = 손으로, 시간대 무시). `check_slot=False` skips the one-per-window
    check — used right before sending a draft made in this very window."""
    from chatbot.dm import load_consent
    from chatbot.memory_consent import shares
    from harness import dm_optout
    now = _now(now)
    consents = consents if consents is not None else load_consent()
    if (consents.get(member) or {}).get("status") != "agreed":
        return "1:1 동의 전"
    if dm_optout.is_off(member):
        return "1:1을 그만둠"
    if left_moim(member):
        return "모임을 떠남"
    if not shares(member):
        return "기억 운영 활용 동의가 없음 (/memory off)"
    if not enabled(member):
        return "/ask off"
    if slot and slot != NOW_SLOT:
        if slot_at(now) != slot:
            return f"{slot} 시간대가 아님"
    mine = questions(member)
    today = now.strftime("%Y-%m-%d")
    if check_slot and slot and slot != NOW_SLOT and any(
            q["slot"] == slot and q["created_at"][:10] == today for q in mine):
        return f"오늘 {slot}에 이미 만들었음"
    if sum(1 for q in mine if (q["sent_at"] or "")[:10] == today) >= DAILY_CAP:
        return f"오늘 이미 {DAILY_CAP}번 물었음"
    if open_question(member, now):
        return "답을 기다리는 질문이 있음"
    sent = [q for q in mine if q["sent_at"]]
    tail = sent[-PAUSE_AFTER:]
    if len(tail) == PAUSE_AFTER and all(q["status"] == "expired" for q in tail):
        until = dt.datetime.strptime(tail[-1]["expires_at"], FMT) + PAUSE_FOR
        if now < until:
            return f"연달아 {PAUSE_AFTER}번 답이 없어 {until:%m/%d %H:%M}까지 쉼"
    talk = _last_talk(member)
    if talk and now - talk < QUIET_FOR:
        return f"1:1 대화 중 (마지막 {talk:%H:%M})"
    return None


def due(now=None):
    """Members to ask in the current window (expires stale questions first). [] outside the windows."""
    now = _now(now)
    expire(now)
    slot = slot_at(now)
    if not slot:
        return []
    from chatbot.dm import load_consent
    consents = load_consent()
    return [m for m in consents if gate(m, now, slot, consents=consents) is None]
