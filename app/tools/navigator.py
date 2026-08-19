"""Навигатор по ПП №719: текст/код продукции → применимая позиция + требования + чек-лист.

Тонкая обёртка над RAG-пайплайном: вытаскивает код ОКПД2 из текста запроса (если не
передан явно), вызывает pipeline.answer и формирует базовый чек-лист документов по
найденной позиции. Используется из API (POST /navigate) и Streamlit-MVP.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.core.prompts import EXPERT_DISCLAIMER
from app.rag import okpd2_ref
from app.rag.pipeline import _target_hit, answer
from app.rag.retriever import Hit
from app.tools import checklist

# Код ОКПД2 в свободном тексте разбирает `okpd2_ref` — ЕДИНСТВЕННОЕ место на весь проект.
# ⚠ Ревью PR #94: фильтр дат стоял ЗДЕСЬ, в одной копии регулярки из семи, а `_anchor_code`,
# `_HAS_CODE_RE`, `retriever._CODE_IN_TEXT`, `meta`, `procedural`, `translate` разбирали текст
# сами и дату от кода не отличали. И сам фильтр был слабее, чем выглядел: `27.12.2023г` давал
# реальный код `27.12` (кириллическая «г» не граница слова, регулярка откатывалась на префикс),
# `01.07.26` и `09.00` проходили целиком, а `_DATE_LIKE` не отсекал НИЧЕГО сверх проверки длины
# сегмента — то есть комментарий приписывал ему работу, которой тот не делал.


# Перечень документов больше НЕ зашит в код (R25): он строится из раздела 4 Приказа ТПП РФ №52
# со ссылками на номера пунктов — см. app/tools/checklist.py. Прежний хардкод из шести пунктов
# был правдоподобным, но выдуманным: ровно то, за что проект ругает языковую модель.


@dataclass
class Navigation:
    query: str
    okpd2_used: str | None
    answer: str
    # Коды, по которым рантайм строил ответ (`Answer.codes`): их может быть больше одного (`EV8`),
    # и «кто целевой» обязано решаться по ним — одно решение, одно место.
    codes: list[str] = field(default_factory=list)
    sources: list[Hit] = field(default_factory=list)
    checklist: list[str] = field(default_factory=list)
    disclaimer: str = EXPERT_DISCLAIMER


def extract_okpd2(text: str) -> str | None:
    """ПЕРВЫЙ код ОКПД2 из текста (им бустится ретрив). Все коды — `extract_okpd2_all`."""
    codes = extract_okpd2_all(text)
    return codes[0] if codes else None


def extract_okpd2_all(text: str) -> list[str]:
    """ВСЕ коды ОКПД2 из текста, в порядке появления, без повторов.

    ⚠ Зачем отдельная функция (`EV8`, issue #88). `extract_okpd2` — это `re.search`, ОДНО
    совпадение, и на вопросе «сравни требования по нашему коду 28.13.14 и по 26.30.50» вторая
    позиция целевой не становилась: её требования уходили из контекста вместе с требованиями прочих
    кандидатов (`EV7`), и ответ сравнивал одну позицию с пустотой. Дефект был вдвойне неприятен
    тем, что на обратном утверждении («просьба сравнить пришла с кодом, значит обе записи остаются
    целевыми») стояло обоснование ЦЕНЫ самой `EV7` — то есть правка ломала ровно тот вопрос,
    безопасность которого доказывала."""
    return okpd2_ref.extract_codes(text)


def build_checklist(hit: Hit | None) -> list[str]:
    """Перечень документов по найденной позиции — из Приказа ТПП РФ №52 (R25).

    Базовые пункты 4.2.x нужны всегда; условные 4.3.x подбираются по ТЕКСТУ требований позиции
    (права на КД/ТД, сервисный центр, техоперации, процентная доля). У каждого пункта в выводе
    стоит его номер — эксперт сверится с первоисточником, а не поверит на слово."""
    if hit is None:
        return checklist.checklist_for()
    # Текст требований позиции: и операции, и «component»-блоки (там лежат обязательные условия —
    # см. pipeline._hit_operations), иначе условные пункты 4.3.x подбирались бы вслепую.
    parts: list[str] = []
    for b in hit.requirement_blocks or []:
        ops = b.get("operations") or []
        parts.extend((o.get("text") or "") for o in ops)
        if not ops:
            parts.append(b.get("component") or "")
        # D9: условие блока (`note`) — такой же текст требования, как операции. У 12 позиций
        # признак условного пункта Приказа живёт ТОЛЬКО там («процентная доля», «доля массы»,
        # «баллы»), и чек-лист /navigate молча терял п. 4.3.5 / 4.3.6 / 4.3.7 — напр. у
        # «Асфальтоукладчиков» (III) и «Установки для обезвреживания медицинских отходов» (VII).
        parts.append(b.get("note") or "")
    joined = " ".join(p for p in parts if p)
    rtype = hit.payload.get("requirement_type")
    has_points = rtype in ("points", "mixed") or any(
        o.get("points") is not None for b in (hit.requirement_blocks or [])
        for o in (b.get("operations") or [])
    )
    return checklist.checklist_for(joined, has_points=has_points, threshold=hit.min_threshold)


def navigate(query: str, okpd2: str | None = None, limit: int = 5) -> Navigation:
    code = okpd2 or extract_okpd2(query)
    ans = answer(query, okpd2=code, limit=limit)
    # ⚠ Чек-лист — по ЦЕЛЕВОЙ позиции, а не по `hits[0]`. До `EV6` они совпадали, и это было
    # третьим независимым выводом «кто целевой»; с расколотой ячейкой ответ пишется про
    # содержательного сиблинга, а перечень документов собирался бы по строке-квалификатору —
    # без её баллов и порога, то есть без условных пунктов 4.3.x Приказа №52 (их вернула D9).
    # ⚠ Коды берём ИЗ ОТВЕТА, а не свой `code` (ревью PR #94). `code` — один, а рантайм строил
    # ответ по `[effective_okpd2] + extra`, где `effective_okpd2` может прийти из перевода ТН ВЭД
    # или из якоря диалога: на `POST /navigate` без явного `okpd2` здесь было None, `_target_hit`
    # уходил в ветку `matched[0]` и мог назвать ДРУГУЮ запись, чем тело ответа. `Answer.codes`
    # ради того и заведён, чтобы «кто целевой» решалось в одном месте.
    checklist = build_checklist(_target_hit(ans.hits, ans.codes))
    return Navigation(
        query=query,
        okpd2_used=code,
        answer=ans.text,
        codes=list(ans.codes or []),
        sources=ans.hits,
        checklist=checklist,
    )
