// KIRO wheel of fortune: admin settings view (frontend host API v1).
const API = "/api/admin/kiro-wheel";

const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const KINDS = {
  nothing: "Без выигрыша",
  days: "Дни подписки",
  traffic: "Трафик (ГБ)",
  premium: "Premium-трафик (код)",
  balance: "Деньги на баланс (₽)",
  discount: "Скидка % (промокод)",
  gift_code: "Подарочный код (дни)",
  manual: "Ручной приз (выдаёт админ)",
};

const ERRORS = {
  invalid_title: "Укажите название (до 80 символов)",
  invalid_days: "Дни: от 1 до 365",
  invalid_gb: "ГБ: от 0.1 до 10000",
  invalid_rub: "Сумма: от 1 до 100000 ₽",
  invalid_percent: "Скидка: от 1 до 99%",
  invalid_valid_days: "Срок действия кода: 1–365 дней",
  invalid_weight: "Вес: целое число от 0",
  invalid_stock: "Остаток: пусто или число от 0",
  image_too_large: "Картинка больше 1 МБ",
  unsupported_image: "Нужен PNG, JPEG, WEBP или GIF",
  user_not_found: "Пользователь не найден",
  forbidden: "Нет прав администратора",
  csrf_failed: "Сессия устарела, обновите страницу",
};

function csrf() {
  const m = document.cookie.match(/(?:^|;\s*)rw_webapp_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

async function api(path, method = "GET", body) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["X-CSRF-Token"] = csrf();
  const res = await fetch(API + path, {
    method,
    credentials: "same-origin",
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) throw new Error(ERRORS[data.error] || data.error || `HTTP ${res.status}`);
  return data;
}

const STYLE = `
.kwa{display:flex;flex-direction:column;gap:14px;color:var(--text);font-size:14px}
.kwa-card{border:1px solid var(--border);border-radius:var(--radius-card,var(--radius,12px));padding:14px;
  background:var(--panel,var(--bg,transparent))}
.kwa-card h3{margin:0 0 12px;font-size:15px;display:flex;justify-content:space-between;align-items:center;gap:8px}
.kwa-form{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px 14px}
.kwa label{display:flex;flex-direction:column;gap:4px;color:var(--muted);font-size:12px}
.kwa label.kwa-check{flex-direction:row;align-items:center;gap:8px;color:var(--text);font-size:14px}
.kwa input,.kwa select,.kwa textarea{font:inherit;color:var(--text);background-color:var(--panel-2,var(--panel,#1f2430));
  border:1px solid var(--border);border-radius:var(--radius-control,8px);padding:7px 9px;color-scheme:inherit}
.kwa select option,.kwa select optgroup{background-color:var(--panel-2,var(--panel,#1f2430));color:var(--text)}
.kwa input[type=checkbox]{accent-color:var(--accent)}
.kwa input[type=color]{padding:2px;height:36px;width:64px}
.kwa textarea{min-height:54px;resize:vertical}
.kwa-btn{border:1px solid var(--border);background:transparent;color:var(--text);border-radius:8px;padding:7px 12px;cursor:pointer;font:inherit}
.kwa-primary{background:var(--accent);border-color:var(--accent);color:var(--accent-contrast,#fff)}
.kwa-danger{color:#e5484d}
.kwa-lvl{display:inline-block;min-width:44px;text-align:center;border-radius:6px;padding:1px 6px;font-size:11px;font-weight:600;
  background:rgba(127,127,127,.18);color:var(--muted)}
.kwa-lvl.warn{background:rgba(245,166,35,.2);color:#f5a623}
.kwa-lvl.error{background:rgba(229,72,77,.2);color:#e5484d}
.kwa-log td{font-size:12px;vertical-align:top}
.kwa-log code{font-size:11px;word-break:break-all;white-space:pre-wrap}
.kwa-table{width:100%;border-collapse:collapse}
.kwa-table td,.kwa-table th{padding:6px;border-bottom:1px solid var(--border);text-align:left;vertical-align:middle}
.kwa-table th{color:var(--muted);font-weight:500;font-size:12px}
.kwa-thumb{width:40px;height:40px;border-radius:10px;background:rgba(127,127,127,.12) center/cover no-repeat;display:flex;
  align-items:center;justify-content:center;font-size:20px}
.kwa-muted{color:var(--muted)}.kwa-off{opacity:.5}
.kwa-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.kwa-stats{display:flex;gap:18px;flex-wrap:wrap}.kwa-stats b{display:block;font-size:20px}
.kwa-banner-prev{width:100%;max-width:420px;aspect-ratio:3/1;border-radius:12px;border:1px dashed rgba(127,127,127,.4);background:#140a05 center/contain no-repeat;
  display:flex;align-items:center;justify-content:center;color:#b9a48a;font-size:12px}
.kwa-img{display:flex;align-items:center;gap:12px}.kwa-img .kwa-thumb{width:72px;height:72px}
.kwa-msg{padding:8px 10px;border-radius:8px;background:rgba(127,127,127,.1)}
.kwa-scroll{overflow-x:auto}
`;

const ICON = { nothing: "🍀", days: "📅", traffic: "📶", premium: "⚡", balance: "💰", discount: "🏷️", gift_code: "🎁", manual: "🏆" };

// Module-level state survives the host re-creating this view: unsaved form input is restored.
const cache = { data: null, at: 0, config: null, editor: null };

function formValues(form) {
  const values = {};
  for (const el of form.elements) {
    if (!el.name || el.type === "file") continue;
    values[el.name] = el.type === "checkbox" ? el.checked : el.value;
  }
  return values;
}

function applyValues(form, values) {
  if (!values) return;
  for (const el of form.elements) {
    if (!el.name || el.type === "file" || !(el.name in values)) continue;
    if (el.type === "checkbox") el.checked = Boolean(values[el.name]);
    else el.value = values[el.name];
  }
}

function thumb(p) {
  return p.image
    ? `<div class="kwa-thumb" style="background-image:url('${esc(p.image)}')"></div>`
    : `<div class="kwa-thumb">${ICON[p.kind] || "🎁"}</div>`;
}

function paramFields(kind, params = {}) {
  const f = (name, label, value, attrs = "") =>
    `<label>${label}<input name="p_${name}" value="${esc(value ?? "")}" ${attrs}></label>`;
  const validity = f("valid_days", "Код действует, дней", params.valid_days ?? 30, 'type="number" min="1" max="365"');
  const badge = f("badge", "Текст ярлыка на барабане (необязательно)", params.badge ?? "", 'maxlength="16" placeholder="например −20% или +3 дня"');
  const tile = `<label class="kwa-check"><input type="checkbox" name="p_tile_on" ${params.tile_bg ? "checked" : ""}> Своя подложка под картинку</label>
    <label>Цвет подложки (для тёмных PNG)<input type="color" name="p_tile_bg" value="${esc(params.tile_bg || "#ffffff")}"></label>`;
  return kindFields(kind, params, f, validity) + badge + tile;
}

function kindFields(kind, params, f, validity) {
  switch (kind) {
    case "days":
      return f("days", "Дней подписки", params.days ?? 1, 'type="number" min="1" max="365"');
    case "traffic":
      return f("gb", "ГБ трафика", params.gb ?? 5, 'type="number" min="0.1" step="0.1"');
    case "premium":
      return f("gb", "ГБ Premium-трафика", params.gb ?? 5, 'type="number" min="0.1" step="0.1"') + validity;
    case "balance":
      return f("rub", "Сумма, ₽", params.rub ?? 50, 'type="number" min="1" step="1"');
    case "discount":
      return (
        f("percent", "Скидка, %", params.percent ?? 10, 'type="number" min="1" max="99"') +
        `<label>На что скидка<select name="p_applies_to">
          <option value="subscription" ${params.applies_to !== "all" ? "selected" : ""}>Подписка</option>
          <option value="all" ${params.applies_to === "all" ? "selected" : ""}>Любая покупка</option></select></label>` +
        validity
      );
    case "gift_code":
      return f("days", "Дней в подарочном коде", params.days ?? 7, 'type="number" min="1" max="365"') + validity;
    case "manual":
      return `<label style="grid-column:1/-1">Текст для победителя<textarea name="p_note">${esc(params.note || "")}</textarea></label>`;
    default:
      return "";
  }
}

const BANNER_URL = "/api/plugins/kiro-wheel/img/";

// Banners keep their proportions (no crop); wider than 1080 px is scaled down, alpha is kept.
async function resizeBanner(file) {
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, 1080 / bitmap.width);
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(bitmap.width * scale));
  canvas.height = Math.max(1, Math.round(bitmap.height * scale));
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  let blob = null;
  for (const quality of [0.9, 0.8, 0.65, 0.5]) {
    blob = await new Promise((r) => canvas.toBlob(r, "image/webp", quality));
    if (blob && blob.type === "image/webp" && blob.size <= 900 * 1024) break;
  }
  if (!blob || blob.type !== "image/webp") blob = await new Promise((r) => canvas.toBlob(r, "image/png"));
  if (blob.size > 1024 * 1024) throw new Error("Картинка слишком большая, уменьшите её");
  const buf = new Uint8Array(await blob.arrayBuffer());
  let binary = "";
  for (let i = 0; i < buf.length; i += 0x8000) binary += String.fromCharCode(...buf.subarray(i, i + 0x8000));
  return btoa(binary);
}

async function resizeImage(file) {
  const bitmap = await createImageBitmap(file);
  const size = 256;
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d");
  const scale = Math.max(size / bitmap.width, size / bitmap.height);
  const w = bitmap.width * scale;
  const h = bitmap.height * scale;
  ctx.drawImage(bitmap, (size - w) / 2, (size - h) / 2, w, h);
  let blob = await new Promise((r) => canvas.toBlob(r, "image/webp", 0.86));
  if (!blob || blob.type !== "image/webp") blob = await new Promise((r) => canvas.toBlob(r, "image/png"));
  const buf = new Uint8Array(await blob.arrayBuffer());
  let binary = "";
  for (let i = 0; i < buf.length; i += 0x8000) binary += String.fromCharCode(...buf.subarray(i, i + 0x8000));
  return btoa(binary);
}

function mountSettings(target) {
  const root = document.createElement("div");
  root.className = "kwa";
  root.innerHTML = `<style>${STYLE}</style><div data-body class="kwa-muted">Загрузка…</div>`;
  target.replaceChildren(root);
  const st = { root, data: null, editing: null, disposed: false };
  const body = () => root.querySelector("[data-body]");

  async function load() {
    try {
      st.data = await api("/overview");
      cache.data = st.data;
      cache.at = Date.now();
      if (!st.disposed) render();
    } catch (err) {
      body().innerHTML = `<div class="kwa-msg">Ошибка: ${esc(err.message)}</div>`;
    }
  }

  function render() {
    const d = st.data;
    const c = d.config;
    const s = d.stats || {};
    const totalChance = d.prizes.filter((p) => p.enabled).reduce((a, p) => a + (p.kind === "nothing" ? 0 : p.chance), 0);
    body().innerHTML = `
      <div class="kwa-card"><h3>Колесо удачи <span class="kwa-muted">${c.enabled ? "🟢 включено" : "⚪ выключено"}</span></h3>
        <div class="kwa-stats">
          <div><b>${s.day ?? 0}</b><span class="kwa-muted">вращений за сутки</span></div>
          <div><b>${s.month ?? 0}</b><span class="kwa-muted">за 30 дней</span></div>
          <div><b>${s.players ?? 0}</b><span class="kwa-muted">игроков всего</span></div>
          <div><b>${Math.round(totalChance)}%</b><span class="kwa-muted">шанс выигрыша</span></div>
        </div></div>

      <div class="kwa-card"><h3>Настройки</h3><form data-config class="kwa-form">
        <label class="kwa-check"><input type="checkbox" name="enabled" ${c.enabled ? "checked" : ""}> Колесо включено</label>
        <label class="kwa-check"><input type="checkbox" name="require_active_subscription" ${c.require_active_subscription ? "checked" : ""}> Только с активной подпиской</label>
        <label class="kwa-check"><input type="checkbox" name="notify_admins_on_manual" ${c.notify_admins_on_manual ? "checked" : ""}> Уведомлять меня о ручных призах</label>
        <label>Заголовок<input name="title" value="${esc(c.title)}" maxlength="80"></label>
        <label>Подзаголовок<input name="subtitle" value="${esc(c.subtitle)}" maxlength="200"></label>
        <label>Бесплатных вращений в день<input name="daily_free_spins" type="number" min="0" max="20" value="${esc(c.daily_free_spins)}"><span class="kwa-muted">Не выдаются, пока включены ежедневные награды</span></label>
        <label>Вращений за каждую оплату<input name="spins_per_payment" type="number" min="0" max="50" value="${esc(c.spins_per_payment)}"></label>
        <label>Максимум накопленных бонусных<input name="max_bonus_spins" type="number" min="0" max="1000" value="${esc(c.max_bonus_spins)}"></label>
        <label>Смещение дня от UTC, ч (МСК = 3)<input name="day_offset_hours" type="number" min="-12" max="14" value="${esc(c.day_offset_hours)}"></label>
        <label class="kwa-check"><input type="checkbox" name="show_feed" ${c.show_feed ? "checked" : ""}> Показывать ленту победителей (ники скрыты: s***od)</label>
        <label class="kwa-check"><input type="checkbox" name="allow_gift" ${c.allow_gift ? "checked" : ""}> Можно подарить приз другу</label>
        <label class="kwa-check"><input type="checkbox" name="allow_reroll" ${c.allow_reroll ? "checked" : ""}> Можно отказаться и крутить ещё раз (1 раз)</label>
        <label>Подарок действует, дней<input name="gift_ttl_days" type="number" min="1" max="90" value="${esc(c.gift_ttl_days)}"></label>
        <label class="kwa-check"><input type="checkbox" name="debug_log" ${c.debug_log ? "checked" : ""}> Подробный журнал (для диагностики, записывает каждый запрос состояния)</label>
        <div style="grid-column:1/-1;margin-top:6px;font-weight:600">Ежедневные награды за вход</div>
        <label class="kwa-check" style="grid-column:1/-1"><input type="checkbox" name="daily_enabled" ${c.daily_enabled ? "checked" : ""}> Включить календарь ежедневных наград (заменяет бесплатное вращение раз в сутки)</label>
        <div style="grid-column:1/-1" class="kwa-muted">Игрок получает билетики (бонусные вращения), если заходит каждый день: серия из 7 дней, пропуск дня сбрасывает её на день 1, после 7-го дня круг начинается заново. Сутки считаются по смещению пояса выше. Вращения за оплату работают как раньше.</div>
        <div style="grid-column:1/-1;display:grid;grid-template-columns:repeat(auto-fit,minmax(90px,1fr));gap:8px">${(c.daily_rewards || [1, 1, 2, 2, 3, 3, 5])
          .map((v, i) => `<label>День ${i + 1}<input name="daily_reward_${i}" type="number" min="0" max="100" value="${esc(v)}"></label>`)
          .join("")}</div>
        <div style="grid-column:1/-1;margin-top:6px;font-weight:600">Оформление</div>
        <label>Фон колеса: верх<input name="bg_from" type="color" value="${esc(c.bg_from)}"></label>
        <label>Фон колеса: низ<input name="bg_to" type="color" value="${esc(c.bg_to)}"></label>
        <label>Текст на колесе<input name="text_color" type="color" value="${esc(c.text_color)}"></label>
        <label>Кнопка «Крутить»: фон<input name="btn_bg" type="color" value="${esc(c.btn_bg)}"></label>
        <label>Кнопка «Крутить»: текст<input name="btn_text" type="color" value="${esc(c.btn_text)}"></label>
        <label>Окно выигрыша: верх<input name="win_from" type="color" value="${esc(c.win_from)}"></label>
        <label>Окно выигрыша: низ<input name="win_to" type="color" value="${esc(c.win_to)}"></label>
        <label>Окно выигрыша: текст<input name="win_text" type="color" value="${esc(c.win_text)}"></label>
        <label class="kwa-check"><input type="checkbox" name="tile_enabled" ${c.tile_enabled ? "checked" : ""}> Подложка под картинки призов</label>
        <label>Цвет подложки<input name="tile_bg" type="color" value="${esc(c.tile_bg)}"></label>
        <div style="grid-column:1/-1;margin-top:6px;font-weight:600">Баннер на главной странице</div>
        <div class="kwa-banner-box" style="grid-column:1/-1">
          <input type="hidden" name="banner_image_id" value="${esc(c.banner_image_id || "")}">
          <div class="kwa-banner-prev" data-banner-prev style="${c.banner_image_id ? `background-image:url('${BANNER_URL}${esc(c.banner_image_id)}')` : ""}">${c.banner_image_id ? "" : "Стандартный логотип"}</div>
          <div class="kwa-actions" style="margin:8px 0 0">
            <label class="kwa-btn kwa-primary" style="cursor:pointer">Загрузить свой баннер<input type="file" accept="image/png,image/jpeg,image/webp,image/gif" data-banner-file hidden></label>
            <button type="button" class="kwa-btn" data-banner-clear ${c.banner_image_id ? "" : "hidden"}>Вернуть стандартный</button>
            <span class="kwa-muted" data-banner-status></span>
          </div>
          <label class="kwa-check" style="margin-top:8px"><input type="checkbox" name="banner_fill" ${c.banner_fill ? "checked" : ""}> Растянуть на всю карточку (края обрезаются до 3:1)</label>
          <p class="kwa-muted" style="margin:6px 0 0;line-height:1.45">Рекомендуемый размер: <b>1080×360 px</b> (пропорции 3:1), PNG или WebP с прозрачным фоном, до 1 МБ. Другие пропорции вписываются автоматически: картинка масштабируется без искажений и не выше 190 px на экране. В режиме «на всю карточку» важное держите ближе к центру: верх и низ могут обрезаться. Изменения применяются после кнопки «Сохранить настройки».</p>
        </div>
      </form><div class="kwa-actions"><button type="button" class="kwa-btn kwa-primary" data-save-config>Сохранить настройки</button></div></div>

      <div class="kwa-card"><h3>Призы <span style="display:inline-flex;gap:8px;flex-wrap:wrap"><button type="button" class="kwa-btn" data-starter title="Добавить готовый набор: 15 призов с картинками. Уже существующие по названию пропускаются.">Стартовый набор (15)</button><button type="button" class="kwa-btn kwa-primary" data-add>+ Добавить приз</button></span></h3>
        <div data-editor></div>
        ${d.prizes.length ? `<div class="kwa-scroll"><table class="kwa-table">
          <tr><th></th><th>Приз</th><th>Вес</th><th>Шанс</th><th>Остаток</th><th>Выпал за 30д</th><th></th></tr>
          ${d.prizes
            .map(
              (p) => `<tr class="${p.enabled ? "" : "kwa-off"}"><td>${thumb(p)}</td>
                <td><b>${esc(p.title)}</b><br><span class="kwa-muted">${esc(p.label)}</span></td>
                <td>${esc(p.weight)}</td><td>${p.enabled ? `${esc(p.chance)}%` : "выкл"}</td>
                <td>${p.stock == null ? "∞" : esc(p.stock)}</td><td>${esc(p.won_30d)}</td>
                <td style="white-space:nowrap"><button type="button" class="kwa-btn" data-edit="${p.id}">Изменить</button>
                <button type="button" class="kwa-btn kwa-danger" data-del="${p.id}">✕</button></td></tr>`
            )
            .join("")}</table></div>` : `<p class="kwa-muted">Призов пока нет. Добавьте несколько и включите колесо.</p>`}
        <p class="kwa-muted" style="margin:10px 0 0">Шанс = вес приза / сумма весов включённых призов. Призы «дни» и «трафик»
          выпадают только пользователям с активной подпиской.</p></div>

      <div class="kwa-card"><h3>Выдать бонусные вращения</h3><div class="kwa-form">
        <label>Пользователь (ID, Telegram ID, @username или ms_…)<input data-grant-user></label>
        <label>Сколько вращений<input data-grant-count type="number" min="1" max="100" value="1"></label>
      </div><div class="kwa-actions"><button type="button" class="kwa-btn kwa-primary" data-grant>Выдать</button></div></div>

      <div class="kwa-card"><h3>Последние вращения</h3>${d.recent.length ? `<div class="kwa-scroll"><table class="kwa-table">
        <tr><th>Когда</th><th>Пользователь</th><th>Приз</th><th>Статус</th></tr>
        ${d.recent
          .map(
            (r) => `<tr><td>${new Date(r.at).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}</td>
              <td>${esc(r.user || "")} <span class="kwa-muted">${esc(r.user_id)}</span></td>
              <td>${esc(r.prize)}${r.code ? `<br><code>${esc(r.code)}</code>` : ""}</td>
              <td class="kwa-muted">${esc(r.status || "")}<br>${r.source === "daily" ? "ежедневное" : r.source === "reroll" ? "перекрутка" : "бонусное"}</td></tr>`
          )
          .join("")}</table></div>` : `<p class="kwa-muted">Вращений ещё не было</p>`}</div>

      <div class="kwa-card"><h3>Журнал плагина <span class="kwa-muted" data-log-counts></span></h3>
        <p class="kwa-muted" style="margin:0 0 10px">Ошибки, отказы и ключевые события колеса. Если у игроков что-то не работает,
          скачайте журнал и отправьте разработчику: ID пользователей и коды подарков в файле скрыты.</p>
        <div class="kwa-form" style="align-items:end">
          <label>Показывать<select data-log-level>
            <option value="">Все события</option><option value="warn">Предупреждения и ошибки</option><option value="error">Только ошибки</option></select></label>
          <label class="kwa-check"><input type="checkbox" data-log-mask checked> Скрыть ID пользователей и коды подарков</label>
        </div>
        <div class="kwa-actions" style="margin:10px 0">
          <button type="button" class="kwa-btn kwa-primary" data-log-download>Скачать журнал</button>
          <button type="button" class="kwa-btn" data-log-copy>Скопировать</button>
          <button type="button" class="kwa-btn" data-log-refresh>Обновить</button>
          <button type="button" class="kwa-btn kwa-danger" data-log-clear>Очистить</button>
        </div>
        <div data-log-msg class="kwa-muted"></div>
        <div data-log-body class="kwa-scroll"><p class="kwa-muted">Загрузка…</p></div>
        <textarea data-log-text readonly hidden style="width:100%;min-height:160px;margin-top:8px"></textarea></div>`;

    root.querySelector("[data-save-config]").addEventListener("click", saveConfig);
    root.querySelector("[data-add]").addEventListener("click", () => openEditor(null));
    root.querySelector("[data-starter]").addEventListener("click", addStarter);
    root.querySelector("[data-grant]").addEventListener("click", grantSpins);
    bindLog();
    root.querySelectorAll("[data-edit]").forEach((b) =>
      b.addEventListener("click", () => openEditor(d.prizes.find((p) => String(p.id) === b.dataset.edit)))
    );
    root.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", () => removePrize(b.dataset.del)));
    const configForm = root.querySelector("[data-config]");
    applyValues(configForm, cache.config);
    bindBanner();
    const keepConfig = () => (cache.config = formValues(configForm));
    configForm.addEventListener("input", keepConfig);
    configForm.addEventListener("change", keepConfig);
    if (cache.editor) {
      const draftPrize = cache.editor.prizeId == null ? null : d.prizes.find((p) => p.id === cache.editor.prizeId) || null;
      openEditor(draftPrize, true);
    }
  }

  async function saveConfig(e) {
    const form = root.querySelector("[data-config]");
    const fd = new FormData(form);
    const body = {
      enabled: form.enabled.checked,
      require_active_subscription: form.require_active_subscription.checked,
      notify_admins_on_manual: form.notify_admins_on_manual.checked,
      show_feed: form.show_feed.checked,
      allow_gift: form.allow_gift.checked,
      allow_reroll: form.allow_reroll.checked,
      tile_enabled: form.tile_enabled.checked,
      debug_log: form.debug_log.checked,
    };
    for (const key of [
      "title", "subtitle", "daily_free_spins", "spins_per_payment", "max_bonus_spins", "day_offset_hours", "gift_ttl_days",
      "bg_from", "bg_to", "text_color", "btn_bg", "btn_text", "win_from", "win_to", "win_text", "tile_bg", "banner_image_id",
    ])
      body[key] = fd.get(key);
    body.banner_fill = form.banner_fill.checked;
    body.daily_enabled = form.daily_enabled.checked;
    body.daily_rewards = [0, 1, 2, 3, 4, 5, 6].map((i) => Number(fd.get(`daily_reward_${i}`)));
    e.target.disabled = true;
    try {
      await api("/config", "PUT", body);
      cache.config = null;
      await load();
    } catch (err) {
      alert(err.message);
      e.target.disabled = false;
    }
  }

  function openEditor(prize, restore = false) {
    if (!restore) cache.editor = null;
    const p = prize || { kind: "days", title: "", description: "", params: {}, weight: 10, stock: null, color: "", image_id: null, image: null, enabled: true, position: 0 };
    st.editing = { ...p };
    const box = root.querySelector("[data-editor]");
    box.innerHTML = `<div class="kwa-card" style="margin-bottom:12px"><h3>${prize ? "Изменить приз" : "Новый приз"}</h3>
      <form class="kwa-form" data-prize>
        <label>Название<input name="title" maxlength="80" value="${esc(p.title)}" required></label>
        <label>Тип приза<select name="kind">${Object.entries(KINDS)
          .map(([k, v]) => `<option value="${k}" ${k === p.kind ? "selected" : ""}>${v}</option>`)
          .join("")}</select></label>
        <div data-params style="display:contents">${paramFields(p.kind, p.params)}</div>
        <label>Вес (чем больше, тем чаще)<input name="weight" type="number" min="0" value="${esc(p.weight)}"></label>
        <label>Остаток (пусто = без лимита)<input name="stock" type="number" min="0" value="${p.stock == null ? "" : esc(p.stock)}"></label>
        <label>Цвет ярлыка на барабане<input name="color" type="color" value="${esc(p.color || "#7c5cff")}"></label>
        <label>Порядок показа<input name="position" type="number" value="${esc(p.position || 0)}"></label>
        <label style="grid-column:1/-1">Описание<textarea name="description" maxlength="300">${esc(p.description)}</textarea></label>
        <div class="kwa-img" style="grid-column:1/-1"><div class="kwa-thumb" data-preview style="${p.image ? `background-image:url('${esc(p.image)}')` : ""}">${p.image ? "" : ICON[p.kind] || "🎁"}</div>
          <label>Картинка приза (PNG/JPEG/WEBP, будет уменьшена до 256×256)<input type="file" accept="image/*" data-file></label>
          ${p.image_id ? `<button type="button" class="kwa-btn" data-clear-img>Убрать картинку</button>` : ""}</div>
        <label class="kwa-check"><input type="checkbox" name="enabled" ${p.enabled ? "checked" : ""}> Приз включён</label>
      </form>
      <div class="kwa-actions"><button type="button" class="kwa-btn kwa-primary" data-save-prize>Сохранить приз</button>
        <button type="button" class="kwa-btn" data-cancel>Отмена</button><span class="kwa-muted" data-status></span></div></div>`;
    const form = box.querySelector("[data-prize]");
    const preview = box.querySelector("[data-preview]");
    const keepDraft = () =>
      (cache.editor = {
        prizeId: prize ? prize.id : null,
        fields: formValues(form),
        image_id: st.editing.image_id,
        image: preview.style.backgroundImage,
      });
    if (restore && cache.editor) {
      const draft = cache.editor;
      if (draft.fields.kind) {
        form.kind.value = draft.fields.kind;
        box.querySelector("[data-params]").innerHTML = paramFields(form.kind.value, {});
      }
      applyValues(form, draft.fields);
      st.editing.image_id = draft.image_id;
      if (draft.image) {
        preview.textContent = "";
        preview.style.backgroundImage = draft.image;
      }
    }
    form.addEventListener("input", keepDraft);
    form.addEventListener("change", keepDraft);
    form.kind.addEventListener("change", () => {
      box.querySelector("[data-params]").innerHTML = paramFields(form.kind.value, {});
      keepDraft();
    });
    box.querySelector("[data-cancel]").addEventListener("click", () => {
      cache.editor = null;
      box.innerHTML = "";
    });
    const clear = box.querySelector("[data-clear-img]");
    if (clear)
      clear.addEventListener("click", () => {
        st.editing.image_id = null;
        const pv = box.querySelector("[data-preview]");
        pv.style.backgroundImage = "";
        pv.textContent = ICON[form.kind.value] || "🎁";
        keepDraft();
      });
    box.querySelector("[data-file]").addEventListener("change", async (ev) => {
      const file = ev.target.files && ev.target.files[0];
      if (!file) return;
      const status = box.querySelector("[data-status]");
      status.textContent = "Загрузка картинки…";
      try {
        const data = await resizeImage(file);
        const res = await api("/images", "POST", { data });
        st.editing.image_id = res.image_id;
        const pv = box.querySelector("[data-preview]");
        pv.textContent = "";
        pv.style.backgroundImage = `url('${res.url}')`;
        status.textContent = "Картинка загружена";
        keepDraft();
      } catch (err) {
        status.textContent = `Ошибка: ${err.message}`;
      }
    });
    box.querySelector("[data-save-prize]").addEventListener("click", async (e) => {
      const fd = new FormData(form);
      const params = {};
      for (const [k, v] of fd.entries()) if (k.startsWith("p_")) params[k.slice(2)] = v;
      if (!params.tile_on) delete params.tile_bg;
      delete params.tile_on;
      const payload = {
        title: fd.get("title"),
        kind: fd.get("kind"),
        description: fd.get("description"),
        params,
        weight: fd.get("weight"),
        stock: fd.get("stock") === "" ? null : fd.get("stock"),
        color: fd.get("color"),
        position: fd.get("position"),
        enabled: form.enabled.checked,
        image_id: st.editing.image_id || null,
      };
      e.target.disabled = true;
      try {
        if (prize) await api(`/prizes/${prize.id}`, "PUT", payload);
        else await api("/prizes", "POST", payload);
        cache.editor = null;
        await load();
      } catch (err) {
        box.querySelector("[data-status]").textContent = `Ошибка: ${err.message}`;
        e.target.disabled = false;
      }
    });
    if (!restore) box.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function bindBanner() {
    const form = root.querySelector("[data-config]");
    const prev = root.querySelector("[data-banner-prev]");
    const status = root.querySelector("[data-banner-status]");
    const clear = root.querySelector("[data-banner-clear]");
    const show = (id) => {
      form.banner_image_id.value = id || "";
      prev.style.backgroundImage = id ? `url('${BANNER_URL}${id}')` : "";
      prev.textContent = id ? "" : "Стандартный логотип";
      clear.hidden = !id;
      form.dispatchEvent(new Event("change"));
    };
    // A restored draft may carry a banner chosen before the page was re-rendered.
    if (form.banner_image_id.value) {
      prev.style.backgroundImage = `url('${BANNER_URL}${form.banner_image_id.value}')`;
      prev.textContent = "";
      clear.hidden = false;
    } else {
      prev.style.backgroundImage = "";
      prev.textContent = "Стандартный логотип";
      clear.hidden = true;
    }
    root.querySelector("[data-banner-file]").addEventListener("change", async (ev) => {
      const file = ev.target.files && ev.target.files[0];
      if (!file) return;
      status.textContent = "Загрузка…";
      try {
        const res = await api("/images", "POST", { data: await resizeBanner(file) });
        show(res.image_id);
        status.textContent = "Загружено, нажмите «Сохранить настройки»";
      } catch (err) {
        status.textContent = `Ошибка: ${err.message}`;
      }
      ev.target.value = "";
    });
    clear.addEventListener("click", () => {
      show("");
      status.textContent = "Будет возвращён стандартный логотип после сохранения";
    });
  }

  async function addStarter() {
    if (!confirm("Добавить стартовый набор из 15 призов с картинками? Призы с уже существующими названиями будут пропущены, остальные призы не изменятся.")) return;
    try {
      const res = await api("/presets/starter", "POST", {});
      alert(`Добавлено призов: ${res.added}. Пропущено (уже есть): ${res.skipped}.`);
      await load();
    } catch (err) {
      alert(err.message);
    }
  }

  async function removePrize(id) {
    if (!confirm("Удалить приз? История выигрышей сохранится.")) return;
    try {
      await api(`/prizes/${id}`, "DELETE");
      await load();
    } catch (err) {
      alert(err.message);
    }
  }

  const LEVEL_LABEL = { debug: "debug", info: "info", warn: "warn", error: "error" };

  function shortDetail(detail) {
    const keys = Object.keys(detail || {});
    if (!keys.length) return "";
    const raw = JSON.stringify(detail);
    return raw.length > 140 ? raw.slice(0, 140) + "…" : raw;
  }

  async function loadLog() {
    const box = root.querySelector("[data-log-body]");
    if (!box) return;
    const level = root.querySelector("[data-log-level]").value;
    try {
      const res = await api(`/logs?limit=100${level ? `&level=${level}` : ""}`);
      const counts = res.counts || {};
      root.querySelector("[data-log-counts]").textContent =
        `ошибок: ${counts.error || 0} · предупреждений: ${counts.warn || 0} · всего записей: ${Object.values(counts).reduce((a, b) => a + b, 0)}`;
      box.innerHTML = res.rows.length
        ? `<table class="kwa-table kwa-log"><tr><th>Когда</th><th>Уровень</th><th>Событие</th><th>Пользователь</th><th>Детали</th></tr>${res.rows
            .map(
              (r) => `<tr><td>${new Date(r.at).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" })}</td>
                <td><span class="kwa-lvl ${esc(r.level)}">${esc(LEVEL_LABEL[r.level] || r.level)}</span></td>
                <td>${esc(r.event)}${r.status ? `<br><span class="kwa-muted">${esc(r.status)} ${esc(r.path || "")}</span>` : ""}</td>
                <td>${esc(r.user_id ?? "")}</td>
                <td><code>${esc(shortDetail(r.detail))}</code></td></tr>`
            )
            .join("")}</table>`
        : `<p class="kwa-muted">Записей нет</p>`;
    } catch (err) {
      box.innerHTML = `<p class="kwa-muted">Не удалось загрузить журнал: ${esc(err.message)}</p>`;
    }
  }

  async function fetchReport() {
    const mask = root.querySelector("[data-log-mask]").checked ? "1" : "0";
    const res = await fetch(`${API}/logs/download?mask=${mask}`, { credentials: "same-origin", headers: { Accept: "text/plain" } });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const disposition = res.headers.get("Content-Disposition") || "";
    const name = (disposition.match(/filename="([^"]+)"/) || [])[1] || "kiro-wheel-log.txt";
    return { text: await res.text(), name };
  }

  function showText(report, note) {
    const area = root.querySelector("[data-log-text]");
    area.hidden = false;
    area.value = report;
    area.focus();
    area.select();
    root.querySelector("[data-log-msg]").textContent = note;
  }

  function bindLog() {
    const msg = root.querySelector("[data-log-msg]");
    const level = root.querySelector("[data-log-level]");
    level.value = cache.logLevel || "";
    level.addEventListener("change", () => {
      cache.logLevel = level.value;
      loadLog();
    });
    root.querySelector("[data-log-refresh]").addEventListener("click", loadLog);
    root.querySelector("[data-log-download]").addEventListener("click", async (e) => {
      e.target.disabled = true;
      try {
        const { text, name } = await fetchReport();
        const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
        const link = document.createElement("a");
        link.href = url;
        link.download = name;
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 10000);
        msg.textContent = `Файл ${name} сохранён. Отправьте его разработчику.`;
      } catch (err) {
        msg.textContent = `Не удалось скачать: ${err.message}`;
      }
      e.target.disabled = false;
    });
    root.querySelector("[data-log-copy]").addEventListener("click", async (e) => {
      e.target.disabled = true;
      try {
        const { text } = await fetchReport();
        try {
          await navigator.clipboard.writeText(text);
          msg.textContent = "Журнал скопирован. Вставьте его в сообщение разработчику.";
        } catch {
          showText(text, "Скопируйте текст ниже вручную (Ctrl+C) и отправьте разработчику.");
        }
      } catch (err) {
        msg.textContent = `Не удалось получить журнал: ${err.message}`;
      }
      e.target.disabled = false;
    });
    root.querySelector("[data-log-clear]").addEventListener("click", async (e) => {
      if (!confirm("Очистить журнал плагина? Это не влияет на призы и вращения игроков.")) return;
      e.target.disabled = true;
      try {
        const res = await api("/logs/clear", "POST", {});
        msg.textContent = `Удалено записей: ${res.removed}`;
        await loadLog();
      } catch (err) {
        msg.textContent = err.message;
      }
      e.target.disabled = false;
    });
    loadLog();
  }

  async function grantSpins(e) {
    const user = root.querySelector("[data-grant-user]").value.trim();
    const spins = root.querySelector("[data-grant-count]").value;
    if (!user) return;
    e.target.disabled = true;
    try {
      const res = await api("/spins", "POST", { user, spins: Number(spins) });
      alert(`Выдано ${res.spins} вращ. пользователю ${res.user_id}`);
    } catch (err) {
      alert(err.message);
    }
    e.target.disabled = false;
  }

  if (cache.data && Date.now() - cache.at < 120000) {
    // Re-created by the host: show the same data and drafts instantly, without reload flicker.
    st.data = cache.data;
    render();
  } else {
    load();
  }
  return st;
}

export function mountView(view, target) {
  return mountSettings(target);
}

export function updateView() {
  // Admin shell props (language, feature flags) do not affect this view. Without this hook
  // the host re-mounts the module on every prop change and wipes unsaved form input.
}

export function unmountView(instance) {
  if (!instance) return;
  instance.disposed = true;
  instance.root.remove();
}
