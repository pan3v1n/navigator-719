"""Эксперименты по тюнингу ретрива (стадия 3) — БЕЗ правки production-ретривера.

Прогоняет golden set через несколько вариантов получения/слияния кандидатов и
сравнивает recall@1/3 + MRR по разделу. Цель — найти конфигурацию, которая
поднимает recall@1 (база 0.86), не роняя recall@3. Все варианты офлайн, без
скачивания моделей (dense e5 + sparse BM25 уже в Qdrant).

Запуск (нужен поднятый Qdrant с pp719):
  PYTHONUTF8=1 .venv\\Scripts\\python scripts\\eval_experiments.py
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings  # noqa: E402
from app.rag.embeddings import embed_query  # noqa: E402
from app.rag.retriever import _client  # noqa: E402
from app.rag.sparse import query_vector  # noqa: E402

GOLDEN = ROOT / "scripts" / "eval_golden.json"
DENSE = "dense"
SPARSE = "bm25"


# --------------------------------------------------------------------------- #
# Размеры разделов (для штрафа за доминирование большого раздела)
# --------------------------------------------------------------------------- #
def section_sizes() -> dict[str, int]:
    from qdrant_client import models

    client = _client()
    sizes: dict[str, int] = {}
    # перебираем известные римские из golden + добираем скроллом фактические
    offset = None
    while True:
        pts, offset = client.scroll(
            collection_name=settings.QDRANT_COLLECTION,
            limit=1000,
            offset=offset,
            with_payload=["section_roman"],
            with_vectors=False,
        )
        for p in pts:
            sec = (p.payload or {}).get("section_roman", "?")
            sizes[sec] = sizes.get(sec, 0) + 1
        if offset is None:
            break
    return sizes


# --------------------------------------------------------------------------- #
# Получение «сырых» кандидатов каждого канала (dense / sparse) с их score
# --------------------------------------------------------------------------- #
def raw_channels(query: str, depth: int = 60):
    """Возвращает (dense_pts, sparse_pts) — списки точек с .score и payload."""
    from qdrant_client import models

    client = _client()
    dvec = embed_query(query)
    idx, val = query_vector(query)
    dense = client.query_points(
        collection_name=settings.QDRANT_COLLECTION,
        query=dvec, using=DENSE, limit=depth, with_payload=True,
    ).points
    sparse = client.query_points(
        collection_name=settings.QDRANT_COLLECTION,
        query=models.SparseVector(indices=idx, values=val),
        using=SPARSE, limit=depth, with_payload=True,
    ).points
    return dense, sparse


def _sec(p) -> str:
    return (p.payload or {}).get("section_roman", "?")


# --------------------------------------------------------------------------- #
# Стратегии слияния → упорядоченный список РАЗДЕЛОВ
# --------------------------------------------------------------------------- #
def rrf_sections(dense, sparse, k: int = 60, w_dense: float = 1.0, w_sparse: float = 1.0):
    """Классический RRF на уровне записей, затем первый ранг раздела.
    Возвращает список разделов в порядке появления (по best-record-RRF)."""
    rrf: dict[str, float] = defaultdict(float)  # по записи (anchor)
    rec_sec: dict[str, str] = {}
    for rank, p in enumerate(dense, 1):
        a = (p.payload or {}).get("source_anchor") or id(p)
        rrf[a] += w_dense / (k + rank)
        rec_sec[a] = _sec(p)
    for rank, p in enumerate(sparse, 1):
        a = (p.payload or {}).get("source_anchor") or id(p)
        rrf[a] += w_sparse / (k + rank)
        rec_sec[a] = _sec(p)
    order = sorted(rrf, key=lambda a: -rrf[a])
    return _dedup([rec_sec[a] for a in order])


def section_rrf(dense, sparse, k: int = 60, sizes=None, penalty: float = 0.0):
    """Агрегируем RRF-вклад ПО РАЗДЕЛАМ (голосование записей), опционально со штрафом
    за размер раздела: score / (size ** penalty). penalty=0 → чистое голосование."""
    agg: dict[str, float] = defaultdict(float)
    for rank, p in enumerate(dense, 1):
        agg[_sec(p)] += 1.0 / (k + rank)
    for rank, p in enumerate(sparse, 1):
        agg[_sec(p)] += 1.0 / (k + rank)
    if penalty and sizes:
        for s in list(agg):
            agg[s] /= max(sizes.get(s, 1), 1) ** penalty
    return sorted(agg, key=lambda s: -agg[s])


def _dedup(seq):
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x); out.append(x)
    return out


# --------------------------------------------------------------------------- #
# Замер
# --------------------------------------------------------------------------- #
def rank_of(sections: list[str], expected: str) -> int | None:
    for i, s in enumerate(sections, 1):
        if s == expected:
            return i
    return None


def metrics(ranks: list[int | None]) -> tuple[float, float, float]:
    n = len(ranks)
    r1 = sum(1 for r in ranks if r and r <= 1) / n
    r3 = sum(1 for r in ranks if r and r <= 3) / n
    mrr = statistics.mean((1.0 / r if r else 0.0) for r in ranks)
    return r1, r3, mrr


def main() -> None:
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    cases = [c for c in data["cases"] if c["in_scope"]]

    print("Считаю размеры разделов…")
    sizes = section_sizes()
    big = sorted(sizes.items(), key=lambda kv: -kv[1])[:5]
    print("Топ-5 разделов по размеру:", ", ".join(f"{s}={n}" for s, n in big))
    print(f"Прогон {len(cases)} in-scope кейсов через варианты…\n")

    variants = {
        "baseline RRF (record)": lambda d, s: rrf_sections(d, s),
        "RRF sparse×2": lambda d, s: rrf_sections(d, s, w_sparse=2.0),
        "RRF dense×2": lambda d, s: rrf_sections(d, s, w_dense=2.0),
        "section-vote (no penalty)": lambda d, s: section_rrf(d, s),
        "section-vote penalty=0.3": lambda d, s: section_rrf(d, s, sizes=sizes, penalty=0.3),
        "section-vote penalty=0.5": lambda d, s: section_rrf(d, s, sizes=sizes, penalty=0.5),
        "section-vote penalty=0.7": lambda d, s: section_rrf(d, s, sizes=sizes, penalty=0.7),
    }
    results: dict[str, list] = {name: [] for name in variants}
    miss_track: dict[str, list] = {name: [] for name in variants}

    for c in cases:
        d, s = raw_channels(c["query"], depth=60)
        exp = c["expected_section"]
        for name, fn in variants.items():
            secs = fn(d, s)
            r = rank_of(secs, exp)
            results[name].append(r)
            if not r or r > 1:
                miss_track[name].append(c["id"])

    print(f"{'вариант':<28} {'recall@1':>9} {'recall@3':>9} {'MRR':>7}")
    print("-" * 60)
    for name in variants:
        r1, r3, mrr = metrics(results[name])
        print(f"{name:<28} {r1:>9.3f} {r3:>9.3f} {mrr:>7.3f}")
    print()
    for name in variants:
        print(f"промахи@1 [{name}]: {miss_track[name]}")


if __name__ == "__main__":
    main()
