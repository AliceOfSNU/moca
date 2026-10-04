"""지식과 가설이 사는 곳 — data/moca.db (SQLite).

파일 세 개에 흩어져 있던 것을 테이블 셋으로 옮긴다. 멤버별로 "이 사람에 대해 무엇을 알고 무엇을 아직
모르는가"를 묻는 질의가 곧 필요해지는데(documents/moca_asks.md), 매번 331줄짜리 JSONL을 전부 읽어
파이썬에서 거르는 구조로는 그 질문에 답할 수 없다.

    knowledge             한 줄이 지식 한 건. 예전 records.jsonl 한 줄과 1:1.
    hypotheses            한 줄이 가설 한 건. 예전 hypotheses.json 원소 하나와 1:1.
    hypothesis_evidence   어떤 지식이 어떤 가설을 지지/약화하는지. 예전 가설 안의 evidence 배열.

## subjects와 members를 왜 따로 테이블로 빼지 않았나

둘 다 여러 값이라 연결 테이블이 자연스러워 보이지만, **이 배열은 순서가 뜻을 가진다**. 문장에 적힌
{s0}·{s1}이 배열의 0번·1번을 가리키고, 보여 줄 때 그 자리에 이름이나 '한 멤버'가 들어간다
(harness/knowledge.py의 render). 연결 테이블로 풀면 순서를 지키는 칸을 따로 둬야 하고, 한 번이라도
어긋나면 다른 사람 이름이 박힌 문장이 나간다. 그래서 JSON 배열 그대로 두고, 멤버로 찾을 일은
json_each로 푼다 — 아래 members_of 뷰가 그것이다.

반대로 evidence는 진짜 다대다이고 순서에 뜻이 없으며, 가설 하나를 셈할 때마다 통째로 읽힌다
(harness/evidence.py의 tally). 그래서 이것만 테이블로 뺐다.

## 지식은 여전히 덧붙이기만 한다

지식은 고쳐 쓰지 않는다. 바뀐 사실은 새 줄로 들어오고, 무엇이 지금 보이는지는 읽을 때 정한다
(knowledge.visible). SQL로 옮겨도 그대로다 — 행을 지우거나 고치지 않는다. 그래야 "왜 그때 그렇게
판단했나"를 나중에 되짚을 수 있다.

    python -m harness.db migrate     # 기존 JSON 파일을 DB로 옮긴다 (여러 번 돌려도 안전)
    python -m harness.db check       # DB와 JSON 파일이 같은 내용인지 대조한다
"""
import json
import sqlite3
import sys

from harness.tasks import DATA_ROOT

DB = DATA_ROOT / "moca.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge (
  id          TEXT PRIMARY KEY,
  statement   TEXT NOT NULL,
  subjects    TEXT NOT NULL,                  -- JSON 배열. 순서가 뜻을 가진다 ({s0} = subjects[0])
  basis       TEXT NOT NULL,                  -- reported | observed | inferred
  source_refs TEXT NOT NULL,                  -- JSON 배열
  origin      TEXT NOT NULL,                  -- JSON 객체 (task, channel, key, group, version, retracted ...)
  created_at  TEXT NOT NULL,
  seq         INTEGER NOT NULL,               -- 들어온 순서. JSONL의 줄 번호를 대신한다
  -- origin 안의 값 중 자주 거르는 것들. 원본은 origin 하나뿐이고 이것들은 거기서 파생된다
  channel        TEXT    GENERATED ALWAYS AS (json_extract(origin, '$.channel'))  STORED,
  origin_key     TEXT    GENERATED ALWAYS AS (json_extract(origin, '$.key'))      STORED,
  origin_group   TEXT    GENERATED ALWAYS AS (json_extract(origin, '$."group"'))  STORED,
  origin_version TEXT    GENERATED ALWAYS AS (json_extract(origin, '$.version'))  STORED,
  task           TEXT    GENERATED ALWAYS AS (json_extract(origin, '$.task'))     STORED,
  retracted      INTEGER GENERATED ALWAYS AS (json_extract(origin, '$.retracted') IS NOT NULL) STORED
);
CREATE INDEX IF NOT EXISTS knowledge_seq     ON knowledge(seq);
CREATE INDEX IF NOT EXISTS knowledge_created ON knowledge(created_at);
CREATE INDEX IF NOT EXISTS knowledge_key     ON knowledge(origin_key);
CREATE INDEX IF NOT EXISTS knowledge_group   ON knowledge(origin_group);
CREATE INDEX IF NOT EXISTS knowledge_channel ON knowledge(channel);
CREATE INDEX IF NOT EXISTS knowledge_basis   ON knowledge(basis);

CREATE TABLE IF NOT EXISTS hypotheses (
  id                TEXT PRIMARY KEY,
  kind              TEXT NOT NULL,
  claim             TEXT NOT NULL,
  members           TEXT NOT NULL DEFAULT '[]',   -- JSON 배열. 순서가 뜻을 가진다
  events            TEXT NOT NULL DEFAULT '[]',
  votes             TEXT NOT NULL DEFAULT '[]',
  -- grounds는 통째로 둔다. knowledge/reasoning 말고 source 같은 칸이 더 붙어 있고(대시보드가 심은 가설),
  -- 칸을 하나씩 펴면 모르는 칸이 조용히 사라진다. 가설 하나를 읽을 때 통째로 쓰이기도 한다.
  grounds           TEXT NOT NULL DEFAULT '{}',
  test              TEXT,
  status            TEXT NOT NULL,
  confidence        TEXT,
  goal_id           TEXT,
  created_by        TEXT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT,
  reviewed_until    TEXT,
  history           TEXT NOT NULL DEFAULT '[]',
  seq               INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS hypotheses_status ON hypotheses(status);
CREATE INDEX IF NOT EXISTS hypotheses_seq    ON hypotheses(seq);

-- knowledge_id에는 외래키를 걸지 않는다. 연결된 지식이 지금 보이지 않는 것은 정상이고
-- (철회됐거나 더 새 기록에 밀렸거나), 셈할 때 조용히 빠지는 것이 지금 동작이다 (evidence.tally).
-- 외래키를 걸면 그 '보이지 않음'과 '없음'을 DB가 구분하지 못해 동작이 달라진다.
CREATE TABLE IF NOT EXISTS hypothesis_evidence (
  hypothesis_id TEXT NOT NULL REFERENCES hypotheses(id) ON DELETE CASCADE,
  knowledge_id  TEXT NOT NULL,
  direction     TEXT NOT NULL,                -- supports | weakens
  weight        TEXT NOT NULL,
  note          TEXT,
  at            TEXT NOT NULL,
  seq           INTEGER NOT NULL              -- 가설 안에서의 원래 순서
);
CREATE INDEX IF NOT EXISTS hypothesis_evidence_h ON hypothesis_evidence(hypothesis_id, seq);
CREATE INDEX IF NOT EXISTS hypothesis_evidence_k ON hypothesis_evidence(knowledge_id);

-- 멤버 이름으로 지식을 찾을 때 쓴다. 순서는 position으로 남겨 두어 {s0}를 되찾을 수 있다.
CREATE VIEW IF NOT EXISTS members_of AS
  SELECT k.id AS knowledge_id, j.value AS member, j.key AS position
  FROM knowledge k, json_each(k.subjects) j;
"""


def connect():
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")    # 루프가 쓰는 동안 대시보드가 읽을 수 있게
    con.executescript(SCHEMA)
    return con


# --- 행 <-> 예전 dict ---------------------------------------------------------------------------
# 지금 코드 곳곳이 이 모양을 그대로 받아 쓰므로(evidence.py, goal_loop.py, dashboard), 저장 방식만
# 바꾸고 바깥으로 나가는 모양은 한 글자도 바꾸지 않는다.

def knowledge_row(r):
    return {"id": r["id"], "statement": r["statement"], "subjects": json.loads(r["subjects"]),
            "basis": r["basis"], "source_refs": json.loads(r["source_refs"]),
            "origin": json.loads(r["origin"]), "created_at": r["created_at"]}


def hypothesis_row(r, evidence):
    h = {"id": r["id"], "kind": r["kind"], "claim": r["claim"], "members": json.loads(r["members"]),
         "events": json.loads(r["events"]), "votes": json.loads(r["votes"]),
         "grounds": json.loads(r["grounds"]),
         "test": r["test"], "status": r["status"], "confidence": r["confidence"],
         "evidence": evidence, "goal_id": r["goal_id"], "created_at": r["created_at"],
         "updated_at": r["updated_at"], "history": json.loads(r["history"])}
    # 없는 채로 저장된 가설이 있으므로, 있을 때만 넣어 예전 파일과 같은 모양을 유지한다
    if r["created_by"] is not None:
        h["created_by"] = r["created_by"]
    if r["reviewed_until"] is not None:
        h["reviewed_until"] = r["reviewed_until"]
    return h


def evidence_row(r):
    return {"ref": r["knowledge_id"], "direction": r["direction"], "weight": r["weight"],
            "note": r["note"], "at": r["at"]}


# --- 옮기기 -------------------------------------------------------------------------------------

def migrate(log=print):
    """예전 JSON 파일을 DB로 옮긴다. 이미 있는 id는 건드리지 않으므로 여러 번 돌려도 안전하다."""
    k_file = DATA_ROOT / "knowledge" / "records.jsonl"
    h_file = DATA_ROOT / "hypotheses" / "hypotheses.json"
    con = connect()
    with con:
        n_k = 0
        if k_file.exists():
            rows = [json.loads(l) for l in k_file.read_text(encoding="utf-8").splitlines() if l.strip()]
            for i, r in enumerate(rows):
                cur = con.execute(
                    "INSERT OR IGNORE INTO knowledge (id, statement, subjects, basis, source_refs, origin,"
                    " created_at, seq) VALUES (?,?,?,?,?,?,?,?)",
                    (r["id"], r["statement"], json.dumps(r["subjects"], ensure_ascii=False), r["basis"],
                     json.dumps(r["source_refs"], ensure_ascii=False),
                     json.dumps(r["origin"], ensure_ascii=False), r["created_at"], i))
                n_k += cur.rowcount
            log(f"  지식 {len(rows)}건 중 {n_k}건 새로 넣음")

        n_h = n_e = 0
        if h_file.exists():
            hyps = json.loads(h_file.read_text(encoding="utf-8"))
            for i, h in enumerate(hyps):
                g = h.get("grounds") or {}
                cur = con.execute(
                    "INSERT OR IGNORE INTO hypotheses (id, kind, claim, members, events, votes,"
                    " grounds, test, status, confidence, goal_id, created_by,"
                    " created_at, updated_at, reviewed_until, history, seq)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (h["id"], h["kind"], h["claim"], json.dumps(h.get("members") or [], ensure_ascii=False),
                     json.dumps(h.get("events") or [], ensure_ascii=False),
                     json.dumps(h.get("votes") or [], ensure_ascii=False),
                     json.dumps(g, ensure_ascii=False), h.get("test"), h["status"], h.get("confidence"), h.get("goal_id"), h.get("created_by"),
                     h["created_at"], h.get("updated_at"), h.get("reviewed_until"),
                     json.dumps(h.get("history") or [], ensure_ascii=False), i))
                if cur.rowcount:
                    n_h += 1
                    for j, e in enumerate(h.get("evidence") or []):
                        con.execute(
                            "INSERT INTO hypothesis_evidence (hypothesis_id, knowledge_id, direction, weight,"
                            " note, at, seq) VALUES (?,?,?,?,?,?,?)",
                            (h["id"], e["ref"], e["direction"], e["weight"], e.get("note"), e["at"], j))
                        n_e += 1
            log(f"  가설 {len(hyps)}건 중 {n_h}건, 근거 연결 {n_e}건 새로 넣음")

        # 연결된 지식이 실제로 있는지 — 없다고 해서 틀린 건 아니지만(셈에서 빠질 뿐) 알아는 둔다
        missing = con.execute(
            "SELECT COUNT(*) FROM hypothesis_evidence e"
            " WHERE NOT EXISTS (SELECT 1 FROM knowledge k WHERE k.id = e.knowledge_id)").fetchone()[0]
        if missing:
            log(f"  주의: 연결된 지식 {missing}건이 knowledge에 없습니다 (셈에서는 그냥 빠집니다)")
    con.close()
    return n_k, n_h


def check(log=print):
    """DB가 예전 파일과 같은 내용인지 대조한다. 옮긴 뒤 한 번 돌려 보라고 있는 것."""
    from harness import hypotheses as H
    from harness import knowledge as K
    k_file = DATA_ROOT / "knowledge" / "records.jsonl"
    h_file = DATA_ROOT / "hypotheses" / "hypotheses.json"
    ok = True

    if k_file.exists():
        want = [json.loads(l) for l in k_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        got = K.load_all(include_hidden=True)
        if want == got:
            log(f"  지식 {len(want)}건: 파일과 DB가 같습니다")
        else:
            ok = False
            log(f"  지식 다름: 파일 {len(want)}건, DB {len(got)}건")
            for a, b in zip(want, got):
                if a != b:
                    log(f"    첫 차이 {a['id']}:\n      파일 {a}\n      DB   {b}")
                    break

    if h_file.exists():
        want = json.loads(h_file.read_text(encoding="utf-8"))
        got = H.load()
        if want == got:
            log(f"  가설 {len(want)}건: 파일과 DB가 같습니다")
        else:
            ok = False
            log(f"  가설 다름: 파일 {len(want)}건, DB {len(got)}건")
            for a, b in zip(want, got):
                if a != b:
                    for key in sorted(set(a) | set(b)):
                        if a.get(key) != b.get(key):
                            log(f"    {a['id']}.{key}:\n      파일 {a.get(key)}\n      DB   {b.get(key)}")
                    break
    return ok


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    what = sys.argv[1] if len(sys.argv) > 1 else "migrate"
    if what == "migrate":
        print(f"{DB}로 옮깁니다")
        migrate()
        print("대조:")
        check()
    elif what == "check":
        print(f"{DB}와 예전 파일을 대조합니다")
        print("같습니다" if check() else "다릅니다")
    else:
        print("쓰임: python -m harness.db [migrate|check]")


if __name__ == "__main__":
    main()
