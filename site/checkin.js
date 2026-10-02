// 체크인 폼: 한 화면에 한 질문. 답은 끝에서 한 번에 보낸다.
"use strict";

const A = { role: null, topics: ["", ""] };   // 모은 답
let steps = [];
let at = 0;

const $id = (id) => document.getElementById(id);
const card = $id("card"), nav = $id("nav");

// 공개 여부는 묻는 자리에서 알린다 (website.md: "입력을 받는 시점에 무엇이 공개인지 고지")
const PUBLIC = { tag: "공개", note: "안내 페이지의 발표 목록에 보여요." };
const PRIVATE = { tag: "비공개", note: "진행 준비용이라 페이지에 올라가지 않아요." };

const SPEAKER = [
  { key: "name", vis: PUBLIC, q: "어떻게 부르면 될까요?",
    label: "이름과 하시는 일", required: true,
    fields: [{ k: "name", ph: "이름 (예: 로하)", max: 40 },
             { k: "job", ph: "직무 (예: 백엔드 개발자)", max: 60 }] },
  { key: "agent_name", vis: PUBLIC, q: "만드신 에이전트의 이름은?",
    label: "에이전트 이름", required: true,
    fields: [{ k: "agent_name", ph: "예: 모카(MOCA)", max: 40 }] },
  { key: "agent_does", vis: PUBLIC, q: "그 에이전트는 무슨 일을 하나요?",
    label: "한 줄로", required: true, help: "한 문장이면 충분해요.",
    fields: [{ k: "agent_does", ph: "예: 소모임 앱을 직접 조작해서 모임을 운영해요", max: 120 }] },
  { key: "minutes", vis: PRIVATE, q: "발표는 몇 분쯤 걸릴까요?",
    label: "예상 소요 시간", help: "기본은 8분이에요. 더 필요하면 적어 주세요 — 순서를 짤 때 참고할게요.",
    fields: [{ k: "minutes", ph: "예: 8분, 10분이면 넉넉해요", max: 30 }] },
  { key: "demo", vis: PRIVATE, q: "시연할 게 있나요?",
    label: "시연", help: "현장에 스마트TV와 HDMI 커넥터가 있어요. 노트북을 연결할 수 있습니다.",
    fields: [{ k: "demo", ph: "예: 노트북으로 실제 동작을 보여줄 수 있어요 / 없어요", max: 200 }] },
  { key: "slides", vis: PRIVATE, q: "발표자료가 따로 있나요?",
    label: "자료와 공유 여부", help: "없어도 괜찮아요. 준비가 부담되면 간단한 템플릿을 드릴게요.",
    fields: [{ k: "slides", ph: "예: 슬라이드 5장 정도, 끝나고 공유해도 괜찮아요", max: 200 }] },
  { key: "repo", vis: { tag: "있으면 공개", note: "적어 주시면 안내 페이지의 발표 목록에 링크로 걸어요." },
    q: "오픈소스라면 링크를 알려주실 수 있나요?",
    label: "저장소 링크", help: "공개된 것만요. 없으면 비워 두세요.",
    fields: [{ k: "repo", ph: "https://github.com/...", max: 200 }] },
  { key: "want", vis: PRIVATE, q: "발표 뒤 40분, 가장 원하는 건 뭔가요?",
    label: "심화 대화에서", help: "발표가 모두 끝나면 40분 동안 함께 이야기해요.",
    choices: [["feedback", "내가 만든 에이전트 피드백 받기"],
              ["worry", "만들며 생긴 고민 같이 보기"],
              ["both", "둘 다"]] },
  { key: "topics", vis: { tag: "공개", note: "주제 글귀와 이름·에이전트 이름이 안내 페이지에 그대로 보여요." },
    q: "그때 함께 볼 것을 한 줄로 적어 주세요",
    label: "피드백 요청 또는 고민", help: "최대 두 개까지요. 하나만 적어도 괜찮아요.",
    topics: true },
];

const LISTENER = [
  { key: "name", vis: PRIVATE, q: "어떻게 부르면 될까요?",
    label: "이름과 하시는 일", required: true,
    help: "자기소개 시간에 참고하려고 여쭤요. 페이지에는 올라가지 않아요.",
    fields: [{ k: "name", ph: "이름", max: 40 },
             { k: "job", ph: "직무 (예: 기획자)", max: 60 }] },
];

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function render() {
  const s = steps[at];
  nav.hidden = false;
  $id("progress").hidden = false;
  $id("progress-fill").style.width = `${Math.round((at / steps.length) * 100)}%`;
  $id("progress-text").textContent = `${at + 1} / ${steps.length}`;
  $id("prev").disabled = false;
  $id("next").textContent = at === steps.length - 1 ? "체크인 끝내기" : "다음";

  let inner = "";
  if (s.topics) {
    inner = A.topics.map((v, i) => `
      <label class="field">
        <span class="field-label">${i === 0 ? "첫 번째" : "두 번째 (선택)"}</span>
        <input type="text" data-topic="${i}" maxlength="160" value="${esc(v)}"
               placeholder="${i === 0 ? "예: 멀티에이전트가 서로 토론하니까 토큰이 녹아요"
                                      : "예: 돈 받고 팔면 써보실 분이 있을까요?"}">
      </label>`).join("");
  } else if (s.choices) {
    inner = `<div class="choices">${s.choices.map(([v, label]) => `
      <button type="button" class="choice ${A[s.key] === v ? "on" : ""}" data-pick="${v}">
        <b>${esc(label)}</b>
      </button>`).join("")}</div>`;
  } else {
    inner = s.fields.map((f) => `
      <label class="field">
        <input type="text" data-k="${f.k}" maxlength="${f.max}" value="${esc(A[f.k] || "")}"
               placeholder="${esc(f.ph)}">
      </label>`).join("");
  }

  card.innerHTML = `
    <div class="step">
      <div class="vis ${s.vis.tag === "비공개" ? "vis-private" : "vis-public"}">
        <span class="vis-tag">${esc(s.vis.tag)}</span>${esc(s.vis.note)}
      </div>
      <h1 class="q">${esc(s.q)}</h1>
      ${s.label ? `<p class="q-label">${esc(s.label)}</p>` : ""}
      ${inner}
      ${s.help ? `<p class="help">${esc(s.help)}</p>` : ""}
      <p class="err" id="err" hidden></p>
    </div>`;

  const first = card.querySelector("input");
  if (first) first.focus();
}

function collect() {
  card.querySelectorAll("[data-k]").forEach((el) => { A[el.dataset.k] = el.value.trim(); });
  card.querySelectorAll("[data-topic]").forEach((el) => { A.topics[+el.dataset.topic] = el.value.trim(); });
}

function validate() {
  const s = steps[at];
  if (!s.required) return null;
  for (const f of s.fields || []) {
    if (!A[f.k]) return "이 칸은 꼭 적어 주세요.";
  }
  return null;
}

function go(delta) {
  collect();
  if (delta > 0) {
    const problem = validate();
    if (problem) {
      const err = $id("err");
      err.textContent = problem;
      err.hidden = false;
      return;
    }
  }
  const next = at + delta;
  if (next < 0) return start();
  if (next >= steps.length) return submit();
  at = next;
  render();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function submit() {
  $id("next").disabled = true;
  $id("next").textContent = "보내는 중…";
  const payload = {
    role: A.role, name: A.name, job: A.job,
    ...(A.role === "speaker" ? {
      agent_name: A.agent_name, agent_does: A.agent_does, minutes: A.minutes,
      demo: A.demo, slides: A.slides, repo: A.repo, want: A.want,
      topics: A.topics.filter(Boolean),
    } : {}),
  };
  let out = null;
  try {
    const r = await fetch("/api/checkin", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    out = await r.json();
    if (!r.ok) throw new Error(out.error || "보내지 못했어요");
  } catch (e) {
    $id("next").disabled = false;
    $id("next").textContent = "다시 보내기";
    const err = $id("err");
    if (err) { err.textContent = e.message || "보내지 못했어요. 잠시 뒤 다시 시도해 주세요."; err.hidden = false; }
    return;
  }
  done();
}

function done() {
  nav.hidden = true;
  $id("progress").hidden = true;
  const address = "서울 서초구 강남대로61길 10 센터프라자 9층 903호";
  card.innerHTML = `
    <div class="step">
      <p class="done-mark">✓</p>
      <h1 class="q">체크인 완료, 고마워요!</h1>
      <p class="help">${A.role === "speaker"
        ? "적어 주신 내용은 진행 순서를 짤 때 그대로 씁니다. 발표 준비가 부담되면 모임 채팅에서 모카에게 말해 주세요 — 간단한 템플릿을 드릴게요."
        : "준비하실 건 없어요. 편하게 오셔서 궁금한 걸 물어봐 주세요."}</p>

      <div class="venue">
        <p class="venue-when"><b>10월 3일 토요일</b> 11:00 – 13:00</p>
        <p class="venue-where"><b>강남 공튜디오</b></p>
        <p class="venue-addr">${esc(address)}
          <button class="copy" type="button" data-copy="${esc(address)}">주소 복사</button>
        </p>
        <div class="map">
          <iframe title="공튜디오 지도" loading="lazy" referrerpolicy="no-referrer-when-downgrade"
            src="https://maps.google.com/maps?q=${encodeURIComponent(address)}&hl=ko&z=17&output=embed"></iframe>
        </div>
      </div>

      <p class="help dim">그날 발표를 들으면서 떠오른 질문은 안내 페이지에서 남길 수 있어요.</p>
      <p><a class="btn-go" href="/">안내 페이지로</a></p>
    </div>`;
}

function start() {
  at = 0;
  A.role = null;
  nav.hidden = true;
  $id("progress").hidden = true;
  card.innerHTML = `
    <div class="step">
      <h1 class="q">체크인</h1>
      <p class="q-sub">10월 3일 토요일 11:00–13:00 · 강남 공튜디오</p>
      <p class="help">한 번에 한 가지만 물어요. <b>다 답하는 데 5분이면 충분합니다.</b><br>
        <span class="dim">10/3(토) 오전 10시까지 작성해 주세요.</span></p>
      <p class="q-label">먼저, 어느 쪽으로 오시나요?</p>
      <div class="choices">
        <button type="button" class="choice" data-role="speaker">
          <b>발표할게요</b><span>만든 에이전트를 소개하고 고민을 나눠요. 8분 정도.</span>
        </button>
        <button type="button" class="choice" data-role="listener">
          <b>듣기만 할게요</b><span>발표를 듣고 질문해요. 준비할 건 없어요.</span>
        </button>
      </div>
    </div>`;
}

document.addEventListener("click", (e) => {
  const role = e.target.closest("[data-role]");
  if (role) {
    A.role = role.dataset.role;
    steps = A.role === "speaker" ? SPEAKER : LISTENER;
    at = 0;
    return render();
  }
  const pick = e.target.closest("[data-pick]");
  if (pick) {
    A[steps[at].key] = pick.dataset.pick;
    return render();
  }
  if (e.target.id === "next") return go(1);
  if (e.target.id === "prev") return go(-1);
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !nav.hidden && e.target.tagName === "INPUT") {
    e.preventDefault();
    go(1);
  }
});

start();
