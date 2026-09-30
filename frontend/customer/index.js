// KIRO wheel of fortune: customer views (host API v1). Views: "wheel" page, "home-card" slot.
// A vertical column of prize "stickers" drifts down on idle and runs a fixed 5 s spin
// (wind-up, fast start, long deceleration, overshoot spring). After a spin the prize is
// pending: keep it, gift it to a friend, or decline once and spin again for free.

const BASE_STEP = 168; // px between item centres at scale 1
const BASE_DRUM = 430; // drum height at scale 1
const SPIN_MS = 5000;

const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const ERRORS = {
  disabled: "Колесо удачи сейчас выключено",
  subscription_required: "Для этого приза нужна активная подписка",
  no_spins: "Вращения закончились",
  no_prizes: "Призы ещё не настроены",
  access_denied: "Доступ ограничен",
  unauthorized: "Войдите в аккаунт",
  pending_prize: "Сначала решите, что делать с выигрышем",
  already_resolved: "Этот приз уже обработан",
  reroll_used: "Бесплатная перекрутка уже использована",
  reroll_disabled: "Перекрутка отключена",
  gift_disabled: "Подарки отключены",
  gift_not_found: "Подарок с таким кодом не найден",
  gift_used: "Этот подарок уже забрали",
  gift_expired: "Срок подарка истёк",
  gift_own: "Нельзя забрать собственный подарок",
  fulfil_failed: "Не удалось выдать приз, попробуйте позже",
};

const KIND_ICON = { nothing: "🍀", days: "📅", traffic: "📶", premium: "⚡", balance: "💰", discount: "🏷️", gift_code: "🎁", manual: "🏆" };
const KIND_COLOR = { nothing: "#6b7280", days: "#2f6fed", traffic: "#0ea5e9", premium: "#f59e0b", balance: "#8b5cf6", discount: "#1fb45a", gift_code: "#ec4899", manual: "#10b981" };

const SLOT_ICON = `<svg data-kw-icon width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="5" width="14" height="15" rx="2"/><path d="M6 5V3.5A1.5 1.5 0 0 1 7.5 2h5A1.5 1.5 0 0 1 14 3.5V5"/><rect x="5.5" y="8.5" width="9" height="5" rx="1"/><path d="M8.5 8.5v5M11.5 8.5v5"/><path d="M6.5 17h7"/><path d="M17 9h2.5a1 1 0 0 1 1 1v3"/><circle cx="20.5" cy="14.5" r="1.5"/></svg>`;

const STYLE = `
.kw{display:flex;flex-direction:column;gap:14px;color:var(--text);padding-bottom:14px;width:100%;max-width:100%;min-width:0;
  box-sizing:border-box;overflow-x:hidden}
.kw [data-body]{display:flex;flex-direction:column;gap:14px}
.kw-stage{position:relative;overflow:hidden;border-radius:24px;padding:16px 14px;color:var(--kt,#fff);width:100%;box-sizing:border-box;
  background:radial-gradient(120% 70% at 50% 42%,rgba(255,255,255,.2),transparent 60%),linear-gradient(170deg,var(--g1),var(--g2));
  box-shadow:0 10px 30px rgba(0,0,0,.25)}
.kw-top{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;position:relative;z-index:3}
.kw-top h2{margin:0;font-size:clamp(18px,5.6vw,24px);line-height:1.15;font-weight:800;text-shadow:0 2px 8px rgba(0,0,0,.18)}
.kw-top p{margin:4px 0 0;font-size:13px;opacity:.9}
.kw-count{flex-shrink:0;display:flex;align-items:center;gap:5px;background:rgba(0,0,0,.28);border-radius:999px;
  padding:5px 10px;font-weight:800;font-size:15px}
.kw-drum{position:relative;margin:0 -14px;perspective:900px;user-select:none;-webkit-user-select:none;overflow:hidden;
  -webkit-mask-image:linear-gradient(transparent,#000 13%,#000 84%,transparent);mask-image:linear-gradient(transparent,#000 13%,#000 84%,transparent)}
.kw-spot{position:absolute;left:50%;top:50%;width:62%;aspect-ratio:1;transform:translate(-50%,-50%);border-radius:50%;
  background:radial-gradient(circle,rgba(255,255,255,.34),rgba(255,255,255,0) 68%)}
.kw-drum.win .kw-spot{animation:kw-flash .9s ease-out}
.kw-it{position:absolute;left:50%;top:50%;width:230px;display:flex;flex-direction:column;align-items:center;transform:translate(-50%,-50%);
  will-change:transform,opacity;-webkit-backface-visibility:hidden;backface-visibility:hidden}
.kw-pic{position:relative;width:128px;height:118px;display:flex;align-items:center;justify-content:center}
.kw-pic img{max-width:100%;max-height:100%;object-fit:contain;filter:drop-shadow(0 10px 14px rgba(0,0,0,.28));pointer-events:none}
.kw-tile{width:118px;height:118px;border-radius:30px;display:flex;align-items:center;justify-content:center;padding:12px;box-sizing:border-box;
  background:var(--tile,#fff);box-shadow:0 10px 18px rgba(0,0,0,.22);transform:rotate(-5deg)}
.kw-tile img{filter:none}
.kw-sticker{width:104px;height:104px;border-radius:30px;display:flex;align-items:center;justify-content:center;font-size:56px;
  background:var(--tile,linear-gradient(145deg,rgba(255,255,255,.95),rgba(255,255,255,.72)));box-shadow:0 10px 18px rgba(0,0,0,.22);
  transform:rotate(-6deg)}
.kw-badge{position:relative;z-index:2;margin-top:-26px;transform:translateX(-14px) rotate(-7deg);padding:6px 14px 7px;border-radius:14px;
  background:var(--c);color:#fff;font-weight:900;font-style:italic;font-size:26px;line-height:1;letter-spacing:-.02em;white-space:nowrap;
  box-shadow:0 6px 0 rgba(0,0,0,.18),0 8px 16px rgba(0,0,0,.2);border:2px solid rgba(255,255,255,.9)}
.kw-cap{margin-top:8px;max-width:210px;text-align:center;font-size:11px;font-weight:800;text-transform:uppercase;line-height:1.15;
  color:var(--kt,#fff);text-shadow:0 1px 3px rgba(0,0,0,.35);transform:rotate(-3deg)}
.kw-it.hit .kw-pic{animation:kw-pop .7s cubic-bezier(.3,1.6,.5,1)}
.kw-feed{position:absolute;left:10px;bottom:104px;z-index:3;display:flex;flex-direction:column;gap:5px;pointer-events:none}
.kw-chip{display:flex;align-items:center;gap:6px;background:rgba(0,0,0,.34);border-radius:12px;padding:4px 8px 4px 4px;
  font-size:11px;line-height:1.2;max-width:190px;animation:kw-in .5s ease-out both;color:#fff}
.kw-chip i{font-style:normal;width:22px;height:22px;border-radius:50%;background:rgba(255,255,255,.9);color:#333;display:flex;
  align-items:center;justify-content:center;font-weight:800;font-size:11px;flex-shrink:0}
.kw-chip span{opacity:.8}
.kw-cta{position:relative;z-index:3;width:100%;margin-top:6px;padding:15px;border:0;border-radius:16px;background:var(--kbb,#fff);
  color:var(--kbt,#1b1b1f);font-size:18px;font-weight:900;font-style:italic;text-transform:uppercase;letter-spacing:.02em;cursor:pointer;
  box-shadow:0 6px 0 rgba(0,0,0,.18);transition:transform .08s}
.kw-cta:active{transform:translateY(3px);box-shadow:0 3px 0 rgba(0,0,0,.18)}
.kw-cta:disabled{opacity:.75;cursor:default;font-style:normal;text-transform:none;font-size:15px;font-weight:700}
.kw-note{position:relative;z-index:3;text-align:center;font-size:12px;opacity:.9;margin-top:8px;min-height:15px}
.kw-conf{position:absolute;left:50%;top:50%;width:9px;height:14px;border-radius:2px;z-index:4;pointer-events:none;
  animation:kw-conf 1.3s cubic-bezier(.2,.7,.4,1) forwards}
.kw-card{border:1px solid var(--border);border-radius:var(--radius,14px);padding:12px 14px}
.kw-card h3{margin:0 0 10px;font-size:15px}
.kw-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(112px,1fr));gap:10px}
.kw-prize{border:1px solid var(--border);border-radius:14px;padding:10px 8px;text-align:center}
.kw-prize .kw-mini-pic{width:58px;height:58px;margin:0 auto 6px;display:flex;align-items:center;justify-content:center;font-size:30px;
  border-radius:16px}
.kw-prize .kw-mini-pic img{max-width:84%;max-height:84%;object-fit:contain}
.kw-prize b{display:block;font-size:13px}.kw-prize span{font-size:12px;color:var(--muted)}
.kw-list{display:flex;flex-direction:column;gap:8px;font-size:14px}
.kw-row{display:flex;justify-content:space-between;align-items:center;gap:10px}
.kw-row span{color:var(--muted);font-size:12px}
.kw-redeem{display:flex;gap:8px}
.kw-redeem input{flex:1;min-width:0;font:inherit;font-size:16px;letter-spacing:.08em;text-transform:uppercase;color:var(--text);
  background:transparent;border:1px solid var(--border);border-radius:12px;padding:10px 12px}
.kw-btn{border:1px solid var(--border);background:transparent;color:var(--text);border-radius:12px;padding:9px 14px;cursor:pointer;font:inherit;font-weight:600;white-space:nowrap}
.kw-btn.acc{background:var(--accent);border-color:var(--accent);color:var(--accent-contrast,#fff)}
.kw-mini{display:flex;align-items:center;gap:12px;border-radius:18px;padding:12px 14px;color:var(--kt,#fff);
  background:linear-gradient(135deg,var(--g1),var(--g2))}
.kw-mini .kw-ico{font-size:30px}.kw-mini div{flex:1;min-width:0}.kw-mini b{display:block}.kw-mini span{font-size:13px;opacity:.9}
.kw-mini button{border:0;border-radius:12px;background:var(--kbb,#fff);color:var(--kbt,#1b1b1f);padding:9px 14px;font-weight:800;cursor:pointer}
@keyframes kw-pop{0%{transform:scale(1)}40%{transform:scale(1.22) rotate(-4deg)}100%{transform:scale(1)}}
@keyframes kw-flash{0%{transform:translate(-50%,-50%) scale(.8);opacity:.6}35%{transform:translate(-50%,-50%) scale(1.5);opacity:1}100%{transform:translate(-50%,-50%) scale(1)}}
@keyframes kw-conf{0%{transform:translate(0,0) rotate(0);opacity:1}100%{transform:translate(var(--dx),var(--dy)) rotate(var(--rot));opacity:0}}
@keyframes kw-in{from{opacity:0;transform:translateX(-12px)}to{opacity:1;transform:none}}
`;

// Rendered on document.body so it is never trapped under the host layout or bottom navigation.
const MODAL_STYLE = `
.kw-overlay{position:fixed;top:0;left:0;right:0;bottom:0;width:100%;height:100%;margin:0;z-index:2147483000;background:rgba(0,0,0,.62);overflow-y:auto;-webkit-overflow-scrolling:touch;
  display:flex;padding:max(16px,env(safe-area-inset-top)) 14px calc(96px + env(safe-area-inset-bottom));box-sizing:border-box;
  animation:kw-fade .25s ease-out;font-family:inherit}
.kw-win{margin:auto;align-self:center;flex:0 1 auto;box-sizing:border-box;background:var(--bg,#16181d);color:var(--text,#fff);border-radius:24px;overflow:hidden;max-width:380px;width:100%;
  text-align:center;animation:kw-rise .45s cubic-bezier(.3,1.4,.5,1);box-shadow:0 20px 50px rgba(0,0,0,.45)}
.kw-win-top{padding:clamp(14px,4vh,24px) 16px clamp(12px,3vh,18px);background:linear-gradient(170deg,var(--w1),var(--w2));color:var(--wt,#fff)}
.kw-win-top .kw-pic{margin:0 auto 10px;width:clamp(96px,30vw,150px);height:clamp(90px,17vh,140px);display:flex;align-items:center;justify-content:center}
.kw-win-top .kw-pic img{max-width:100%;max-height:100%;object-fit:contain;filter:drop-shadow(0 10px 14px rgba(0,0,0,.28))}
.kw-win-top .kw-tile{width:clamp(90px,17vh,130px);height:clamp(90px,17vh,130px);border-radius:30px;display:flex;align-items:center;
  justify-content:center;padding:12px;box-sizing:border-box;background:var(--tile,#fff);transform:rotate(-5deg)}
.kw-win-top .kw-tile img{filter:none}
.kw-win-top .kw-sticker{width:100px;height:100px;border-radius:30px;display:flex;align-items:center;justify-content:center;font-size:54px;
  background:var(--tile,rgba(255,255,255,.9))}
.kw-win-top .kw-badge{display:inline-block;padding:6px 14px 7px;border-radius:14px;background:var(--c);color:#fff;font-weight:900;
  font-style:italic;font-size:24px;line-height:1;transform:rotate(-5deg);border:2px solid rgba(255,255,255,.9);box-shadow:0 5px 0 rgba(0,0,0,.18)}
.kw-win-top h3{margin:12px 0 0;font-size:clamp(17px,5vw,21px);color:var(--wt,#fff)}
.kw-win-body{padding:14px 16px 16px;display:flex;flex-direction:column;gap:8px}
.kw-win-body p{margin:0;color:var(--muted,#9aa1ad);font-size:14px;line-height:1.35}
.kw-code{display:flex;gap:8px}.kw-code code{flex:1;padding:10px;border:1px dashed var(--accent,#7c5cff);border-radius:12px;
  font-size:18px;letter-spacing:.08em;color:var(--text,#fff);word-break:break-all}
.kw-act{width:100%;padding:13px;border:0;border-radius:14px;font-size:16px;font-weight:700;cursor:pointer;font-family:inherit}
.kw-act.main{background:var(--accent,#7c5cff);color:var(--accent-contrast,#fff)}
.kw-act.alt{background:rgba(127,127,127,.16);color:var(--text,#fff)}
.kw-act.ghost{background:transparent;color:var(--muted,#9aa1ad);font-weight:600;padding:8px}
.kw-act:disabled{opacity:.55}
.kw-small{border:1px solid var(--border,#333);background:transparent;color:var(--text,#fff);border-radius:12px;padding:0 12px;cursor:pointer;font-family:inherit}
@keyframes kw-fade{from{opacity:0}to{opacity:1}}
@keyframes kw-rise{from{transform:translateY(40px) scale(.92);opacity:0}to{transform:none;opacity:1}}
`;

const mod = (a, n) => ((a % n) + n) % n;
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const easeOutQuart = (t) => 1 - (1 - t) ** 4;
const easeInOutSine = (t) => -(Math.cos(Math.PI * t) - 1) / 2;
const easeOutBack = (t, s = 1.4) => 1 + (s + 1) * (t - 1) ** 3 + s * (t - 1) ** 2;
const now = () => (window.performance && performance.now ? performance.now() : Date.now());

function colorOf(p) {
  return p.color || KIND_COLOR[p.kind] || "#6b7280";
}

function tileOf(p, theme) {
  if (p.tile) return p.tile;
  return theme && theme.tile_enabled ? theme.tile_bg : "";
}

function picHtml(p, theme) {
  const tile = tileOf(p, theme);
  const tileStyle = tile ? ` style="--tile:${esc(tile)}"` : "";
  if (p.image) {
    const img = `<img src="${esc(p.image)}" alt="" draggable="false">`;
    return tile ? `<div class="kw-pic"><div class="kw-tile"${tileStyle}>${img}</div></div>` : `<div class="kw-pic">${img}</div>`;
  }
  return `<div class="kw-pic"><div class="kw-sticker"${tileStyle}>${KIND_ICON[p.kind] || "🎁"}</div></div>`;
}

function stickerHtml(p, theme) {
  return `${picHtml(p, theme)}<div class="kw-badge" style="--c:${esc(colorOf(p))}">${esc(p.badge || p.label)}</div>
    <div class="kw-cap">${esc(p.title)}</div>`;
}

function timeLeft(iso) {
  const ms = new Date(iso).getTime() - Date.now();
  if (!(ms > 0)) return "скоро";
  const h = Math.floor(ms / 3600000);
  const m = Math.floor((ms % 3600000) / 60000);
  return h ? `${h} ч ${m} мин` : `${m} мин`;
}

function ago(iso) {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 90) return "только что";
  if (s < 3600) return `${Math.round(s / 60)} мин назад`;
  if (s < 86400) return `${Math.round(s / 3600)} ч назад`;
  return `${Math.round(s / 86400)} дн назад`;
}

function themeVars(t = {}) {
  const v = (k, d) => esc(t[k] || d);
  return `--g1:${v("bg_from", "#ff3b1d")};--g2:${v("bg_to", "#f7821b")};--kt:${v("text_color", "#ffffff")};` +
    `--kbb:${v("btn_bg", "#ffffff")};--kbt:${v("btn_text", "#1b1b1f")};--w1:${v("win_from", "#ff3b1d")};` +
    `--w2:${v("win_to", "#f7821b")};--wt:${v("win_text", "#ffffff")}`;
}

function errorText(err, fallback) {
  return ERRORS[err?.error] || fallback;
}

// ------------------------------------------------------------------ bottom navigation icon

const NAV_LABELS = new Set(["Колесо удачи", "Wheel of fortune"]);

function patchNavIcon() {
  document.querySelectorAll("nav button[aria-label], .bottom-nav button[aria-label]").forEach((button) => {
    if (!NAV_LABELS.has(button.getAttribute("aria-label"))) return;
    const svg = button.querySelector("svg");
    if (!svg || svg.hasAttribute("data-kw-icon")) return;
    svg.outerHTML = SLOT_ICON;
  });
  arrangeNav();
}

// Core appends plugin items after "Settings". The bar is a CSS grid, so `order` moves the wheel
// to the middle, and the Support entry (reachable from Settings via our card) leaves the bar.
function arrangeNav() {
  document.querySelectorAll("nav.bottom-nav").forEach((nav) => {
    const items = Array.from(nav.children);
    const primary = items.filter((el) => el.matches("button[data-nav-level=primary]:not(.rail-admin-entry)"));
    const wheel = primary.find((el) => NAV_LABELS.has(el.getAttribute("aria-label")));
    if (!wheel) return;
    const rest = primary.filter((el) => el !== wheel);
    // Support and Settings are the only core buttons with the attention marker; Support comes first.
    const marked = rest.filter((el) => el.classList.contains("attention-wrap"));
    const support = marked.length > 1 ? marked[0] : null;
    if (support && !support.hasAttribute("data-kw-hidden")) {
      support.setAttribute("data-kw-hidden", "1");
      support.style.display = "none";
    }
    const visible = rest.filter((el) => el !== support);
    const at = Math.floor((visible.length + 1) / 2);
    const sequence = [...visible.slice(0, at), wheel, ...visible.slice(at)];
    sequence.forEach((el, i) => {
      if (el.style.order !== String(i + 1)) el.style.order = String(i + 1);
    });
    const count = sequence.length;
    if (nav.style.getPropertyValue("--bottom-nav-visible-items") !== String(count + 0) && support) {
      nav.style.setProperty("--bottom-nav-visible-items", String(count));
    }
    // Non-primary children (brand, settings subnav, admin entry) keep their place after the buttons.
    items.filter((el) => !primary.includes(el)).forEach((el, i) => {
      const order = el.classList.contains("rail-brand") ? "0" : String(100 + i);
      if (el.style.order !== order) el.style.order = order;
    });
  });
}

function installNavIcon(label) {
  if (label) NAV_LABELS.add(label);
  patchNavIcon();
  if (window.__kiroWheelNavObserver) return;
  let queued = false;
  const observer = new MutationObserver(() => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => {
      queued = false;
      patchNavIcon();
    });
  });
  observer.observe(document.body, { childList: true, subtree: true });
  window.__kiroWheelNavObserver = observer;
}

// ------------------------------------------------------------------ drum engine

function createDrum(el, prizes, theme) {
  const n = prizes.length;
  const nodes = new Map();
  const overrides = new Map();
  const itemFor = (k) => overrides.get(k) || prizes[mod(k, n)];
  const baseRot = (k) => (mod(k * 37, 13) - 6) * 1.1;
  let pos = 0;
  let anim = null;
  let lastPos = 0;
  let lastT = now();
  let raf = 0;
  let idleAt = now() + 1400;
  let idle = true;
  let stopped = false;
  let unit = 1;
  let step = BASE_STEP;

  function resize() {
    const width = el.clientWidth || 360;
    const height = window.innerHeight || 780;
    unit = clamp(Math.min(width / 390, height / 860), 0.66, 1.25);
    step = BASE_STEP * unit;
    el.style.height = `${Math.round(BASE_DRUM * unit)}px`;
  }

  function nodeFor(k) {
    let node = nodes.get(k);
    if (!node) {
      node = document.createElement("div");
      node.className = "kw-it";
      node.innerHTML = stickerHtml(itemFor(k), theme);
      el.appendChild(node);
      nodes.set(k, node);
    }
    return node;
  }

  function paint(t) {
    const dt = Math.max(1, t - lastT) / 1000;
    const speed = Math.abs(pos - lastPos) / dt;
    lastPos = pos;
    lastT = t;
    const blur = idle ? 0 : clamp((speed - 2.5) * 0.7, 0, 9);
    const lo = Math.floor(pos) - 3;
    const hi = Math.ceil(pos) + 3;
    for (const [k, node] of nodes) {
      if (k < lo || k > hi || Math.abs(pos - k) > 2.6) {
        node.remove();
        nodes.delete(k);
      }
    }
    for (let k = lo; k <= hi; k++) {
      const d = pos - k; // > 0: below centre, items travel downward
      const ad = Math.abs(d);
      if (ad > 2.6) continue;
      const node = nodeFor(k);
      const scale = clamp(1 - 0.24 * ad, 0.45, 1) * unit;
      const opacity = ad <= 1 ? 1 : clamp(1 - (ad - 1) * 0.72, 0, 1);
      const rot = baseRot(k) + d * 7;
      const tilt = clamp(-d * 16, -40, 40);
      node.style.transform =
        `translate(-50%,-50%) translate3d(0,${(d * step).toFixed(1)}px,0) rotateX(${tilt.toFixed(1)}deg) ` +
        `rotate(${rot.toFixed(1)}deg) scale(${scale.toFixed(3)})`;
      node.style.opacity = opacity.toFixed(3);
      node.style.zIndex = String(100 - Math.round(ad * 10));
      const b = blur * (ad < 0.5 ? 0.7 : 1) + (ad > 1.5 ? (ad - 1.5) * 2 : 0);
      node.style.filter = b > 0.2 ? `blur(${b.toFixed(1)}px)` : "none";
    }
  }

  function frame(t) {
    if (stopped) return;
    t = typeof t === "number" ? t : now();
    if (anim) {
      const p = clamp((t - anim.start) / anim.dur, 0, 1);
      pos = anim.fn(p);
      if (p >= 1) {
        const done = anim.done;
        anim = null;
        if (done) done(t);
      }
    } else if (idle && t >= idleAt && !document.hidden) {
      const from = pos;
      anim = { start: t, dur: 720, fn: (p) => from + easeOutBack(p, 1.25) };
      anim.done = () => {
        pos = Math.round(pos);
        idleAt = now() + 1500;
      };
    }
    paint(t);
    raf = requestAnimationFrame(frame);
  }

  function targetFor(prize, minDist) {
    const start = Math.round(pos);
    for (let k = start + minDist; k < start + minDist + n; k++) if (itemFor(k).id === prize.id) return k;
    const k = start + minDist;
    overrides.set(k, prize);
    const stale = nodes.get(k);
    if (stale) {
      stale.remove();
      nodes.delete(k);
    }
    return k;
  }

  function jumpTo(prize) {
    idle = false;
    anim = null;
    pos = targetFor(prize, 0);
  }

  // The duration is fixed at 5 s on purpose: the reveal is the game, so it ignores reduced-motion.
  function spinTo(prize) {
    return new Promise((resolve) => {
      idle = false;
      const target = targetFor(prize, Math.max(3 * n, 24));
      const over = 0.22 + Math.random() * 0.12;
      const windUp = 260;
      const settle = 640;
      const run = SPIN_MS - windUp - settle;
      const from = pos;
      let finished = false;
      const finish = () => {
        if (finished) return;
        finished = true;
        clearTimeout(safety);
        anim = null;
        pos = target;
        paint(now());
        resolve(nodes.get(target));
      };
      // requestAnimationFrame pauses in hidden/background WebViews: never leave the result hanging.
      const safety = setTimeout(finish, SPIN_MS + 400);
      anim = {
        start: now(),
        dur: windUp,
        fn: (p) => from - 0.28 * easeInOutSine(p),
        done: (t) => {
          const a = pos;
          const b = target + over;
          anim = {
            start: t,
            dur: run,
            fn: (p) => a + (b - a) * easeOutQuart(p),
            done: (t2) => {
              const c = pos;
              anim = {
                start: t2,
                dur: settle,
                fn: (p) => c + (target - c) * easeOutBack(p, 2.2),
                done: finish,
              };
            },
          };
        },
      };
    });
  }

  function resumeIdle() {
    idle = true;
    idleAt = now() + 2600;
  }

  const onResize = () => resize();
  window.addEventListener("resize", onResize);
  resize();
  raf = requestAnimationFrame(frame);
  return {
    spinTo,
    jumpTo,
    resumeIdle,
    destroy() {
      stopped = true;
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", onResize);
    },
  };
}

function confetti(stage) {
  const colors = ["#ffffff", "#ffe14d", "#7cf29b", "#6ec6ff", "#ff7ab8", "#b18cff"];
  for (let i = 0; i < 34; i++) {
    const c = document.createElement("i");
    c.className = "kw-conf";
    const angle = Math.random() * Math.PI * 2;
    const dist = 110 + Math.random() * 170;
    c.style.background = colors[i % colors.length];
    c.style.setProperty("--dx", `${Math.cos(angle) * dist}px`);
    c.style.setProperty("--dy", `${Math.sin(angle) * dist - 60}px`);
    c.style.setProperty("--rot", `${Math.random() * 720 - 360}deg`);
    c.style.animationDelay = `${Math.random() * 120}ms`;
    stage.appendChild(c);
    setTimeout(() => c.remove(), 1600);
  }
}

// ------------------------------------------------------------------ modal (portal on document.body)

function openModal(theme) {
  const overlay = document.createElement("div");
  overlay.className = "kw-overlay";
  overlay.setAttribute("style", themeVars(theme));
  overlay.innerHTML = `<style>${MODAL_STYLE}</style><div class="kw-win"></div>`;
  // Attach to <html>: a transformed <body> in some WebViews would re-anchor position:fixed.
  document.documentElement.appendChild(overlay);
  const prevOverflow = document.body.style.overflow;
  document.body.style.overflow = "hidden";
  return {
    el: overlay,
    win: overlay.querySelector(".kw-win"),
    close() {
      document.body.style.overflow = prevOverflow;
      overlay.remove();
    },
  };
}

function giftMessage(prize, link) {
  return `Держи подарок из Колеса удачи! 🎁\nТвой подарок: ${prize}\nАктивируй его по ссылке: ${link}`;
}

function shareGift(link, prizeTitle, host) {
  const message = giftMessage(prizeTitle, link);
  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg && typeof tg.openTelegramLink === "function") {
    // Only `url` is passed so the message keeps our wording with the link at the end.
    tg.openTelegramLink(`https://t.me/share/url?url=${encodeURIComponent(message)}`);
    return;
  }
  if (navigator.share) {
    navigator.share({ text: message }).catch(() => {});
    return;
  }
  if (navigator.clipboard) navigator.clipboard.writeText(message).then(() => host.notify("Текст подарка скопирован"), () => host.notify(link));
  else host.notify(link);
}

// Gift links open the Mini App with startapp=wg_<CODE>; Core ignores that payload, the wheel handles it.
const GIFT_KEY = "kiroWheelGiftCode";

function giftFromStart() {
  const tg = window.Telegram && window.Telegram.WebApp;
  const params = new URLSearchParams(location.search);
  const raw = (tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param) || params.get("tgWebAppStartParam") || params.get("startapp") || "";
  const m = String(raw).match(/^wg_([A-Za-z0-9]{6,16})$/);
  return m ? m[1].toUpperCase() : "";
}

function takeGiftCode() {
  const fromUrl = new URLSearchParams(location.search).get("wheel_gift") || "";
  let stored = "";
  try {
    stored = sessionStorage.getItem(GIFT_KEY) || "";
  } catch {}
  return (fromUrl || stored || "").toUpperCase();
}

function forgetGiftCode() {
  try {
    sessionStorage.removeItem(GIFT_KEY);
  } catch {}
}

// ------------------------------------------------------------------ wheel page

function mountWheel(target, props) {
  const host = props.host;
  const root = document.createElement("div");
  root.className = "kw plugin-host";
  root.innerHTML = `<style>${STYLE}</style><div data-body><p style="color:var(--muted)">Загрузка…</p></div>`;
  target.replaceChildren(root);
  const st = { host, root, data: null, drum: null, busy: false, disposed: false, timers: [], modal: null };
  let giftParam = takeGiftCode();

  async function refresh(full) {
    try {
      const data = await host.request("/state");
      if (st.disposed) return;
      const redraw = full || !st.data || JSON.stringify([st.data.prizes, st.data.theme]) !== JSON.stringify([data.prizes, data.theme]);
      st.data = data;
      installNavIcon(data.title);
      if (redraw || !st.drum) render();
      else updateStatus();
    } catch (err) {
      if (!st.disposed)
        root.querySelector("[data-body]").innerHTML = `<p style="color:var(--muted)">${esc(errorText(err, "Не удалось загрузить колесо"))}</p>`;
    }
  }

  function ctaText(s) {
    if (st.data.pending) return "Заберите выигрыш";
    if (s.can_spin) return "Крутить";
    if (s.reason === "no_spins") return `Следующее вращение через ${timeLeft(s.next_free_at)}`;
    return ERRORS[s.reason] || "Недоступно";
  }

  function noteText(d) {
    const parts = [];
    if (d.rules.daily_free_spins) parts.push(`${d.rules.daily_free_spins} бесплатное вращение в день`);
    if (d.rules.spins_per_payment) parts.push(`+${d.rules.spins_per_payment} за каждую оплату`);
    if (d.state.bonus) parts.push(`бонусных: ${d.state.bonus}`);
    return parts.join(" · ");
  }

  function updateStatus() {
    const d = st.data;
    const s = d.state;
    const cta = root.querySelector(".kw-cta");
    if (!cta) return;
    cta.disabled = st.busy || !(s.can_spin || d.pending);
    cta.textContent = st.busy ? "Крутим…" : ctaText(s);
    root.querySelector(".kw-count b").textContent = String(s.available);
    root.querySelector(".kw-note").textContent = noteText(d);
    root.querySelector(".kw-feed").innerHTML = (d.feed || [])
      .slice(0, 2)
      .map((f) => `<div class="kw-chip"><i>${esc((f.name || "?").slice(0, 1).toUpperCase())}</i><div>${esc(f.name)} · ${esc(f.badge)}<br><span>${esc(ago(f.at))}</span></div></div>`)
      .join("");
    renderGifts();
    renderHistory();
  }

  function renderGifts() {
    const box = root.querySelector("[data-gifts]");
    if (!box) return;
    const typed = box.querySelector("[data-code]")?.value;
    const mine = st.data.gifts || [];
    const statusText = { open: "ждёт друга", claimed: "друг забрал", cancelled: "отменён", expired: "срок истёк" };
    box.innerHTML = `<h3>Подарки</h3>
      <div class="kw-redeem"><input data-code maxlength="16" placeholder="Код подарка от друга" autocomplete="off">
        <button type="button" class="kw-btn acc" data-redeem>Получить</button></div>
      ${mine.length ? `<div class="kw-list" style="margin-top:12px">${mine
        .map(
          (g) => `<div class="kw-row"><div><b>${esc(g.prize)}</b><br><span>Код ${esc(g.code)} · ${esc(statusText[g.status] || g.status)}${g.status === "open" ? ` · до ${new Date(g.expires_at).toLocaleDateString("ru-RU")}` : ""}</span></div>
          <div style="display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end">
          ${g.status === "open" && g.link ? `<button type="button" class="kw-btn acc" data-send="${esc(g.code)}">Отправить</button>` : ""}
          ${g.status === "open" || g.status === "expired" ? `<button type="button" class="kw-btn" data-take="${esc(g.code)}">Вернуть себе</button>` : ""}</div></div>`
        )
        .join("")}</div>` : ""}`;
    box.querySelector("[data-code]").value = typed ?? giftParam;
    box.querySelector("[data-redeem]").addEventListener("click", redeem);
    box.querySelectorAll("[data-take]").forEach((b) => b.addEventListener("click", () => takeBack(b.dataset.take)));
    box.querySelectorAll("[data-send]").forEach((b) =>
      b.addEventListener("click", () => {
        const g = mine.find((x) => x.code === b.dataset.send);
        if (g) shareGift(g.link, g.prize, host);
      })
    );
  }

  function renderHistory() {
    const box = root.querySelector("[data-history]");
    if (!box) return;
    const h = st.data.history || [];
    box.style.display = h.length ? "" : "none";
    box.innerHTML = `<h3>Ваши выигрыши</h3><div class="kw-list">${h
      .map(
        (x) => `<div class="kw-row"><div><b>${esc(x.prize.title)}</b><br><span>${esc(x.status_label || "")}${x.result && x.result.code ? ` · код <code>${esc(x.result.code)}</code>` : ""}</span></div>
          <span>${new Date(x.at).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}</span></div>`
      )
      .join("")}</div>`;
  }

  function render() {
    const d = st.data;
    const body = root.querySelector("[data-body]");
    if (st.drum) {
      st.drum.destroy();
      st.drum = null;
    }
    root.setAttribute("style", themeVars(d.theme));
    if (!d.enabled || !d.prizes.length) {
      body.innerHTML = `<div class="kw-card"><h3>${esc(d.title)}</h3><p style="color:var(--muted);margin:0">${esc(d.enabled ? ERRORS.no_prizes : ERRORS.disabled)}</p></div>`;
      return;
    }
    body.innerHTML = `
      <div class="kw-stage">
        <div class="kw-top"><div><h2>${esc(d.title)}</h2><p>${esc(d.subtitle)}</p></div>
          <div class="kw-count" title="Доступно вращений"><b>0</b>🎟️</div></div>
        <div class="kw-drum"><div class="kw-spot"></div></div>
        <div class="kw-feed"></div>
        <button type="button" class="kw-cta"></button>
        <div class="kw-note"></div>
      </div>
      <div class="kw-card" data-gifts></div>
      <div class="kw-card"><h3>Что можно выиграть</h3><div class="kw-grid">${d.prizes
        .map((p) => {
          const tile = tileOf(p, d.theme);
          return `<div class="kw-prize"><div class="kw-mini-pic"${tile ? ` style="background:${esc(tile)}"` : ""}>
            ${p.image ? `<img src="${esc(p.image)}" alt="">` : KIND_ICON[p.kind] || "🎁"}</div><b>${esc(p.title)}</b><span>${esc(p.label)}</span></div>`;
        })
        .join("")}</div></div>
      <div class="kw-card" data-history></div>`;
    st.drum = createDrum(body.querySelector(".kw-drum"), d.prizes, d.theme);
    body.querySelector(".kw-cta").addEventListener("click", onCta);
    updateStatus();
    if (d.pending && !st.modal) {
      st.drum.jumpTo(d.pending.prize);
      showDecision(d.pending);
    }
    if (giftParam && !d.pending && !st.modal) offerGift(giftParam);
  }

  function onCta() {
    if (st.data.pending) return showDecision(st.data.pending);
    spinNow();
  }

  async function animateTo(pending) {
    const winnerNode = await st.drum.spinTo(pending.prize);
    if (st.disposed) return;
    const drumEl = root.querySelector(".kw-drum");
    drumEl.classList.remove("win");
    void drumEl.offsetWidth;
    drumEl.classList.add("win");
    if (winnerNode) winnerNode.classList.add("hit");
    if (!pending.nothing) confetti(root.querySelector(".kw-stage"));
    await new Promise((r) => st.timers.push(setTimeout(r, 700)));
  }

  async function spinNow() {
    if (st.busy || !st.drum) return;
    st.busy = true;
    updateStatus();
    let outcome;
    try {
      const id = (window.crypto && crypto.randomUUID && crypto.randomUUID()) || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
      outcome = await host.request("/spin", { method: "POST", body: JSON.stringify({ request_id: id }) });
    } catch (err) {
      st.busy = false;
      host.notify(errorText(err, "Не удалось запустить колесо, попробуйте ещё раз"));
      await refresh(false);
      return;
    }
    st.data.state = outcome.state;
    st.data.pending = outcome.pending;
    await animateTo(outcome.pending);
    if (st.disposed) return;
    st.busy = false;
    updateStatus();
    showDecision(outcome.pending);
  }

  function prizeTop(p, heading) {
    return `<div class="kw-win-top">${picHtml(p, st.data.theme)}<div class="kw-badge" style="--c:${esc(colorOf(p))}">${esc(p.badge || p.label)}</div>
      <h3>${heading}</h3></div>`;
  }

  function showDecision(pending) {
    if (st.modal) st.modal.close();
    const p = pending.prize;
    const m = openModal(st.data.theme);
    st.modal = m;
    const ttl = st.data.rules.gift_ttl_days;
    m.win.innerHTML = `${prizeTop(p, pending.nothing ? esc(p.title) : `Вы выиграли: ${esc(p.title)}`)}
      <div class="kw-win-body">
        <p>${esc(pending.nothing ? p.description || "В этот раз без приза." : p.label)}</p>
        <button type="button" class="kw-act main" data-a="keep">${pending.nothing ? "Понятно" : "Забрать приз"}</button>
        ${pending.can_gift ? `<button type="button" class="kw-act alt" data-a="gift">🎁 Подарить другу</button>` : ""}
        ${pending.can_reroll ? `<button type="button" class="kw-act alt" data-a="reroll">🔄 ${pending.nothing ? "Крутить ещё раз" : "Отказаться и крутить ещё раз"}</button>
          <p style="font-size:12px">Повторное вращение бесплатное, но только один раз${pending.nothing ? "" : ". Текущий приз сгорит"}.</p>` : ""}
        ${pending.can_gift ? `<p style="font-size:12px">Подарок передаётся по коду, он действует ${esc(ttl)} дн.</p>` : ""}
      </div>`;
    m.win.querySelectorAll("[data-a]").forEach((b) => b.addEventListener("click", () => resolve(pending, b.dataset.a, b)));
  }

  async function resolve(pending, action, button) {
    if (st.busy) return;
    st.busy = true;
    st.modal.win.querySelectorAll("button").forEach((b) => (b.disabled = true));
    button.textContent = "…";
    let out;
    try {
      out = await host.request("/resolve", { method: "POST", body: JSON.stringify({ spin_id: pending.spin_id, action }) });
    } catch (err) {
      st.busy = false;
      host.notify(errorText(err, "Не удалось выполнить действие"));
      if (st.modal) st.modal.close();
      st.modal = null;
      await refresh(false);
      return;
    }
    st.data.state = out.state;
    if (action === "reroll") {
      st.modal.close();
      st.modal = null;
      st.data.pending = out.pending;
      updateStatus();
      await animateTo(out.pending);
      if (st.disposed) return;
      st.busy = false;
      updateStatus();
      showDecision(out.pending);
      return;
    }
    st.busy = false;
    st.data.pending = null;
    if (action === "keep") showKept(out);
    else showGift(out);
    refresh(false);
  }

  function showKept(out) {
    const p = out.prize;
    const r = out.result || {};
    st.modal.win.innerHTML = `${prizeTop(p, p.kind === "nothing" ? esc(p.title) : "Приз ваш! 🎉")}
      <div class="kw-win-body">
        ${r.text ? `<p>${esc(r.text)}</p>` : ""}
        ${r.code ? `<div class="kw-code"><code>${esc(r.code)}</code><button type="button" class="kw-small" data-copy>Копировать</button></div>` : ""}
        <button type="button" class="kw-act main" data-close>Готово</button></div>`;
    bindModal(r.code);
  }

  function showGift(out) {
    const p = out.prize;
    st.modal.win.innerHTML = `${prizeTop(p, "Подарок готов 🎁")}
      <div class="kw-win-body">
        <p>Отправьте другу ссылку: она откроет приложение, и подарок сразу будет готов к получению. Действует до
          ${esc(new Date(out.expires_at).toLocaleDateString("ru-RU"))}; пока его не забрали, подарок можно вернуть себе.</p>
        <div class="kw-code"><code>${esc(out.code)}</code><button type="button" class="kw-small" data-copy>Копировать</button></div>
        <button type="button" class="kw-act main" data-share>Отправить другу</button>
        <button type="button" class="kw-act ghost" data-close>Готово</button></div>`;
    bindModal(out.code);
    st.modal.win.querySelector("[data-share]").addEventListener("click", () => shareGift(out.link || out.code, p.title, host));
  }

  function bindModal(code) {
    const m = st.modal;
    m.win.querySelector("[data-close]").addEventListener("click", () => {
      m.close();
      if (st.modal === m) st.modal = null;
    });
    const copy = m.win.querySelector("[data-copy]");
    if (copy)
      copy.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(code);
          copy.textContent = "Скопировано";
        } catch {
          host.notify(code);
        }
      });
  }

  function offerGift(code) {
    const m = openModal(st.data.theme);
    st.modal = m;
    m.win.innerHTML = `<div class="kw-win-top"><div class="kw-pic"><div class="kw-sticker">🎁</div></div>
        <h3>Вам прислали подарок!</h3></div>
      <div class="kw-win-body"><p>Код подарка: <b>${esc(code)}</b>. Нажмите, чтобы забрать его на свой аккаунт.</p>
        <button type="button" class="kw-act main" data-get>Получить подарок</button>
        <button type="button" class="kw-act ghost" data-close>Позже</button></div>`;
    m.win.querySelector("[data-close]").addEventListener("click", () => {
      m.close();
      if (st.modal === m) st.modal = null;
    });
    m.win.querySelector("[data-get]").addEventListener("click", () => {
      root.querySelector("[data-code]").value = code;
      m.close();
      st.modal = null;
      redeem();
    });
  }

  async function redeem() {
    const input = root.querySelector("[data-code]");
    const code = input.value.trim();
    if (!code || st.busy) return;
    st.busy = true;
    try {
      const out = await host.request("/gift/redeem", { method: "POST", body: JSON.stringify({ code }) });
      input.value = "";
      forgetGiftCode();
      giftParam = "";
      if (st.modal) st.modal.close();
      st.modal = openModal(st.data.theme);
      showKept(out);
      st.busy = false;
      await refresh(false);
    } catch (err) {
      st.busy = false;
      if (["gift_used", "gift_expired", "gift_own", "gift_not_found"].includes(err?.error)) {
        forgetGiftCode();
        giftParam = "";
      }
      host.notify(errorText(err, "Не удалось получить подарок"));
    }
  }

  async function takeBack(code) {
    if (st.busy) return;
    st.busy = true;
    try {
      const out = await host.request("/gift/cancel", { method: "POST", body: JSON.stringify({ code }) });
      st.busy = false;
      st.data.pending = out.pending;
      showDecision(out.pending);
      await refresh(false);
    } catch (err) {
      st.busy = false;
      host.notify(errorText(err, "Не удалось вернуть подарок"));
    }
  }

  refresh(true);
  st.timers.push(setInterval(() => !st.busy && !document.hidden && st.data && st.drum && updateStatus(), 30000));
  return st;
}

// ------------------------------------------------------------------ home card

function mountHomeCard(target, props) {
  const host = props.host;
  const root = document.createElement("div");
  root.className = "plugin-host";
  root.innerHTML = `<style>${STYLE}</style>`;
  target.replaceChildren(root);
  const st = { root, disposed: false, timers: [] };
  const incoming = giftFromStart();
  let handled = "";
  try {
    handled = sessionStorage.getItem(`${GIFT_KEY}:done`) || "";
  } catch {}
  if (incoming && handled !== incoming) {
    // A gift link opens the wheel page, which then offers the gift.
    try {
      sessionStorage.setItem(GIFT_KEY, incoming);
      sessionStorage.setItem(`${GIFT_KEY}:done`, incoming);
    } catch {}
    host.navigate("wheel");
    return st;
  }
  host
    .request("/state")
    .then((d) => {
      installNavIcon(d.title);
      if (st.disposed || !d.enabled || !d.prizes.length) return;
      const s = d.state;
      const text = d.pending
        ? "У вас есть неполученный приз"
        : s.can_spin
          ? `Доступно вращений: ${s.available}`
          : s.reason === "no_spins"
            ? `Следующее через ${timeLeft(s.next_free_at)}`
            : ERRORS[s.reason] || "";
      const card = document.createElement("div");
      card.className = "kw-mini";
      card.setAttribute("style", themeVars(d.theme));
      card.innerHTML = `<div class="kw-ico">🎡</div><div><b>${esc(d.title)}</b><span>${esc(text)}</span></div>
        <button type="button">${d.pending ? "Забрать" : s.can_spin ? "Крутить" : "Открыть"}</button>`;
      card.querySelector("button").addEventListener("click", () => host.navigate("wheel"));
      root.appendChild(card);
    })
    .catch(() => {});
  return st;
}

// ------------------------------------------------------------------ settings entry: Support

function mountSettingsSupport(target, props) {
  const host = props.host;
  const root = document.createElement("div");
  root.className = "plugin-host";
  root.innerHTML = `<style>
    .kw-link{display:flex;align-items:center;gap:12px;width:100%;box-sizing:border-box;padding:14px 16px;border-radius:var(--radius,14px);
      border:1px solid var(--border,#2b3140);background:var(--panel,transparent);color:var(--text,#eef1f6);font:inherit;text-align:left;cursor:pointer}
    .kw-link svg{flex:none;opacity:.9}
    .kw-link div{flex:1;min-width:0}
    .kw-link b{display:block;font-size:15px}
    .kw-link span{display:block;margin-top:2px;font-size:12.5px;color:var(--muted,#8b93a3)}
  </style>
  <button type="button" class="kw-link">
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3.5"/><path d="M5.6 5.6l3.9 3.9M14.5 14.5l3.9 3.9M18.4 5.6l-3.9 3.9M9.5 14.5l-3.9 3.9"/></svg>
    <div><b>Поддержка</b><span>Обращения и ответы от команды</span></div>
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 6l6 6-6 6"/></svg>
  </button>`;
  target.replaceChildren(root);
  root.querySelector("button").addEventListener("click", () => host.navigateSection("support"));
  installNavIcon();
  return { root, disposed: false, timers: [] };
}

export function mountView(view, element, props) {
  installNavIcon();
  if (view === "home-card") return mountHomeCard(element, props);
  if (view === "settings-support") return mountSettingsSupport(element, props);
  return mountWheel(element, props);
}

export function updateView() {
  // Host prop changes (language, subscription snapshot) must not restart a running spin.
}

export function unmountView(instance) {
  if (!instance) return;
  instance.disposed = true;
  (instance.timers || []).forEach((t) => {
    clearTimeout(t);
    clearInterval(t);
  });
  if (instance.drum) instance.drum.destroy();
  if (instance.modal) instance.modal.close();
  instance.root.remove();
}
