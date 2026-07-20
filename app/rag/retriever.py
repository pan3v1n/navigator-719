"""Гибридный поиск по базе знаний ПП №719 в Qdrant (dense e5 + sparse BM25, RRF).

Возвращает наиболее релевантные позиции приложения для запроса. Если передан код
ОКПД2 — позиции с совпадающим (по иерархии) кодом поднимаются наверх (буст), а при
наличии точных совпадений гарантированно добавляются в выдачу (жёсткая подстраховка).

См. docs/V1_SPEC.md, Шаг 2–3. Используется из app/rag/pipeline.py и API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

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


def _to_hit(p) -> Hit:
    pl = p.payload or {}
    return Hit(
        score=p.score,
        section_roman=pl.get("section_roman", "?"),
        section_title=pl.get("section_title", ""),
        product_name=pl.get("product_name", ""),
        okpd2_codes=pl.get("okpd2_codes") or [],
        min_threshold=pl.get("min_threshold"),
        requirement_blocks=pl.get("requirement_blocks") or [],
        source_anchor=pl.get("source_anchor"),
        payload=pl,
    )


def _hybrid(query: str, limit: int, qfilter=None, collection: str | None = None):
    from qdrant_client import models

    dvec = embed_query(query)
    idx, val = query_vector(query)
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


def search(query: str, okpd2: str | None = None, limit: int = 5, pool: int = 40) -> list[Hit]:
    """Гибридный поиск. При наличии okpd2 — буст совпадающих по коду позиций и
    жёсткая подстраховка (отдельный фильтрованный запрос по префиксам кода)."""
    from qdrant_client import models

    hits = [_to_hit(p) for p in _hybrid(query, pool)]

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
            for p in _hybrid(query, max(limit, 12), qfilter=qfilter):
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


def search_cases(query: str, limit: int = 3) -> list[dict]:
    """Поиск по подтверждённым экспертом кейсам (коллекция verified_cases).

    Возвращает список payload'ов кейсов со score в `_score`. Если коллекции нет или
    она пуста — пустой список (петля кейсов опциональна, база работает и без неё)."""
    client = _client()
    name = settings.QDRANT_CASES_COLLECTION
    try:
        if not client.collection_exists(name):
            return []
    except Exception:  # noqa: BLE001 — Qdrant недоступен: не валим основной поиск
        return []
    points = _hybrid(query, limit, collection=name)
    out: list[dict] = []
    for p in points:
        pl = dict(p.payload or {})
        pl["_score"] = p.score
        out.append(pl)
    return out


def search_rules(query: str, limit: int = 6) -> list[dict]:
    """Гибрид-поиск по корпусу «Правила ведения реестра» (отдельная коллекция pp719_rules).

    Возвращает payload'ы пунктов Правил со score в `_score`. Если коллекции нет или Qdrant
    недоступен — пустой список (процедурный путь тогда честно деферится, см. pipeline).
    БЕЗ ОКПД2-буста и реранкера (то и другое заточено под товарные записи, не под прозу норм)."""
    client = _client()
    name = settings.QDRANT_RULES_COLLECTION
    try:
        if not client.collection_exists(name):
            return []
    except Exception:  # noqa: BLE001 — Qdrant недоступен: не валим ответ, деферим
        return []
    out: list[dict] = []
    for p in _hybrid(query, limit, collection=name):
        pl = dict(p.payload or {})
        pl["_score"] = p.score
        out.append(pl)
    return out
