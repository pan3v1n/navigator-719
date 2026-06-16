"""Локальный BM25-sparse вектор без скачивания моделей.

Qdrant хранит sparse-вектор как (indices, values). С модификатором коллекции
`Modifier.IDF` сервер сам считает IDF по корпусу, а мы передаём BM25-составляющую TF
(с сатурацией и нормализацией по длине документа). Терм → индекс получаем стабильным
хэшем (mmh3, u32). Токенизация и стемминг (Snowball, русский) одинаковы на индексации
и в запросе. mmh3 и py-rust-stemmers ставятся как зависимости, саму модель BM25 качать
не требуется (важно при нестабильной сети).

Документ: values = tf·(k1+1) / (tf + k1·(1−b + b·dl/avgdl))   ← BM25, нормировка по длине.
Запрос:   values = tf                                          ← общий множитель, на ранг не влияет.
"""

from __future__ import annotations

import re
from collections import Counter
from functools import lru_cache

import mmh3
from py_rust_stemmers import SnowballStemmer

# Буквы (рус/лат) и цифры; одиночные символы отбрасываем как шум.
_TOKEN_RE = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)

BM25_K1 = 1.2
BM25_B = 0.75


@lru_cache(maxsize=1)
def _stemmer() -> SnowballStemmer:
    return SnowballStemmer("russian")


def tokenize(text: str) -> list[str]:
    raw = [t.lower() for t in _TOKEN_RE.findall(text) if len(t) > 1]
    return _stemmer().stem_words(raw) if raw else []


def _term_freqs(text: str) -> tuple[Counter[int], int]:
    toks = tokenize(text)
    return Counter(mmh3.hash(t, signed=False) for t in toks), len(toks)


def doc_length(text: str) -> int:
    return len(tokenize(text))


def query_vector(text: str) -> tuple[list[int], list[float]]:
    """Sparse-вектор запроса: частоты термов (нормировку по длине применяем к документам)."""
    counts, _ = _term_freqs(text)
    return list(counts.keys()), [float(v) for v in counts.values()]


def document_vector(
    text: str, avgdl: float, k1: float = BM25_K1, b: float = BM25_B
) -> tuple[list[int], list[float]]:
    """Sparse-вектор документа: BM25 TF с сатурацией и нормировкой по длине.
    `avgdl` — средняя длина документа в корпусе (в токенах)."""
    counts, dl = _term_freqs(text)
    denom = k1 * (1.0 - b + b * (dl / avgdl if avgdl else 1.0))
    indices: list[int] = []
    values: list[float] = []
    for h, tf in counts.items():
        indices.append(h)
        values.append(float(tf * (k1 + 1.0) / (tf + denom)))
    return indices, values
