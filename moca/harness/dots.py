"""Dots에게 활동 준비를 맡긴다 — 작업 요청서를 채워 Google Drive에 올린다.

활동의 기획이 끝나면 프로그램 모카가 delegate_to_dots를 부른다. 하네스는 작업 요청서
(documents/dots_request_template.md)의 빈자리를 채워 Drive의 MOCA/workspace/activities/<활동 id>/request.md로
올리고, Dots는 그것을 읽어 같은 폴더의 output/에 결과물을 올린다 (documents/program_agent_steps.md).

**빈자리는 둘로 나뉜다.**

- 하네스가 기록에서 채우는 것: 요청 번호, 모임, 프로그램, 활동 이름과 종류, 시간, 장소, 인원.
  이미 정해진 사실이라 모델이 다시 받아 적게 하지 않는다 — 받아 적다 틀리면 그대로 바깥에 나간다.
- 모델이 쓰는 것(도구의 인자): 목표, 대상, 진행 방식, 준비물, 그리고 기록에 없는 소요 시간·비용·마감.
  모카 안에서 쓰는 말(entry/exit state, 슬롯)을 바깥 사람이 읽을 글로 옮기는 일이라 모델의 몫이다.

**요청서는 바깥으로 나간다** (Google Drive, 그리고 다른 회사의 에이전트). 그래서 멤버 이름은 어떤 자리에도
들어가지 않게 하네스가 막는다. 모델이 쓴 글에 {s0} 같은 자리표시자나 멤버의 앱 이름·별칭·실명이 있으면
거절하고, 기록에서 가져온 글의 자리표시자는 '멤버'로 바꾼다. 이름은 부탁이 아니라 검사로 막는다 —
모델이 지침을 읽었다고 지켰다는 보장은 없다.

정해지지 않은 값은 '미정'으로 넣고, Dots는 그것을 [미정: …]으로 남긴다 (요청서의 규칙).

Drive와는 rclone으로 이야기한다 (원격 이름은 MOCA_DRIVE_REMOTE, 기본 gdrive). 연결이 없으면 맡기지 않고
거절한다 — 맡겼다고 생각한 모카가 오지 않을 결과물을 기다리게 두지 않기 위해서다.

    python -m harness.dots preview a_… fields.json   # 채운 요청서를 보기만 한다 (올리지 않음)
    python -m harness.dots delegate a_… fields.json  # 실제로 올린다
"""
import json
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

from harness import activities as A
from harness import programs as P

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "documents" / "dots_request_template.md"
REMOTE = os.environ.get("MOCA_DRIVE_REMOTE", "gdrive")
DRIVE_ROOT = "MOCA/workspace/activities"
REQUEST_FILE = "request.md"
SITE_URL = os.environ.get("MOCA_ACTIVITY_SITE_URL", "")   # 활동 사이트를 어디에 올릴지 정해지면 채운다
UNDECIDED = "미정"
DUE_DAYS = 3
# 모임 소개 — 모카의 프롬프트(chatbot/agent.py base_prompt)에 쓰는 것과 같은 공개된 소개
MOIM_INTRO = ("AI를 일과 일상에 들여놓는 방법을 나누고 AI와 함께하는 미래를 토론하며, "
              "에이전트(모카)를 중심으로 연결된 새로운 모임 형태를 실험하는 모임이야.")

# 모델이 쓰는 칸과 길이 상한. 앞의 셋은 비어 있으면 맡기지 않는다 — Dots가 무엇을 만들지 알 수 없다
MODEL_FIELDS = {"activity_goal": 300, "activity_audience": 300, "activity_outline": 2000,
                "participant_prep": 400, "duration": 40, "fee": 60,
                "signup_deadline": 60, "checkin_deadline": 60}
REQUIRED = ("activity_goal", "activity_audience", "activity_outline")
DELEGABLE = ("draft", "scheduled")


def folder(activity_id):
    return f"{DRIVE_ROOT}/{activity_id}"


# --- 이름이 새지 않게 -------------------------------------------------------------------------------

def member_names():
    """바깥으로 나가면 안 되는 이름들: 앱 이름, 별칭, 가입인사의 실명. 두 글자 이상만 — 한 글자 이름('닝')은
    다른 낱말 속에 너무 흔해서 검사가 아무 글이나 거절하게 된다. 모카 자신은 빼고."""
    from chatbot.profiles import profiles
    names = set()
    for display, p in profiles().items():
        for n in (display, p.get("nickname"), p.get("name")):
            if n and len(n.strip()) >= 2:
                names.add(n.strip())
    return names - {"모카", "MOCA", "Moca", "moca"}


def leaks(text, names=None):
    """이 글에서 바깥으로 나가면 안 되는 것. 빈 목록이면 통과."""
    found = []
    if re.search(r"\{s\d+\}", text or ""):
        found.append("{s…} 자리표시자")
    names = names if names is not None else member_names()
    # 이름 앞에 글자가 붙어 있으면 다른 낱말이다 — '제로'는 멤버 별칭이지만 '실제로'는 아니다.
    # 뒤는 보지 않는다: '제로님', '제로가'처럼 조사가 붙은 이름도 이름이다
    found += sorted(n for n in names if re.search(rf"(?<![0-9A-Za-z가-힣]){re.escape(n)}", text or ""))
    return found


def _anonymous(text):
    """기록에서 가져온 글의 자리표시자를 '멤버'로. 이름을 채워 넣지 않는다."""
    return re.sub(r"\{s\d+\}(님)?", "멤버", text or "")


# --- 채우기 ----------------------------------------------------------------------------------------

def _program(activity):
    return next((p for p in P.load() if p["id"] == activity["program_id"]), None)


def _member_count():
    from chatbot.profiles import profiles
    return str(len([d for d in profiles() if d not in ("MOCA", "모카")]))


def harness_values(activity, program, request_id, due):
    """하네스가 기록에서 채우는 칸. 모델은 이 값을 건드리지 않는다."""
    from chatbot.config import MOIM_NAME
    size = ((program.get("users") or {}).get("size") or {}).get("max")
    kind = activity.get("activity_type") or UNDECIDED
    if activity.get("mode"):
        kind += f" · {'온라인' if activity['mode'] == 'online' else '오프라인' if activity['mode'] == 'offline' else activity['mode']}"
    return {
        "request_id": request_id,
        "requested_at": time.strftime("%Y-%m-%d %H:%M"),
        "due": due,
        "output_folder": f"{folder(activity['id'])}/output",
        "moim_name": MOIM_NAME,
        "moim_intro": MOIM_INTRO,
        "member_count": _member_count(),
        "program_title": _anonymous(program["title"]),
        "program_purpose": _anonymous(program.get("purpose") or UNDECIDED),
        "activity_title": _anonymous(activity.get("title") or UNDECIDED),
        "activity_type": kind,
        "time": activity.get("when") or UNDECIDED,
        "place": activity.get("location") or UNDECIDED,
        "seats": f"최대 {size}명" if size else UNDECIDED,
        "site_url": SITE_URL or UNDECIDED,
    }


def check(activity_id, program_id, fields, preview=False):
    """맡겨도 되는가. Returns ((activity, program, model values), None) or (None, why not).
    `preview`는 상태와 중복 검사를 건너뛴다 — 지난 활동으로도 요청서 모양을 볼 수 있게."""
    a = A.get(activity_id)
    if a is None:
        return None, f"없는 활동입니다: {activity_id}"
    if program_id and a["program_id"] != program_id:
        return None, f"활동 {activity_id}는 이 프로그램의 것이 아닙니다"
    if not preview:
        if a["status"] not in DELEGABLE:
            return None, f"{activity_id}는 '{A.STATUSES[a['status']]}' 상태입니다. 기획 중이거나 정모가 잡힌 활동만 맡깁니다"
        if (a.get("dots") or {}).get("status") == "requested":
            return None, (f"{activity_id}는 이미 Dots에게 맡겼습니다 ({a['dots']['request_id']}). "
                          "결과물이 올 때까지 다시 맡기지 마세요")
    p = _program(a)
    if p is None:
        return None, f"활동의 프로그램 {a['program_id']}를 찾을 수 없습니다"

    unknown = set(fields) - set(MODEL_FIELDS) - {"activity_id", "due"}
    if unknown:
        return None, f"모르는 칸입니다: {sorted(unknown)}. 쓸 수 있는 칸: {sorted(MODEL_FIELDS)}"
    values, names = {}, member_names()
    for key, limit in MODEL_FIELDS.items():
        v = fields.get(key)
        v = v.strip() if isinstance(v, str) else ""
        if not v:
            if key in REQUIRED:
                return None, f"{key}를 써야 합니다 — Dots가 무엇을 만들지 알 수 없습니다"
            v = UNDECIDED
        if len(v) > limit:
            return None, f"{key}가 너무 깁니다 ({len(v)}자, {limit}자 이내)"
        hit = leaks(v, names)
        if hit:
            return None, (f"{key}에 바깥으로 나가면 안 되는 것이 있습니다: {hit}. 요청서는 Google Drive와 다른 "
                          "회사의 에이전트로 나갑니다. 멤버는 '진행자', '참가자'처럼 역할로 쓰세요")
        values[key] = v
    return (a, p, values), None


def render(values):
    """템플릿을 채운다. 맨 앞의 하네스용 주석은 떼고 Dots가 읽을 본문만."""
    text = TEMPLATE.read_text(encoding="utf-8")
    body = text.split("-->", 1)[1].lstrip("\n") if text.lstrip().startswith("<!--") else text
    for key, value in values.items():
        body = body.replace("{{" + key + "}}", value)
    left = sorted(set(re.findall(r"\{\{(\w+)\}\}", body)))
    if left:
        raise ValueError(f"채우지 못한 칸: {left}")
    return body


def build(activity_id, program_id, fields, preview=False):
    """Returns ((request text, meta), None) or (None, why not)."""
    got, problem = check(activity_id, program_id, fields, preview=preview)
    if problem:
        return None, problem
    a, p, model = got
    request_id = f"d_{time.strftime('%Y%m%d')}_{secrets.token_hex(2)}"
    due = (fields.get("due") or "").strip() or time.strftime("%Y-%m-%d", time.localtime(time.time() + DUE_DAYS * 86400))
    values = harness_values(a, p, request_id, due) | model
    text = render(values)
    # 마지막으로 한 번 더, 채워 넣은 값 전부를 — 기록에서 가져온 칸에 이름이 그대로 적혀 있을 수도 있다.
    # 템플릿의 고정된 글은 보지 않는다: 우리가 쓰고 검토한 글이고, '실제로' 같은 낱말에 걸린다
    hit = leaks("\n".join(values.values()))
    if hit:
        return None, f"요청서에 멤버 이름이 남아 있습니다: {hit}. 활동이나 프로그램 기록의 글을 고친 뒤 다시 맡기세요"
    return (text, {"request_id": request_id, "due": due, "folder": folder(activity_id),
                   "output_folder": values["output_folder"]}), None


# --- 올리기 ----------------------------------------------------------------------------------------

def rclone():
    exe = shutil.which("rclone")
    if exe:
        return exe
    # 윈도에 winget으로 깐 rclone은 셸을 다시 열기 전까지 PATH에 없다
    found = sorted(pathlib.Path(os.environ.get("LOCALAPPDATA", "")).glob(
        "Microsoft/WinGet/Packages/Rclone*/rclone-*/rclone.exe"))
    return str(found[-1]) if found else None


def connected():
    """Drive에 올릴 수 있는가. Returns (rclone 경로, None) or (None, why not)."""
    exe = rclone()
    if exe is None:
        return None, "Drive 연결이 없습니다 (rclone이 설치되지 않음)"
    r = subprocess.run([exe, "listremotes"], capture_output=True, text=True)
    if f"{REMOTE}:" not in r.stdout.split():
        return None, f"Drive 연결이 없습니다 (rclone에 '{REMOTE}' 원격이 없음)"
    return exe, None


def upload(text, meta):
    exe, problem = connected()
    if problem:
        return problem
    target = f"{REMOTE}:{meta['folder']}/{REQUEST_FILE}"
    exists = subprocess.run([exe, "lsf", f"{REMOTE}:{meta['folder']}", "--include", REQUEST_FILE],
                            capture_output=True, text=True)
    if REQUEST_FILE in exists.stdout.split():
        return f"Drive에 이미 요청서가 있습니다: {meta['folder']}/{REQUEST_FILE} — 덮어쓰지 않습니다"
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / REQUEST_FILE
        path.write_text(text, encoding="utf-8")
        r = subprocess.run([exe, "copyto", str(path), target, "-q"], capture_output=True, text=True)
    if r.returncode != 0:
        return f"Drive에 올리지 못했습니다: {(r.stderr or '').strip()[:200]}"
    return None


def record(activity_id, meta, log=None):
    records = A.load()
    a = A.get(activity_id, records)
    now = time.strftime(A.FMT)
    a["dots"] = {"request_id": meta["request_id"], "folder": meta["folder"],
                 "output_folder": meta["output_folder"], "requested_at": now, "due": meta["due"],
                 "status": "requested"}
    a.setdefault("history", []).append({"at": now, "what": f"Dots에게 준비를 맡김 ({meta['request_id']})"})
    a["updated_at"] = now
    A.save(records)
    if log:
        log(f"  Dots에게 맡김: {activity_id} → {meta['folder']}/{REQUEST_FILE} ({meta['request_id']})")


def delegate(activity_id, program_id, fields, log=None, dry_run=False):
    built, problem = build(activity_id, program_id, fields)
    if problem:
        return None, problem
    text, meta = built
    if dry_run:
        return meta, None
    problem = upload(text, meta)
    if problem:
        return None, problem
    record(activity_id, meta, log)
    return meta, None


class Tools:
    """프로그램 모카의 도구 (admin/goal_loop.py가 작업으로 실행한다)."""

    def __init__(self, program_id, log=None, dry_run=False):
        self.program_id = program_id
        self.log = log
        self.dry_run = dry_run

    def delegate_to_dots(self, activity_id=None, **fields):
        meta, problem = delegate(activity_id, self.program_id, fields, log=self.log, dry_run=self.dry_run)
        if problem:
            return f"delegate_to_dots 실패: {problem}"
        if self.dry_run:
            return f"(dry-run) {activity_id}의 작업 요청서를 만들었습니다 ({meta['request_id']}). 올리지는 않았습니다"
        return (f"delegate_to_dots 완료: {activity_id}의 작업 요청서({meta['request_id']})를 Drive "
                f"{meta['folder']}/{REQUEST_FILE}에 올렸습니다. 마감 {meta['due']}. Dots가 소개글, 배너, 모임 공지 노트 "
                f"재료, 신청·체크인 웹사이트를 {meta['output_folder']}에 올립니다. 결과물을 가져오는 기능은 아직 없으니 "
                "결과가 들어왔다고 가정하지 마세요")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 3 or sys.argv[1] not in ("preview", "delegate"):
        print("쓰임: python -m harness.dots [preview|delegate] <activity_id> [fields.json]")
        return
    what, activity_id = sys.argv[1], sys.argv[2]
    fields = json.loads(pathlib.Path(sys.argv[3]).read_text(encoding="utf-8")) if len(sys.argv) > 3 else {}
    if what == "preview":
        built, problem = build(activity_id, None, fields, preview=True)
        if problem:
            print(f"맡기지 않음: {problem}")
            return
        text, meta = built
        print(text)
        print(f"\n--- {meta['request_id']} → {meta['folder']}/{REQUEST_FILE} (올리지 않음)")
        return
    meta, problem = delegate(activity_id, None, fields, log=print)
    print(f"맡기지 않음: {problem}" if problem else f"올림: {meta}")


if __name__ == "__main__":
    main()
