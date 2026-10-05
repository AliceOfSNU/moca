"""에이전트 커피챗 안내 페이지와 체크인 폼.

정적 파일을 그대로 내보내고, 체크인 제출을 파일 하나에 쌓는다. DB는 쓰지 않는다 — 참가자가 열 명이고
프로세스가 꺼졌다 켜져도 파일만 남으면 되기 때문이다.

    python site/server.py --port 8123                     # 로컬에서 보기
    SITE_DATA=/opt/moca/site-data python site/server.py --port 80

저장되는 곳: $SITE_DATA/checkins.json (기본값은 이 파일 옆의 data/).
발표자가 적은 '한 줄 주제'의 키워드는 제출 직후가 아니라 뒤에서 따로 만든다. 모델 호출이 느리거나
실패해도 제출은 이미 저장돼 있어야 하기 때문이다.
"""
import argparse
import json
import os
import pathlib
import queue
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = pathlib.Path(__file__).resolve().parent
STATIC = HERE
# 저장 위치는 정적 파일 바깥이어야 한다. site/ 안에 두면 제출 내용이 그대로 내려받힌다.
DATA = pathlib.Path(os.environ.get("SITE_DATA") or (HERE.parent / "site-data"))
STORE = DATA / "checkins.json"

MODEL = os.environ.get("SITE_MODEL", "gpt-6.1-sol")
KEYWORDS_PER_TOPIC = (2, 3)
KEYWORD_LIMIT = 12          # 12자 이하 (website.md)
MAX_TOPICS = 2              # 한 사람이 올릴 수 있는 '한 줄 주제'
LIMITS = {"name": 40, "job": 60, "agent_name": 40, "agent_does": 120, "minutes": 30,
          "demo": 200, "slides": 200, "repo": 200, "topic": 160}

_lock = threading.Lock()

# 홈페이지에 나가도 되는 칸. 폼에서도 이 목록 그대로 '공개'라고 알린다.
PUBLIC_SPEAKER_FIELDS = ("name", "job", "agent_name", "agent_does", "repo")


# --- 저장 -------------------------------------------------------------------------------------------
def load():
    if not STORE.exists():
        return {"submissions": []}
    try:
        return json.loads(STORE.read_text(encoding="utf-8"))
    except ValueError:
        return {"submissions": []}


def save(data):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STORE)


def _clean(value, limit):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def add(body):
    """체크인 하나를 받는다. Returns (record, None) or (None, why not)."""
    role = body.get("role")
    if role not in ("speaker", "listener"):
        return None, "발표자인지 청취자인지 골라 주세요"
    name, job = _clean(body.get("name"), LIMITS["name"]), _clean(body.get("job"), LIMITS["job"])
    if not name:
        return None, "이름을 적어 주세요"
    if not job:
        return None, "어떤 일을 하시는지 적어 주세요"

    rec = {"id": uuid.uuid4().hex[:10], "role": role, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "name": name, "job": job}

    if role == "speaker":
        agent_name = _clean(body.get("agent_name"), LIMITS["agent_name"])
        agent_does = _clean(body.get("agent_does"), LIMITS["agent_does"])
        if not agent_name:
            return None, "에이전트 이름을 적어 주세요"
        if not agent_does:
            return None, "에이전트가 하는 일을 한 줄로 적어 주세요"
        topics = []
        for t in (body.get("topics") or [])[:MAX_TOPICS]:
            text = _clean(t, LIMITS["topic"])
            if text:
                topics.append({"text": text, "keywords": []})
        rec.update(agent_name=agent_name, agent_does=agent_does,
                   minutes=_clean(body.get("minutes"), LIMITS["minutes"]),
                   demo=_clean(body.get("demo"), LIMITS["demo"]),
                   slides=_clean(body.get("slides"), LIMITS["slides"]),
                   repo=_clean(body.get("repo"), LIMITS["repo"]),
                   want=body.get("want") if body.get("want") in ("feedback", "worry", "both") else None,
                   topics=topics)

    with _lock:
        data = load()
        data["submissions"].append(rec)
        save(data)
    if rec["role"] == "speaker" and rec.get("topics"):
        # 제출은 이미 저장됐다. 키워드는 뒤에서 붙인다 — 모델이 느리거나 죽어도 체크인은 남아야 한다.
        _queue.put(rec["id"])
    return rec, None


# --- 키워드 ------------------------------------------------------------------------------------------
PROMPT = """모임에서 함께 이야기할 '한 줄 주제'에 키워드를 붙이는 일이야.

## 이미 쓰고 있는 키워드
{existing}

## 이번 주제
{topic}

규칙:
- 키워드 {lo}~{hi}개. 각각 12자 이하의 한국어(또는 널리 쓰는 영어) 명사구.
- **이미 쓰고 있는 키워드부터 본다.** 뜻이 겹치는 것이 하나라도 있으면 반드시 그것을 글자 그대로 쓴다.
  '토큰 소모'가 이미 있는데 '토큰 사용량'을 새로 만드는 식은 안 된다 — 같은 이야기가 둘로 쪼개져
  무엇이 많이 나왔는지 보이지 않게 된다. 새 키워드는 겹치는 것이 정말 없을 때만 만든다.
- 주제에 실제로 적힌 것만. 없는 내용을 추측해 넣지 마.
- JSON만: {{"keywords": ["...", "..."]}}"""

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["keywords"],
          "properties": {"keywords": {"type": "array", "items": {"type": "string"}}}}


def existing_keywords(data=None):
    data = data if data is not None else load()
    seen = []
    for s in data["submissions"]:
        for t in s.get("topics") or []:
            for k in t.get("keywords") or []:
                if k not in seen:
                    seen.append(k)
    return seen


def make_keywords(topic, existing):
    """Returns a list of keywords (possibly empty — 실패는 체크인을 막지 않는다)."""
    try:
        from openai import OpenAI
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            env = os.environ.get("SITE_ENV") or (HERE.parent / "moca" / ".env")
            from dotenv import dotenv_values
            key = (dotenv_values(env) or {}).get("OPENAI_API_KEY")
        if not key:
            return []
        client = OpenAI(api_key=key)
        lo, hi = KEYWORDS_PER_TOPIC
        resp = client.responses.create(
            model=MODEL, reasoning={"effort": "high"},
            input=PROMPT.format(existing="\n".join(f"- {k}" for k in existing) or "(아직 없음)",
                                topic=topic, lo=lo, hi=hi),
            text={"format": {"type": "json_schema", "name": "keywords", "strict": True, "schema": SCHEMA}})
        out = json.loads(resp.output_text)
        words, seen = [], set()
        for k in out.get("keywords") or []:
            k = _clean(k, KEYWORD_LIMIT)
            if k and k not in seen:
                seen.add(k)
                words.append(k)
        return words[:hi]
    except Exception as e:                      # 키워드가 없어도 주제 자체는 그대로 보인다
        print(f"  키워드 생성 실패: {e}", flush=True)
        return []


def _fill_keywords(submission_id):
    with _lock:
        data = load()
        rec = next((s for s in data["submissions"] if s["id"] == submission_id), None)
        topics = [t["text"] for t in (rec or {}).get("topics") or []]
    for i, text in enumerate(topics):
        # 주제마다 그때까지 쌓인 키워드를 다시 읽는다. 모아 놓은 목록을 들고 시작하면
        # 바로 앞 주제가 만든 키워드를 못 봐서 '토큰 소모'와 '토큰 사용량'이 따로 생긴다.
        words = make_keywords(text, existing_keywords())
        with _lock:                             # 그 사이 다른 제출이 들어왔을 수 있으니 다시 읽고 쓴다
            data = load()
            rec = next((s for s in data["submissions"] if s["id"] == submission_id), None)
            if rec and i < len(rec.get("topics") or []):
                rec["topics"][i]["keywords"] = words
                save(data)
        print(f"  keywords {submission_id}#{i}: {words}", flush=True)


# 키워드는 한 번에 하나씩 만든다. 동시에 돌리면 서로가 만든 키워드를 보지 못해 같은 뜻의
# 키워드가 여러 개 생기고, 워드클라우드가 묶어 주는 의미가 사라진다.
_queue = queue.Queue()


def _worker():
    while True:
        sid = _queue.get()
        try:
            _fill_keywords(sid)
        except Exception as e:
            print(f"  키워드 작업 실패 {sid}: {e}", flush=True)
        finally:
            _queue.task_done()


threading.Thread(target=_worker, daemon=True).start()


# --- 홈페이지에 나가는 것 ------------------------------------------------------------------------------
def public():
    """공개라고 고지한 것만. 비공개 칸(소요 시간·시연·발표자료)과 청취자 명단은 내보내지 않는다."""
    data = load()
    speakers, cloud = [], {}
    for s in data["submissions"]:
        if s["role"] != "speaker":
            continue
        speakers.append({k: s.get(k, "") for k in PUBLIC_SPEAKER_FIELDS} | {"id": s["id"]})
        for t in s.get("topics") or []:
            for k in t.get("keywords") or ["(키워드 만드는 중)"]:
                cloud.setdefault(k, []).append(
                    {"text": t["text"], "name": s["name"], "agent_name": s.get("agent_name", "")})
    keywords = [{"word": w, "count": len(v), "topics": v}
                for w, v in sorted(cloud.items(), key=lambda kv: (-len(kv[1]), kv[0]))]
    listeners = sum(1 for s in data["submissions"] if s["role"] == "listener")
    return {"speakers": speakers, "keywords": keywords,
            "counts": {"speakers": len(speakers), "listeners": listeners,
                       "total": len(speakers) + listeners}}


# --- HTTP -------------------------------------------------------------------------------------------
# 내보내도 되는 것만 적는다. 모르는 확장자는 내보내지 않는다 — 예전에 쓰던 data/ 같은 것이
# 정적 폴더 안에 남아 있으면, 받아둔 체크인이 그대로 내려받힌다.
CTYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css",
          ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
          ".svg": "image/svg+xml", ".webp": "image/webp", ".ico": "image/x-icon",
          ".woff2": "font/woff2"}


class Handler(BaseHTTPRequestHandler):
    server_version = "moca-site"

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/public":
            return self._send(200, public())
        name = {"/": "index.html", "": "index.html", "/checkin": "checkin.html"}.get(path, path.lstrip("/"))
        file = (STATIC / name).resolve()
        if (STATIC not in file.parents or not file.is_file()
                or file.suffix not in CTYPES            # 허용한 종류만
                or DATA.resolve() in file.parents
                or "data" in {part.lower() for part in file.relative_to(STATIC).parts[:-1]}):
            return self._send(404, {"error": "not found"})
        self._send(200, file.read_bytes(), CTYPES[file.suffix] + "; charset=utf-8")

    def do_POST(self):
        if urlparse(self.path).path != "/api/checkin":
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > 16_000:
            return self._send(413, {"error": "너무 깁니다"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"error": "JSON이 아닙니다"})
        rec, problem = add(body)
        if problem:
            return self._send(400, {"error": problem})
        return self._send(200, {"ok": True, "id": rec["id"]})


def main():
    ap = argparse.ArgumentParser(description="에이전트 커피챗 사이트")
    ap.add_argument("--port", type=int, default=8123)
    ap.add_argument("--bind", default="127.0.0.1")
    args = ap.parse_args()
    DATA.mkdir(parents=True, exist_ok=True)
    print(f"에이전트 커피챗: http://{args.bind}:{args.port}  (저장: {STORE})", flush=True)
    ThreadingHTTPServer((args.bind, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
