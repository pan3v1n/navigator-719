"""Гибридный поиск по базе знаний ПП №719 в Qdrant (dense e5 + sparse BM25, RRF).

Возвращает наиболее релевантные позиции приложения для запроса. Если передан код
ОКПД2 — позиции с совпадающим (по иерархии) кодом поднимаются наверх (буст), а при
наличии точных совпадений гарантированно добавляются в выдачу (жёсткая подстраховка).

См. docs/V1_SPEC.md, Шаг 2–3. Используется из app/rag/pipeline.py и API.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from loguru import logger

from app.core.config import settings
from app.rag.embeddings import embed_query
from app.rag.sparse import query_vector

DENSE = "dense"
SPARSE = "bm25"


@dataclass
class Hit:
    score: float
    section_roman: str
    section_title: str
    product_name: str
    okpd2_codes: list[str]
    min_threshold: str | None
    requirement_blocks: list[dict]
    source_anchor: str | None
    okpd2_match: bool = False
    payload: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# ОКПД2: иерархическое сопоставление кодов
# --------------------------------------------------------------------------- #
def _segments(code: str) -> list[str]:
    return [s for s in code.strip().replace(" ", "").split(".") if s]


def okpd2_match(rec_codes: list[str], query_code: str) -> bool:
    """True, если код записи и код запроса лежат на одной ветке ОКПД2 (один — префикс
    другого посегментно). Так «29.20.23.110» матчит группу «29.20.23», а «29.20» —
    более частную «29.20.23»."""
    q = _segments(query_code)
    if not q:
        return False
    for rc in rec_codes:
        r = _segments(rc)
        n = min(len(q), len(r))
        if n and q[:n] == r[:n]:
            return True
    return False


def _prefixes(code: str) -> list[str]:
    segs = _segments(code)
    return [".".join(segs[: i + 1]) for i in range(len(segs))]


# --------------------------------------------------------------------------- #
# Поиск
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def _client():
    from qdrant_client import QdrantClient

    return QdrantClient(url=settings.QDRANT_URL, timeout=30)


def _clean_section_title(title: str | None) -> str:
    """Название раздела без задвоения (R28).

    В части записей `section_title` содержит название дважды через перенос строки
    («Продукция судостроения\\n\\nXVIII. Продукция судостроения») — дефект нарезки заголовка,
    исправленный в `structure_kb.section_title`. Здесь подчищаем ЕЩЁ РАЗ, в рантайме: индекс
    Qdrant переиндексируется не сразу, а искажённое название уходит эксперту в ответ."""
    first = (title or "").strip().split("\n")[0].lstrip("#").strip()
    left, dot, right = first.partition(".")
    if dot and left.strip().isupper() and left.strip("IVXLC ") == "":
        first = right.strip()
    return first


def _to_hit(p) -> Hit:
    pl = p.payload or {}
    return Hit(
        score=p.score,
        section_roman=pl.get("section_roman", "?"),
        section_title=_clean_section_title(pl.get("section_title")),
        product_name=pl.get("product_name", ""),
        okpd2_codes=pl.get("okpd2_codes") or [],
        min_threshold=pl.get("min_threshold"),
        requirement_blocks=pl.get("requirement_blocks") or [],
        source_anchor=pl.get("source_anchor"),
        payload=pl,
    )


def _hybrid(query: str, limit: int, qfilter=None, collection: str | None = None,
            qvec: list[float] | None = None):
    """Гибрид dense+sparse с RRF. `qvec` — заранее посчитанный dense-вектор запроса: на один
    вопрос к Qdrant уходит несколько обращений (позиции + подстраховка по коду + кейсы + guard),
    и e5-large незачем прогонять на каждое. Не передан — считаем сами (обратная совместимость:
    eval-скрипты зовут search/search_cases напрямую)."""
    from qdrant_client import models

    dvec = qvec if qvec is not None else embed_query(query)
    idx, val = query_vector(query)  # sparse дёшев (mmh3+Snowball локально) — переиспользовать нечего
    res = _client().query_points(
        collection_name=collection or settings.QDRANT_COLLECTION,
        prefetch=[
            models.Prefetch(query=dvec, using=DENSE, limit=max(limit, 20), filter=qfilter),
            models.Prefetch(
                query=models.SparseVector(indices=idx, values=val),
                using=SPARSE,
                limit=max(limit, 20),
                filter=qfilter,
            ),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=limit,
        with_payload=True,
    )
    return res.points


def search(query: str, okpd2: str | None = None, limit: int = 5, pool: int = 40,
           qvec: list[float] | None = None) -> list[Hit]:
    """Гибридный поиск. При наличии okpd2 — буст совпадающих по коду позиций и
    жёсткая подстраховка (отдельный фильтрованный запрос по префиксам кода).

    `qvec` — заранее посчитанный вектор запроса (см. `_hybrid`). Если не передан, считаем ОДИН
    раз здесь и переиспользуем в подстраховке по коду — раньше второй запрос эмбеддил тот же
    текст заново."""
    from qdrant_client import models

    if qvec is None:
        qvec = embed_query(query)
    hits = [_to_hit(p) for p in _hybrid(query, pool, qvec=qvec)]

    if okpd2:
        for h in hits:
            h.okpd2_match = okpd2_match(h.okpd2_codes, okpd2)

        # Ветку кода (родители + дети) подтягиваем ВСЕГДА, а не только при отсутствии совпадений:
        # пул мог включить родителя (26.51), но пропустить дочерние позиции (26.51.52.120). Две ветви
        # OR: РОДИТЕЛИ — код записи равен префиксу запроса (okpd2_codes ∋ один из префиксов); ДЕТИ/сам —
        # префикс-набор записи содержит код запроса (okpd2_prefixes ∋ okpd2), т.е. класс-код «26.51.52»
        # находит «26.51.52.120» (T5, поиск по частичному коду).
        prefixes = _prefixes(okpd2)
        if prefixes:
            qfilter = models.Filter(should=[
                models.FieldCondition(key="okpd2_codes", match=models.MatchAny(any=prefixes)),
                models.FieldCondition(key="okpd2_prefixes", match=models.MatchAny(any=[okpd2.strip()])),
            ])
            seen = {h.source_anchor for h in hits}
            for p in _hybrid(query, max(limit, 12), qfilter=qfilter, qvec=qvec):
                h = _to_hit(p)
                if h.source_anchor not in seen:
                    h.okpd2_match = True
                    seen.add(h.source_anchor)
                    hits.append(h)

        # Стабильная пересортировка: совпавшие по коду — выше, далее по score.
        hits.sort(key=lambda h: (not h.okpd2_match, -h.score))

    return hits[:limit]


def dense_top1(query: str, qvec: list[float] | None = None) -> float:
    """Косинусное сходство top-1 по ЧИСТО dense-поиску (e5). Сигнал релевантности для
    out-of-scope guard: на продукции вне 719 оно стабильно ниже, чем на профильной
    (калибровка — `docs/eval_report.md`). `qvec` — заранее посчитанный вектор запроса
    (чтобы не эмбедить дважды), иначе считаем сами."""
    dvec = qvec if qvec is not None else embed_query(query)
    res = _client().query_points(
        collection_name=settings.QDRANT_COLLECTION,
        query=dvec,
        using=DENSE,
        limit=1,
        with_payload=False,
    )
    return float(res.points[0].score) if res.points else 0.0


# Код ОКПД2 в тексте запроса. Дубль регулярки из app/tools/navigator, чтобы retriever
# (нижний слой) не тянул зависимость от tools (верхний) — иначе импорт закольцуется.
_CODE_IN_TEXT = re.compile(r"\b\d{2}\.\d{2}(?:\.\d+)*\b")


def _case_code_hit(query: str, payload: dict) -> bool:
    """Назван ли в запросе код ОКПД2, попадающий в ветку кода этого кейса.

    Зачем отдельный путь мимо dense-порога: код — детерминированный сигнал, и он
    АВТОРИТЕТНЕЕ семантики (тот же принцип, что у товарного ОКПД2-буста). Замер показал,
    что без этого пути порог срезает ровно те кейсы, ради которых заводился лексический
    канал: «что означает код 27.40.33.130» давал dense 0.847 и отсекался, хотя код в
    запросе совпадает с кодом кейса буквально."""
    code = payload.get("okpd2")
    if not code:
        return False
    return any(okpd2_match([str(code)], m) or okpd2_match([m], str(code))
               for m in _CODE_IN_TEXT.findall(query or ""))


def _case_dense_scores(qvec: list[float], probe: int) -> dict:
    """id точки → ЧИСТЫЙ dense-косинус по коллекции кейсов (для порога релевантности, R30).

    Отдельный лёгкий запрос: фьюжн-score из `_hybrid` мерит РАНГ, а не близость, и для отсечки
    непригоден. Коллекция кейсов мала (десятки записей), поэтому проба с запасом дешева."""
    res = _client().query_points(
        collection_name=settings.QDRANT_CASES_COLLECTION,
        query=qvec, using=DENSE, limit=probe, with_payload=False,
    )
    return {p.id: float(p.score) for p in res.points}


def search_cases(query: str, limit: int = 3, qvec: list[float] | None = None) -> list[dict]:
    """Поиск по подтверждённым экспертом кейсам (коллекция verified_cases).

    Возвращает payload'ы кейсов со score в `_score` и dense-косинусом в `_dense`. Если коллекции
    нет, она пуста ИЛИ ни один кейс не прошёл порог релевантности — пустой список (петля кейсов
    опциональна, база работает и без неё). `qvec` — тот же вектор запроса, что и у товарного
    поиска (см. `_hybrid`).

    ПОРОГ РЕЛЕВАНТНОСТИ (R30) — почему он здесь обязателен. Кейс уходит в контекст с ВЫСШИМ
    приоритетом (правило 1а промпта навигатора), выше первоисточника. Поэтому нерелевантный
    кейс — не безобидный шум, а правдоподобная дезинформация: на «оказываем юридические услуги»
    подтягивался кейс про НИОКР грузового автотранспорта, а на веб-студию — кейс «нет в
    приложении → идите путём СТ-1». Плюс наличие кейса гасит out-of-scope-гард в пайплайне,
    так что два предохранителя выключали друг друга.

    Отсекаем по ЧИСТОМУ dense-косинусу, а НЕ по фьюжн-score: последний — результат RRF-слияния
    рангов, на маленькой коллекции что-то всегда оказывается первым с нормированным 1.0, и
    абсолютной релевантности в нём нет. На dense-косинусе полоса чистая (замер 12.08.2026,
    12 кейсов): уместные — от 0.839, посторонние — до 0.803. Порог `CASE_RELEVANCE_MIN`.
    Ранжирование при этом остаётся гибридным — лексический канал нужен, чтобы кейс находился
    по коду ОКПД2 и точным терминам.

    Сам запрос под try: кейсы — НЕОБЯЗАТЕЛЬНОЕ обогащение контекста, их сбой не должен
    ронять уже найденный товарный ответ (раньше исключение из `_hybrid` улетало наверх)."""
    name = settings.QDRANT_CASES_COLLECTION
    try:
        client = _client()
        if not client.collection_exists(name):
            return []
        dvec = qvec if qvec is not None else embed_query(query)
        points = _hybrid(query, limit, collection=name, qvec=dvec)
        if not points:
            return []
        # проба с запасом: гибрид мог поднять запись, которой нет в dense-топе того же размера
        dense = _case_dense_scores(dvec, probe=max(limit * 8, 64))
    except Exception as e:  # noqa: BLE001 — Qdrant недоступен: не валим основной поиск
        # но НЕ молча: беззвучный отказ здесь выключает всю петлю обучения, и снаружи это
        # неотличимо от «подходящих кейсов не нашлось». На таком молчании проект уже горел
        # (classifiers/*.tsv в T9, POSIX-путь graphify).
        logger.warning("петля кейсов недоступна, отвечаем без неё: {}: {}", type(e).__name__, e)
        return []
    out: list[dict] = []
    for p in points:
        pl = dict(p.payload or {})
        cos = dense.get(p.id)
        by_code = _case_code_hit(query, pl)
        if not by_code and (cos is None or cos < settings.CASE_RELEVANCE_MIN):
            continue  # не показать лучше, чем показать чужое
        pl["_score"] = p.score
        pl["_dense"] = cos
        pl["_by_code"] = by_code
        out.append(pl)
    return out


def search_rules(query: str, limit: int = 6) -> list[dict]:
    """Гибрид-поиск по корпусу «Правила ведения реестра» (отдельная коллекция pp719_rules).

    Возвращает payload'ы пунктов Правил со score в `_score`. Если коллекции нет или Qdrant
    недоступен — пустой список (процедурный путь тогда честно деферится, см. pipeline).
    БЕЗ ОКПД2-буста и реранкера (то и другое заточено под товарные записи, не под прозу норм).

    Отказоустойчивость (R4): САМ ЗАПРОС тоже под try — иначе падение Qdrant в момент поиска
    улетало исключением наверх и превращалось в 503, а честный процедурный дефер (ради которого
    эта ветка и возвращает пустой список) был недостижим."""
    name = settings.QDRANT_RULES_COLLECTION
    try:
        client = _client()
        if not client.collection_exists(name):
            return []
        points = _hybrid(query, limit, collection=name)
    except Exception:  # noqa: BLE001 — Qdrant недоступен: не валим ответ, деферим
        return []
    out: list[dict] = []
    for p in points:
        pl = dict(p.payload or {})
        pl["_score"] = p.score
        out.append(pl)
    return out
