"""Обёртка над локальной моделью эмбеддингов multilingual-e5-large.

e5 требует префиксы: "query: " для поисковых запросов и "passage: " для
индексируемых документов. Векторы нормализуются (L2) — для косинусной близости.
Модель загружается лениво и кешируется на уровне процесса.
"""

from __future__ import annotations

from functools import lru_cache

from sentence_transformers import SentenceTransformer

from app.core.config import settings


@lru_cache(maxsize=1)
def _model() -> SentenceTransformer:
    return SentenceTransformer(settings.EMBEDDING_MODEL)


def embed_query(text: str) -> list[float]:
    return _model().encode(
        f"query: {text}", normalize_embeddings=True
    ).tolist()


def embed_passages(texts: list[str]) -> list[list[float]]:
    prefixed = [f"passage: {t}" for t in texts]
    vectors = _model().encode(prefixed, normalize_embeddings=True)
    return [v.tolist() for v in vectors]
