// 모카 dashboard frontend. One snapshot of the loop's state arrives over SSE (/api/stream) whenever
// any of moca's data files changes; every view is rendered from that snapshot.
"use strict";

let S = null;                 // latest state from the server
let connected = false;
const ui = { selectedNode: {}, openItems: new Set(), knowledgeDesc: true, formDraft: null, formMsg: null };

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
  queued: "대기열", running: "진행 중", succeeded: "성공", failed: "실패", unknown: "?" };
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
      <div class="stat"><div class="n">${st.by_basis.inferred || 0}</div><div class="l">모카의 추론</div></div>
      <div class="stat"><div class="n">${st.today}</div><div class="l">오늘 생김</div></div>
      ${tasks.map(([t, n]) => `<div class="stat"><div class="n">${n}</div><div class="l">${esc(t)}</div></div>`).join("")}
    </div>
    <div class="panel wide"><h2>지식 (운영 모카가 보는 그대로 — 허락하지 않은 멤버는 '한 멤버')</h2>
      ${rows.length ? `<div class="table-wrap"><table><thead><tr><th id="k-sort">created_at ${ui.knowledgeDesc ? "▼" : "▲"}</th><th>내용</th><th>근거</th><th>작업</th><th>id</th></tr></thead><tbody>
        ${rows.map((k) => `<tr><td class="mono">${esc(k.created_at)}</td><td>${esc(k.statement)}</td>
          <td>${k.basis === "reported" ? "직접 말함" : "추론"} <span class="muted">(${k.sources}개 메시지)</span></td>
          <td class="mono">${esc(k.task || "")}</td><td class="mono">${esc(k.id)}</td></tr>`).join("")}
      </tbody></table></div>` : `<p class="empty">아직 쌓인 지식이 없습니다. 모임 채팅 작업이 끝나면 여기에 쌓입니다.</p>`}
    </div>`;
  const sort = $("#k-sort");
  if (sort) sort.onclick = () => { ui.knowledgeDesc = !ui.knowledgeDesc; render(); };
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

window.addEventListener("hashchange", () => { ui.formMsg = null; render(); });
fetch("/api/state").then((r) => r.json()).then((s) => { S = s; render(); });
// ?snapshot: load once without the live connection (for screenshots and saved pages)
if (!new URLSearchParams(location.search).has("snapshot")) connect();
