// 에이전트 커피챗 — 별, 궤도, 서로 다른 속도로 흐르는 색깔 원.
"use strict";

const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* --- 별 ------------------------------------------------------------------------------------- */
(function stars() {
  const canvas = document.getElementById("stars");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  let dots = [];
  let w = 0, h = 0;

  function build() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    w = canvas.clientWidth;
    h = canvas.clientHeight;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    // 화면이 넓을수록 몇 개 더, 그래도 성글게 — 별보다 먼지에 가깝게
    const n = Math.round((w * h) / 9000);
    dots = Array.from({ length: n }, () => ({
      x: Math.random() * w,
      y: Math.random() * h,
      r: Math.random() * 1.1 + 0.25,
      base: Math.random() * 0.4 + 0.18,
      amp: Math.random() * 0.33,
      speed: Math.random() * 0.0011 + 0.0004,
      phase: Math.random() * Math.PI * 2,
    }));
  }

  function draw(t) {
    ctx.clearRect(0, 0, w, h);
    for (const d of dots) {
      const a = reduced ? d.base : d.base + d.amp * Math.sin(t * d.speed + d.phase);
      ctx.globalAlpha = Math.max(0, Math.min(1, a));
      ctx.fillStyle = "#ffffff";
      ctx.beginPath();
      ctx.arc(d.x, d.y, d.r, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.globalAlpha = 1;
  }

  build();
  draw(0);
  if (!reduced) {
    let raf = 0;
    const loop = (t) => { draw(t); raf = requestAnimationFrame(loop); };
    raf = requestAnimationFrame(loop);
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) cancelAnimationFrame(raf);
      else raf = requestAnimationFrame(loop);
    });
  }
  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { build(); draw(performance.now()); }, 150);
  });
})();

/* --- 스크롤에 따라 서로 다른 속도로 움직이는 원과 궤도 ----------------------------------------- */
(function parallax() {
  if (reduced) return;
  const layers = [...document.querySelectorAll("[data-depth]")];
  if (!layers.length) return;

  let ticking = false;
  function apply() {
    const y = window.scrollY || window.pageYOffset;
    for (const el of layers) {
      const depth = parseFloat(el.dataset.depth) || 0;
      // rotate(var(--rot)) 를 함께 써서 궤도의 기울기를 덮어쓰지 않는다 (원에는 --rot이 없어 0deg)
      el.style.transform = `translate3d(0, ${(-y * depth).toFixed(1)}px, 0) rotate(var(--rot, 0deg))`;
    }
    ticking = false;
  }
  window.addEventListener("scroll", () => {
    if (!ticking) { ticking = true; requestAnimationFrame(apply); }
  }, { passive: true });
  apply();
})();

/* --- 주소 복사 -------------------------------------------------------------------------------- */
document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-copy]");
  if (!btn) return;
  const text = btn.dataset.copy;
  let ok = false;
  try {
    await navigator.clipboard.writeText(text);   // https 또는 localhost에서만 된다
    ok = true;
  } catch {
    // 안 되는 환경에서는 조용히 선택만 해 준다 — 아무 일도 안 일어난 것처럼 보이지 않게
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.cssText = "position:fixed;top:-1000px;opacity:0";
    document.body.appendChild(ta);
    ta.select();
    try { ok = document.execCommand("copy"); } catch { ok = false; }
    ta.remove();
  }
  const was = btn.textContent;
  btn.textContent = ok ? "복사했어요" : "복사하지 못했어요";
  btn.classList.toggle("done", ok);
  setTimeout(() => { btn.textContent = was; btn.classList.remove("done"); }, 1800);
});
