"""Who the members are: the 가입인사 form, read into fields, plus who has been around lately.

The 가입인사 template is fixed (이름 / 별칭 / 나이 / 직업 또는 분야 / 사는 곳 / 하고 싶은 말), so the harness reads
it with plain parsing — no model, nothing invented. A post that doesn't follow the form is kept as raw text.
These are posts every member can read, written for 모카 ("모카가 당신을 부를 이름입니다"), so they are treated as
모임 public data: unlike what 모카 learns in conversation (chatbot/members.py), no 활용 범위 gate applies.

The roster is everyone who has spoken in the 모임 chat — the app makes every new member say hello when joining.

Scale: prompts get counts and a short list of the recently active; everyone else is one member_search away.

Usage (from the moca/ directory):
    python -m chatbot.profiles            # the 운영 모카 block, as it would be shown
    python -m chatbot.profiles 루트        # search
"""
import collections
import datetime as dt
import json
import re
import sys

from chatbot.config import DEVELOPER
from chatbot.store import ROOT, ChatStore

BOARD = ROOT / "data" / "board"
NAMED_ROWS = 8          # 운영 모카 프롬프트에 이름으로 보여 줄 최근 활동 멤버 수
RECENT_DAYS = 14
ME = "MOCA"

FIELDS = {"이름": "name", "별칭": "nickname", "나이": "age", "직업 또는 분야": "job", "직업": "job",
          "사는 곳": "lives", "하고 싶은 말": "message"}
# the template's own hints, which members often leave in place: "한수(모카가 당신을 부를 이름입니다.)"
HINT = re.compile(r"\s*\((?:예시[^)]*|모카[^)]*|자유롭게)\)\s*$")


def _clean(value):
    return HINT.sub("", (value or "").strip()).strip()


def parse_intro(text):
    """The labeled fields of a 가입인사 post. {} when the post doesn't follow the form."""
    fields = {}
    for line in (text or "").splitlines():
        m = re.match(r"\s*([^:：]{1,12})\s*[:：]\s*(.*)$", line)
        if m and m.group(1).strip() in FIELDS:
            value = _clean(m.group(2))
            if value:
                fields[FIELDS[m.group(1).strip()]] = value
    return fields


def birth_year(age):
    """'96년생' -> 1996, '00년생' -> 2000."""
    m = re.search(r"(\d{2,4})\s*년생", age or "")
    if not m:
        return None
    year = int(m.group(1))
    return year if year > 1000 else (1900 + year if year >= 30 else 2000 + year)


def region(lives):
    """'서울 마포구' -> 마포, '안양시 만안구' -> 안양, '충남 아산시' -> 아산, '영등포' -> 영등포.
    Outside Seoul the city says more than the district; inside, the district does."""
    words = (lives or "").replace(",", " ").split()
    city = next((w for w in words if w.endswith("시") and w not in ("서울시", "서울특별시")), None)
    if city:
        return city[:-1]
    gu = next((w for w in words if w.endswith("구")), None)
    if gu:
        return gu[:-1]
    return words[-1] if words else None


def _roster():
    """{display name: last time seen in the chat} for everyone who has spoken, whispers folded in."""
    seen = {}
    for m in ChatStore().history(100000):
        if m["mine"]:
            continue
        name = m["sender"].replace("(귓속말)", "").strip()
        seen[name] = max(seen.get(name, ""), m.get("read_at") or "")
    return seen


def _intros():
    index = json.loads((BOARD / "index.json").read_text(encoding="utf-8")) if (BOARD / "index.json").exists() else {}
    out = {}
    for post in index.get("posts", []):
        if post["category"] != "가입인사" or post["pinned"] or post["author"] == ME:
            continue
        path = BOARD / post["path"]
        body = path.read_text(encoding="utf-8").split("\n\n", 1)[-1] if path.exists() else ""
        out.setdefault(post["author"], {"post": post["path"], "posted": post["time"], "raw": body.strip()[:300],
                                        **parse_intro(body)})
    return out


_CACHE = {"at": 0.0, "table": None}


def profiles(max_age=30):
    """Every member: {display: {nickname, name, born, job, lives, region, message, intro, last_seen, recent}}.
    Cached for a few seconds: a prompt renders many knowledge lines, each asking for a name."""
    import time
    if _CACHE["table"] is not None and time.time() - _CACHE["at"] < max_age:
        return _CACHE["table"]
    _CACHE["table"], _CACHE["at"] = _build(), time.time()
    return _CACHE["table"]


def _build():
    roster, intros = _roster(), _intros()
    since = (dt.datetime.now() - dt.timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%d")
    recent, greeted = collections.Counter(), set()
    for m in ChatStore().history(100000):
        if m["mine"]:
            continue
        who = m["sender"].replace("(귓속말)", "").strip()
        if who not in greeted:
            greeted.add(who)  # 앱이 가입할 때 시키는 첫인사는 참여로 세지 않는다
            continue
        if (m.get("read_at") or "") >= since:
            recent[who] += 1
    out = {}
    for display in dict.fromkeys(list(roster) + list(intros)):
        intro = intros.get(display, {})
        out[display] = {
            "display": display, "nickname": intro.get("nickname") or display, "name": intro.get("name"),
            "born": birth_year(intro.get("age")), "job": intro.get("job"), "lives": intro.get("lives"),
            "region": region(intro.get("lives")), "message": intro.get("message"),
            "intro": "form" if intro.get("nickname") or intro.get("job") else ("free" if intro else None),
            "raw": intro.get("raw") if intro and not intro.get("job") else None,
            "last_seen": roster.get(display, "")[:10] or None, "recent": recent.get(display, 0)}
    return out


def call_name(display, table=None):
    """What 모카 should call this member: their 별칭 if the intro gave one."""
    p = (table or profiles()).get(display)
    return p["nickname"] if p else display


def _counts(values, top=6):
    c = collections.Counter(v for v in values if v)
    missing = sum(1 for v in values if not v)
    parts = [f"{k} {n}" for k, n in c.most_common(top)]
    rest = sum(n for _, n in c.most_common()[top:])
    return ", ".join(parts + ([f"기타 {rest}"] if rest else []) + ([f"미기재 {missing}"] if missing else [])) or "(정보 없음)"


def _decade(year):
    return f"{year % 100 // 10 * 10:02d}년대생" if year else None


def row(p):
    bits = [p["name"] if p["name"] and p["name"] != p["nickname"] else None,
            f"{p['born'] % 100:02d}년생" if p["born"] else None, p["job"],
            f"사는 곳 {p['lives']}" if p["lives"] else None]
    head = f"{p['nickname']}" + (f" (앱 이름 {p['display']})" if p["display"] != p["nickname"] else "")
    detail = " · ".join(b for b in bits if b)
    say = f" — \"{p['message'][:60]}\"" if p["message"] else (" — (자기소개 없음)" if not p["intro"] else "")
    return f"- {head}{': ' + detail if detail else ''}{say} · 최근 {RECENT_DAYS}일 채팅 {p['recent']}회 (가입 첫인사 제외)"


def composition_block():
    """운영 모카's view of the membership: counts always, names only for the recently active."""
    table = profiles()
    members = [p for p in table.values() if p["display"] != ME]
    intro = [p for p in members if p["intro"]]
    active = sorted(members, key=lambda p: (p["recent"], p["last_seen"] or ""), reverse=True)
    lines = [f"## 모임 구성 (모임 채팅에서 본 적 있는 멤버 {len(members)}명 · 자기소개 {len(intro)}명 기준)",
             "- 사는 곳 (자기소개에 적은 곳이야. 일하는 곳이나 모일 수 있는 곳과 다를 수 있으니, 이것만으로 "
             "누가 어디까지 올 수 있는지 판단하지 마. 궁금하면 물어봐): " + _counts([p["region"] for p in intro]),
             "- 하는 일: " + _counts([p["job"] for p in intro]),
             "- 나이대: " + _counts([_decade(p["born"]) for p in intro]),
             f"- 최근 {RECENT_DAYS}일 가입 첫인사 말고 모임 채팅에 말한 멤버: "
             f"{sum(1 for p in members if p['recent'])}명 / {len(members)}명, 메시지 {sum(p['recent'] for p in members)}개"
             + (f" (그중 {DEVELOPER} {table[DEVELOPER]['recent']}개)" if DEVELOPER in table else ""),
             f"- 최근 활동한 멤버 (최대 {NAMED_ROWS}명, 부를 이름 기준. 나머지는 member_search로 찾아):"]
    lines += ["  " + row(p) for p in active[:NAMED_ROWS]]
    lines.append("- 채팅에 한 번 말하고 떠난 사람도 이 목록에 남을 수 있어. 오래 조용한 멤버를 곧 올 사람으로 세지 마.")
    return "\n".join(lines)


def search(query, limit=10):
    q = (query or "").strip().lower()
    hits = [p for p in profiles().values() if p["display"] != ME and q and any(
        q in (p.get(k) or "").lower() for k in ("display", "nickname", "name", "job", "lives", "message", "raw"))]
    return "\n".join(row(p) for p in hits[:limit]) or f"'{query}'에 맞는 멤버가 없습니다."


def nickname_block():
    """For chat and 1:1 prompts: call people what they asked to be called."""
    table = profiles()
    pairs = [f"{p['display']} → {p['nickname']}" for p in table.values()
             if p["display"] not in (ME,) and p["nickname"] != p["display"]]
    lines = ["\n\n## 멤버를 부르는 이름",
             "- 멤버를 부를 때는 자기소개에서 '모카가 당신을 부를 이름'으로 적은 별칭으로 불러. 채팅 기록에는 앱 이름으로 보여.",
             "- 멘션(@)은 프로그램이 앱 이름으로 붙이니 신경 쓰지 마."]
    if pairs:
        lines.append("- 앱 이름 → 부를 이름: " + ", ".join(pairs[:60]))
    return "\n".join(lines)


def _irago(word):
    """'이라고' after a final consonant, '라고' after a vowel (루트라고 / 한별이라고)."""
    last = (word or "")[-1:]
    return "이라고" if "가" <= last <= "힣" and (ord(last) - 0xAC00) % 28 else "라고"


def own_block(display):
    """For a 1:1 prompt: just this member."""
    p = profiles().get(display)
    if not p:
        return ""
    if p["nickname"] != display:
        return (f"\n\n## 이 멤버를 부르는 이름\n- 앱 이름은 '{display}'이지만 자기소개에서 "
                f"'{p['nickname']}'{_irago(p['nickname'])} 불러 달라고 했어. 그렇게 불러.")
    return ""


MEMBER_TOOL = {
    "type": "function", "name": "member_search",
    "description": "멤버 자기소개(부를 이름, 나이, 하는 일, 사는 곳, 한마디)와 최근 채팅 활동을 찾는다. "
                   "이름, 별칭, 지역, 직업 같은 낱말로 검색한다.",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    print(search(sys.argv[1]) if len(sys.argv) > 1 else composition_block())


if __name__ == "__main__":
    main()
