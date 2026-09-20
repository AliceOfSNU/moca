"""모카's main loop: 모임 chat replies (chat_rules.txt), board sync + 가입인사 greetings, 1:1 conversations,
the morning-after 기억 활용 범위 question (chatbot/memory_consent.py), and once a day the 운영 모카 round
that looks after 정모 (admin/agent.py).

Between cycles the app is parked on 내모임, so 소모임 posts push notifications for new chat messages,
new posts and 1:1 messages; each notification wakes the loop for the matching job.
A cheap preview check and a slow fallback timer cover pushes that never arrive.

Usage (from the moca/ directory):
    python run.py                    # run until stopped
    python run.py --once --dry-run   # one cycle, compose replies but do not send
    python run.py --confirm          # ask in the terminal before each send
"""
import argparse
import shutil
import sys
import time
import traceback

from admin.agent import load_state as load_admin_state, save_state as save_admin_state
from admin.events import sync_events
from admin.goal_loop import GoalLoop
from harness.goals import focus as goals_due
from chatbot.agent import ChatAgent, answerable, format_line, is_call, secret
from chatbot.dm import (DMAgent, ask_memory_scope, converse, greet_newcomers, has_consented, load_consent,
                        member_dir)
from chatbot.group_task import GroupChatTasks
from chatbot.memory_consent import due as scope_due
from harness import presence
from harness.tasks import needs_chat as task_needs_chat
from somoim.ui import SomoimUI
from chatbot.posts import load_index, sync_posts
from somoim.board import SomoimBoard
from chatbot.config import MOIM_NAME, MOIM_NAMES, same_name
from chatbot.store import DATA, ChatStore
from cua.agent import openai_client, run_task
from cua.android import AndroidDevice
from somoim.chat import SomoimChat
from somoim.direct import DirectChat, inbox_rows
from somoim.events import SomoimEvents
from somoim.notifications import NotificationWatcher



def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(DATA / "run.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def open_chat(chat, dev, client):
    if chat.open():
        return True
    log("UI 트리로 채팅 화면을 찾지 못함 → Astra 화면 조작으로 복구 시도")
    run_task(dev, f"소모임 앱에서 '{MOIM_NAME}' 모임의 '채팅' 탭 화면을 여세요. 팝업이나 안내창은 닫으세요. "
                  "아무것도 입력하거나 전송하지 마세요.", client=client, blocked={"type"}, max_steps=25, quiet=True)
    return chat.open()


def cycle(args, dev, chat, store, agent, client):
    """Read new messages and answer calls. Returns the number of new messages read, or None if the chat couldn't be opened."""
    if not open_chat(chat, dev, client):
        log("채팅 화면을 열 수 없음. 이번 회차는 건너뜀")
        return None

    first_run = not store.anchor
    msgs, found = chat.read_since(store.anchor, backlog=args.backlog)
    if not msgs:
        log("새 메시지 없음")
        return 0
    backlog = first_run or not found
    if not first_run and not found:
        log(f"경고: 지난번 읽은 위치를 찾지 못함. 최근 {len(msgs)}개 메시지를 읽었지만 답장은 하지 않음")

    read = msgs
    msgs = [m for m in read if not secret(m)]
    if len(msgs) < len(read):
        # never described, never stored, never shown to the model; only the read position moves past them
        log(f"'/secret' 메시지 {len(read) - len(msgs)}개는 읽지 않고 넘어감 (기록·모델 전달 안 함)")
    if not msgs:
        store.record([], anchor=read)
        return 0
    for m in msgs:
        if m.get("_crop"):
            m["photo_desc"] = agent.describe_photo(m.pop("_crop"))
    store.record(msgs, anchor=read)
    log(f"{'이전 기록' if backlog else '새 메시지'} {len(msgs)}개:")
    for m in msgs:
        log("  " + format_line(m))

    calls = {}
    for m in msgs:
        if is_call(m) and not store.answered(m):
            calls.setdefault(m["sender"], []).append(m)
    if not calls:
        if not backlog:
            join_in(args, chat, store, agent, msgs)
        return len(msgs)
    if backlog and not args.answer_backlog:
        log(f"이전 기록의 호출 {sum(map(len, calls.values()))}건은 답하지 않음 (--answer-backlog로 변경)")
        return len(msgs)

    replies = agent.respond(store.history(args.context), calls,
                            senders={m["sender"] for m in msgs if answerable(m)})
    if not args.dry_run:
        # mark before sending: a failed or partial send must not turn into a double reply next cycle
        store.mark_answered([m for ms in calls.values() for m in ms])
    mention_each = len(calls) > 1  # chat_rules: tag members only when several people called 모카
    for caller in calls:
        text = replies.get(caller)
        if not text:
            log(f"{caller}에게 보낼 답장이 생성되지 않음")
            continue
        mention = caller if mention_each else None
        log(f"답장 → {caller}{' (멘션)' if mention else ''}: {text}")
        if args.dry_run:
            chat.send(text, mention, dry_run=True)  # fills and checks the input box, then clears it
            continue
        if args.confirm:
            if not sys.stdin.isatty() or input("  보낼까요? [y/N] ").strip().lower() != "y":
                log("  보내지 않음")
                continue
        sent = chat.send(text, mention)
        log("  전송 완료" if sent else "  전송 실패")
    return len(msgs)


def join_in(args, chat, store, agent, msgs):
    """Nobody called 모카, so let it decide whether to join the conversation.
    Unprompted replies are spaced out by --join-gap so 모카 doesn't talk over the 모임."""
    new = [m for m in msgs if answerable(m) and not store.answered(m)]
    if not new:
        return
    since = time.time() - store.state.get("last_join_in", 0)
    if since < args.join_gap:
        log(f"부르지 않은 메시지 {len(new)}개: 마지막 자발적 답장 후 {since / 60:.0f}분밖에 안 지나 건너뜀")
        return
    text, reason = agent.join_in(store.history(args.context), new)
    if not text:
        log(f"부르지 않은 메시지 {len(new)}개: 답하지 않기로 함 ({reason})")
        return
    log(f"자발적 답장 → {new[-1]['sender']} ({reason}): {text}")
    if args.dry_run:
        chat.send(text, dry_run=True)
        return
    store.mark_answered(new)  # before sending, so a failed send doesn't become a repeat
    store.remember("last_join_in", time.time())
    log("  전송 완료" if chat.send(text) else "  전송 실패")


def chat_session(args, dev, chat, store, agent, client):
    """Run 모임 chat cycles until a read finds nothing new, then park the app.
    Messages that arrive while the chat is open produce no notification, so the chat is only left
    right after a read that came up empty. Returns when that final read started."""
    for attempt in range(2):
        for _ in range(4):
            read_started = time.time()
            if not cycle(args, dev, chat, store, agent, client):
                break
        # 운영 모카's Group Chat Task, if any (harness/tasks.py): ask, check progress, or report
        if chat.open() and GroupChatTasks(agent, store, chat, log, dry_run=args.dry_run).turn():
            read_started = time.time()
            cycle(args, dev, chat, store, agent, client)  # record 모카's own message before leaving the chat
        if args.once:
            return read_started
        if not chat.park():
            log("내모임 화면으로 돌아가지 못함 (알림이 오지 않을 수 있음)")
            return read_started
        # a message sent between the last read and leaving the chat gets no notification;
        # the 내모임 list previews the latest message, so compare it with what was read
        preview, newest = chat.preview(), (store.anchor or [None])[-1]
        if preview and newest and not newest["photo"] and preview != (newest["text"], newest["time"]):
            log(f"채팅을 나오는 사이 새 메시지가 온 것 같음 ({preview[0]!r}) → 다시 읽기")
            continue
        return read_started
    return read_started


def dm_session(args, chat, dm_agent, members, check_inbox):
    """Answer 1:1 conversations: the members named by notifications, plus (when `check_inbox`)
    any conversation whose inbox preview differs from the last message 모카 has stored."""
    members = list(members)
    if check_inbox:
        for row in inbox_rows(chat) or []:
            last = ChatStore(member_dir(row["member"])).history(1)
            if not last or last[-1]["text"] != row["preview"]:
                members.append(row["member"])
    for member in dict.fromkeys(members):
        dm = DirectChat(chat, member, log=log)
        # like the 모임 chat, an open conversation gets no notifications: keep reading until nothing is new
        for _ in range(3):
            if not converse(dm, dm_agent, log, dry_run=args.dry_run):
                break


def memory_session(args, chat, dm_agent):
    """Ask members, the morning after they entered the 1:1, what 모카 may use what it remembers for
    (documents/personal_intelligence.md 3). Their answer is read on their next 1:1 turn."""
    ask_memory_scope(chat, dm_agent, log, dry_run=args.dry_run)


def memory_due(args):
    """Only in the morning, and only while somebody is actually waiting to be asked. Asking marks the
    member, so there is no daily state to keep here."""
    if args.no_memory_ask or time.localtime().tm_hour < args.memory_hour:
        return False
    return any(scope_due(member, consent) for member, consent in load_consent().items())


def admin_session(args, chat, client, daily=False):
    """운영 모카's goal loop (admin/goal_loop.py): re-read 정모 from the app, then let every goal that is due
    take its steps. `daily` is the once-a-day tick; other wake-ups come from goals whose wait is over."""
    log("운영 모카 목표 루프" + (" (매일 점검)" if daily else ""))
    events_ui = SomoimEvents(chat)
    sync_events(events_ui, log)
    handled = GoalLoop(client, events_ui, log, dry_run=args.dry_run, daily_hour=args.admin_hour).run(daily=daily)
    if not handled:
        log("  지금 다룰 목표 없음")
    if daily:
        ADMIN_TICK["date"] = time.strftime("%Y-%m-%d")
        if not args.dry_run:
            save_admin_state(load_admin_state() | {"last_run": time.strftime("%Y-%m-%d %H:%M:%S")})


def admin_due(args):
    """True once a day, after --admin-hour. A dry run leaves no state file, so the tick is also kept here."""
    if args.no_admin or time.localtime().tm_hour < args.admin_hour:
        return False
    today = time.strftime("%Y-%m-%d")
    return ADMIN_TICK["date"] != today and not load_admin_state().get("last_run", "").startswith(today)


def post_session(args, chat, dm_agent):
    """Save new board posts (and, every --post-resync seconds, catch edits and deletions),
    then greet the authors of 가입인사 posts."""
    full = time.time() - load_index()["last_full_sync"] >= args.post_resync
    log(f"게시판 {'전체 재동기화' if full else '새 글 확인'}")
    sync_posts(SomoimBoard(chat), log, full=full)
    intros = [{"author": e["author"], "title": e["listed_title"], "time": e["listed_time"]}
              for e in load_index()["posts"] if e["category"] == "가입인사" and not e["pinned"]]
    greet_newcomers(chat, dm_agent, log, dry_run=args.dry_run, posts=intros)


def session(args, dev, chat, store, agent, dm_agent, client, work):
    """Do the work a trigger asked for. Returns when the 모임 chat was last read, or None if it wasn't."""
    if work["dm"] or work["inbox"]:
        presence.activity("dm", members=sorted(work["dm"]), inbox=work["inbox"])
        dm_session(args, chat, dm_agent, work["dm"], work["inbox"])
    if work["memory"]:
        presence.activity("memory")
        memory_session(args, chat, dm_agent)
    if work["post"]:
        presence.activity("post")
        post_session(args, chat, dm_agent)
    if work["admin"]:
        presence.activity("goal", daily=work.get("daily", False))
        admin_session(args, chat, client, daily=work.get("daily", False))
    if work["chat"]:
        presence.activity("chat")
        return chat_session(args, dev, chat, store, agent, client)
    if not args.once:
        chat.park()
    return None


ADMIN_TICK = {"date": None}  # the day 운영 모카 last ran in this process


def no_work():
    return {"chat": False, "post": False, "inbox": False, "dm": set(), "admin": False, "memory": False}


def everything():
    return dict(no_work(), chat=True, post=True, inbox=True)


def classify(event):
    """What a 소모임 notification asks for: ("dm", member), ("post", None), ("chat", None), or (None, why it's ignored).
    Observed in 소모임 5.8.2 -- 101: 모임 chat (title = sender) or new post (title = "새글");
    102: 게시판 activity (new post, comment on 모카's post); 103: a chat message aimed at 모카
    (@mention, or a reply prefixed "(나에게 답장)"); 104: 1:1 message (title = "[DM] name")."""
    title, text = event.get("title") or "", event.get("text") or ""
    if event.get("subText") == "1:1메시지" or title.startswith("[DM] "):
        return "dm", title.removeprefix("[DM] ").strip()
    if not any(same_name(event.get("subText"), name) for name in MOIM_NAMES):
        return None, f"다른 모임 ({event.get('subText')})"
    if title == "새글" or text.startswith("모임에 게시글을 작성했습니다"):
        return "post", None
    if event["id"] in ("101", "103"):  # 103 = a chat message aimed at 모카 (an @mention, or a reply to its message)
        return "chat", None
    return None, f"게시판 활동 ({text.splitlines()[0] if text else event['id']})"


def add_work(work, event):
    kind, detail = classify(event)
    if kind is None:
        log(f"알림 무시: {detail}")
        return False
    # a member's 1:1 messages stay out of the log until they agree to the 1:1 notice
    text = event["text"] if kind != "dm" or has_consented(detail) else "(동의 전 1:1 메시지 — 내용 기록 안 함)"
    log(f"알림 [{kind}] {event['title']}: {text}")
    if kind == "dm":
        work["dm"].add(detail)
    else:
        work[kind] = True
    return True


def wait_for_trigger(args, watcher, chat, last_cycle):
    """Block until a notification asks for work, the 내모임 chat preview changes without one,
    or the fallback timer fires. Returns (reason, work)."""
    baseline = chat.preview()
    while True:
        if memory_due(args):
            return "기억 활용 범위 질문", dict(no_work(), memory=True)
        # a task waiting for its opening question, or past its deadline and waiting for its report
        if not args.dry_run and task_needs_chat() and time.time() - last_cycle >= args.min_gap:
            return "운영 작업", dict(no_work(), chat=True)
        if admin_due(args):
            return "운영 점검 시각", dict(no_work(), admin=True, daily=True)
        # a goal whose wait is over (its task finished), or that has not taken a step yet
        if not args.dry_run and not args.no_admin and goals_due() and time.time() - last_cycle >= args.min_gap:
            return "목표 진행", dict(no_work(), admin=True)
        remaining = last_cycle + args.fallback - time.time()
        if remaining <= 0:
            return "예비 타이머", everything()
        event = watcher.wait(min(remaining, args.peek))
        if event is None:
            current = chat.preview()
            if baseline and current and current != baseline:
                log(f"알림 없이 내모임 미리보기가 바뀜: {current[0]!r}")
                return "미리보기 변경", {"chat": True, "post": False, "inbox": False, "dm": set()}
            baseline = baseline or current
            continue
        work = no_work()
        if not add_work(work, event):
            continue
        time.sleep(args.settle)  # let a burst of messages arrive before reading
        for e in watcher.drain():
            add_work(work, e)
        time.sleep(max(0, last_cycle + args.min_gap - time.time()))
        return "알림", work


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run a single cycle")
    ap.add_argument("--fallback", type=int, default=1800, help="seconds before checking without a notification")
    ap.add_argument("--peek", type=int, default=120, help="seconds between cheap checks of the 내모임 preview")
    ap.add_argument("--settle", type=int, default=20, help="seconds to wait after a notification before reading")
    ap.add_argument("--min-gap", type=int, default=60, help="minimum seconds between cycles")
    ap.add_argument("--post-resync", type=int, default=10800,
                    help="seconds between full board re-syncs that catch edited and deleted posts")
    ap.add_argument("--dry-run", action="store_true", help="compose replies but do not send")
    ap.add_argument("--confirm", action="store_true", help="ask before each send")
    ap.add_argument("--answer-backlog", action="store_true", help="also answer calls found on the first read")
    ap.add_argument("--backlog", type=int, default=30, help="messages to read on the first run")
    ap.add_argument("--context", type=int, default=60, help="recent messages given to the chat agent")
    ap.add_argument("--memory-hour", type=int, default=9,
                    help="hour of the day (0-23) after which 모카 asks members the 기억 활용 범위 question")
    ap.add_argument("--no-memory-ask", action="store_true", help="never ask the 기억 활용 범위 question")
    ap.add_argument("--admin-hour", type=int, default=10,
                    help="hour of the day (0-23) after which 운영 모카 does its daily 정모 round")
    ap.add_argument("--no-admin", action="store_true", help="skip the daily 운영 모카 round")
    ap.add_argument("--join-gap", type=int, default=600,
                    help="minimum seconds between replies 모카 sends without being called (0 = no limit)")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    client = openai_client()
    # heartbeat on every successful screen read; a gap becomes a sleep period 모카 is told about (harness/presence.py)
    SomoimUI.on_read = lambda: presence.seen(log=log)
    dev = AndroidDevice(scale=0.5)
    chat = SomoimChat(dev, MOIM_NAMES, log=log)
    if args.dry_run:
        # a dry run must not move the real read position, or its calls would never be answered for real
        shutil.rmtree(DATA / "dry_run", ignore_errors=True)
        store = ChatStore(DATA / "dry_run")
        store.state.update(ChatStore().state)
    else:
        store = ChatStore()
    agent, dm_agent = ChatAgent(client, log=log), DMAgent(client, log=log)
    watcher = None if args.once else NotificationWatcher(dev, log=log)
    reason, work = "시작", dict(everything(), admin=admin_due(args) or bool(goals_due()), daily=admin_due(args),
                              memory=memory_due(args))
    retried = False
    while True:
        log(f"── 회차 시작 ({reason}: {', '.join(k for k in ('chat', 'post', 'inbox', 'admin', 'memory') if work[k]) or ''}"
            f"{' dm=' + ','.join(work['dm']) if work['dm'] else ''})")
        cycle_started = time.time()
        chat_read = None
        failed = False
        try:
            chat_read = session(args, dev, chat, store, agent, dm_agent, client, work)
        except Exception:
            log("오류:\n" + traceback.format_exc())
            failed = True
        if args.once:
            break
        if failed and not retried:
            # the messages behind this work would otherwise wait for the next notification or the fallback timer
            log(f"오류로 끝난 일을 {args.min_gap}초 뒤 한 번 더 시도")
            presence.activity("idle")
            time.sleep(args.min_gap)
            reason, retried = "오류 후 재시도", True
            continue
        retried = False
        if chat_read:
            # 모임 chat notifications posted before its final read are already covered by it
            watcher.drain(before=chat_read, where=lambda e: classify(e)[0] == "chat")
        log("알림 대기 중")
        presence.activity("idle")
        reason, work = wait_for_trigger(args, watcher, chat, cycle_started)


if __name__ == "__main__":
    main()
