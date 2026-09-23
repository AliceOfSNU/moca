// 모카 dashboard frontend. One snapshot of the loop's state arrives over SSE (/api/stream) whenever
// any of moca's data files changes; every view is rendered from that snapshot.
"use strict";

let S = null;                 // latest state from the server
let connected = false;
const ui = { selectedNode: {}, openItems: new Set(), knowledgeDesc: true, formDraft: null, formMsg: null, planNotes: {}, planMsg: null };

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const FINISHED = ["achieved", "closed"];

// --- json with syntax highlighting ------------------------------------------------------------
function jsonHtml(obj) {
  const text = JSON.stringify(obj, null, 2) ?? "null";
  return esc(text).replace(
    /(&quot;(?:[^&]|&(?!quot;))*?&quot;)(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:e[+-]?\d+)?)/gi,
    (m, str, colon, lit, num) => {
      if (str) return colon ? `<span class="jk">${str}</span>${colon}` : `<span class="js">${str}</span>`;
      if (lit) return `<span class="jl">${lit}</span>`;
      return `<span class="jn">${num}</span>`;
    });
}
const jsonBox = (title, obj) => `<div class="json-box"><div class="head"><span>${esc(title)}</span></div><pre class="json">${jsonHtml(obj)}</pre></div>`;

// --- goal helpers --------------------------------------------------------------------------------
const goalsById = () => Object.fromEntries(S.goals.map((g) => [g.id, g]));
const childrenOf = (id) => S.goals.filter((g) => g.parent_id === id);
function rootOf(g) { const by = goalsById(); while (g && g.parent_id) g = by[g.parent_id]; return g; }
function subtreeIds(id) { return [id, ...childrenOf(id).flatMap((c) => subtreeIds(c.id))]; }
function displayStatus(g) {
  if (FINISHED.includes(g.status)) return g.status;
  if (g.wait) return "waiting";
  if (g.cooldown_until && g.cooldown_until > S.now) return "cooldown";
  return g.status;
}
const statusLabel = { active: "진행 중", waiting: "대기", achieved: "달성", closed: "닫힘", cooldown: "쉬는 중",
  queued: "대기열", running: "진행 중", succeeded: "성공", failed: "실패", unknown: "?",
  open: "검증 전", supported: "뒷받침됨", confirmed: "충분히 뒷받침됨", weakened: "약해짐", refuted: "폐기",
  planned: "계획됨", paused: "멈춤", finished: "끝남", dropped: "그만둠", missing: "없어짐",
  draft: "기획 중", scheduled: "정모 잡힘", held: "지난 활동", canceled: "취소됨" };
const chip = (s) => `<span class="chip s-${esc(s)}">${esc(statusLabel[s] || s)}</span>`;

// the goal the harness would pick next in a tree (same rule as moca's harness/goals.py: focus)
function nextFocus(rootId) {
  const by = goalsById();
  const tasksById = Object.fromEntries(S.tasks.map((t) => [t.id, t]));
  const waitOver = (g) => !g.wait || g.wait.task_ids.every((id) => !tasksById[id] || ["succeeded", "failed"].includes(tasksById[id].status));
  const pick = (g) => {
    if (!g || g.status !== "active") return null;
    const open = childrenOf(g.id).filter((c) => !FINISHED.includes(c.status));
    if (open.length) return pick(open[0]);
    if (g.cooldown_until && S.now < g.cooldown_until) return null;
    return waitOver(g) ? g : null;
  };
  return pick(by[rootId]);
}

// --- header: connection + sessions ---------------------------------------------------------------
function renderHeader() {
  const L = S.loop;
  const snapshot = new URLSearchParams(location.search).has("snapshot");
  $("#live").innerHTML = `<span class="dot ${connected ? "on" : snapshot ? "" : "off"}"></span>${connected ? "실시간" : snapshot ? "스냅샷 (실시간 아님)" : "연결 끊김 — 다시 연결 중"}
    <span class="muted">· ${esc(S.now)}</span>`;
  const act = L.activity;
  const d = L.detail || {};
  const goalWhat = act === "goal"
    ? (d.goal_id ? `${d.goal_id} · ${d.phase === "deciding" ? `step ${d.step} 판단 중` : `step ${d.step}: ${d.last ? d.last.kind + " → " + d.last.verdict : ""}`}` : "목표 루프 시작")
    : "쉬는 중";
  const others = { dm: `1:1 대화 ${(d.members || []).join(", ")}${d.inbox ? " · 받은편지함 확인" : ""}`, post: "게시판 확인", memory: "기억 활용 범위 질문" };
  $("#sessions").innerHTML = `
    <div class="session ${L.alive ? "on" : ""}" style="${L.alive ? "" : "border-color:var(--red)"}">
      <div class="name"><span class="dot ${L.alive ? "on" : "off"}"></span>루프 ${L.alive ? "실행 중" : "멈춤"}</div>
      <div class="what">마지막 신호 ${esc(L.last_sign || "없음")}${act && L.alive ? ` · ${esc(act)} (${esc(L.since || "")}부터)` : ""}</div>
    </div>
    <div class="session goal ${act === "goal" ? "on" : ""}">
      <div class="name"><span class="dot"></span>목표 세션 (운영 모카)</div><div class="what">${esc(goalWhat)}</div>
    </div>
    <div class="session chat ${act === "chat" ? "on" : ""}">
      <div class="name"><span class="dot"></span>채팅 세션 (채팅 모카)</div>
      <div class="what">${act === "chat" ? "모임 채팅 읽고 답하는 중 (진행 중인 작업도 이때 처리)" : "쉬는 중"}</div>
    </div>
    <div class="session other ${others[act] ? "on" : ""}">
      <div class="name"><span class="dot"></span>그 밖의 일</div><div class="what">${esc(others[act] || (act === "idle" ? "알림 기다리는 중" : "없음"))}</div>
    </div>`;
}

// --- view: goals (form + list) -------------------------------------------------------------------
function renderGoals() {
  const roots = S.goals.filter((g) => !g.parent_id).slice().reverse();
  const draft = ui.formDraft || { objective: "", criteria: [""], id: "" };
  ui.formDraft = draft;
  const blocked = !S.can_add_goal;
  const active = roots.find((g) => !FINISHED.includes(g.status));
  const list = roots.map((g) => {
    const ids = subtreeIds(g.id).slice(1);
    const done = ids.filter((id) => FINISHED.includes(goalsById()[id].status)).length;
    const st = displayStatus(g);
    return `<a class="goal-card" href="#/goal/${encodeURIComponent(g.id)}">
      <div class="row"><span class="obj">${esc(g.objective)}</span>${chip(st)}</div>
      <div class="muted" style="font-size:12px">${esc(g.id)} · ${esc(g.created_at)} · ${esc(g.created_by || "")} · 하위 목표 ${done}/${ids.length} 끝남</div>
      ${ids.length ? `<div class="bar"><span style="width:${(100 * done / ids.length).toFixed(0)}%"></span></div>` : ""}
      ${g.outcome ? `<div class="muted" style="margin-top:6px;font-size:12px"><b>결과</b> ${esc(g.outcome.summary)}</div>` : ""}
    </a>`;
  }).join("") || `<p class="empty">아직 목표가 없습니다. 왼쪽에서 첫 목표를 만들어 보세요.</p>`;

  $("#view").innerHTML = `<div class="grid">
    <div class="panel">
      <h2>새 최상위 목표</h2>
      ${blocked ? `<div class="note warn">진행 중인 목표가 있어 새 목표를 넣을 수 없습니다: <a href="#/goal/${encodeURIComponent(active.id)}">${esc(active.objective)}</a></div>` : ""}
      <form id="goal-form">
        <label for="f-obj">목표 (objective)</label>
        <input type="text" id="f-obj" maxlength="200" placeholder="예: 첫 자율스터디 정모를 개설한다" value="${esc(draft.objective)}" ${blocked ? "disabled" : ""}>
        <label>완료 기준 (completion_criteria, 1~5개)</label>
        <div id="f-crits">${draft.criteria.map((c, i) => `<div class="crit">
          <input type="text" data-crit="${i}" maxlength="200" placeholder="예: 정모가 등록되었음을 확인했다" value="${esc(c)}" ${blocked ? "disabled" : ""}>
          <button type="button" data-del="${i}" ${blocked || draft.criteria.length < 2 ? "disabled" : ""}>−</button></div>`).join("")}</div>
        <button type="button" id="f-add" ${blocked || draft.criteria.length >= 5 ? "disabled" : ""}>+ 기준 추가</button>
        <label for="f-id">id (선택, 비우면 자동)</label>
        <input type="text" id="f-id" placeholder="g_first_study" value="${esc(draft.id)}" ${blocked ? "disabled" : ""}>
        <div style="margin-top:12px;display:flex;gap:8px;align-items:center">
          <button type="submit" class="primary" ${blocked ? "disabled" : ""}>운영 모카에게 전달</button>
          <span class="muted" style="font-size:12px">하위 목표는 운영 모카가 필요하면 스스로 나눕니다.</span>
        </div>
        ${ui.formMsg ? `<div class="note ${ui.formMsg.ok ? "ok" : "err"}">${esc(ui.formMsg.text)}</div>` : ""}
      </form>
      ${jsonBox("미리보기 (goals.json에 들어갈 모양)", previewGoal(draft))}
    </div>
    <div class="panel"><h2>최상위 목표 (${roots.length})</h2>${list}</div>
  </div>`;
  bindForm();
}

function previewGoal(d) {
  return { id: d.id || "(자동)", parent_id: null, objective: d.objective, completion_criteria: d.criteria.filter((c) => c.trim()),
    status: "active", outcome: null, created_by: "모임장(대시보드)" };
}

function bindForm() {
  const form = $("#goal-form");
  if (!form) return;
  const d = ui.formDraft;
  const refreshPreview = () => { const box = form.parentElement.querySelector(".json-box pre"); if (box) box.innerHTML = jsonHtml(previewGoal(d)); };
  $("#f-obj").oninput = (e) => { d.objective = e.target.value; refreshPreview(); };
  $("#f-id").oninput = (e) => { d.id = e.target.value; refreshPreview(); };
  form.querySelectorAll("[data-crit]").forEach((el) => (el.oninput = (e) => { d.criteria[+el.dataset.crit] = e.target.value; refreshPreview(); }));
  form.querySelectorAll("[data-del]").forEach((el) => (el.onclick = () => { d.criteria.splice(+el.dataset.del, 1); renderGoals(); }));
  $("#f-add").onclick = () => { d.criteria.push(""); renderGoals(); };
  form.onsubmit = async (e) => {
    e.preventDefault();
    const body = { objective: d.objective, completion_criteria: d.criteria, id: d.id || undefined };
    const res = await fetch("/api/goals", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const out = await res.json();
    if (res.ok) {
      ui.formDraft = null;
      ui.formMsg = { ok: true, text: `추가했습니다: ${out.goal.id}. 루프가 2분 안에 첫 step을 시작합니다.` };
    } else ui.formMsg = { ok: false, text: out.error };
    render();
  };
}

// --- view: one goal ------------------------------------------------------------------------------
function renderGoal(id) {
  const by = goalsById();
  const root = by[id] ? rootOf(by[id]) : null;
  if (!root) { $("#view").innerHTML = `<p class="pad">목표 ${esc(id)}를 찾을 수 없습니다. <a href="#/">목록으로</a></p>`; return; }
  const ids = new Set(subtreeIds(root.id));
  const focus = nextFocus(root.id);
  const running = S.loop.activity === "goal" && ids.has(S.loop.detail?.goal_id) ? S.loop.detail.goal_id : null;
  const current = running || focus?.id || null;
  const sel = ui.selectedNode[root.id] || current || root.id;

  const node = (g) => {
    const st = displayStatus(g);
    const kids = childrenOf(g.id);
    return `<li><button class="node b-${st} ${g.id === sel ? "sel" : ""} ${g.id === current ? "focus" : ""}" data-node="${esc(g.id)}">
        <div class="top-row">${chip(st)}<span class="gid">${esc(g.id)}</span>
          ${g.id === running ? `<span class="focus-tag">● 지금 판단 중</span>` : g.id === current ? `<span class="focus-tag">◆ 다음 차례</span>` : ""}
          ${g.created_by === "운영 모카" ? `<span class="muted" style="font-size:11px">운영 모카가 나눔</span>` : ""}</div>
        <div class="obj">${esc(g.objective)}</div>
        ${g.outcome ? `<div class="result"><b>결과</b> ${esc(g.outcome.summary)}${(g.outcome.limitations || []).length ? `<br><b>한계</b> ${g.outcome.limitations.map(esc).join(" / ")}` : ""}</div>` : ""}
        ${g.wait ? `<div class="result">기다리는 작업: ${g.wait.task_ids.map(esc).join(", ")}</div>` : ""}
      </button>${kids.length ? `<ul>${kids.map(node).join("")}</ul>` : ""}</li>`;
  };

  const steps = S.steps.filter((s) => ids.has(s.goal_id)).slice().reverse().slice(0, 30);
  const stepItems = steps.map((s) => {
    const key = `step:${s.n}`;
    const title = s.kind === "task_result" ? `결과 도착 ${s.task_id}: ${s.outcome}` : s.kind === "cooldown" ? "쉬어 감" : `${s.kind} → ${s.verdict}`;
    const desc = s.kind === "task_result" ? s.summary : s.reason;
    return `<details class="item" data-key="${key}" ${ui.openItems.has(key) ? "open" : ""}>
      <summary><span class="when">${esc(s.at)}</span><span class="kind">${esc(s.goal_id)}</span><span class="label">${esc(title)}</span>
      ${s.note ? `<span class="desc">${esc(s.note)}</span>` : ""}${desc ? `<span class="desc">${esc(desc)}</span>` : ""}</summary>
      <pre class="json">${jsonHtml(s)}</pre></details>`;
  }).join("") || `<p class="empty">아직 step이 없습니다.</p>`;

  const tasks = S.tasks.filter((t) => ids.has(t.goal_id)).slice().reverse();
  const taskItems = tasks.map((t) => {
    const key = `task:${t.id}`;
    const what = t.kind === "group_chat" ? t.instruction : `${t.executor?.name} ${JSON.stringify(t.arguments || {})}`;
    return `<details class="item task t-${esc(t.status)} ${t.consumed ? "consumed" : ""}" data-key="${key}" ${ui.openItems.has(key) ? "open" : ""}>
      <summary>${chip(t.status)}<span class="kind">${esc(t.kind === "group_chat" ? "모임 채팅" : "도구")}</span>
        <span class="kind">${esc(t.executor ? `${t.executor.type}:${t.executor.name}` : "?")}</span>
        <span class="label">${esc(t.id)}</span>${t.consumed ? `<span class="muted" style="font-size:11px">운영 모카가 읽음</span>` : ""}
        <span class="desc">${esc(what || "")}${t.summary ? ` → ${esc(t.summary)}` : ""}</span></summary>
      <pre class="json">${jsonHtml(t.file || t)}</pre></details>`;
  }).join("") || `<p class="empty">이 목표 트리의 작업이 아직 없습니다.</p>`;

  $("#view").innerHTML = `
    <p style="margin:0"><a href="#/">← 목표 목록</a></p>
    <h1>${esc(root.objective)}</h1>
    <p class="muted" style="margin-top:0">${esc(root.id)} · ${chip(displayStatus(root))} · 만든 이 ${esc(root.created_by || "")} · ${esc(root.created_at)}</p>
    <div class="grid">
      <div class="panel"><h2>목표 트리</h2><div class="tree"><ul>${node(root)}</ul></div>
        ${jsonBox(`목표 ${sel}`, by[sel])}</div>
      <div class="panel"><h2>에이전트 상태</h2>
        <p style="margin:0 0 8px">${running ? `운영 모카가 지금 <b>${esc(running)}</b>를 판단하는 중 (${esc(S.loop.detail.phase === "deciding" ? "step 고르는 중" : "step 처리함")})`
          : current ? `다음 차례: <b>${esc(current)}</b>` : `지금 차례인 목표 없음 (기다리는 중이거나 끝남)`}</p>
        <h2 style="margin-top:12px">최근 step (새로운 순)</h2>${stepItems}</div>
      <div class="panel"><h2>작업</h2>${taskItems}</div>
      <div class="panel wide"><h2>메시지 흐름</h2>${flowHtml(S.flow.filter((f) => ids.has(f.goal_id)))}</div>
    </div>`;
  document.querySelectorAll("[data-node]").forEach((el) => (el.onclick = () => { ui.selectedNode[root.id] = el.dataset.node; render(); }));
  bindDetails();
}

function bindDetails() {
  document.querySelectorAll("details[data-key]").forEach((el) =>
    el.addEventListener("toggle", () => (el.open ? ui.openItems.add(el.dataset.key) : ui.openItems.delete(el.dataset.key))));
}

// --- message flow: lanes 운영 모카 | 하네스 | 채팅 모카 | 모임 채팅·도구 -------------------------------
const LANES = { admin: 0, harness: 1, chat: 2, group: 3, tool: 3 };
function flowHtml(items) {
  if (!items.length) return `<p class="empty">아직 오간 메시지가 없습니다.</p>`;
  const rows = items.slice(-200).map((f) => {
    const a = LANES[f.from], b = LANES[f.to];
    const lo = Math.min(a, b), hi = Math.max(a, b);
    // the message spans the lanes it travels between; 2 + index = grid column (column 1 is the time)
    const span = `grid-column:${2 + lo} / ${3 + hi}`;
    const dir = b > a ? "right" : b < a ? "left" : "";
    return `<div class="flow-row"><div class="t">${esc(f.at.slice(5))}</div>
      ${[0, 1, 2, 3].map((i) => `<div class="lane-cell" style="grid-row:1;grid-column:${2 + i}"></div>`).join("")}
      <div class="arrow" style="${span}">${dir ? `<div class="line ${dir}" style="left:12.5%;right:12.5%"></div>` : ""}
        <div class="msg ${esc(f.type)}" title="${esc(f.detail || "")}">${esc(f.label)}</div></div>
      ${f.detail ? `<div class="flow-detail">${esc(f.detail)}</div>` : ""}</div>`;
  }).join("");
  return `<div class="flow"><div class="flow-head"><div>시각</div><div>운영 모카</div><div>하네스</div><div>채팅 모카</div><div>모임 채팅 · 도구</div></div>${rows}</div>`;
}

function renderFlow() {
  $("#view").innerHTML = `<div class="panel wide"><h2>메시지 흐름 (전체, 최근 200개)</h2>
    <p class="muted" style="margin-top:0">운영 모카의 step, 하네스의 작업 전달과 결과 보고를 시간순으로 보여 줍니다. 칸 사이 화살표가 누가 누구에게 넘겼는지입니다.</p>
    ${flowHtml(S.flow)}</div>`;
}

// --- view: knowledge -----------------------------------------------------------------------------
function renderKnowledge() {
  const st = S.knowledge_stats;
  const rows = S.knowledge.slice();
  if (ui.knowledgeDesc) rows.reverse();
  const tasks = Object.entries(st.by_task).sort((a, b) => b[1] - a[1]).slice(0, 6);
  $("#view").innerHTML = `
    <div class="stats">
      <div class="stat"><div class="n">${st.total}</div><div class="l">전체</div></div>
      <div class="stat"><div class="n">${st.by_basis.reported || 0}</div><div class="l">멤버가 직접 말함</div></div>
      <div class="stat"><div class="n">${st.by_basis.observed || 0}</div><div class="l">하네스가 관찰함</div></div>
      <div class="stat"><div class="n">${st.by_basis.inferred || 0}</div><div class="l">모카의 추론</div></div>
      <div class="stat"><div class="n">${st.today}</div><div class="l">오늘 생김</div></div>
      ${tasks.map(([t, n]) => `<div class="stat"><div class="n">${n}</div><div class="l">${esc(t)}</div></div>`).join("")}
    </div>
    <div class="panel wide"><h2>지식 (운영 모카가 보는 그대로 — 허락하지 않은 멤버는 '한 멤버')</h2>
      ${rows.length ? `<div class="table-wrap"><table><thead><tr><th id="k-sort">created_at ${ui.knowledgeDesc ? "▼" : "▲"}</th><th>내용</th><th>근거</th><th>작업</th><th>id</th></tr></thead><tbody>
        ${rows.map((k) => `<tr><td class="mono">${esc(k.created_at)}</td><td>${esc(k.statement)}</td>
          <td>${({ reported: "직접 말함", observed: "관찰", inferred: "추론" })[k.basis] || esc(k.basis)} <span class="muted">(${k.sources}개 출처)</span></td>
          <td class="mono">${esc(k.task || "")}</td><td class="mono">${esc(k.id)}</td></tr>`).join("")}
      </tbody></table></div>` : `<p class="empty">아직 쌓인 지식이 없습니다. 모임 채팅 작업이 끝나면 여기에 쌓입니다.</p>`}
    </div>`;
  const sort = $("#k-sort");
  if (sort) sort.onclick = () => { ui.knowledgeDesc = !ui.knowledgeDesc; render(); };
}

// --- view: hypotheses (seeding form + list) ------------------------------------------------------
const confLabel = { low: "낮음", medium: "보통", high: "높음" };

function hypoDraft() {
  ui.hypoDraft = ui.hypoDraft || { claim: "", kind: "need", members: "", knowledge_ids: [], events: [], votes: [], reasoning: "", test: "" };
  return ui.hypoDraft;
}

function hypoBody(d) {
  return { claim: d.claim, kind: d.kind, members: d.members.split(",").map((m) => m.trim()).filter(Boolean),
    knowledge_ids: d.knowledge_ids, events: d.events, votes: d.votes, reasoning: d.reasoning, test: d.test };
}

function renderHypotheses() {
  const O = S.hypothesis_options;
  const d = hypoDraft();
  const L = O.limits;
  const checks = (field, items, label) => items.length ? `<label>${label}</label><div class="checks">${items.map((it) => {
    const value = typeof it === "string" ? it : it.id;
    return `<label class="check"><input type="checkbox" data-check="${field}" value="${esc(value)}" ${d[field].includes(value) ? "checked" : ""}> ${typeof it === "string" ? esc(it) : `${esc(it.statement)} <span class="muted mono">${esc(it.id)}</span>`}</label>`;
  }).join("")}</div>` : "";
  const live = S.hypotheses.filter((h) => h.status !== "refuted").length;

  const dir = { supports: "지지", weakens: "약화" }, wt = { weak: "약", moderate: "중", strong: "강" };
  const evidenceHtml = (h) => {
    const ev = h.evidence_shown || [], hist = h.history || [];
    const seen = h.reviewed_until ? `검토: ${esc(h.reviewed_until)}까지` : "아직 검토 안 됨";
    const items = ev.map((e) => `<li class="${e.visible ? "" : "dim"}"><b>${dir[e.direction] || esc(e.direction)}·${wt[e.weight] || esc(e.weight)}</b> ${esc(e.statement)}
      <span class="muted mono">${esc(e.ref)}</span>${e.note ? `<div class="muted">${esc(e.note)}</div>` : ""}</li>`).join("");
    const changes = hist.map((c) => `<li>${esc(c.at)} · ${chip(c.from)} → ${chip(c.to)}${c.confidence ? ` 확신 ${esc(confLabel[c.confidence] || c.confidence)}` : ""}
      <span class="muted">(지지 ${c.tally.nS}출처 ${c.tally.S}점 · 약화 ${c.tally.nW}출처 ${c.tally.W}점)</span><div class="muted">${esc(c.reason)}</div></li>`).join("");
    return `<div class="hypo-line"><b>증거 ${ev.length}건</b> <span class="muted">${seen}</span></div>
      ${items ? `<ul class="cites">${items}</ul>` : ""}
      ${changes ? `<div class="hypo-line"><b>상태 변화</b></div><ul class="cites">${changes}</ul>` : ""}`;
  };
  const cards = S.hypotheses.map((h) => {
    const key = `hypo:${h.id}`;
    const tags = [
      h.members_shown.length ? `대상 ${h.members_shown.map(esc).join(", ")}` : "모임 전체",
      ...(h.events || []).map((e) => `정모 ${esc(e)}`), ...(h.votes || []).map((v) => `투표 ${esc(v)}`)];
    return `<div class="hypo-card ${h.status === "refuted" ? "dim" : ""}">
      <div class="row"><span class="obj">${esc(h.claim_shown)}</span>${chip(h.status)}</div>
      <div class="muted" style="font-size:12px">${esc(h.id)} · ${esc(h.kind_label)} · ${esc(h.created_by || "")} · ${esc(h.created_at)}
        ${h.confidence ? ` · 확신 ${esc(confLabel[h.confidence] || h.confidence)}` : ""}</div>
      <div class="tags">${tags.map((t) => `<span class="tag">${t}</span>`).join("")}</div>
      <div class="hypo-line"><b>근거</b> ${esc(h.grounds?.reasoning || "")}${h.grounds?.source === "developer" ? ` <span class="muted">(개발자 관찰)</span>` : ""}</div>
      ${h.grounds_shown.length ? `<ul class="cites">${h.grounds_shown.map((k) => `<li>${esc(k.statement)} <span class="muted mono">${esc(k.id)}</span></li>`).join("")}</ul>` : ""}
      <div class="hypo-line"><b>확인 방법</b> ${esc(h.test)}</div>
      ${evidenceHtml(h)}
      <details class="item" data-key="${key}" ${ui.openItems.has(key) ? "open" : ""}><summary><span class="label">원본 JSON</span></summary>
        <pre class="json">${jsonHtml(h)}</pre></details>
    </div>`;
  }).join("") || `<p class="empty">아직 가설이 없습니다. 왼쪽에서 첫 가설을 심어 보세요.</p>`;

  $("#view").innerHTML = `<div class="grid">
    <div class="panel">
      <h2>새 가설 심기</h2>
      <p class="muted" style="margin-top:0;font-size:12px">운영 모카가 세운 것과 같은 규칙으로 들어갑니다. 다만 여기서 심는 가설은 저장된 지식 대신
        로하의 관찰을 근거로 삼을 수 있고, 모카에게는 '로하(대시보드)'가 세운 것으로 보입니다.</p>
      <form id="hypo-form">
        <label for="f-h-claim">가설 (claim, ${L.claim}자 이내)</label>
        <textarea id="f-h-claim" maxlength="${L.claim}" rows="2" placeholder="예: 여러 멤버가 AI를 업무에 더 잘 쓰는 방법을 배우고 싶어 한다">${esc(d.claim)}</textarea>
        <label for="f-h-kind">종류 (kind)</label>
        <select id="f-h-kind">${Object.entries(O.kinds).map(([k, v]) => `<option value="${k}" ${d.kind === k ? "selected" : ""}>${esc(v)} (${k})</option>`).join("")}</select>
        <label for="f-h-members">대상 멤버 (앱에 보이는 이름, 쉼표로 구분 · 모임 전체면 비움)</label>
        <input type="text" id="f-h-members" placeholder="예: 정재용, 하루" value="${esc(d.members)}">
        ${checks("knowledge_ids", O.knowledge, "근거가 되는 지식 (선택 · 멤버가 말한 것과 관찰된 사실만, 모카의 추론은 제외)")}
        ${checks("events", O.events, "대상 정모 (선택)")}
        ${checks("votes", O.votes, "대상 투표 (선택)")}
        <label for="f-h-reasoning">근거 (reasoning, ${L.reasoning}자 이내) — 왜 이렇게 보는지</label>
        <textarea id="f-h-reasoning" maxlength="${L.reasoning}" rows="3">${esc(d.reasoning)}</textarea>
        <label for="f-h-test">확인 방법 (test, ${L.test}자 이내) — 무엇을 보면 뒷받침되거나 약해지는지</label>
        <textarea id="f-h-test" maxlength="${L.test}" rows="2">${esc(d.test)}</textarea>
        <div style="margin-top:12px;display:flex;gap:8px;align-items:center">
          <button type="submit" class="primary">가설 심기</button>
          <span class="muted" style="font-size:12px">폐기되지 않은 가설 ${live}/15</span>
        </div>
        ${ui.hypoMsg ? `<div class="note ${ui.hypoMsg.ok ? "ok" : "err"}">${esc(ui.hypoMsg.text)}</div>` : ""}
      </form>
      ${jsonBox("미리보기 (보낼 내용 — 이름은 저장할 때 자리표시자로 바뀜)", hypoBody(d))}
    </div>
    <div class="panel"><h2>가설 (${S.hypotheses.length}) — 운영 모카가 보는 그대로</h2>${cards}</div>
  </div>`;
  bindHypoForm();
  bindDetails();
}

function bindHypoForm() {
  const form = $("#hypo-form");
  if (!form) return;
  const d = hypoDraft();
  const refresh = () => { const box = form.parentElement.querySelector(".json-box pre"); if (box) box.innerHTML = jsonHtml(hypoBody(d)); };
  const text = { "f-h-claim": "claim", "f-h-members": "members", "f-h-reasoning": "reasoning", "f-h-test": "test" };
  Object.entries(text).forEach(([id, field]) => ($("#" + id).oninput = (e) => { d[field] = e.target.value; refresh(); }));
  $("#f-h-kind").onchange = (e) => { d.kind = e.target.value; refresh(); };
  form.querySelectorAll("[data-check]").forEach((el) => (el.onchange = () => {
    const list = d[el.dataset.check];
    const i = list.indexOf(el.value);
    if (el.checked && i < 0) list.push(el.value);
    if (!el.checked && i >= 0) list.splice(i, 1);
    refresh();
  }));
  form.onsubmit = async (e) => {
    e.preventDefault();
    const res = await fetch("/api/hypotheses", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(hypoBody(d)) });
    const out = await res.json();
    if (res.ok) {
      ui.hypoDraft = null;
      ui.hypoMsg = { ok: true, text: `심었습니다: ${out.hypothesis.id}. 운영 모카가 다음에 깨어날 때 봅니다.` };
    } else ui.hypoMsg = { ok: false, text: out.error };
    render();
  };
}

// --- view: programs (form + list) ------------------------------------------------------------------
const typeLabel = { linear: "단계형", recurring: "정기" };
const repeatLabel = { constant: "정해진 횟수", conditional: "조건이 맞는 동안", infinite: "끝없이" };

function progDraft() {
  ui.progDraft = ui.progDraft || { type: "linear", title: "", purpose: "", hypotheses: {}, reasoning: "", goal_id: "",
    members: "", criteria: "", size_min: "", size_max: "", entry_state: "", exit_state: "", measure: "", duration_days: "",
    interval_days: "", repeat_kind: "infinite", repeat_count: "", repeat_condition: "", format: "" };
  return ui.progDraft;
}

function progBody(d) {
  const int = (v) => (String(v).trim() === "" ? null : Number(v));
  const body = { type: d.type, title: d.title, purpose: d.purpose,
    hypotheses: Object.entries(d.hypotheses).map(([id, why]) => ({ id, why })), reasoning: d.reasoning,
    goal_id: d.goal_id || null, members: d.members.split(",").map((m) => m.trim()).filter(Boolean),
    criteria: d.criteria, size_min: int(d.size_min), size_max: int(d.size_max) };
  if (d.type === "linear") Object.assign(body, { entry_state: d.entry_state, exit_state: d.exit_state, measure: d.measure,
    duration_days: int(d.duration_days) });
  else Object.assign(body, { interval_days: int(d.interval_days), repeat_kind: d.repeat_kind,
    repeat_count: d.repeat_kind === "constant" ? int(d.repeat_count) : null,
    repeat_condition: d.repeat_kind === "conditional" ? d.repeat_condition : null, format: d.format });
  return body;
}

const activityLabel = { individual_study: "각자 스터디", presentation: "발표", hands_on: "실습", discussion: "토론",
  show_and_tell: "결과물 공유", clinic: "질문·상담", collab_project: "함께 만들기", social: "친목", other: "기타" };
const modeLabel = { offline: "오프라인", online: "온라인", either: "온·오프라인" };
const fillLabel = { volunteer: "희망자 모집", vote: "투표", moca: "모카가 정함", fixed: "고정" };
const whoLabel = { member_volunteer: "맡은 멤버", all_participants: "참가자 모두", moca: "모카" };
const phaseLabel = { before: "전", during: "중", after: "후" };
const typeOf = (x) => x.activity_type === "other" ? (x.type_label || "기타") : activityLabel[x.activity_type] || x.activity_type;

function slotsHtml(slots) {
  return slots.length ? `<ul class="cites">${slots.map((s) => `<li><b>${esc(s.name)}</b> (${esc(s.count)}) · ${esc(fillLabel[s.fill_by] || s.fill_by)} — ${esc(s.how)}
    ${s.fallback ? `<div class="muted">안 채워지면: ${esc(s.fallback)}</div>` : ""}</li>`).join("")}</ul>` : "";
}

function planGroundsHtml(p) {
  const g = p.plan.grounds;
  const k = p.plan_grounds_shown.map((x) => `<li>${esc(x.statement)} <span class="muted mono">${esc(x.id)}</span><div class="muted">→ ${esc(x.supports)}</div></li>`);
  const s = g.sources.map((x) => `<li><a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title || x.url)}</a> — ${esc(x.finding)}<div class="muted">→ ${esc(x.supports)}</div></li>`);
  const q = (p.plan.research || {}).queries || [];
  return `<div class="hypo-line"><b>기획 근거</b> ${esc(g.reasoning)}</div>
    ${k.length || s.length ? `<ul class="cites">${k.join("")}${s.join("")}</ul>` : ""}
    ${q.length ? `<div class="hypo-line muted" style="font-size:12px"><b>검색어</b> ${q.map(esc).join(" · ")}</div>` : ""}`;
}

function planHtml(p) {
  const t = p.plan;
  if (!t) {
    const fail = p.plan_failure ? `<div class="flag">기획 실패 (${esc(p.plan_failure.at)}): ${esc(p.plan_failure.problems.join("; "))}</div>` : "";
    return `<div class="plan"><div class="hypo-line"><b>${p.type === "recurring" ? "템플릿" : "스케치"}</b>
      <span class="muted">아직 없음 — 다음 운영 라운드에 기획 담당이 채웁니다</span></div>${fail}</div>`;
  }
  let body;
  if (p.type === "recurring") {
    body = `<div class="hypo-line"><b>템플릿 v${t.version}</b> ${esc(typeOf(t))} · ${esc(modeLabel[t.mode] || t.mode)} · ${t.duration_minutes}분 — ${esc(t.summary)}</div>
      <div class="hypo-line"><b>한 회차</b> ${esc(t.entry_state)} → ${esc(t.exit_state)} <span class="muted">(확인: ${esc(t.check)})</span></div>
      <ol class="cites">${t.agenda.map((a) => `<li><b>${esc(a.title)}</b> ${a.minutes}분 — ${esc(a.what)}</li>`).join("")}</ol>
      ${t.slots.length ? `<div class="hypo-line"><b>정할 것</b></div>${slotsHtml(t.slots)}` : ""}
      <div class="hypo-line"><b>역할</b></div><ul class="cites">${t.roles.map((r) => `<li>${esc(r.role)} · ${esc(whoLabel[r.who] || r.who)} · 정모 ${esc(phaseLabel[r.phase] || r.phase)} — ${esc(r.what)}</li>`).join("")}</ul>
      ${t.preparation.length ? `<div class="hypo-line"><b>준비</b></div><ul class="cites">${t.preparation.map((x) => `<li>${esc(x.when)} · ${esc(x.who)} — ${esc(x.what)}</li>`).join("")}</ul>` : ""}
      <div class="hypo-line"><b>장소</b> ${esc(t.logistics.place_rule)} · <b>시간</b> ${esc(t.logistics.time_rule)} · <b>비용</b> ${esc(t.logistics.cost)}</div>
      ${t.variations.length ? `<div class="hypo-line"><b>예외</b></div><ul class="cites">${t.variations.map((v) => `<li>${esc(v)}</li>`).join("")}</ul>` : ""}`;
  } else {
    body = `<div class="hypo-line"><b>스케치 v${t.version}</b> ${esc(t.summary)} <span class="muted">(${t.join_until}단계까지 합류 가능)</span></div>
      <ol class="cites">${t.stages.map((s) => `<li><b>day ${s.day} · ${esc(s.title)}</b> <span class="muted">${esc(typeOf(s))} · ${esc(modeLabel[s.mode] || s.mode)}</span>
        <div>${esc(s.entry_state)} → <b>${esc(s.exit_state)}</b></div>
        ${s.needs_from_before ? `<div class="muted">앞 단계에서 필요한 것: ${esc(s.needs_from_before)}</div>` : ""}
        <div>${esc(s.outline)}</div>
        ${s.between ? `<div class="muted">다음까지: ${esc(s.between)}</div>` : ""}
        <div class="muted">확인: ${esc(s.check)} · 못 닿으면: ${esc(s.if_not_reached)}</div>
        ${slotsHtml(s.slots)}</li>`).join("")}</ol>`;
  }
  const hist = (p.plan_history || []).length;
  const brief = p.type === "recurring"
    ? `<div class="hypo-line"><b>템플릿 v${t.version}</b> ${esc(typeOf(t))} · ${esc(modeLabel[t.mode] || t.mode)} · ${t.duration_minutes}분 — ${esc(t.summary)}</div>
       <div class="hypo-line muted">${t.agenda.map((a) => `${esc(a.title)} ${a.minutes}분`).join(" → ")}</div>
       ${t.slots.length ? `<div class="hypo-line"><b>정할 것</b> ${t.slots.map((s) => `${esc(s.name)} <span class="muted">(${esc(fillLabel[s.fill_by] || s.fill_by)})</span>`).join(", ")}</div>` : ""}`
    : `<div class="hypo-line"><b>스케치 v${t.version}</b> ${esc(t.summary)}</div>
       <ol class="cites">${t.stages.map((s) => `<li>day ${s.day} · <b>${esc(s.title)}</b> <span class="muted">${esc(typeOf(s))}</span></li>`).join("")}</ol>`;
  const key = `plan:${p.id}`;
  return `<div class="plan">${brief}
    <details class="item" data-key="${key}" ${ui.openItems.has(key) ? "open" : ""}><summary><span class="label">전체 보기 (근거·검색어 포함)</span></summary>
      ${body}${planGroundsHtml(p)}</details>
    <div class="muted" style="font-size:12px">${esc(t.created_at)}${t.note ? ` · 요청 메모: ${esc(t.note)}` : ""}${hist ? ` · 이전 버전 ${hist}개` : ""}</div></div>`;
}

const actStatus = { draft: "기획 중", scheduled: "정모 잡힘", held: "지난 활동", canceled: "취소됨" };

function activitiesHtml(p) {
  const acts = p.activities || [];
  const agent = p.agent ? `<span class="muted">담당 모카: 목표 ${esc(p.agent.goal_id)}</span>` : `<span class="muted">담당 모카 없음 (기획이 채워지면 붙습니다)</span>`;
  if (!acts.length) return `<div class="hypo-line"><b>활동</b> ${agent} — 아직 기획한 회차 없음</div>`;
  return `<div class="hypo-line"><b>활동 (${acts.length})</b> ${agent}</div>
    <ul class="cites">${acts.map((a) => {
      const left = (a.slots || []).filter((s) => !s.value);
      const done = (a.slots || []).filter((s) => s.value);
      return `<li class="${a.status === "canceled" ? "dim" : ""}">
        ${chip(a.status)} <b>${esc(a.session ? a.session + "회차" : a.stage + "단계")} · ${esc(a.title)}</b>
        <span class="muted mono">${esc(a.id)}</span>
        <div class="muted">${esc(a.when || "시각 미정")} · ${esc(a.location || "장소 미정")}${a.event ? ` · 정모 '${esc(a.event)}'` : ""}</div>
        ${done.length ? `<div>정함: ${done.map((s) => `${esc(s.name)} — ${esc(s.value)}`).join(" · ")}</div>` : ""}
        ${left.length ? `<div class="muted">아직 정할 것: ${left.map((s) => `${esc(s.name)} (${esc(fillLabel[s.fill_by] || s.fill_by)})`).join(", ")}</div>` : ""}
        ${(a.goals || []).length ? `<div class="muted">목표: ${a.goals.map((g) => `${esc(g.objective)} [${esc(statusLabel[g.status] || g.status)}]`).join(" · ")}</div>` : ""}
      </li>`;
    }).join("")}</ul>`;
}

function rewriteHtml(p) {
  if (!["planned", "active", "paused"].includes(p.status)) return "";
  if (p.plan_request) return `<div class="note ok">다시 작성 요청됨 (${esc(p.plan_request.at)})${p.plan_request.note ? `: ${esc(p.plan_request.note)}` : ""} — 다음 운영 라운드에 반영</div>`;
  if (!p.plan) return "";
  const id = `f-p-note-${p.id}`;
  return `<div class="rewrite"><textarea id="${esc(id)}" data-note="${esc(p.id)}" rows="2" maxlength="500"
      placeholder="다시 작성할 때 기획 담당에게 줄 메모 (예: 2시간은 길다, 온라인도 되게)">${esc(ui.planNotes[p.id] || "")}</textarea>
    <button type="button" data-rewrite="${esc(p.id)}">다시 작성 요청</button></div>`;
}

function programCard(p) {
  const key = `prog:${p.id}`, sh = p.shown, size = p.users.size;
  const who = [sh.criteria, sh.members.length ? sh.members.join(", ") : "",
    size.min != null || size.max != null ? `${size.min ?? "?"}~${size.max ?? "?"}명` : ""].filter(Boolean);
  let shape;
  if (p.type === "linear") {
    shape = `<div class="hypo-line"><b>시작 전</b> ${esc(sh.entry_state)}</div>
      <div class="hypo-line"><b>끝난 뒤</b> ${esc(sh.exit_state)} <span class="muted">(${p.linear.duration_days}일)</span></div>
      <div class="hypo-line"><b>확인 방법</b> ${esc(sh.measure)}</div>`;
  } else {
    const r = p.recurring.repeats;
    const times = r.kind === "constant" ? `${r.count}회` : r.kind === "conditional" ? `조건: ${esc(sh.condition)}` : "끝없이";
    shape = `<div class="hypo-line"><b>형식</b> ${esc(sh.format)}</div>
      <div class="hypo-line"><b>반복</b> 약 ${p.recurring.interval_days}일마다 · ${times}</div>`;
  }
  const grounds = p.grounds_shown.map((g) => `<li>${chip(g.status)} ${esc(g.claim)} <span class="muted mono">${esc(g.id)}</span>
    <div>→ ${esc(g.why)}</div></li>`).join("");
  return `<div class="hypo-card ${["finished", "dropped"].includes(p.status) ? "dim" : ""}">
    <div class="row"><span class="obj">${esc(sh.title)}</span>${chip(p.status)}</div>
    <div class="muted" style="font-size:12px">${esc(p.id)} · ${esc(p.type_label)} · ${esc(p.created_by || "")} · ${esc(p.created_at)}
      ${p.goal_id ? ` · 목표 ${esc(p.goal_id)}` : ""}</div>
    ${p.flags.map((f) => `<div class="flag">⚠ ${esc(f)}</div>`).join("")}
    <div class="hypo-line"><b>목적</b> ${esc(sh.purpose)}</div>
    <div class="hypo-line"><b>대상</b> ${who.map(esc).join(" · ")}</div>
    ${shape}
    <div class="hypo-line"><b>존재 근거</b> ${esc(sh.reasoning)}</div>
    <ul class="cites">${grounds}</ul>
    ${planHtml(p)}
    ${activitiesHtml(p)}
    ${rewriteHtml(p)}
    ${ui.planMsg && ui.planMsg.id === p.id ? `<div class="note ${ui.planMsg.ok ? "ok" : "err"}">${esc(ui.planMsg.text)}</div>` : ""}
    <details class="item" data-key="${key}" ${ui.openItems.has(key) ? "open" : ""}><summary><span class="label">원본 JSON</span></summary>
      <pre class="json">${jsonHtml(p)}</pre></details>
  </div>`;
}

function renderPrograms() {
  const O = S.program_options, L = O.limits, R = O.ranges, d = progDraft();
  const live = S.programs.filter((p) => ["planned", "active", "paused"].includes(p.status)).length;
  const field = (id, key, label, rows = 2) => `<label for="${id}">${label} (${L[key === "repeat_condition" ? "condition" : key]}자 이내)</label>
    <textarea id="${id}" data-field="${key}" maxlength="${L[key === "repeat_condition" ? "condition" : key]}" rows="${rows}">${esc(d[key])}</textarea>`;
  const num = (id, key, label, [lo, hi]) => `<div><label for="${id}">${label}</label>
    <input type="number" id="${id}" data-field="${key}" min="${lo}" max="${hi}" value="${esc(d[key])}"></div>`;
  const hypos = O.hypotheses.length ? `<div class="checks" style="max-height:none">${O.hypotheses.map((h) => {
    const on = h.id in d.hypotheses;
    return `<label class="check"><input type="checkbox" data-hypo="${esc(h.id)}" ${on ? "checked" : ""}> ${chip(h.status)} ${esc(h.claim)} <span class="muted mono">${esc(h.id)}</span></label>
      ${on ? `<div class="why"><textarea id="f-p-why-${esc(h.id)}" data-why="${esc(h.id)}" maxlength="${L.why}" rows="2"
        placeholder="이 가설이 이 프로그램을 왜 필요하게 만드는지 (${L.why}자 이내)">${esc(d.hypotheses[h.id])}</textarea></div>` : ""}`;
  }).join("")}</div>` : `<p class="note err">뒷받침됨 이상인 가설이 없어서 지금은 프로그램을 만들 수 없습니다.</p>`;
  const typed = d.type === "linear"
    ? `${field("f-p-entry", "entry_state", "시작 전 상태 (entry_state)")}${field("f-p-exit", "exit_state", "끝난 뒤 상태 (exit_state)")}
       ${field("f-p-measure", "measure", "확인 방법 (measure) — 끝난 상태에 닿았는지 무엇으로 보는지")}
       <div class="pair">${num("f-p-duration", "duration_days", `기간 (일, ${R.duration_days.join("~")})`, R.duration_days)}</div>`
    : `${field("f-p-format", "format", "형식 (format) — 반복되는 주된 활동 하나, 처음 온 사람도 알 수 있게")}
       <div class="pair">${num("f-p-interval", "interval_days", `주기 (대략 며칠마다, ${R.interval_days.join("~")})`, R.interval_days)}
         <div><label for="f-p-repeat">반복</label><select id="f-p-repeat">${Object.entries(O.repeat_kinds).map(([k, v]) =>
           `<option value="${k}" ${d.repeat_kind === k ? "selected" : ""}>${esc(v)} (${k})</option>`).join("")}</select></div></div>
       ${d.repeat_kind === "constant" ? `<div class="pair">${num("f-p-count", "repeat_count", `횟수 (${R.count.join("~")})`, R.count)}</div>` : ""}
       ${d.repeat_kind === "conditional" ? field("f-p-cond", "repeat_condition", "조건 — 언제까지 이어가거나 멈추는지") : ""}`;

  $("#view").innerHTML = `<div class="grid">
    <div class="panel">
      <h2>새 프로그램</h2>
      <p class="muted" style="margin-top:0;font-size:12px">운영 모카가 만든 것과 같은 규칙으로 들어갑니다. 뒷받침됨 이상인 가설에 근거해야 하고,
        '계획됨'으로 시작합니다. 활동(Activity)은 아직 없고, 멤버에게는 보이지 않습니다.</p>
      <form id="prog-form">
        <label for="f-p-type">종류 (type)</label>
        <select id="f-p-type">${Object.entries(O.types).map(([k, v]) => `<option value="${k}" ${d.type === k ? "selected" : ""}>${esc(v)} (${k})</option>`).join("")}</select>
        <label for="f-p-title">이름 (title, ${L.title}자 이내)</label>
        <input type="text" id="f-p-title" data-field="title" maxlength="${L.title}" value="${esc(d.title)}">
        ${field("f-p-purpose", "purpose", "목적 (purpose) — 누구에게 무엇이 어떻게 달라지는지", 3)}
        <label>존재 근거가 되는 가설 (하나 이상 · 뒷받침됨 이상)</label>${hypos}
        ${field("f-p-reasoning", "reasoning", "왜 이 형식인지 (reasoning)", 3)}
        <label for="f-p-goal">목표 (선택)</label>
        <select id="f-p-goal"><option value="">(없음)</option>${O.goals.map((g) => `<option value="${esc(g.id)}" ${d.goal_id === g.id ? "selected" : ""}>${esc(g.id)} — ${esc(g.objective)}</option>`).join("")}</select>
        ${field("f-p-criteria", "criteria", "대상 (criteria) — 누구를 위한 것인지")}
        <label for="f-p-members">참여할 만한 멤버 (앱에 보이는 이름, 쉼표로 구분 · 선택)</label>
        <input type="text" id="f-p-members" data-field="members" value="${esc(d.members)}">
        <div class="pair">${num("f-p-min", "size_min", "최소 인원 (선택)", R.size)}${num("f-p-max", "size_max", "최대 인원 (선택)", R.size)}</div>
        ${typed}
        <div style="margin-top:12px;display:flex;gap:8px;align-items:center">
          <button type="submit" class="primary" ${O.hypotheses.length ? "" : "disabled"}>프로그램 만들기</button>
          <span class="muted" style="font-size:12px">끝나지 않은 프로그램 ${live}/${O.max_live}</span>
        </div>
        ${ui.progMsg ? `<div class="note ${ui.progMsg.ok ? "ok" : "err"}">${esc(ui.progMsg.text)}</div>` : ""}
      </form>
      ${jsonBox("미리보기 (보낼 내용 — 이름은 저장할 때 자리표시자로 바뀜)", progBody(d))}
    </div>
    <div class="panel"><h2>프로그램 (${S.programs.length}) — 운영 모카가 보는 그대로</h2>
      ${S.programs.map(programCard).join("") || `<p class="empty">아직 프로그램이 없습니다.</p>`}</div>
  </div>`;
  bindProgForm();
  bindRewrite();
  bindDetails();
}

function bindRewrite() {
  document.querySelectorAll("[data-note]").forEach((el) => (el.oninput = () => { ui.planNotes[el.dataset.note] = el.value; }));
  document.querySelectorAll("[data-rewrite]").forEach((el) => (el.onclick = async () => {
    const id = el.dataset.rewrite;
    const res = await fetch("/api/programs/plan", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, note: ui.planNotes[id] || "" }) });
    const out = await res.json();
    if (res.ok) { delete ui.planNotes[id]; ui.planMsg = null; } else ui.planMsg = { id, ok: false, text: out.error };
    render();
  }));
}

function bindProgForm() {
  const form = $("#prog-form");
  if (!form) return;
  const d = progDraft();
  const refresh = () => { const box = form.parentElement.querySelector(".json-box pre"); if (box) box.innerHTML = jsonHtml(progBody(d)); };
  form.querySelectorAll("[data-field]").forEach((el) => (el.oninput = () => { d[el.dataset.field] = el.value; refresh(); }));
  form.querySelectorAll("[data-why]").forEach((el) => (el.oninput = () => { d.hypotheses[el.dataset.why] = el.value; refresh(); }));
  form.querySelectorAll("[data-hypo]").forEach((el) => (el.onchange = () => {
    if (el.checked) d.hypotheses[el.dataset.hypo] = d.hypotheses[el.dataset.hypo] || "";
    else delete d.hypotheses[el.dataset.hypo];
    render();
  }));
  $("#f-p-type").onchange = (e) => { d.type = e.target.value; render(); };
  $("#f-p-goal").onchange = (e) => { d.goal_id = e.target.value; refresh(); };
  const rep = $("#f-p-repeat");
  if (rep) rep.onchange = (e) => { d.repeat_kind = e.target.value; render(); };
  form.onsubmit = async (e) => {
    e.preventDefault();
    const res = await fetch("/api/programs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(progBody(d)) });
    const out = await res.json();
    if (res.ok) {
      ui.progDraft = null;
      ui.progMsg = { ok: true, text: `만들었습니다: ${out.program.id}. 운영 모카가 다음에 깨어날 때 봅니다.` };
    } else ui.progMsg = { ok: false, text: out.error };
    render();
  };
}

// --- routing + live data -------------------------------------------------------------------------
function render() {
  if (!S) return;
  renderHeader();
  const [, route, arg] = (location.hash || "#/").split("/");
  document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("on", a.dataset.route === (route || "goals") || (route === "goal" && a.dataset.route === "goals")));
  // keep the goal form's focus and cursor across live updates
  const active = document.activeElement;
  const keep = active && active.id && active.id.startsWith("f-") ? { id: active.id, pos: active.selectionStart } :
    active && active.dataset && active.dataset.crit !== undefined ? { crit: active.dataset.crit, pos: active.selectionStart } : null;
  if (route === "goal" && arg) renderGoal(decodeURIComponent(arg));
  else if (route === "knowledge") renderKnowledge();
  else if (route === "hypotheses") renderHypotheses();
  else if (route === "programs") renderPrograms();
  else if (route === "flow") renderFlow();
  else renderGoals();
  if (keep) {
    const el = keep.id ? $("#" + keep.id) : $(`[data-crit="${keep.crit}"]`);
    if (el && !el.disabled) { el.focus(); try { el.setSelectionRange(keep.pos, keep.pos); } catch (_) {} }
  }
}

function connect() {
  const es = new EventSource("/api/stream");
  es.addEventListener("state", (e) => { connected = true; S = JSON.parse(e.data); render(); });
  es.onerror = () => { connected = false; if (S) renderHeader(); };
}

window.addEventListener("hashchange", () => { ui.formMsg = null; ui.hypoMsg = null; ui.progMsg = null; render(); });
fetch("/api/state").then((r) => r.json()).then((s) => { S = s; render(); });
// ?snapshot: load once without the live connection (for screenshots and saved pages)
if (!new URLSearchParams(location.search).has("snapshot")) connect();
