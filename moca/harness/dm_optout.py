"""멤버가 1:1 서비스를 그만두는 길 — 기록을 지우고, 다시 말을 걸지 않는다.

모카가 로하의 노트북에서 국내 데이터센터의 서버로 옮겨 가면서, 멤버들에게 무엇이 옮겨 가는지 알리고 원하지
않으면 1:1을 그만둘 수 있게 했다 (2026-10-01). 그 약속을 코드로 지키는 곳이다. 약속한 것은 두 가지다:

    "지금까지의 1:1 대화 기록과 회원님 메모를 지우고, 앞으로 모카가 1:1로 말을 걸지 않아요."

그래서 모델이 아니라 하네스가 문장을 알아보고, 하네스가 지운다. 모델이 알아채 주기를 기다리지 않는다.

지우는 것
- data/dm/<멤버>/ 전체: 1:1 대화 기록과 읽은 위치
- data/members/<멤버>/notes.jsonl: 모카가 멤버에 대해 적어 둔 메모
- 그 메모에서 나온 지식 기록: 내용을 비우고 tombstone으로 남긴다 (id는 남는다 — 가설이 그 id를 근거로
  들고 있을 수 있어서, 줄을 지우면 참조가 끊긴다. 읽을 수 있는 내용은 남기지 않는다)

지우지 않는 것 (그 사람만의 것이 아니거나, 공개된 것)
- 모임 채팅에 쓴 메시지, 게시판에 올린 글, 가입인사에서 나온 사실
- 정모 참석·투표 참여 기록, 별조각

그만둔 뒤
- 1:1 동의 상태가 'off'가 된다. 다시 동의를 묻지 않는다 — 그만두겠다는 사람에게 안내문을 또 보내는 것은
  그만두게 해 주지 않는 것과 같다.
- 루프는 그 사람의 1:1을 열지 않는다. 모카가 먼저 보내는 안내(harness/direct.py)도 막힌다.
- 메모를 운영에 쓰는 동의도 함께 내려간다.
- 모임 채팅에서 @모카를 부르는 것은 그대로다. 모임을 나가는 것과는 상관이 없다.

되돌리려면 멤버가 1:1을 다시 시작하면 된다 (`python -m harness.dm_optout --undo <이름>`으로 상태만 되돌린다.
지운 기록은 돌아오지 않는다).

Usage (from the moca/ directory):
    python -m harness.dm_optout 김한수          # 로하가 대신 처리해 줄 때
    python -m harness.dm_optout --list
"""
import argparse
import json
import shutil
import sys
import time

from harness.tasks import DATA_ROOT

FMT = "%Y-%m-%d %H:%M:%S"
OFF = "off"
PHRASES = ("1:1 그만", "1:1그만", "/dm off", "일대일 그만", "1:1 중단", "1:1 서비스 그만")


def asked(text):
    """Did the member ask to stop? Matched by the harness on their own words, like /secret is."""
    squashed = (text or "").replace(" ", "").lower()
    return any(p.replace(" ", "").lower() in squashed for p in PHRASES)


# --- 상태 ------------------------------------------------------------------------------------------

def is_off(member):
    from chatbot.dm import load_consent
    return load_consent().get(member, {}).get("status") == OFF


def off_members():
    from chatbot.dm import load_consent
    return sorted(m for m, r in load_consent().items() if r.get("status") == OFF)


def _set_status(member, status, by):
    from chatbot.dm import load_consent, save_consent
    consents = load_consent()
    record = consents.get(member, {})
    record.update(status=status, dm_off_at=time.strftime(FMT) if status == OFF else None, dm_off_by=by)
    consents[member] = record
    save_consent(consents)


# --- 지우기 ----------------------------------------------------------------------------------------

def _forget_transcript(member):
    from chatbot.dm import member_dir
    path = member_dir(member)
    if not path.exists():
        return 0
    lines = 0
    transcript = path / "transcript.jsonl"
    if transcript.exists():
        lines = len([l for l in transcript.read_text(encoding="utf-8").splitlines() if l.strip()])
    shutil.rmtree(path, ignore_errors=True)
    return lines


def _forget_notes(member):
    """The memo file 모카 keeps about this member."""
    path = DATA_ROOT / "members"
    for folder in path.iterdir() if path.exists() else []:
        if folder.is_dir() and folder.name.rsplit("-", 1)[0] == member:
            notes = folder / "notes.jsonl"
            if notes.exists():
                kept = len([l for l in notes.read_text(encoding="utf-8").splitlines() if l.strip()])
                notes.unlink()
                return kept
    return 0


def _redact_knowledge(member):
    """Empty every knowledge record that came from this member's memos, keeping the id as a tombstone.

    Rewriting the file is the only way to make the text actually gone — an appended tombstone would hide the
    record but leave it readable on disk, and we told the member it would be deleted. The id and created_at
    stay so a hypothesis still resolves what it once rested on (harness/evidence.py reads by id)."""
    from harness import knowledge
    if not knowledge.RECORDS.exists():
        return [], 0
    rows, redacted = [], []
    for line in knowledge.RECORDS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        o = r.get("origin") or {}
        if o.get("channel") in knowledge.PRIVATE_CHANNELS and member in (r.get("subjects") or []):
            redacted.append(r["id"])
            r = {**r, "statement": "", "subjects": [], "source_refs": [],
                 "origin": {**o, "retracted": "member_request", "retracted_at": time.strftime(FMT)}}
        rows.append(r)
    tmp = knowledge.RECORDS.with_suffix(".forget.tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    tmp.replace(knowledge.RECORDS)
    return redacted, len(rows)


def _drop_refs(ids):
    """Take the redacted ids out of every hypothesis's grounds and evidence, so nothing rests on what is gone."""
    from harness import hypotheses as H
    if not ids:
        return 0
    gone, records, touched = set(ids), H.load(), 0
    for h in records:
        before = (list(h["grounds"].get("knowledge") or []), list(h.get("evidence") or []))
        h["grounds"]["knowledge"] = [k for k in (h["grounds"].get("knowledge") or []) if k not in gone]
        h["evidence"] = [e for e in (h.get("evidence") or []) if e.get("ref") not in gone]
        if (h["grounds"]["knowledge"], h["evidence"]) != before:
            h["updated_at"] = time.strftime(FMT)
            touched += 1
    if touched:
        H.save(records)
    return touched


def forget(member, by="member", log=None):
    """Carry out the promise. Returns what was removed, for the log and for telling the member."""
    from chatbot import memory_consent
    from harness import direct
    messages = _forget_transcript(member)
    notes = _forget_notes(member)
    ids, _ = _redact_knowledge(member)
    hypotheses = _drop_refs(ids)
    direct.set_optout(member, how=f"1:1 그만 ({by})")
    try:
        memory_consent.set_sharing(member, False, how="1:1 그만")
    except Exception:
        pass
    _set_status(member, OFF, by)
    report = {"member": member, "messages": messages, "notes": notes,
              "knowledge": len(ids), "hypotheses": hypotheses, "by": by, "at": time.strftime(FMT)}
    if log:
        log(f"{member}님이 1:1 서비스를 그만둠 ({by}) → 1:1 기록 {messages}개, 메모 {notes}개 지움, "
            f"지식 {len(ids)}건 비움, 가설 {hypotheses}개에서 근거 제거. 앞으로 1:1로 말을 걸지 않습니다")
    return report


def undo(member, log=None):
    """Let a member back in if they ask. The records stay gone."""
    _set_status(member, "pending", "undo")
    from harness import direct
    direct.set_optout(member, on=False)
    if log:
        log(f"{member}님의 1:1 그만 상태를 되돌림 (지운 기록은 돌아오지 않습니다)")


GOODBYE = ("알겠어요. 지금까지의 1:1 대화 기록과 회원님에 대한 메모를 지웠어요. 앞으로 제가 1:1로 먼저 "
           "말을 걸지 않을게요.\n모임 채팅에서는 그대로 @모카로 불러 주셔도 괜찮아요. 언제든 다시 1:1로 "
           "말을 걸어 주시면 그때 다시 안내드릴게요.")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="멤버의 1:1 서비스를 그만두게 하거나, 그 상태를 되돌립니다")
    ap.add_argument("member", nargs="?")
    ap.add_argument("--undo", action="store_true", help="그만둔 상태만 되돌린다 (지운 기록은 돌아오지 않음)")
    ap.add_argument("--list", action="store_true", help="그만둔 멤버")
    args = ap.parse_args()
    if args.list or not args.member:
        print("1:1을 그만둔 멤버:", ", ".join(off_members()) or "(없음)")
        return
    if args.undo:
        undo(args.member, log=print)
        return
    print(json.dumps(forget(args.member, by="로하", log=print), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
