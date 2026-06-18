"""Сверка СОСТАВА продуктов базы знаний ПП №719 (code-level reconciliation).

`verify_structured.py` проверяет баллы (галлюцинации/потери чисел). Этот скрипт
проверяет ДРУГОЕ и более фундаментальное: что состав продуктов каждого раздела
верен сквозь всю цепочку данных.

Три слоя сверки:
  A. Чанк ↔ канонический срез приложения из pp719_full.txt
     (rechunk_appendix.find_appendix_sections). Ловит расхождение чанка с исходником:
     потерянные/лишние строки-продукты, съеденные границы разделов.
  B. JSON ↔ parse_section(чанк). Ловит расхождение структурированного JSON с тем,
     что реально лежит в чанке: пропавший/задвоенный продукт, перепутанные коды.

Сверка идёт по КОДАМ ОКПД2 строк-продуктов (детерминированно, без LLM, бесплатно) —
это самый жёсткий и однозначный сигнал. Имена сверяются мягко (LLM их нормализует).

Запуск:
  .venv/Scripts/python.exe scripts/reconcile_kb.py            # все 29 разделов
  .venv/Scripts/python.exe scripts/reconcile_kb.py III X      # конкретные
  .venv/Scripts/python.exe scripts/reconcile_kb.py --verbose  # печатать списки кодов
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.rechunk_appendix import find_appendix_sections, FULL  # noqa: E402
from scripts.structure_kb import parse_section  # noqa: E402
from scripts.verify_structured import chunk_for, struct_for, ALL_ROMANS  # noqa: E402


def product_codes(products) -> list[list[str]]:
    """Последовательность списков кодов ОКПД2 по строкам-продуктам (порядок важен)."""
    return [list(p.okpd2_codes) for p in products]


def json_codes(data) -> list[list[str]]:
    return [
        list(p.get("okpd2_codes") or [])
        for p in data
        if p.get("record_type") != "section_methodology"
    ]


def _flat(seq: list[list[str]]) -> list[str]:
    return [c for codes in seq for c in codes]


def _multiset_diff(a: list[str], b: list[str]) -> tuple[list[str], list[str]]:
    """Возвращает (есть в a, нет в b) и (есть в b, нет в a) с учётом кратности."""
    from collections import Counter

    ca, cb = Counter(a), Counter(b)
    only_a = list((ca - cb).elements())
    only_b = list((cb - ca).elements())
    return sorted(only_a), sorted(only_b)


def check(roman: str, canon_bodies: dict[str, str], verbose: bool) -> bool:
    chunk_f, js_f = chunk_for(roman), struct_for(roman)
    if not chunk_f or not js_f:
        print(f"=== Раздел {roman} ⚠: файлы не найдены (chunk={chunk_f}, json={js_f})")
        return False

    chunk_text = chunk_f.read_text(encoding="utf-8")
    data = json.loads(js_f.read_text(encoding="utf-8"))

    _, chunk_products = parse_section(chunk_text)
    chunk_seq = product_codes(chunk_products)
    json_seq = json_codes(data)

    canon_body = canon_bodies.get(roman)
    canon_seq: list[list[str]] | None = None
    if canon_body is not None:
        _, canon_products = parse_section(canon_body)
        canon_seq = product_codes(canon_products)

    # --- Слой A: чанк ↔ канонический исходник ---
    a_ok = True
    a_msg = "— (нет канонического среза)"
    if canon_seq is not None:
        lost_a, extra_a = _multiset_diff(_flat(canon_seq), _flat(chunk_seq))
        a_ok = not lost_a and not extra_a and len(canon_seq) == len(chunk_seq)
        a_msg = (
            f"исходник={len(canon_seq)} строк, чанк={len(chunk_seq)} строк; "
            f"в исходнике но не в чанке={lost_a or '—'}; в чанке но не в исходнике={extra_a or '—'}"
        )

    # --- Слой B: JSON ↔ чанк ---
    lost_b, extra_b = _multiset_diff(_flat(chunk_seq), _flat(json_seq))
    b_ok = not lost_b and not extra_b and len(chunk_seq) == len(json_seq)
    b_msg = (
        f"чанк={len(chunk_seq)} строк, JSON={len(json_seq)} продуктов; "
        f"в чанке но не в JSON={lost_b or '—'}; в JSON но не в чанке={extra_b or '—'}"
    )

    flag = "✅" if (a_ok and b_ok) else "⚠"
    print(f"=== Раздел {roman} {flag} ===")
    print(f"  A (чанк↔исходник): {'✅' if a_ok else '⚠'}  {a_msg}")
    print(f"  B (JSON↔чанк):     {'✅' if b_ok else '⚠'}  {b_msg}")
    if verbose:
        print(f"    коды чанка:  {_flat(chunk_seq)}")
        print(f"    коды JSON:   {_flat(json_seq)}")
    return a_ok and b_ok


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    verbose = "--verbose" in sys.argv
    romans = args or ALL_ROMANS

    text = FULL.read_text(encoding="utf-8")
    canon_bodies = {r: body for r, _t, body in find_appendix_sections(text)}

    ok_count = 0
    for r in romans:
        if check(r, canon_bodies, verbose):
            ok_count += 1
        print()
    print(f"ИТОГО чисто: {ok_count}/{len(romans)}")


if __name__ == "__main__":
    main()
