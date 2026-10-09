"""ИНН: форма и контрольные цифры (алгоритм ФНС). Организацию и ИНН в кабинете вписывает сам
пользователь (решение владельца 09.10.2026) — контрольная сумма отсекает опечатки и выдуманные
номера ещё до подтверждения администратором."""

from __future__ import annotations

_W10 = (2, 4, 10, 3, 5, 9, 4, 6, 8)
_W11 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
_W12 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)


def _check(digits: list[int], weights: tuple[int, ...]) -> int:
    return sum(w * d for w, d in zip(weights, digits)) % 11 % 10


def inn_valid(inn: str) -> bool:
    """10 цифр — организация, 12 — ИП и физлицо; контрольные цифры — по весам ФНС."""
    if not (inn.isascii() and inn.isdigit() and len(inn) in (10, 12)):
        return False
    d = [int(c) for c in inn]
    if len(d) == 10:
        return _check(d, _W10) == d[9]
    return _check(d, _W11) == d[10] and _check(d, _W12) == d[11]
