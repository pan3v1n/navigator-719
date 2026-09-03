"""Оракул кейсов СТ-1 / ТН ВЭД приёмочного набора (`K14` #35, шаг 5) — общий для замеров уровня 2.

Отдельным модулем по тому же правилу, что и `eval_documents`: определение «ответ верно передал
условие достаточной переработки» обязано быть в ОДНОМ месте. Потребитель сейчас один
(`eval_answers.py`), но у документного оракула он тоже начинался с одного.

⚠⚠ НЕЗАВИСИМОСТЬ ОТ РАНТАЙМА. Оракул НЕ зовёт `app.rag.st1_ref` — лукап и есть проверяемый
продукт. Позови его — и снятие лукапа сделало бы метрику зелёной (класс `O3` #104, ровно тот, из-за
которого документный оракул не зовёт `documents_ref.unverified_documents`). Здесь читается ТАБЛИЦА
ФАКТОВ, то есть общий для обоих первоисточник, и только как ПОЛОЖИТЕЛЬНЫЙ КОНТРОЛЬ ожиданий.

⚠ ПОЧЕМУ НЕТ ПРОВЕРКИ «не назвал чужое условие». Первая редакция запрещала величину соседней
позиции рядом с кодом (8402 не в Перечне, а сосед 8403 — есть, с условием «50 %»). Отброшено:
верный ответ вправе сказать «в отличие от позиции 8403, где…», и предохранитель ударил бы по
правильному тексту. Тот же дефект ловится БЕЗ риска ложной тревоги: если модель приписала 8402
чужое условие, она НЕ сформулировала общее правило — а его требует `expect_general_rule`.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.console import enable_utf8  # noqa: E402

enable_utf8()  # #107: файл в `scripts/` приходит СО защитой, а не с исключением

ST1_TABLE = ROOT / "knowledge_base" / "classifiers" / "tnved_st1_conditions.json"

# Общее правило Соглашения: смена товарной позиции на уровне любого из первых четырёх знаков.
# ⚠ Формы взяты из первоисточника и из того, как правило пересказывают: «первых четырёх знаков»,
# «изменение товарной позиции», «на уровне четырёх знаков».
GENERAL_RULE_RE = re.compile(
    r"измен\w+\s+(?:классификационн\w+\s+код\w*|товарн\w+\s+позици|позици)"
    r"|перв\w+\s+четыр\w+\s+знак|четыр\w+\s+знак\w*\s+код", re.I)


def _norm(s: str) -> str:
    """Пробелы и неразрывные пробелы к одному виду: «50 %» и «50%» — одно и то же утверждение."""
    return re.sub(r"\s+", " ", (s or "").replace(" ", " ")).strip()


def load_table() -> dict:
    return json.loads(ST1_TABLE.read_text(encoding="utf-8"))


def _condition_of(table: dict, code: str) -> str | None:
    """Условие Перечня для кода. None — кода в Перечне нет (действует общее правило)."""
    code = str(code).replace(" ", "")
    for row in table["rows"]:
        if str(row.get("code", "")).replace(" ", "") == code:
            return row.get("condition") or ""
    return None


def check_st1_reference(cases: list[dict]) -> None:
    """⚠⚠ ПОЛОЖИТЕЛЬНЫЙ КОНТРОЛЬ: ожидания набора сверены с ПЕРВОИСТОЧНИКОМ, а не с моей памятью.

    Без него кейс можно завести с числом, которого в Перечне нет, — и метрика «условие доехало»
    честно показывала бы ноль на ВЕРНЫХ ответах. Останавливаемся на старте, до платных вызовов.
    """
    table = load_table()
    problems: list[str] = []
    for c in cases:
        if c.get("kind") != "st1":
            continue
        code = c.get("st1_code")
        if not code:
            continue
        cond = _condition_of(table, code)
        present = cond is not None
        if present != bool(c.get("in_perechen")):
            problems.append(
                f"#{c['id']}: код {code} {'ЕСТЬ' if present else 'ОТСУТСТВУЕТ'} в Перечне, "
                f"а кейс объявляет in_perechen={c.get('in_perechen')}")
            continue
        if not present:
            continue
        cond_n = _norm(cond)
        for num in c.get("expect_numbers") or []:
            if num not in cond_n:
                problems.append(f"#{c['id']}: числа «{num}» нет в условии Перечня для {code}")
        for group in c.get("expect_keywords") or []:
            if not any(k.lower() in cond_n.lower() for k in group):
                problems.append(
                    f"#{c['id']}: ни одно из {group} не встречается в условии Перечня для {code}")
    if problems:
        raise SystemExit(
            "Ожидания кейсов СТ-1 разошлись с таблицей фактов:\n  " + "\n  ".join(problems) +
            "\nОстановка намеренная: измерять по ожиданиям, которых нет в первоисточнике, "
            "значит красить верные ответы в красный.")


def st1_row(case: dict, text: str) -> dict:
    """Что кейс СТ-1 обязан показать. Эталон — Перечень и Соглашение, НЕ вывод сервиса."""
    from eval_documents import _SOURCE_RECOGNIZERS

    t = _norm(text)
    nums = list(case.get("expect_numbers") or [])
    groups = [list(g) for g in (case.get("expect_keywords") or [])]
    missing_nums = [n for n in nums if n not in t]
    missing_groups = [g for g in groups if not any(k.lower() in t.lower() for k in g)]

    want_rule = bool(case.get("expect_general_rule"))
    rule_found = bool(GENERAL_RULE_RE.search(t))
    want_src = list(case.get("expect_sources") or [])
    return {
        "st1_code": case.get("st1_code") or "",
        "in_perechen": case.get("in_perechen"),
        "missing_numbers": missing_nums,
        "missing_keywords": missing_groups,
        # Условие доехало = все ожидаемые величины и все смысловые группы на месте.
        "condition_ok": not missing_nums and not missing_groups,
        "want_general_rule": want_rule,
        "general_rule_found": rule_found,
        # ⚠ Правило требуется только там, где оно ОЖИДАЕТСЯ. Для позиции ИЗ Перечня общее правило
        # не применяется, и требовать его значило бы штрафовать за верный ответ.
        "general_rule_ok": (rule_found if want_rule else None),
        "want_sources": want_src,
        "named_sources": [s for s in want_src if _SOURCE_RECOGNIZERS[s].search(text or "")],
    }
