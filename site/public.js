// 안내 페이지에 체크인 결과를 붙인다. 공개라고 고지한 것만 서버가 내려준다(/api/public).
"use strict";

(function publicData() {
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const listEl = document.getElementById("speakers-list");
  const cloudEl = document.getElementById("cloud");
  const topicsEl = document.getElementById("cloud-topics");
  if (!listEl || !cloudEl) return;

  let picked = null;

  function speakers(list) {
    if (!list.length) return;   // 비어 있을 때의 안내는 HTML에 이미 있다
    listEl.innerHTML = list.map((s) => `
      <article class="speaker">
        <h3 class="speaker-agent">${esc(s.agent_name)}</h3>
        <p class="speaker-does">${esc(s.agent_does)}</p>
        <p class="speaker-who">${esc(s.name)}<span class="dim"> · ${esc(s.job)}</span></p>
        ${s.repo ? `<p class="speaker-repo"><a href="${esc(s.repo)}" target="_blank"
           rel="noopener noreferrer">${esc(s.repo.replace(/^https?:\/\//, ""))}</a></p>` : ""}
      </article>`).join("");
  }

  function cloud(words) {
    if (!words.length) return;
    const most = Math.max(...words.map((w) => w.count));
    cloudEl.innerHTML = words.map((w) => {
      // 크기는 그 키워드에 걸린 주제 수로 (website.md)
      const size = (1 + (w.count - 1) / Math.max(most - 1, 1) * 1.6).toFixed(2);
      return `<button type="button" class="word" data-word="${esc(w.word)}"
        style="font-size:${size}rem">${esc(w.word)}<span class="word-n">${w.count}</span></button>`;
    }).join("");
  }

  function showTopics(words, word) {
    const hit = words.find((w) => w.word === word);
    if (!hit) return;
    topicsEl.hidden = false;
    // 원문 그대로 — 모델이 요약하지 않는다
    topicsEl.innerHTML = `
      <p class="cloud-head"><b>${esc(word)}</b><span class="dim"> · ${hit.count}개</span></p>
      ${hit.topics.map((t) => `
        <blockquote class="topic">
          <p class="topic-text">${esc(t.text)}</p>
          <footer class="topic-who">${esc(t.name)}<span class="dim"> · ${esc(t.agent_name)}</span></footer>
        </blockquote>`).join("")}`;
    [...cloudEl.querySelectorAll(".word")].forEach((b) =>
      b.classList.toggle("on", b.dataset.word === word));
  }

  async function load() {
    let data;
    try {
      const r = await fetch("/api/public", { cache: "no-store" });
      if (!r.ok) return;
      data = await r.json();
    } catch { return; }

    speakers(data.speakers || []);
    cloud(data.keywords || []);

    cloudEl.onclick = (e) => {
      const b = e.target.closest("[data-word]");
      if (!b) return;
      if (picked === b.dataset.word) {            // 한 번 더 누르면 접는다
        picked = null;
        topicsEl.hidden = true;
        [...cloudEl.querySelectorAll(".word")].forEach((x) => x.classList.remove("on"));
        return;
      }
      picked = b.dataset.word;
      showTopics(data.keywords, picked);
    };
  }

  load();
  setInterval(load, 30000);   // 당일에 체크인이 들어오면 새로고침 없이 따라붙는다
})();
