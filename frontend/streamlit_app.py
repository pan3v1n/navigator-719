"""Streamlit MVP навигатора ПП №719 (Курская ТПП).

Поле ввода описания продукции (+ код ОКПД2) → применимая позиция приложения к 719,
ключевые требования, источники и чек-лист документов. Вызывает navigate() напрямую
(in-process), отдельный API-сервер для MVP не нужен.

СТАТУС: ранний MVP, вытеснен веб-чатом 1.0 (`main.py` + `app/web/`). Оставлен как запасной путь и
демонстрация движка без auth/БД. В рантайме сервиса НЕ участвует, `.dockerignore` исключает
`frontend/` из образа, поэтому streamlit вынесен из `requirements.txt` в `requirements-dev.txt`.

Запуск (нужен поднятый Qdrant и DEEPSEEK_API_KEY в .env):
  .venv/Scripts/pip install -r requirements-dev.txt
  .venv/Scripts/streamlit run frontend/streamlit_app.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# доступ к пакету app при запуске из Streamlit
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.tools.navigator import Navigation, navigate  # noqa: E402

st.set_page_config(page_title="Навигатор ПП №719", page_icon="🧭", layout="centered")


@st.cache_data(show_spinner=False)
def _run(query: str, okpd2: str | None, limit: int) -> Navigation:
    return navigate(query, okpd2=okpd2 or None, limit=limit)


st.title("🧭 Навигатор ПП РФ №719")
st.caption(f"{settings.ORG_NAME} · подтверждение производства промышленной продукции в РФ")

with st.form("nav"):
    query = st.text_area(
        "Опишите продукцию или задайте вопрос",
        placeholder="например: производим прицепы и полуприцепы для легковых автомобилей",
        height=110,
    )
    col1, col2 = st.columns([2, 1])
    with col1:
        okpd2 = st.text_input("Код ОКПД2 (если известен)", placeholder="29.20.23")
    with col2:
        limit = st.slider("Позиций", 1, 10, 5)
    submitted = st.form_submit_button("🔎 Найти", use_container_width=True)

if submitted:
    if not query or len(query.strip()) < 2:
        st.warning("Введите описание продукции или вопрос.")
        st.stop()
    try:
        with st.spinner("Ищу по базе знаний и формирую анализ…"):
            nav = _run(query.strip(), okpd2.strip(), limit)
    except Exception as e:  # noqa: BLE001
        st.error(f"Ошибка: {e}\n\nПроверьте, что поднят Qdrant и задан DEEPSEEK_API_KEY в .env.")
        st.stop()

    if nav.okpd2_used:
        st.info(f"Использован код ОКПД2: **{nav.okpd2_used}**")

    st.subheader("Анализ")
    st.markdown(nav.answer)

    if nav.sources:
        st.subheader("Найденные позиции 719")
        for i, h in enumerate(nav.sources, 1):
            mark = "✅ совпадение по коду" if h.okpd2_match else f"score {h.score:.2f}"
            with st.expander(f"{i}. {h.product_name[:80]} — {mark}"):
                if h.section_title:
                    st.caption(f"Раздел: {h.section_title}")
                if h.okpd2_codes:
                    st.write("**ОКПД2:** " + ", ".join(h.okpd2_codes))
                if h.min_threshold:
                    st.write(f"**Порог:** {h.min_threshold}")
                ops = [
                    (o.get("text", ""), o.get("points"))
                    for b in h.requirement_blocks
                    for o in (b.get("operations") or [])
                ]
                if ops:
                    st.write("**Ключевые операции:**")
                    for text, pts in ops[:15]:
                        p = f" — {pts} балл." if pts is not None else " — балл зависит от условий"
                        st.write(f"- {text}{p}")
                    if len(ops) > 15:
                        st.caption(f"… ещё {len(ops) - 15} операций")
                if h.source_anchor:
                    st.caption(h.source_anchor)

    if nav.checklist:
        st.subheader("📋 Чек-лист документов")
        for item in nav.checklist:
            st.markdown(f"- {item}")

    st.warning(nav.disclaimer)
