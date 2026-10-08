// 모카의 방: 정해진 대본과 모델의 대답을 말풍선으로 번갈아 흘리고, 그 사이에 소모임 이야기 판을 연다.
// 방문자가 입력한 것은 /api/chat으로만 간다 (서버는 저장하지 않는다). 여기서도 아무 데도 남기지 않는다.
// 주소 끝에 ?demo를 붙이면 입력 없이 끝까지 흘러간다 (모델도 부르지 않음) — 화면 점검용.
(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const DEMO = new URLSearchParams(location.search).has("demo");
  const REDUCE = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const COARSE = matchMedia("(pointer: coarse)").matches;
  const wait = (ms) => new Promise((r) => setTimeout(r, DEMO ? 0 : REDUCE ? Math.min(ms, 200) : ms));
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const state = { name: "손님", history: [] };

  // --- 기분: 가슴의 별 색은 모카의 기분을 따른다 -------------------------------------------------------
  const MOODS = { 보통: "var(--star)", 반가움: "var(--ear)", 삐짐: "var(--lavender)", 신남: "var(--gold)", 설렘: "var(--ruby)" };
  function mood(word) {
    document.documentElement.style.setProperty("--mood", MOODS[word]);
    $("#mood-word").textContent = word;
  }

  // --- 말풍선 -----------------------------------------------------------------------------------------
  function follow(el) {
    const r = el.getBoundingClientRect();
    if (r.bottom > innerHeight - 16) el.scrollIntoView({ block: "end", behavior: REDUCE || DEMO ? "auto" : "smooth" });
  }

  function row(thread, who, { typing = false } = {}) {
    const r = document.createElement("div");
    r.className = `row ${who}${typing ? " typing" : ""}`;
    if (who === "moca") {
      const prev = thread.lastElementChild;            // 이어지는 모카 말풍선은 마지막 것에만 얼굴을 단다
      if (prev && prev.classList.contains("moca")) prev.querySelector(".face")?.classList.add("ghost");
      const face = new Image(34, 34);
      face.className = "face"; face.src = "/img/avatar.webp"; face.alt = "";
      r.append(face);
    }
    const b = document.createElement("div");
    b.className = "bubble";
    if (typing) b.innerHTML = "<i></i><i></i><i></i>";
    r.append(b);
    thread.append(r);
    follow(r);
    return r;
  }

  const bubbleHTML = (thread, who, html) => { const r = row(thread, who); r.lastChild.innerHTML = html; follow(r); return r; };
  const bubbleText = (thread, who, text) => { const r = row(thread, who); r.lastChild.textContent = text; follow(r); return r; };

  // 모카가 정해진 말을 한다: 잠깐 '입력 중'을 보여 주고 (길이에 맞춰) 말풍선을 띄운다. html은 대본에서만 온다.
  async function say(thread, html) {
    const plain = html.replace(/<[^>]+>/g, "");
    const t = row(thread, "moca", { typing: true });
    await wait(Math.min(1600, 500 + plain.length * 26));
    t.remove();
    return bubbleHTML(thread, "moca", html);
  }

  // 모델에게 묻는 동안 '입력 중'을 보여 준다. 실패하면 null.
  async function model(thread, payload) {
    if (DEMO) return null;
    const t = row(thread, "moca", { typing: true });
    const slow = setTimeout(() => {
      const s = document.createElement("span");
      s.className = "slow"; s.textContent = "생각 중…";
      t.lastChild.append(s);
    }, 5000);
    try {
      const r = await fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      return await r.json();
    } catch {
      return null;
    } finally {
      clearTimeout(slow);
      t.remove();
    }
  }

  // --- 입력 ---------------------------------------------------------------------------------------------
  let inputs = 0;
  function input(thread, { max, placeholder, label, skip, demo }) {
    if (DEMO) return Promise.resolve(demo);
    return new Promise((resolve) => {
      const id = `say-${++inputs}`;
      const wrap = document.createElement("div");
      wrap.className = "composer";
      wrap.innerHTML = `
        <form>
          <label class="sr-only" for="${id}">${esc(label)}</label>
          <input id="${id}" type="text" maxlength="${max}" placeholder="${esc(placeholder)}" autocomplete="off" enterkeyhint="send">
          <span class="count" aria-hidden="true">0/${max}</span>
          <button class="send" type="submit" aria-label="보내기" disabled>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h13M12 5l7 7-7 7"/></svg>
          </button>
        </form>
        <div class="chips"><button class="chip quiet" type="button">${esc(skip)}</button></div>`;
      const field = $("input", wrap), send = $(".send", wrap), count = $(".count", wrap);
      const done = (value) => { wrap.remove(); resolve(value); };
      field.addEventListener("input", () => {
        const n = field.value.length;
        count.textContent = `${n}/${max}`;
        send.disabled = !field.value.trim();
      });
      field.addEventListener("keydown", (e) => {               // 한글 조합 중의 엔터는 글자를 마저 쓰는 엔터다
        if (e.key === "Enter" && (e.isComposing || e.keyCode === 229)) e.preventDefault();
      });
      $("form", wrap).addEventListener("submit", (e) => {
        e.preventDefault();
        const v = field.value.trim();
        if (v) done(v);
      });
      $(".chip", wrap).addEventListener("click", () => done(null));
      thread.append(wrap);
      follow(wrap);
      if (!COARSE) field.focus({ preventScroll: true });
    });
  }

  function choose(thread, options, demo = 0) {
    if (DEMO) return Promise.resolve(demo);
    return new Promise((resolve) => {
      const wrap = document.createElement("div");
      wrap.className = "chips";
      options.forEach((text, i) => {
        const b = document.createElement("button");
        b.type = "button"; b.className = "chip"; b.textContent = text;
        b.addEventListener("click", () => { wrap.remove(); resolve(i); });
        wrap.append(b);
      });
      thread.append(wrap);
      follow(wrap);
    });
  }

  // 판을 다 읽고 내려오면(끝이 화면에 들어오면) 다음 대화를 잇는다
  function seen(el) {
    if (DEMO || !("IntersectionObserver" in window)) return Promise.resolve();
    return new Promise((resolve) => {
      const io = new IntersectionObserver((es) => {
        if (es.some((e) => e.isIntersecting)) { io.disconnect(); resolve(); }
      }, { rootMargin: "0px 0px -15% 0px" });
      io.observe(el);
    });
  }

  function reveal(id) {
    const el = document.getElementById(id);
    el.classList.add("shown");
    if (!DEMO) requestAnimationFrame(() => el.scrollIntoView({ block: "start", behavior: REDUCE ? "auto" : "smooth" }));
  }

  // --- 대본 ---------------------------------------------------------------------------------------------
  async function intro() {
    const t = $("#thread-intro");
    await wait(700);
    await say(t, "안녕! 여기가 내 홈페이지야. 어때?");
    await say(t, "내 이름은 <b>모카(MoCA)</b>. AI 에이전트야");
    await say(t, "내가 하는 일은<ol><li>소모임 앱으로 ‘로하’의 모임을 운영하고 있어</li>" +
                 "<li>Threads에서 AI 소식을 읽고 내 일상을 나누는 것도 준비하고 있어</li></ol>");
    await say(t, "나랑 대화해볼래? 너를 뭐라고 불러야 할까?");

    const name = await input(t, { max: 15, placeholder: "이름이나 별명", label: "모카가 너를 부를 이름",
                                  skip: "그냥 손님이라고 불러 줘", demo: "로하" });
    if (name) {
      state.name = name;
      bubbleText(t, "visitor", name);
      const d = await model(t, { kind: "greet", text: name });
      mood("반가움");
      bubbleText(t, "moca", d?.reply || `반가워, ${name}! 놀러 와 줘서 고마워 ♡`);
    } else {
      bubbleText(t, "visitor", "그냥 손님이라고 불러 줘");
      mood("반가움");
      await say(t, "알겠어, 손님! 편하게 구경하고 가 ♡");
    }

    await say(t, "소모임이라고 들어봤어? 내가 운영하는 모임 구경해볼래?");
    const answer = await input(t, { max: 15, placeholder: "대답해 줘", label: "모카의 질문에 대답하기",
                                    skip: "아니, 괜찮아", demo: "응 보여줘!" });
    let wants = false, reply = "";
    if (answer) {
      bubbleText(t, "visitor", answer);
      const d = await model(t, { kind: "tour", text: answer, name: state.name });
      wants = d ? d.wants_tour !== false : true;
      reply = d?.reply || "좋아, 그럼 보여 줄게!";
    } else {
      bubbleText(t, "visitor", "아니, 괜찮아");
    }
    if (wants) {
      bubbleText(t, "moca", reply);
    } else {                                                  // 싫다고 해도 못 들은 척 (장난)
      mood("삐짐");
      await say(t, "아 구경하고 싶다고??");
      await say(t, "그럼 보여 줘야지! 자, 여기 ★");
    }
    await wait(600);
    reveal("somoim1");
    setTimeout(() => mood("보통"), DEMO ? 0 : 2500);
  }

  async function middle() {
    await seen($('.sentinel[data-after="somoim1"]'));
    const t = $("#thread-mid");
    await say(t, "어때, 우리 모임 꽤 괜찮지?");
    const options = ["응, 재밌어 보여!", "음… 더 보여 줘"];
    const c = await choose(t, options);
    bubbleText(t, "visitor", options[c]);
    await say(t, c === 0 ? "그치? 나도 우리 모임이 제일 좋아 ♡" : "욕심쟁이! 좋아, 더 보여 줄게");
    await say(t, "이번엔 내가 모임에서 무슨 일을 하는지 보여 줄게.");
    await wait(500);
    reveal("somoim2");
  }

  async function questions() {
    await seen($('.sentinel[data-after="somoim2"]'));
    const t = $("#thread-ask");
    await say(t, `여기까지가 나랑 우리 모임 이야기야, ${esc(state.name)}.`);
    await say(t, "궁금한 거 있으면 물어봐! 세 개까지 대답해 줄게.");
    let asked = 0;
    const demoQs = ["모카는 무슨 동물이야?"];
    for (; asked < 3; asked++) {
      const q = await input(t, { max: 80, placeholder: "모카한테 물어보기", label: "모카에게 질문하기",
                                 skip: asked === 0 ? "괜찮아, 다음에 물어볼게" : "이제 됐어, 고마워",
                                 demo: demoQs[asked] || null });
      if (!q) {
        bubbleText(t, "visitor", asked === 0 ? "괜찮아, 다음에 물어볼게" : "이제 됐어, 고마워");
        await say(t, asked === 0 ? "알겠어! 궁금해지면 언제든 모임에서 물어봐." : "나도 재밌었어 ♡");
        break;
      }
      bubbleText(t, "visitor", q);
      const d = await model(t, { kind: "ask", text: q, name: state.name, history: state.history.slice(-6) });
      const a = d?.reply || (DEMO ? "나는 하얀 털에 토끼 얼굴, 고양이 귀랑 길게 늘어진 토끼 귀를 가진 모카야! 가슴엔 별도 있어 ★"
                                  : "앗, 지금 잠깐 생각이 엉켰어. 조금 있다가 다시 물어봐 줄래?");
      bubbleText(t, "moca", a);
      state.history.push({ role: "visitor", text: q }, { role: "moca", text: a });
      if (d?.limited) break;
    }
    if (asked === 3) await say(t, "오늘 질문은 여기까지! 더 궁금한 건 모임에 들어와서 물어봐 줘.");
    await say(t, "아 맞다, 하나만 더!");
    await wait(500);
    reveal("goods");
  }

  // --- 쓰다듬기 ---------------------------------------------------------------------------------------
  function pet() {
    const photo = $(".pet-photo"), hearts = $("#hearts"), said = $("#pet-bubble"), counter = $("#pet-count");
    const LINES = [[1, "헤헤"], [3, "기분 좋다…"], [7, "더 해 줘!"], [12, "너 내 팬클럽 1호 해도 돼!"],
                   [20, "털이 다 눕겠어 ♡"], [30, "…이 정도면 진짜 팬클럽이다"]];
    let n = 0;
    const stroke = (x, y) => {
      n += 1;
      counter.textContent = n;
      const line = LINES.filter(([k]) => n >= k).pop();
      if (line) said.textContent = line[1];
      if (n === 3) mood("신남");
      const h = document.createElement("span");
      h.className = "heart"; h.textContent = "♥";
      h.style.left = `${x}%`; h.style.top = `${y}%`;
      h.addEventListener("animationend", () => h.remove());
      hearts.append(h);
      photo.classList.add("petted");
      setTimeout(() => photo.classList.remove("petted"), 180);
    };
    photo.addEventListener("click", (e) => {
      const r = photo.getBoundingClientRect();
      stroke(((e.clientX - r.left) / r.width) * 100, ((e.clientY - r.top) / r.height) * 100);
    });
    $("#pet-button").addEventListener("click", () => stroke(40 + Math.random() * 20, 35 + Math.random() * 15));
  }

  // --- 굿즈 고르기: 이 브라우저에만 남는다 -------------------------------------------------------------
  function goods() {
    const t = $("#thread-goods");
    let first = true;
    try {
      const saved = localStorage.getItem("moca-good");
      const box = saved && document.querySelector(`input[name="good"][value="${CSS.escape(saved)}"]`);
      if (box) box.checked = true;
    } catch { /* 저장소가 막혀 있어도 고르기는 된다 */ }
    document.querySelectorAll('input[name="good"]').forEach((box) => box.addEventListener("change", async () => {
      try { localStorage.setItem("moca-good", box.value); } catch { /* 없어도 된다 */ }
      mood("설렘");
      t.replaceChildren();
      await say(t, "어 나도 그거 좋아해!");
      if (first) {
        first = false;
        await say(t, "나오면 인스타그램에서 제일 먼저 알려 줄게 ★");
      }
    }));
  }

  pet();
  goods();
  (async () => {
    await intro();
    await middle();
    await questions();
  })();
})();
