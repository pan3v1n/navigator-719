// Чат UI 1.0: fetch POST /api/chat → рендер сообщений + источников + история бесед. Без внешних либ.
const scroll = document.getElementById("chat-scroll");
const messages = document.getElementById("messages");
const hero = document.getElementById("hero");
const form = document.getElementById("chat-form");
const input = document.getElementById("input");
const send = document.getElementById("send");
const history = document.getElementById("history");
const main = document.getElementById("main");
let sessionId = null;

function el(cls) {
  const d = document.createElement("div");
  d.className = cls;
  return d;
}

function scrollDown() {
  scroll.scrollTop = scroll.scrollHeight;
}

function downloadUrl(url) {
  const a = document.createElement("a");
  a.href = url;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

// Меню чата у «⋮»: «Экспортировать чат» (→ выбор формата) + «Удалить чат».
let menuEl = null;
function closeMenu() {
  if (menuEl) { menuEl.remove(); menuEl = null; document.removeEventListener("click", closeMenu); }
}
function openItemMenu(sid, item, anchor) {
  closeMenu();
  menuEl = document.createElement("div");
  menuEl.className = "dropdown";

  function renderMain() {
    menuEl.innerHTML = "";
    const expBtn = document.createElement("button");
    expBtn.className = "dd-item";
    expBtn.textContent = "Экспортировать чат";
    expBtn.addEventListener("click", (e) => { e.stopPropagation(); renderFormats(); });
    menuEl.appendChild(expBtn);
    const delBtn = document.createElement("button");
    delBtn.className = "dd-item danger";
    delBtn.textContent = "Удалить чат";
    delBtn.addEventListener("click", (e) => { e.stopPropagation(); closeMenu(); deleteConversation(sid, item); });
    menuEl.appendChild(delBtn);
  }

  function renderFormats() {
    menuEl.innerHTML = "";
    const head = document.createElement("div");
    head.className = "dd-head-back";
    const back = document.createElement("button");
    back.className = "dd-back";
    back.type = "button";
    back.title = "Назад";
    back.innerHTML = '<svg viewBox="0 0 24 24" fill="none" width="15" height="15"><path d="M15 6l-6 6 6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    back.addEventListener("click", (e) => { e.stopPropagation(); renderMain(); });
    const title = document.createElement("span");
    title.textContent = "Формат";
    head.appendChild(back);
    head.appendChild(title);
    menuEl.appendChild(head);
    [["JSON", "json"], ["Markdown", "md"], ["Текст", "txt"]].forEach(([label, fmt]) => {
      const b = document.createElement("button");
      b.className = "dd-item";
      b.textContent = label;
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        downloadUrl("/api/conversations/" + sid + "/export?fmt=" + fmt);
        closeMenu();
      });
      menuEl.appendChild(b);
    });
  }

  renderMain();
  document.body.appendChild(menuEl);
  const r = anchor.getBoundingClientRect();
  menuEl.style.top = r.bottom + 4 + "px";
  menuEl.style.left = Math.min(r.left, window.innerWidth - 190) + "px";
  setTimeout(() => document.addEventListener("click", closeMenu), 0);
}

// Минимальный БЕЗОПАСНЫЙ рендер markdown ответа движка (**жирный**, • списки, абзацы).
// Сначала экранируем HTML (защита от XSS), потом добавляем ТОЛЬКО свои теги.
function renderMarkdown(text) {
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const inline = (s) => esc(s).replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  const lines = (text || "").split(/\r?\n/);
  let html = "";
  let inList = false;
  for (const raw of lines) {
    const line = raw.trim();
    const bullet = /^[•\-*]\s+/.test(line);
    if (bullet) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += "<li>" + inline(line.replace(/^[•\-*]\s+/, "")) + "</li>";
    } else {
      if (inList) { html += "</ul>"; inList = false; }
      if (line) {
        const sec = /^\*\*/.test(line) ? ' class="sec"' : ""; // заголовок раздела (жирный лейбл) — с отступом
        html += "<p" + sec + ">" + inline(line) + "</p>";
      }
    }
  }
  if (inList) html += "</ul>";
  return html;
}

function addUser(text) {
  const wrap = el("msg user");
  const b = el("bubble");
  b.textContent = text; // вопрос эксперта — как есть (без XSS)
  wrap.appendChild(b);
  messages.appendChild(wrap);
  scrollDown();
  return wrap;
}

function addAssistant(text, sources) {
  const wrap = el("msg assistant");
  const b = el("bubble");
  b.innerHTML = renderMarkdown(text);
  wrap.appendChild(b);
  messages.appendChild(wrap);
  if (sources) addSources(wrap, sources);
  return wrap;
}

function addPending() {
  const wrap = el("msg assistant");
  const b = el("bubble");
  b.innerHTML = '<div class="typing"><span></span><span></span><span></span></div>';
  wrap.appendChild(b);
  messages.appendChild(wrap);
  scrollDown();
  return wrap;
}

// Официальный текст ПП №719 на Контур.Норматив (для кликабельных источников).
const KONTUR_719 = "https://normativ.kontur.ru/document/1/506613-postanovlenie-pravitelstva-rf-ot-17-07-2015-n-719";

// Ссылка на документ 719 + текстовый фрагмент (#:~:text=…): браузер (Chrome/Edge) прокручивает
// к позиции в таблице. Цель — код ОКПД2 (в таблице он есть дословно) либо наименование продукции.
function konturLink(s) {
  const codes = s.okpd2 || [];
  const anchor = codes.length ? codes[0] : (s.product_name || "").slice(0, 40);
  return KONTUR_719 + (anchor ? "#:~:text=" + encodeURIComponent(anchor) : "");
}

function addSources(wrap, sources) {
  if (!sources || !sources.length) return;
  const det = document.createElement("details");
  det.className = "sources";
  const sum = document.createElement("summary");
  sum.textContent = "Источники (" + sources.length + ")";
  det.appendChild(sum);
  sources.forEach((s, i) => {
    const a = document.createElement("a");
    a.className = "src-item";
    a.href = konturLink(s);
    a.target = "_blank";
    a.rel = "noopener";
    a.title = "Открыть в тексте ПП №719 (Контур.Норматив)";
    const mark = s.okpd2_match ? " (совпадение по коду)" : "";
    const codes = (s.okpd2 || []).join(", ");
    let t = "[" + (i + 1) + "] " + s.product_name + " — " + (s.section || "") + mark;
    if (codes) t += " · ОКПД2 " + codes;
    if (s.source_anchor) t += " · " + s.source_anchor;
    a.textContent = t;
    det.appendChild(a);
  });
  wrap.appendChild(det);
}

// пометка о незаземлённых числах: faithfulness-guard поймал число баллов/%, которого нет в источнике
function addUnverifiedFlag(wrap, nums) {
  if (!nums || !nums.length) return;
  const f = el("answer-flag");
  f.innerHTML = '<svg viewBox="0 0 24 24" fill="none" width="16" height="16"><path d="M12 3.5l9 16.5H3l9-16.5z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M12 10v4.5M12 17.6h.01" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>';
  const s = document.createElement("span");
  s.textContent = "Проверьте показатели: " + nums.join(", ") + " — эти числа не найдены в тексте источника, сверьте с ПП №719.";
  f.appendChild(s);
  wrap.appendChild(f);
}

// --- обратная связь на ответ: звёзды 0..5 + отметить ошибку (исправление) + коммент к диалогу ---
async function postFeedback(payload) {
  try {
    const r = await fetch("/api/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    return r.ok;
  } catch (e) { return false; }
}

function mkFbLink(text, onClick) {
  const a = document.createElement("button");
  a.type = "button"; a.className = "fb-link"; a.textContent = text;
  a.addEventListener("click", onClick);
  return a;
}

function flash(anchor, text) {
  let f = anchor.querySelector(":scope > .fb-flash");
  if (!f) { f = document.createElement("span"); f.className = "fb-flash"; anchor.appendChild(f); }
  f.textContent = text;
  clearTimeout(f._t);
  f._t = setTimeout(() => { f.textContent = ""; }, 2000);
}

function addFeedbackBar(wrap, messageId, sid) {
  if (!messageId) return; // без id ответа привязать оценку нельзя (редкий сбой лога)
  const bar = el("fb-bar");

  // поле «textarea + Сохранить» внутри панели
  const addSaveField = (w, placeholder, btnText, onSave) => {
    const ta = document.createElement("textarea");
    ta.className = "fb-ta"; ta.rows = 3; ta.placeholder = placeholder;
    const btn = document.createElement("button");
    btn.type = "button"; btn.className = "fb-save"; btn.textContent = btnText;
    btn.addEventListener("click", async () => {
      const val = ta.value.trim();
      if (!val) return;
      if (await onSave(val)) { ta.value = ""; flash(w, "спасибо!"); }
    });
    w.appendChild(ta); w.appendChild(btn);
  };
  const mkPanel = (build) => {
    const w = el("fb-panel hidden");
    build(w);
    return {
      wrap: w,
      toggle: () => { w.classList.toggle("hidden"); const t = w.querySelector("textarea"); if (!w.classList.contains("hidden") && t) t.focus(); },
      hide: () => w.classList.add("hidden"),
    };
  };

  // «Оценить ответ» → звёзды 0..5 (клик = сразу, повтор по активной = сброс в 0) + комментарий к ответу
  const ratePanel = mkPanel((w) => {
    const stars = el("fb-stars");
    let current = 0;
    const glyphs = [];
    const paint = (n) => glyphs.forEach((g, i) => g.classList.toggle("on", i < n));
    for (let i = 1; i <= 5; i++) {
      const s = document.createElement("button");
      s.type = "button"; s.className = "fb-star"; s.textContent = "★"; s.title = i + " из 5";
      s.addEventListener("mouseenter", () => paint(i));
      s.addEventListener("mouseleave", () => paint(current));
      s.addEventListener("click", async () => {
        current = current === i ? 0 : i;
        paint(current);
        if (await postFeedback({ kind: "answer", message_id: messageId, session_id: sid, rating: current }))
          flash(w, current === 0 ? "0 — учтено" : "оценка сохранена");
      });
      glyphs.push(s); stars.appendChild(s);
    }
    w.appendChild(stars);
    addSaveField(w, "Комментарий к этому ответу (необязательно):", "Сохранить комментарий",
      (val) => postFeedback({ kind: "answer", message_id: messageId, session_id: sid, comment: val }));
  });

  const errPanel = mkPanel((w) => addSaveField(w, "Что не так? Верное значение / позиция:", "Сохранить исправление",
    (val) => postFeedback({ kind: "answer", message_id: messageId, session_id: sid, correction: val })));

  const dlgPanel = mkPanel((w) => addSaveField(w, "Комментарий ко всему диалогу:", "Отправить комментарий",
    (val) => postFeedback({ kind: "dialog", session_id: sid, comment: val })));

  // ссылки-действия: раскрывают ровно одну панель (остальные прячут)
  const panels = [ratePanel, errPanel, dlgPanel];
  const only = (p) => { panels.forEach((x) => { if (x !== p) x.hide(); }); p.toggle(); };
  const actions = el("fb-actions");
  const rateLink = mkFbLink("Оценить ответ", () => only(ratePanel));
  rateLink.classList.add("primary");
  actions.appendChild(rateLink);
  actions.appendChild(mkFbLink("отметить ошибку", () => only(errPanel)));
  actions.appendChild(mkFbLink("комментарий к диалогу", () => only(dlgPanel)));
  bar.appendChild(actions);
  bar.appendChild(ratePanel.wrap); bar.appendChild(errPanel.wrap); bar.appendChild(dlgPanel.wrap);

  wrap.appendChild(bar);
}

// --- история бесед в сайдбаре ---
function setActive(sid) {
  history.querySelectorAll(".history-item").forEach((x) =>
    x.classList.toggle("active", x.dataset.sid === sid)
  );
}

function addHistoryItem(sid, title, prepend) {
  if (history.querySelector('[data-sid="' + sid + '"]')) { setActive(sid); return; }
  const item = el("history-item");
  item.dataset.sid = sid;
  item.title = title || "Диалог";
  const label = document.createElement("span");
  label.className = "hi-title";
  label.textContent = title || "Диалог";
  item.appendChild(label);
  const menuBtn = document.createElement("button");
  menuBtn.className = "hi-export";
  menuBtn.type = "button";
  menuBtn.title = "Меню чата";
  menuBtn.textContent = "⋮";
  menuBtn.addEventListener("click", (e) => { e.stopPropagation(); openItemMenu(sid, item, menuBtn); });
  item.appendChild(menuBtn);
  item.addEventListener("click", () => openConversation(sid));
  if (prepend) history.prepend(item); else history.appendChild(item);
  setActive(sid);
}

function removeHistoryItem(sid) {
  const it = history.querySelector('[data-sid="' + sid + '"]');
  if (it) it.remove();
}

async function deleteConversation(sid, item) {
  if (!confirm("Удалить этот диалог? Действие необратимо.")) return;
  try {
    const r = await fetch("/api/conversations/" + sid, { method: "DELETE" });
    if (!r.ok) return;
    item.remove();
    if (sessionId === sid) {
      messages.innerHTML = "";
      sessionId = null;
      main.classList.add("empty");
    }
  } catch (e) { /* сеть */ }
}

async function loadConversations() {
  try {
    const r = await fetch("/api/conversations");
    if (!r.ok) return;
    const list = await r.json();
    history.innerHTML = "";
    list.forEach((c) => addHistoryItem(c.session_id, c.title, false));
    setActive(sessionId);
  } catch (e) { /* сеть — не критично */ }
}

async function openConversation(sid) {
  try {
    const r = await fetch("/api/conversations/" + sid);
    if (!r.ok) return;
    const data = await r.json();
    messages.innerHTML = "";
    main.classList.remove("empty");
    sessionId = sid;
    data.messages.forEach((m) => {
      if (m.role === "user") addUser(m.content);
      else addAssistant(m.content, m.sources);
    });
    setActive(sid);
    scrollDown();
  } catch (e) { /* сеть */ }
}

// --- отправка вопроса ---
function genId() {
  return window.crypto && crypto.randomUUID
    ? crypto.randomUUID()
    : "c" + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
}

async function ask(text) {
  main.classList.remove("empty");
  const isNew = !sessionId;
  if (isNew) {
    // новую беседу заводим СРАЗУ (id на клиенте) и добавляем в историю ДО ответа
    sessionId = genId();
    addHistoryItem(sessionId, text, true);
  }
  addUser(text);
  const pending = addPending();
  const bubble = pending.querySelector(".bubble");
  send.disabled = true;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 120000);  // клиентский таймаут (бэкенд режет DeepSeek на 30с/вызов)
  try {
    const r = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, session_id: sessionId }),
      signal: ctrl.signal,
    });
    if (r.status === 401) { window.location = "/login"; return; }
    if (r.status === 403) { window.location = "/profile"; return; }  // профиль/согласие не заполнены
    if (!r.ok) {
      bubble.textContent = "Ошибка: сервис недоступен, повторите запрос.";
      if (isNew) { removeHistoryItem(sessionId); sessionId = null; } // убрать фантомный пункт
      return;
    }
    const data = await r.json();
    sessionId = data.session_id;
    setActive(sessionId);
    bubble.innerHTML = renderMarkdown(data.answer);
    addUnverifiedFlag(pending, data.unverified_numbers);
    addSources(pending, data.sources);
    addFeedbackBar(pending, data.message_id, sessionId);
  } catch (e) {
    bubble.textContent = "Ошибка сети, повторите запрос.";
    if (isNew) { removeHistoryItem(sessionId); sessionId = null; }
  } finally {
    clearTimeout(timer);
    send.disabled = false;
    scrollDown();
  }
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  input.style.height = "auto";
  ask(text);
});

// авто-рост textarea; Enter — отправка, Shift+Enter — перенос строки
input.addEventListener("input", () => {
  input.style.height = "auto";
  const cap = window.innerHeight * 0.5; // растём вверх до половины экрана, потом — прокрутка
  input.style.height = Math.min(input.scrollHeight, cap) + "px";
  input.style.overflowY = input.scrollHeight > cap ? "auto" : "hidden";
});
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
});

// «Новый диалог» — текущая беседа уже в истории (сохранена в БД); чистим экран, начинаем новую
const newChat = document.getElementById("new-chat");
if (newChat) newChat.addEventListener("click", () => {
  messages.innerHTML = "";
  sessionId = null;
  main.classList.add("empty");
  setActive(null);
  input.focus();
});

// --- экспорт всех диалогов: окно настроек (формат / период дат / источники) ---
const exportBtn = document.getElementById("export-btn");
const exportModal = document.getElementById("export-modal");
if (exportBtn) exportBtn.addEventListener("click", () => exportModal.classList.remove("hidden"));
const exportCancel = document.getElementById("export-cancel");
if (exportCancel) exportCancel.addEventListener("click", () => exportModal.classList.add("hidden"));
const exportForm = document.getElementById("export-form");
if (exportForm) exportForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target;
  const p = new URLSearchParams();
  p.set("fmt", f.fmt.value);
  if (f.date_from.value) p.set("date_from", f.date_from.value);
  if (f.date_to.value) p.set("date_to", f.date_to.value);
  p.set("sources", f.sources.checked ? "1" : "0");
  downloadUrl("/api/export?" + p.toString());
  exportModal.classList.add("hidden");
});

// --- сворачивание/разворачивание сайдбара (состояние в localStorage) ---
const appEl = document.getElementById("app");
function setCollapsed(on) {
  appEl.classList.toggle("collapsed", on);
  try { localStorage.setItem("sidebarCollapsed", on ? "1" : "0"); } catch (e) {}
}
const sideCollapse = document.getElementById("side-collapse");
const sideExpand = document.getElementById("side-expand");
if (sideCollapse) sideCollapse.addEventListener("click", () => setCollapsed(true));
if (sideExpand) sideExpand.addEventListener("click", () => setCollapsed(false));
try { if (localStorage.getItem("sidebarCollapsed") === "1") appEl.classList.add("collapsed"); } catch (e) {}

// --- поиск по чатам (фильтр истории по заголовку) ---
const searchBtn = document.getElementById("search-btn");
const searchBox = document.getElementById("search-box");
const searchInput = document.getElementById("search-input");
function filterHistory(qq) {
  const q = (qq || "").trim().toLowerCase();
  history.querySelectorAll(".history-item").forEach((it) => {
    const label = it.querySelector(".hi-title");
    const t = (label ? label.textContent : "").toLowerCase();
    it.style.display = !q || t.indexOf(q) !== -1 ? "" : "none";
  });
}
if (searchBtn) searchBtn.addEventListener("click", () => {
  if (appEl.classList.contains("collapsed")) setCollapsed(false); // развернуть для поиска
  searchBox.classList.toggle("hidden");
  if (!searchBox.classList.contains("hidden")) { searchInput.focus(); }
  else { searchInput.value = ""; filterHistory(""); }
});
if (searchInput) searchInput.addEventListener("input", () => filterHistory(searchInput.value));

// форма обратной связи
const modal = document.getElementById("fb-modal");
document.getElementById("fb-open").addEventListener("click", () => modal.classList.remove("hidden"));
document.getElementById("fb-cancel").addEventListener("click", () => modal.classList.add("hidden"));
document.getElementById("fb-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const payload = {
    rating: f.rating.value ? parseInt(f.rating.value, 10) : null,
    matched: f.matched.value || null,
    comment: f.comment.value || null,
  };
  const r = await fetch("/api/feedback", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (r.ok) {
    document.getElementById("fb-thanks").classList.remove("hidden");
    setTimeout(() => modal.classList.add("hidden"), 1200);
  }
});

// загрузить историю бесед при открытии
loadConversations();

// --- онбординг: попап при первом входе (localStorage) + кнопка «Как пользоваться» ---
(function initOnboarding() {
  const modal = document.getElementById("onboarding-modal");
  if (!modal) return;
  const steps = Array.from(modal.querySelectorAll(".ob-step"));
  const dotsWrap = document.getElementById("ob-dots");
  const prevBtn = document.getElementById("ob-prev");
  const nextBtn = document.getElementById("ob-next");
  const skipBtn = document.getElementById("ob-skip");
  let i = 0;
  const dots = steps.map((_, k) => {
    const d = document.createElement("span");
    d.className = "ob-dot";
    d.addEventListener("click", () => go(k));
    dotsWrap.appendChild(d);
    return d;
  });
  function render() {
    steps.forEach((s, k) => s.classList.toggle("hidden", k !== i));
    dots.forEach((d, k) => d.classList.toggle("on", k === i));
    prevBtn.style.visibility = i === 0 ? "hidden" : "visible";
    nextBtn.textContent = i === steps.length - 1 ? "Начать работу" : "Далее";
  }
  function go(k) { i = Math.max(0, Math.min(steps.length - 1, k)); render(); }
  function open() { go(0); modal.classList.remove("hidden"); }
  function close() {
    modal.classList.add("hidden");
    try { localStorage.setItem("onboarding719Seen", "1"); } catch (e) {}
  }
  prevBtn.addEventListener("click", () => go(i - 1));
  nextBtn.addEventListener("click", () => { if (i === steps.length - 1) close(); else go(i + 1); });
  if (skipBtn) skipBtn.addEventListener("click", close);
  const openBtn = document.getElementById("ob-open");
  if (openBtn) openBtn.addEventListener("click", open);
  let seen = false;
  try { seen = localStorage.getItem("onboarding719Seen") === "1"; } catch (e) {}
  if (!seen) open();
})();
