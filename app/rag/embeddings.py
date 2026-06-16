"""Обёртка над локальной моделью эмбеддингов multilingual-e5-large.

e5 требует префиксы: "query: " для поисковых запросов и "passage: " для
индексируемых документов. Векторы нормализуются (L2) — для косинусной близости.
Модель загружается лениво и кешируется на уровне процесса.
"""

from __future__ import annotations

import os
from functools import lru_cache

from sentence_transformers import SentenceTransformer

from app.core.config import settings

# Размер батча энкодера: на многоядерном CPU крупнее батч = выше пропускная способность.
ENCODE_BATCH = 64


@lru_cache(maxsize=1)
def _model() -> SentenceTransformer:
    # Задействуем все логические ядра CPU (по умолчанию torch берёт не все).
    try:
        import torch

        torch.set_num_threads(os.cpu_count() or 1)
    except Exception:  # noqa: BLE001 — torch всегда есть, но не падаем на тюнинге
        pass
    return SentenceTransformer(settings.EMBEDDING_MODEL)


def embed_query(text: str) -> list[float]:
    return _model().encode(
        f"query: {text}", normalize_embeddings=True
    ).tolist()


def embed_passages(texts: list[str], batch_size: int = ENCODE_BATCH) -> list[list[float]]:
    prefixed = [f"passage: {t}" for t in texts]
    vectors = _model().encode(
        prefixed, normalize_embeddings=True, batch_size=batch_size
    )
    return [v.tolist() for v in vectors]
