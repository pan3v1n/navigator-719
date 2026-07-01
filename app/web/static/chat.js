// Чат UI 1.0: fetch POST /api/chat → рендер сообщений + источников. Без внешних либ.
const messages = document.getElementById("messages");
const form = document.getElementById("chat-form");
const input = document.getElementById("input");
const send = document.getElementById("send");
let sessionId = null;

function el(cls) {
  const d = document.createElement("div");
  d.className = cls;
  return d;
}

function addMessage(role, text) {
  const wrap = el("msg " + role);
  const bubble = el("bubble");
  bubble.textContent = text; // textContent → без XSS; CSS white-space:pre-wrap сохранит переносы
  wrap.appendChild(bubble);
  messages.appendChild(wrap);
  messages.scrollTop = messages.scrollHeight;
  return wrap;
}

function addSources(wrap, sources) {
  if (!sources || !sources.length) return;
  const det = document.createElement("details");
  det.className = "sources";
  const sum = document.createElement("summary");
  sum.textContent = "Источники (" + sources.length + ")";
  det.appendChild(sum);
  sources.forEach((s, i) => {
    const item = el("src-item");
    const mark = s.okpd2_match ? " ✓ совпадение по коду" : "";
    const codes = (s.okpd2 || []).join(", ");
    let t = "[" + (i + 1) + "] " + s.product_name + " — " + (s.section || "") + mark;
    if (codes) t += " · ОКПД2 " + codes;
    if (s.source_anchor) t += " · " + s.source_anchor;
    item.textContent = t;
    det.appendChild(item);
  });
  wrap.appendChild(det);
}

async function ask(text) {
  addMessage("user", text);
  const thinking = addMessage("assistant", "…думаю");
  send.disabled = true;
  try {
    const r = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, session_id: sessionId }),
    });
    if (r.status === 401) { window.location = "/login"; return; }
    if (!r.ok) {
      thinking.querySelector(".bubble").textContent = "Ошибка: сервис недоступен, повторите запрос.";
      return;
    }
    const data = await r.json();
    sessionId = data.session_id;
    thinking.querySelector(".bubble").textContent = data.answer;
    addSources(thinking, data.sources);
  } catch (e) {
    thinking.querySelector(".bubble").textContent = "Ошибка сети, повторите запрос.";
  } finally {
    send.disabled = false;
    messages.scrollTop = messages.scrollHeight;
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
  input.style.height = input.scrollHeight + "px";
});
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
});

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
