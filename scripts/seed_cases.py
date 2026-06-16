"""Загрузка/переиндексация кейсов эксперта ТПП в коллекцию Qdrant `verified_cases`.

Кейсы лежат в knowledge_base/cases/*.json (файлы с префиксом `_` пропускаются — шаблоны).
Коллекция гибридная (dense e5 + sparse BM25), как основная база. Пересоздаётся целиком —
кейсов немного, кэш эмбеддингов не нужен.

Запуск (нужен поднятый Qdrant; DEEPSEEK не требуется — только локальный эмбеддер):
  .venv/Scripts/python.exe scripts/seed_cases.py
  .venv/Scripts/python.exe scripts/seed_cases.py --query "прицепы для легковых"  # проверить поиск
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings  # noqa: E402
from app.rag.embeddings import embed_passages  # noqa: E402
from app.rag.sparse import doc_length, document_vector  # noqa: E402

CASES_DIR = ROOT / "knowledge_base" / "cases"
DENSE = "dense"
SPARSE = "bm25"


def build_text(case: dict) -> str:
    parts = [
        case.get("query") or "",
        case.get("product_name") or "",
        case.get("expert_answer") or "",
        f"ОКПД2: {case['okpd2']}" if case.get("okpd2") else "",
    ]
    return "\n".join(p for p in parts if p)


def load_cases() -> list[dict]:
    cases: list[dict] = []
    for f in sorted(CASES_DIR.glob("*.json")):
        if f.name.startswith("_"):
            continue
        data = json.loads(f.read_text(encoding="utf-8"))
        items = data if isinstance(data, list) else [data]
        for it in items:
            if not it.get("query") or not it.get("expert_answer"):
                print(f"  ⚠ пропуск (нет query/expert_answer): {f.name}")
                continue
            it["_file"] = f.name
            cases.append(it)
    return cases


def make_client():
    from qdrant_client import QdrantClient

    return QdrantClient(url=settings.QDRANT_URL, timeout=30)


def recreate_collection(client) -> None:
    from qdrant_client import models

    name = settings.QDRANT_CASES_COLLECTION
    if client.collection_exists(name):
        client.delete_collection(name)
    client.create_collection(
        collection_name=name,
        vectors_config={
            DENSE: models.VectorParams(
                size=settings.EMBEDDING_DIM, distance=models.Distance.COSINE
            )
        },
        sparse_vectors_config={
            SPARSE: models.SparseVectorParams(modifier=models.Modifier.IDF)
        },
    )
    client.create_payload_index(name, "okpd2", models.PayloadSchemaType.KEYWORD)


def index_cases(client, cases: list[dict]) -> None:
    from qdrant_client import models

    name = settings.QDRANT_CASES_COLLECTION
    texts = [build_text(c) for c in cases]
    lengths = [doc_length(t) for t in texts]
    avgdl = (sum(lengths) / len(lengths)) if lengths else 1.0
    dense_vecs = embed_passages(texts)

    points = []
    for c, text, dvec in zip(cases, texts, dense_vecs):
        idx, val = document_vector(text, avgdl)
        payload = dict(c)
        payload["text"] = text
        points.append(
            models.PointStruct(
                id=str(uuid5(NAMESPACE_URL, f"case|{c.get('source','')}|{c['query']}")),
                vector={
                    DENSE: dvec,
                    SPARSE: models.SparseVector(indices=idx, values=val),
                },
                payload=payload,
            )
        )
    client.upsert(collection_name=name, points=points)


def main() -> None:
    ap = argparse.ArgumentParser(description="Переиндексация кейсов эксперта в verified_cases")
    ap.add_argument("--query", help="после загрузки прогнать тестовый поиск по кейсам")
    args = ap.parse_args()

    cases = load_cases()
    print(f"Кейсов к загрузке: {len(cases)}")
    client = make_client()
    recreate_collection(client)  # создаём даже пустую — чтобы поиск не падал
    if cases:
        index_cases(client, cases)
    info = client.get_collection(settings.QDRANT_CASES_COLLECTION)
    print(f"✅ Коллекция '{settings.QDRANT_CASES_COLLECTION}': точек = {info.points_count}")

    if args.query:
        from app.rag.retriever import search_cases

        print(f"\n🔎 Тест поиска по кейсам: «{args.query}»")
        for i, h in enumerate(search_cases(args.query, limit=3), 1):
            print(f"  {i}. score={h['_score']:.3f} | {h.get('product_name', '')[:50]} | {h.get('source', '')}")


if __name__ == "__main__":
    main()
