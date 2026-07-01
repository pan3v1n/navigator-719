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

// Выпадающее меню экспорта чата у «⋮»: «Экспортировать чат» → выбор формата (JSON/Markdown/Текст).
let menuEl = null;
function closeMenu() {
  if (menuEl) { menuEl.remove(); menuEl = null; document.removeEventListener("click", closeMenu); }
}
function openExportMenu(sid, anchor) {
  closeMenu();
  menuEl = document.createElement("div");
  menuEl.className = "dropdown";
  const step1 = document.createElement("button");
  step1.className = "dd-item";
  step1.textContent = "Экспортировать чат";
  step1.addEventListener("click", (e) => {
    e.stopPropagation();
    menuEl.innerHTML = "";
    const head = document.createElement("div");
    head.className = "dd-head";
    head.textContent = "Формат";
    menuEl.appendChild(head);
    [["JSON", "json"], ["Markdown", "md"], ["Текст", "txt"]].forEach(([label, fmt]) => {
      const b = document.createElement("button");
      b.className = "dd-item";
      b.textContent = label;
      b.addEventListener("click", (e2) => {
        e2.stopPropagation();
        downloadUrl("/api/conversations/" + sid + "/export?fmt=" + fmt);
        closeMenu();
      });
      menuEl.appendChild(b);
    });
  });
  menuEl.appendChild(step1);
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
  const exp = document.createElement("button");
  exp.className = "hi-export";
  exp.type = "button";
  exp.title = "Экспортировать диалог";
  exp.textContent = "⋮";
  exp.addEventListener("click", (e) => { e.stopPropagation(); openExportMenu(sid, exp); });
  item.appendChild(exp);
  const del = document.createElement("button");
  del.className = "hi-del";
  del.type = "button";
  del.title = "Удалить диалог";
  del.textContent = "×";
  del.addEventListener("click", (e) => { e.stopPropagation(); deleteConversation(sid, item); });
  item.appendChild(del);
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
  try {
    const r = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, session_id: sessionId }),
    });
    if (r.status === 401) { window.location = "/login"; return; }
    if (!r.ok) {
      bubble.textContent = "Ошибка: сервис недоступен, повторите запрос.";
      if (isNew) { removeHistoryItem(sessionId); sessionId = null; } // убрать фантомный пункт
      return;
    }
    const data = await r.json();
    sessionId = data.session_id;
    setActive(sessionId);
    bubble.innerHTML = renderMarkdown(data.answer);
    addSources(pending, data.sources);
  } catch (e) {
    bubble.textContent = "Ошибка сети, повторите запрос.";
    if (isNew) { removeHistoryItem(sessionId); sessionId = null; }
  } finally {
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

// экспорт всех диалогов пользователя (скачивание JSON)
const exportBtn = document.getElementById("export-btn");
if (exportBtn) exportBtn.addEventListener("click", () => downloadUrl("/api/export"));

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
