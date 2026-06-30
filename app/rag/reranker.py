"""LLM-реранкер top-k кандидатов через DeepSeek (стадия 2 плана улучшения RAG).

Гибрид-поиск надёжно поднимает верный раздел в top-3/5 (recall@3=1.0), но top-1 иногда
занимает семантический сосед (экскаваторы↔тяжмаш, стреловые краны↔судовые). Реранкер просит
DeepSeek переупорядочить кандидатов по СМЫСЛУ запроса. RF-friendly: без скачивания моделей,
один лёгкий вызов.

Принципы безопасности:
  • применять ТОЛЬКО когда нет совпадения по коду ОКПД2 (`okpd2_match`) — при совпадении код
    авторитетнее любого LLM-суждения (гейт в вызывающем коде: pipeline / eval);
  • на ошибке/таймауте/битом JSON возвращаем ИСХОДНЫЙ порядок — реранкер никогда не роняет
    и не теряет выдачу (только переставляет);
  • temperature=0 для воспроизводимости.
"""

from __future__ import annotations

import json

from app.core.config import settings
from app.rag.retriever import Hit

RERANK_SYSTEM = (
    "Ты — ранжировщик позиций нормативной базы (Постановление Правительства РФ №719). "
    "Дан ЗАПРОС о промышленной продукции и пронумерованные КАНДИДАТЫ — позиции приложения. "
    "Верни номера кандидатов в порядке УБЫВАНИЯ смысловой релевантности запросу. Ориентируйся "
    "на ВИД продукции и её назначение, а не на длину описания. Ответ — СТРОГО JSON-объект вида "
    '{"order": [номера]}, включи КАЖДЫЙ номер ровно один раз.'
)


def _candidate_line(i: int, h: Hit) -> str:
    sect = h.section_title or f"Раздел {h.section_roman}"
    codes = ", ".join(h.okpd2_codes) if h.okpd2_codes else "—"
    return f"[{i}] раздел: {sect}; продукция: {h.product_name}; ОКПД2: {codes}"


def rerank(query: str, hits: list[Hit], timeout: float = 20.0) -> list[Hit]:
    """Переупорядочивает hits по релевантности запросу через DeepSeek.

    Возвращает НОВЫЙ список из тех же Hit (без потерь). При любой ошибке — исходный порядок.
    """
    if len(hits) < 2:
        return hits
    cands = "\n".join(_candidate_line(i, h) for i, h in enumerate(hits))
    user = f"ЗАПРОС: {query}\n\nКАНДИДАТЫ:\n{cands}"
    try:
        from app.rag.pipeline import _client  # лениво — разрываем цикл импорта

        resp = _client().chat.completions.create(
            model=settings.DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": RERANK_SYSTEM},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            response_format={"type": "json_object"},
            timeout=timeout,
        )
        order = json.loads(resp.choices[0].message.content or "{}").get("order", [])
    except Exception:  # noqa: BLE001 — сеть/парсинг упали: не роняем выдачу
        return hits

    out: list[Hit] = []
    seen: set[int] = set()
    for idx in order:
        if isinstance(idx, int) and 0 <= idx < len(hits) and idx not in seen:
            seen.add(idx)
            out.append(hits[idx])
    for i, h in enumerate(hits):  # недостающие/битые номера — в исходном порядке в хвост
        if i not in seen:
            out.append(h)
    return out
