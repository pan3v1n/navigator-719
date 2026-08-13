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

// «Прилипание» к низу: во время анимации ответа скроллим вниз ТОЛЬКО если пользователь и так внизу.
// Полистал вверх (колесо/тач) — отцепляемся и не мешаем читать; вернулся к низу — прицепляемся снова.
let stickBottom = true;
function atBottom() {
  return scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 80;
}
function scrollDown() {
  // форсированно (действие пользователя: отправка вопроса / открытие беседы) — всегда вниз
  scroll.scrollTop = scroll.scrollHeight;
  stickBottom = true;
}
function autoScroll() {
  // мягко (стриминг ответа): не дёргаем вниз, если пользователь листает выше
  if (stickBottom) scroll.scrollTop = scroll.scrollHeight;
}
scroll.addEventListener("wheel", (e) => { if (e.deltaY < 0) stickBottom = false; }, { passive: true });
scroll.addEventListener("touchmove", () => { if (!atBottom()) stickBottom = false; }, { passive: true });
scroll.addEventListener("scroll", () => { if (atBottom()) stickBottom = true; });

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
function renderMarkdown(text, streaming) {
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const inline = (s) => esc(s).replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  // строка-разделитель Markdown-таблицы: |---|:--:|---| и т.п.
  const isSep = (s) => /^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$/.test(s);
  const cells = (s) => s.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((c) => c.trim());
  let lines = (text || "").split(/\r?\n/);
  // При стриминге таблица приходит по строкам, и до разделителя «|---|---|» её шапка выглядит
  // строкой с палками. На ответе с перечнем документов это секунды визуального мусора, поэтому
  // недособранный хвост таблицы просто не показываем — он появится, когда придут данные.
  if (streaming) {
    let start = lines.length;
    while (start > 0 && lines[start - 1].includes("|")) start--;
    const block = lines.slice(start);
    if (block.length) {
      // строк меньше трёх (шапка + разделитель + первая строка данных) — таблицы ещё нет;
      // иначе прячем только последнюю строку: она может быть недописана на полсимвола
      lines = block.some(isSep) && block.length >= 3 ? lines.slice(0, -1) : lines.slice(0, start);
    }
  }
  let html = "";
  let inList = false;
  const closeList = () => { if (inList) { html += "</ul>"; inList = false; } };
  let i = 0;
  while (i < lines.length) {
    const line = lines[i].trim();
    // ТАБЛИЦА: строка с «|» и следующая — разделитель «|---|---|»
    if (line.includes("|") && i + 1 < lines.length && isSep(lines[i + 1].trim())) {
      closeList();
      const head = cells(line);
      let body = "";
      i += 2;
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
        body += "<tr>" + cells(lines[i].trim()).map((x) => "<td>" + inline(x) + "</td>").join("") + "</tr>";
        i++;
      }
      html += '<div class="tbl-wrap"><table><thead><tr>'
        + head.map((x) => "<th>" + inline(x) + "</th>").join("")
        + "</tr></thead><tbody>" + body + "</tbody></table></div>";
      continue;
    }
    const bullet = /^[•\-*]\s+/.test(line);
    if (bullet) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += "<li>" + inline(line.replace(/^[•\-*]\s+/, "")) + "</li>";
    } else {
      closeList();
      if (line) {
        const sec = /^\*\*/.test(line) ? ' class="sec"' : ""; // заголовок раздела (жирный лейбл) — с отступом
        html += "<p" + sec + ">" + inline(line) + "</p>";
      }
    }
    i++;
  }
  closeList();
  return html;
}

function addUser(text) {
  const wrap = el("msg user");
  const b = el("bubble");
  b.textContent = text; // вопрос эксперта — как есть (без XSS)
  wrap.appendChild(b);
  messages.appendChild(wrap);
  scrollDown();
  updateJumpBtn(); // T16: обновить навигатор по вопросам чата
  return wrap;
}

function addAssistant(text, sources) {
  const wrap = el("msg assistant");
  const b = el("bubble");
  b.innerHTML = renderMarkdown(text);
  wrap.appendChild(b);
  wrap.dataset.raw = text;  // исходный markdown — его и копируем, а не текст из DOM
  messages.appendChild(wrap);
  addAnswerTools(wrap);
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

// Официальный текст ПП №719 на Контур.Норматив (для кликабельных источников) — ДЕЙСТВУЮЩАЯ
// редакция от 27.06.2026 (действует с 01.07.2026), совпадает с индексируемым текстом базы.
// ⚠️ При вступлении в силу новой редакции обновить documentId (у каждой редакции Контура свой id).
const KONTUR_719 = "https://normativ.kontur.ru/document?moduleId=1&documentId=506899";

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
    // s.url — прямая ссылка на первоисточник (процедурные: Правила/ПП №719/Приказ №52).
    // Товарные источники приходят без url → строим ссылку по ОКПД2/наименованию в тексте 719.
    a.href = s.url || konturLink(s);
    a.target = "_blank";
    a.rel = "noopener";
    a.title = s.url ? "Открыть первоисточник (Контур.Норматив)" : "Открыть в тексте ПП №719 (Контур.Норматив)";
    const mark = s.okpd2_match ? " (совпадение по коду)" : "";
    const codes = (s.okpd2 || []).join(", ");
    let t = "[" + (i + 1) + "] " + s.product_name + (s.section ? " — " + s.section : "") + mark;
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

// Подпись к вынесенному ответу. R3: ответ, покинувший сервис (экспорт, копирование, пересылка),
// обязан нести пометку о происхождении — получатель не должен принять черновик ИИ за заключение.
const SHARE_NOTE =
  "\n\n— Ответ ИИ-ассистента «Навигатор ПП №719» (предварительно; окончательное решение " +
  "принимает уполномоченный эксперт ТПП).";

function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  // http-контекст (пилот работает без TLS) — clipboard API недоступен, нужен старый путь
  return new Promise((resolve, reject) => {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy") ? resolve() : reject(); } catch (e) { reject(e); }
    document.body.removeChild(ta);
  });
}

// Кнопки под ответом: копировать и поделиться. Живут отдельно от панели оценки — та требует
// message_id, а копировать нужно уметь всегда, даже если запись в лог не удалась.
function addAnswerTools(wrap) {
  const tools = el("msg-tools");

  const mkTool = (tip, svg, onClick) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "tool-btn";
    b.dataset.tip = tip;              // подпись показывается по hover/фокусу (CSS ::after)
    b.setAttribute("aria-label", tip);
    b.innerHTML = svg;
    b.addEventListener("click", () => onClick(b));
    return b;
  };

  const done = (btn, tip) => {
    const prev = btn.dataset.tip;
    btn.dataset.tip = tip;
    btn.classList.add("ok");
    setTimeout(() => { btn.dataset.tip = prev; btn.classList.remove("ok"); }, 1600);
  };

  const raw = () => (wrap.dataset.raw || wrap.querySelector(".bubble")?.innerText || "");

  const copySvg = '<svg viewBox="0 0 24 24" fill="none" width="17" height="17">'
    + '<rect x="9" y="9" width="11" height="11" rx="2" stroke="currentColor" stroke-width="1.8"/>'
    + '<path d="M5 15V5a2 2 0 0 1 2-2h8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>';
  const shareSvg = '<svg viewBox="0 0 24 24" fill="none" width="17" height="17">'
    + '<path d="M12 16V4m0 0L8 8m4-4l4 4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>'
    + '<path d="M5 14v4a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>';

  tools.appendChild(mkTool("Копировать ответ", copySvg, (btn) => {
    copyText(raw() + SHARE_NOTE)
      .then(() => done(btn, "Скопировано"))
      .catch(() => done(btn, "Не удалось скопировать"));
  }));

  tools.appendChild(mkTool("Поделиться", shareSvg, (btn) => {
    const text = raw() + SHARE_NOTE;
    // Системный шаринг там, где он есть (мобильные, часть десктопов); иначе — в буфер обмена:
    // публичной ссылки на диалог у сервиса нет и быть не должно — переписка персональная.
    if (navigator.share) {
      navigator.share({ title: "Навигатор ПП №719", text }).catch(() => {});
      return;
    }
    copyText(text)
      .then(() => done(btn, "Ответ в буфере — вставьте в письмо"))
      .catch(() => done(btn, "Не удалось скопировать"));
  }));

  wrap.appendChild(tools);
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
      updateJumpBtn(); // T16
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
    updateJumpBtn(); // T16: показать/скрыть навигатор по вопросам открытой беседы
  } catch (e) { /* сеть */ }
}

// --- отправка вопроса ---
function genId() {
  return window.crypto && crypto.randomUUID
    ? crypto.randomUUID()
    : "c" + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
}

// Сервер отклонил вопрос по содержимому (422). Возвращаем текст в поле ввода и показываем причину:
// человеку нужно отредактировать вопрос, а не потерять его.
async function handleRejected(r, text, pending) {
  let msg = "В вопросе есть данные, которые нельзя отправлять в сервис. Измените запрос.";
  try { const d = await r.json(); if (d && d.detail) msg = d.detail; } catch (e) {}
  if (pending) pending.remove();
  input.value = text;
  input.style.height = "auto";
  showInputBlock(msg);
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
    // T18: сперва пробуем стриминг (постепенный вывод, как Claude/ChatGPT). Если он не стартовал
    // или оборвался до финала — фолбэк на обычный /api/chat (рваная РФ-сеть → длинный SSE хрупок).
    const streamed = await askStream(text, pending, bubble);
    if (!streamed) await askFallback(text, pending, bubble, isNew);
  } finally {
    send.disabled = false;
    autoScroll();  // в конце ответа не выдёргиваем пользователя вниз, если он читает выше
  }
}

// Стриминг: fetch SSE-поток → дописываем delta в пузырь → на done навешиваем источники+оценку.
// Возврат: true = завершилось финалом (done / редирект на login|profile); false = нужен фолбэк.
async function askStream(text, pending, bubble) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 120000);
  let acc = "";
  let done = null;
  try {
    const r = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, session_id: sessionId }),
      signal: ctrl.signal,
    });
    if (r.status === 401) { window.location = "/login"; return true; }
    if (r.status === 403) { window.location = "/profile"; return true; }  // профиль/согласие не заполнены
    // 422 — сервер нашёл в вопросе то, чего нельзя отправлять. Фолбэк бессмысленен: там та же
    // проверка. Возвращаем вопрос в поле, чтобы человек его отредактировал, а не набирал заново.
    if (r.status === 422) { await handleRejected(r, text, pending); return true; }
    if (!r.ok || !r.body) return false;  // не стартовал → фолбэк
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done: rdone } = await reader.read();
      if (rdone) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) !== -1) {  // SSE-события разделены пустой строкой
        const evtext = buf.slice(0, idx).replace(/^data:\s?/, "").trim();
        buf = buf.slice(idx + 2);
        if (!evtext) continue;
        let ev;
        try { ev = JSON.parse(evtext); } catch (e) { continue; }
        if (ev.type === "delta") {
          acc += ev.text;
          bubble.innerHTML = renderMarkdown(acc, true);  // инкрементальный рендер (без хвоста таблицы)
          autoScroll();  // следуем за текстом, только если пользователь не листает выше
        } else if (ev.type === "done") {
          done = ev;
        } else if (ev.type === "error") {
          return false;  // движок упал на сервере → фолбэк
        }
      }
    }
    if (!done) return false;  // поток оборвался без финала → фолбэк (лог на сервере не писался)
    sessionId = done.session_id;
    setActive(sessionId);
    bubble.innerHTML = renderMarkdown(acc);  // финальный ре-рендер полного текста
    pending.dataset.raw = acc;
    addAnswerTools(pending);
    addUnverifiedFlag(pending, done.unverified_numbers);
    addSources(pending, done.sources);
    addFeedbackBar(pending, done.message_id, sessionId);
    return true;
  } catch (e) {
    return false;  // сеть / abort → фолбэк
  } finally {
    clearTimeout(timer);
  }
}

// Фолбэк: обычный НЕ-стриминг /api/chat (сохранён как надёжный путь на рваной сети).
async function askFallback(text, pending, bubble, isNew) {
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
    if (r.status === 422) {
      await handleRejected(r, text, pending);
      if (isNew) { removeHistoryItem(sessionId); sessionId = null; }
      return;
    }
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
  }
}

// Данные, которые нельзя отправлять во внешнюю модель. Правила ПОВТОРЯЮТ серверные
// (app/core/sensitive.py) намеренно: здесь проверка мгновенная и текст вообще не покидает браузер,
// там — обязательная, потому что клиентскую можно обойти. Оба списка видов сверяет тест.
const SENSITIVE_RULES = [
  { kind: "restricted", name: "пометка ограниченного доступа",
    re: /совершенно\s+секретно|\bсекретно\b|для\s+служебного\s+пользования|\bдсп\b|коммерческ\w+\s+тайн\w+|служебн\w+\s+тайн\w+|государственн\w+\s+тайн\w+/i },
  { kind: "passport", name: "данные паспорта", re: /паспорт\w*[^0-9]{0,20}\d{4}\s?\d{6}\b/i },
  { kind: "snils", name: "СНИЛС", re: /\b\d{3}[\s-]?\d{3}[\s-]?\d{3}[\s-]?\d{2}\b/, check: "snils" },
  { kind: "inn_person", name: "ИНН физического лица", re: /\b\d{12}\b/, check: "inn12" },
  { kind: "card", name: "номер банковской карты", re: /\b(?:\d[ -]?){13,19}\b/, check: "luhn" },
  { kind: "phone", name: "номер телефона", soft: true,
    re: /\+7[\s\-()]*\d[\d\s\-()]{8,14}\d|\b8[\s-]*\(\d{3,5}\)[\s-]*\d[\d\s-]{4,10}\d|(?:тел|телефон|моб|звон|whats|viber|вайбер)\w*[^0-9+]{0,12}\+?[78][\d\s\-()]{9,16}\d/i },
  { kind: "email", name: "адрес электронной почты", soft: true, re: /\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b/ },
];

// Контрольные суммы отделяют документ от случайного совпадения по длине: технический текст полон
// длинных чисел, и без проверки разрядов блокировка сыпалась бы на кодах и датах.
function digitsOnly(s) { return (s.match(/\d/g) || []).join(""); }
function checkValue(kind, d) {
  if (kind === "snils") {
    if (d.length !== 11 || /^(\d)\1{8}/.test(d)) return false;
    let sum = 0;
    for (let i = 0; i < 9; i++) sum += Number(d[i]) * (9 - i);
    let c = sum % 101;
    if (c === 100 || c === 101) c = 0;
    return c === Number(d.slice(9));
  }
  if (kind === "inn12") {
    if (d.length !== 12) return false;
    const w1 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8];
    const w2 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8];
    const s1 = w1.reduce((a, w, i) => a + w * Number(d[i]), 0) % 11 % 10;
    const s2 = w2.reduce((a, w, i) => a + w * Number(d[i]), 0) % 11 % 10;
    return s1 === Number(d[10]) && s2 === Number(d[11]);
  }
  if (kind === "luhn") {
    if (d.length < 13 || d.length > 19) return false;
    let sum = 0, alt = false;
    for (let i = d.length - 1; i >= 0; i--) {
      let n = Number(d[i]);
      if (alt) { n *= 2; if (n > 9) n -= 9; }
      sum += n; alt = !alt;
    }
    return sum % 10 === 0;
  }
  return true;
}

function detectSensitive(text) {
  const found = [];
  SENSITIVE_RULES.forEach((rule) => {
    const hit = { name: rule.name, soft: !!rule.soft };
    if (!rule.check) {
      if (rule.re.test(text)) found.push(hit);
      return;
    }
    const re = new RegExp(rule.re.source, "g");
    let m;
    while ((m = re.exec(text)) !== null) {
      if (checkValue(rule.check, digitsOnly(m[0]))) { found.push(hit); break; }
    }
  });
  return found;
}

function softMessage(names) {
  const listed = names.length === 1 ? names[0]
    : names.slice(0, -1).join(", ") + " и " + names[names.length - 1];
  return "Похоже, в вопросе есть " + listed + ". Текст вопроса уходит во внешнюю языковую модель — " +
         "если эти данные не нужны для ответа, лучше их убрать.";
}

function sensitiveMessage(names) {
  if (names.length === 1 && names[0] === "пометка ограниченного доступа") {
    return "Похоже, в вопросе есть сведения ограниченного доступа. Такие сведения нельзя передавать " +
           "в сервис — уберите их из текста и отправьте вопрос снова.";
  }
  const listed = names.length === 1 ? names[0]
    : names.slice(0, -1).join(", ") + " и " + names[names.length - 1];
  return "Похоже, в вопросе есть " + listed + ". Сервис не передаёт персональные данные во внешнюю " +
         "языковую модель — уберите их из текста и отправьте вопрос снова. Для ответа по ПП №719 " +
         "достаточно наименования продукции и кода ОКПД2 или ТН ВЭД.";
}

// Полоса над полем ввода. Два режима: жёсткий — отправка не пойдёт, пока вопрос не изменят;
// мягкий — предупреждаем и даём отправить осознанно, кнопкой в самой полосе.
const WARN_SVG = '<svg viewBox="0 0 24 24" fill="none" width="17" height="17"><path d="M12 8v5" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/><circle cx="12" cy="16.4" r="1.1" fill="currentColor"/><path d="M10.3 3.9L2.6 17.4A2 2 0 0 0 4.3 20.4h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>';

function showInputBlock(text, onProceed) {
  let box = document.getElementById("input-block");
  if (!box) {
    box = el("input-block");
    box.id = "input-block";
    form.parentNode.insertBefore(box, form);
  }
  box.className = "input-block" + (onProceed ? " soft" : "");
  box.innerHTML = WARN_SVG + "<span></span>";
  box.querySelector("span").textContent = text;
  if (onProceed) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "input-block-go";
    btn.textContent = "Отправить как есть";
    btn.addEventListener("click", () => { hideInputBlock(); onProceed(); });
    box.appendChild(btn);
  }
  box.classList.remove("hidden");
  input.focus();
}
function hideInputBlock() {
  const box = document.getElementById("input-block");
  if (box) box.classList.add("hidden");
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  const found = detectSensitive(text);
  const hard = found.filter((f) => !f.soft).map((f) => f.name);
  const soft = found.filter((f) => f.soft).map((f) => f.name);
  if (hard.length) {
    showInputBlock(sensitiveMessage(hard));
    return;   // вопрос остаётся в поле — его нужно отредактировать, а не потерять
  }
  const proceed = () => {
    hideInputBlock();
    input.value = "";
    input.style.height = "auto";
    ask(text);
  };
  // Телефон и почта: предупреждаем, но отправить даём — они часто нужны по делу, и жёсткая
  // блокировка мешала бы работе чаще, чем защищала.
  if (soft.length) { showInputBlock(softMessage(soft), proceed); return; }
  proceed();
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
  updateJumpBtn(); // T16: скрыть навигатор (пустой чат)
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
// Онбординг-тур: затемняем экран, оставляя «окно» вокруг одной области, и объясняем её назначение.
// Затемнение делает не отдельный оверлей, а огромная тень самого окна (box-shadow на 9999px):
// так подсветка и затемнение — один элемент, и перемещение между шагами анимируется одним transition,
// без рассинхрона слоёв.
const TOUR_STEPS = [
  { sel: "#input", title: "Спросите своими словами",
    text: "Опишите продукцию («требования к чиллерам»), укажите код ОКПД2 или код ТН ВЭД из " +
          "сертификата — его переведу в ОКПД2 по переходному ключу. Код даёт точную привязку к позиции." },
  { sel: "#chat-scroll", title: "Ответ со ссылками на первоисточник", pad: 6,
    text: "Под ответом — источники: позиция приложения, пункт Правил или Приказа №52. Числа в баллах " +
          "и сроки проверяются автоматически, неподтверждённые помечаются. Ответ ИИ — предварительный " +
          "ориентир: решение принимает уполномоченный эксперт ТПП." },
  { sel: "#new-chat", title: "Новый диалог под новую тему",
    text: "Сервис помнит контекст беседы и удерживает код, о котором идёт речь. Для другой продукции " +
          "начните новый диалог — так контексты не смешаются." },
  { sel: "#history", title: "История и экспорт", pad: 4,
    text: "Диалоги сохраняются: к ним можно вернуться, найти нужный поиском, а по «⋮» — выгрузить " +
          "или удалить. В любой выгрузке остаётся пометка о предварительном характере ответов." },
  { sel: "#fb-open", title: "Оценка — главный способ улучшить сервис",
    text: "Под каждым ответом есть «Оценить ответ» и «отметить ошибку». Разбор ошибок экспертами " +
          "попадает в базу проверенных случаев, и сервис начинает отвечать верно — без дообучения модели." },
  { sel: "#help-group", title: "Справка всегда рядом",
    text: "Здесь — знакомство с интерфейсом, справочный центр с частыми вопросами, условия " +
          "использования и политика конфиденциальности." },
];

(function initTour() {
  const root = document.getElementById("tour");
  if (!root) return;
  const spot = document.getElementById("tour-spot");
  const card = document.getElementById("tour-card");
  const titleEl = document.getElementById("tour-title");
  const textEl = document.getElementById("tour-text");
  const stepNo = document.getElementById("tour-step-no");
  const dotsWrap = document.getElementById("tour-dots");
  const prevBtn = document.getElementById("tour-prev");
  const nextBtn = document.getElementById("tour-next");
  const skipBtn = document.getElementById("tour-skip");

  let steps = [];
  let i = 0;

  // Шаг без видимой цели пропускаем: сайдбар скрыт на узком экране, «Логи диалогов» есть только у
  // админа — подсвечивать пустоту хуже, чем не показать шаг вовсе.
  const visible = (s) => {
    const t = document.querySelector(s.sel);
    return t && t.getBoundingClientRect().width > 0 && t.getBoundingClientRect().height > 0;
  };

  // Карточка встаёт в свободную сторону от подсветки, а не в центр экрана: поле ввода живёт внизу,
  // и центрированная карточка накрывала ровно ту область, которую подсвечивает.
  function placeCard(r) {
    const gap = 20;
    const h = card.offsetHeight;
    const vh = window.innerHeight;
    const above = r.top - gap;                 // сколько места над подсветкой
    const below = vh - r.bottom - gap;         // и под ней
    let top;
    if (above >= h) top = r.top - gap - h;             // цель внизу → карточка выше неё
    else if (below >= h) top = r.bottom + gap;         // цель вверху → карточка ниже
    else top = above >= below ? gap : Math.max(gap, vh - h - gap);  // не помещается — в большую часть
    card.style.top = Math.max(gap, Math.min(top, vh - h - gap)) + "px";
  }

  function place() {
    const step = steps[i];
    const target = document.querySelector(step.sel);
    if (!target) { next(); return; }
    const r = target.getBoundingClientRect();
    const pad = step.pad === undefined ? 8 : step.pad;
    spot.style.top = (r.top - pad) + "px";
    spot.style.left = (r.left - pad) + "px";
    spot.style.width = (r.width + pad * 2) + "px";
    spot.style.height = (r.height + pad * 2) + "px";
    titleEl.textContent = step.title;
    textEl.textContent = step.text;
    stepNo.textContent = "Шаг " + (i + 1) + " из " + steps.length;
    prevBtn.style.visibility = i === 0 ? "hidden" : "visible";
    nextBtn.textContent = i === steps.length - 1 ? "Понятно" : "Далее";
    Array.from(dotsWrap.children).forEach((d, k) => d.classList.toggle("on", k === i));
    card.classList.remove("swap");
    void card.offsetWidth;   // рестарт анимации появления текста
    card.classList.add("swap");
    placeCard(r);
  }

  function go(k) { i = Math.max(0, Math.min(steps.length - 1, k)); place(); }
  function next() { if (i >= steps.length - 1) close(); else go(i + 1); }

  function open() {
    steps = TOUR_STEPS.filter(visible);
    if (!steps.length) return;
    dotsWrap.innerHTML = "";
    steps.forEach((_, k) => {
      const d = document.createElement("span");
      d.className = "tour-dot";
      d.addEventListener("click", () => go(k));
      dotsWrap.appendChild(d);
    });
    i = 0;
    root.classList.remove("hidden");
    root.setAttribute("aria-hidden", "false");
    document.body.classList.add("tour-on");
    place();
    nextBtn.focus();
  }

  function close() {
    root.classList.add("hidden");
    root.setAttribute("aria-hidden", "true");
    document.body.classList.remove("tour-on");
    try { localStorage.setItem("onboarding719Seen", "1"); } catch (e) {}
  }

  prevBtn.addEventListener("click", () => go(i - 1));
  nextBtn.addEventListener("click", next);
  if (skipBtn) skipBtn.addEventListener("click", close);
  document.addEventListener("keydown", (e) => {
    if (root.classList.contains("hidden")) return;
    if (e.key === "Escape") close();
    if (e.key === "ArrowRight") next();
    if (e.key === "ArrowLeft") go(i - 1);
  });
  // окно меняет размер / страница скроллится — подсветка обязана оставаться на цели
  const follow = () => { if (!root.classList.contains("hidden")) place(); };
  window.addEventListener("resize", follow);
  window.addEventListener("scroll", follow, true);

  const openBtn = document.getElementById("ob-open");
  if (openBtn) openBtn.addEventListener("click", open);

  let seen = false;
  try { seen = localStorage.getItem("onboarding719Seen") === "1"; } catch (e) {}
  if (!seen) setTimeout(open, 400);  // даём интерфейсу отрисоваться, иначе позиции «прыгают»
})();

// Меню «Справка» в сайдбаре. Раскрытие по наведению делает CSS; здесь — клик и клавиатура:
// на тач-экране hover не существует, и без этого до пунктов нельзя добраться вовсе.
(function initHelpMenu() {
  const group = document.getElementById("help-group");
  const toggle = document.getElementById("help-toggle");
  if (!group || !toggle) return;
  const set = (on) => {
    group.classList.toggle("open", on);
    toggle.setAttribute("aria-expanded", on ? "true" : "false");
  };
  toggle.addEventListener("click", (e) => {
    e.stopPropagation();
    set(!group.classList.contains("open"));
  });
  document.addEventListener("click", (e) => { if (!group.contains(e.target)) set(false); });
  group.addEventListener("keydown", (e) => { if (e.key === "Escape") { set(false); toggle.focus(); } });
})();

// Мобильная «шторка»-сайдбар: гамбургер открывает, бэкдроп / переход по пункту — закрывает.
(function initDrawer() {
  const sidebar = document.querySelector(".sidebar");
  const backdrop = document.getElementById("drawer-backdrop");
  const btn = document.getElementById("menu-btn");
  if (!sidebar || !backdrop || !btn) return;
  const open = () => { sidebar.classList.add("open"); backdrop.classList.add("open"); };
  const close = () => { sidebar.classList.remove("open"); backdrop.classList.remove("open"); };
  btn.addEventListener("click", open);
  backdrop.addEventListener("click", close);
  sidebar.addEventListener("click", (e) => {   // переход по пункту закрывает шторку на мобиле
    if (window.innerWidth > 760) return;
    if (e.target.closest("#new-chat, #history, a.nav-item, .logout")) close();
  });
})();

// --- T16: навигация по вопросам ВНУТРИ текущего чата (jump-to-question) ---
// В длинной беседе (несколько вопросов) приходилось скролить, чтобы вернуться к прежнему запросу
// (жалоба экспертов). Плавающая кнопка открывает список вопросов чата → клик прокручивает к нему.
const jumpBtn = document.getElementById("jump-btn");
let jumpMenu = null;
function closeJump() {
  if (jumpMenu) { jumpMenu.remove(); jumpMenu = null; document.removeEventListener("click", closeJump); }
}
function updateJumpBtn() {
  if (!jumpBtn) return;
  const n = messages.querySelectorAll(".msg.user").length;
  jumpBtn.classList.toggle("hidden", n < 3); // показываем, только когда есть что листать
  if (n < 3) closeJump();
}
function openJumpMenu() {
  closeJump();
  const qs = Array.from(messages.querySelectorAll(".msg.user"));
  if (!qs.length) return;
  jumpMenu = document.createElement("div");
  jumpMenu.className = "jump-menu";
  const head = document.createElement("div");
  head.className = "jump-head";
  head.textContent = "Вопросы в этом чате";
  jumpMenu.appendChild(head);
  qs.forEach((q, i) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "jump-item";
    const txt = (q.textContent || "").trim();
    b.textContent = (i + 1) + ". " + (txt.length > 70 ? txt.slice(0, 70) + "…" : txt);
    b.title = txt;
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      q.scrollIntoView({ behavior: "smooth", block: "center" });
      q.classList.add("q-flash");
      setTimeout(() => q.classList.remove("q-flash"), 1200);
      closeJump();
    });
    jumpMenu.appendChild(b);
  });
  document.body.appendChild(jumpMenu);
  const r = jumpBtn.getBoundingClientRect();
  jumpMenu.style.left = Math.max(12, r.right - jumpMenu.offsetWidth) + "px";   // выровнять по правому краю кнопки
  jumpMenu.style.top = Math.max(12, r.top - jumpMenu.offsetHeight - 8) + "px"; // над кнопкой
  setTimeout(() => document.addEventListener("click", closeJump), 0);
}
if (jumpBtn) jumpBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  if (jumpMenu) closeJump(); else openJumpMenu();
});
