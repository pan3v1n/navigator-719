"""Оценка стоимости DeepSeek-запросов в рублях — для учёта затрат в админ-логах.

Единственная платная статья стека: DeepSeek (эмбеддинг e5 и Qdrant локальны = бесплатно).
Цены DeepSeek: ~$0.27/1M входных, ~$1.10/1M выходных токенов (проверять на актуальность).
Курс ~90 ₽/$. Оценочно — для наглядности эксперту, не для бухгалтерии.
"""

from __future__ import annotations

USD_PER_1M_INPUT = 0.27
USD_PER_1M_OUTPUT = 1.10
RUB_PER_USD = 90.0


def cost_rub(prompt_tokens: int | None, completion_tokens: int | None) -> float:
    """Оценочная стоимость запроса в рублях по числу токенов."""
    pt = prompt_tokens or 0
    ct = completion_tokens or 0
    usd = pt / 1_000_000 * USD_PER_1M_INPUT + ct / 1_000_000 * USD_PER_1M_OUTPUT
    return round(usd * RUB_PER_USD, 4)
