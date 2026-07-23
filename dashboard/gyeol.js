/**
 * GYEOL · gyeol.js  v1.0
 * 스타일시트가 못 하는 것만 담당하는 최소 동작 레이어입니다. 의존성 없음, 약 3KB.
 *
 *   focus trap · Escape 스택 · 토스트 큐 · 대비 안전 브랜드 설정 · 테마/밀도 저장
 *
 * React 를 쓴다면 이 파일 대신 Base UI 를 쓰세요. 시각은 CSS 가 그대로 담당합니다.
 */
const G = (() => {
  const raf = (fn) => requestAnimationFrame(() => requestAnimationFrame(fn));
  const FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

  /* ─────────────────────────────────────────────
     1. 대비 안전 브랜드
     밝은 브랜드에서 흰 글자가 깔리는 문제를 막습니다.
     ───────────────────────────────────────────── */
  const srgb = (c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  function luminance(hex) {
    const h = hex.replace("#", "");
    const n = h.length === 3 ? h.split("").map((c) => c + c).join("") : h;
    const [r, g, b] = [0, 2, 4].map((i) => parseInt(n.slice(i, i + 2), 16) / 255);
    return 0.2126 * srgb(r) + 0.7152 * srgb(g) + 0.0722 * srgb(b);
  }
  function contrast(a, b) {
    const [x, y] = [luminance(a), luminance(b)].sort((p, q) => q - p);
    return (x + 0.05) / (y + 0.05);
  }

  /**
   * 브랜드 색을 설정하고, 그 위에 올라갈 글자색을 대비 기준에 맞춰 자동 선택합니다.
   * @returns {{ratio:number, level:'AAA'|'AA'|'FAIL', onBrand:string}}
   */
  function setBrand(hex, el = document.documentElement) {
    el.style.setProperty("--g-brand-source", hex);
    const white = contrast(hex, "#FFFFFF");
    const black = contrast(hex, "#0B0F14");
    const onBrand = white >= black ? "#FFFFFF" : "#0B0F14";
    const ratio = Math.max(white, black);
    el.style.setProperty("--g-fg-on-brand", onBrand);
    const level = ratio >= 7 ? "AAA" : ratio >= 4.5 ? "AA" : "FAIL";
    if (level === "FAIL") {
      console.warn(`[GYEOL] 브랜드 ${hex} 는 어떤 글자색과도 대비 4.5:1 을 넘지 못합니다 (최대 ${ratio.toFixed(2)}:1). 더 어둡거나 더 밝은 색을 쓰세요.`);
    }
    return { ratio, level, onBrand };
  }

  /* ─────────────────────────────────────────────
     2. Escape 스택 — 가장 나중에 열린 것부터 닫힙니다
     ───────────────────────────────────────────── */
  const stack = [];
  function pushLayer(close) {
    stack.push(close);
    return () => { const i = stack.indexOf(close); if (i > -1) stack.splice(i, 1); };
  }
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || !stack.length) return;
    e.stopPropagation();
    stack.pop()();
  });

  /* ─────────────────────────────────────────────
     3. 포커스 트랩
     열면 안쪽 첫 요소로 이동, 닫으면 열었던 자리로 복귀합니다.
     ───────────────────────────────────────────── */
  function trap(node) {
    const restore = document.activeElement;
    const onKey = (e) => {
      if (e.key !== "Tab") return;
      const items = [...node.querySelectorAll(FOCUSABLE)].filter((n) => n.offsetParent !== null);
      if (!items.length) return;
      const first = items[0], last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    };
    node.addEventListener("keydown", onKey);
    raf(() => (node.querySelector("[autofocus]") || node.querySelector(FOCUSABLE) || node).focus());
    return () => {
      node.removeEventListener("keydown", onKey);
      if (restore && restore.isConnected) restore.focus();
    };
  }

  /* ─────────────────────────────────────────────
     4. 오버레이 (시트 · 모달 · 드로어)
     data-overlay / data-drawer 상태는 CSS 가 읽습니다.
     ───────────────────────────────────────────── */
  function overlay(host, panel, { attr = "data-overlay", onClose } = {}) {
    let release = null, unstack = null;
    const api = {
      open() {
        if (host.getAttribute(attr) === "open") return;
        host.setAttribute(attr, "open");
        panel.setAttribute("aria-hidden", "false");
        release = trap(panel);
        unstack = pushLayer(api.close);
      },
      close() {
        if (host.getAttribute(attr) !== "open") return;
        host.setAttribute(attr, "closed");
        panel.setAttribute("aria-hidden", "true");
        release?.(); unstack?.(); release = unstack = null;
        onClose?.();
      },
      toggle() { host.getAttribute(attr) === "open" ? api.close() : api.open(); },
    };
    host.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", api.close));
    panel.setAttribute("aria-hidden", "true");
    return api;
  }

  /* ─────────────────────────────────────────────
     5. 토스트 큐 — 연속 작업에서 메시지가 덮이지 않습니다
     ───────────────────────────────────────────── */
  let region = null;
  function ensureRegion() {
    if (region) return region;
    region = document.createElement("div");
    region.className = "g-toasts";
    region.setAttribute("role", "status");
    region.setAttribute("aria-live", "polite");
    document.body.appendChild(region);
    return region;
  }
  /**
   * @param {string} message
   * @param {{action?:string, onAction?:Function, duration?:number, tone?:'default'|'critical'}} opts
   */
  function toast(message, opts = {}) {
    const { action, onAction, duration = action ? 6000 : 3200, tone = "default" } = opts;
    const el = document.createElement("div");
    el.className = "g-toast" + (tone === "critical" ? " g-toast--critical" : "");
    el.innerHTML = `<span class="g-grow"></span>`;
    el.firstChild.textContent = message;
    if (action) {
      const b = document.createElement("button");
      b.className = "g-toast__action";
      b.textContent = action;
      b.addEventListener("click", () => { onAction?.(); dismiss(); });
      el.appendChild(b);
    }
    ensureRegion().appendChild(el);
    raf(() => el.setAttribute("data-open", "true"));
    const timer = setTimeout(dismiss, duration);
    function dismiss() {
      clearTimeout(timer);
      el.setAttribute("data-open", "false");
      el.addEventListener("transitionend", () => el.remove(), { once: true });
      setTimeout(() => el.remove(), 600);
    }
    return { dismiss };
  }

  /* ─────────────────────────────────────────────
     6. 메뉴 — 바깥 클릭 · Escape · aria-expanded 자동
     ───────────────────────────────────────────── */
  function menu(trigger, panel) {
    let unstack = null;
    const api = {
      open() {
        panel.dataset.open = "true";
        trigger.setAttribute("aria-expanded", "true");
        unstack = pushLayer(api.close);
        raf(() => panel.querySelector(FOCUSABLE)?.focus());
      },
      close() {
        if (panel.dataset.open !== "true") return;
        panel.dataset.open = "false";
        trigger.setAttribute("aria-expanded", "false");
        unstack?.(); unstack = null;
      },
      toggle() { panel.dataset.open === "true" ? api.close() : api.open(); },
    };
    trigger.setAttribute("aria-haspopup", "menu");
    trigger.setAttribute("aria-expanded", "false");
    trigger.addEventListener("click", (e) => { e.stopPropagation(); api.toggle(); });
    document.addEventListener("click", (e) => { if (!panel.contains(e.target)) api.close(); });
    panel.addEventListener("click", (e) => { if (e.target.closest("button")) api.close(); });
    return api;
  }

  /* ─────────────────────────────────────────────
     7. 테마 · 밀도 — 저장하고 복원합니다
     ───────────────────────────────────────────── */
  function prefs(storageKey = "gyeol") {
    const root = document.documentElement;
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem(storageKey) || "{}"); } catch {}
    if (saved.theme) root.setAttribute("data-theme", saved.theme);
    if (saved.density) root.setAttribute("data-density", saved.density);
    if (saved.brand) setBrand(saved.brand);
    const save = () => { try { localStorage.setItem(storageKey, JSON.stringify(saved)); } catch {} };
    return {
      get: () => ({ ...saved }),
      set(key, value) {
        saved[key] = value;
        if (key === "brand") setBrand(value); else root.setAttribute("data-" + key, value);
        save();
      },
    };
  }

  return { setBrand, contrast, luminance, trap, overlay, toast, menu, prefs, pushLayer };
})();

if (typeof module !== "undefined") module.exports = G;
