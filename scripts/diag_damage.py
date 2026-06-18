"""Точный учёт ущерба: строки-продукты (код|ИМЯ|требования), которые classify()
ошибочно отнёс НЕ к product. Считаем только строки с НЕПУСТЫМ именем во 2-й ячейке
и кодом-кандидатом в 1-й (из/Из + код) — это молча потерянные/смерженные продукты."""
from __future__ import annotations
import re, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.structure_kb import iter_rows, split_fields, classify  # noqa: E402
from scripts.verify_structured import chunk_for, ALL_ROMANS  # noqa: E402

# код-кандидат в первом поле: «из/Из CODE» где CODE = NN или NN.NN…
CAND = re.compile(r"^(?:из\s+)?\d{2}(?:\.\d+)*\s*$", re.IGNORECASE)

total = 0
for roman in ALL_ROMANS:
    cf = chunk_for(roman)
    if not cf:
        continue
    text = cf.read_text(encoding="utf-8")
    lost = []
    for row in iter_rows(text):
        fields = split_fields(row)
        f0 = fields[0].strip()
        name = fields[1].strip() if len(fields) > 1 else ""
        kind, _ = classify(fields)
        if kind != "product" and CAND.match(f0) and name:
            lost.append((f0, name[:50]))
    if lost:
        total += len(lost)
        print(f"=== {roman}: {len(lost)} потерянных/смерженных продуктов ===")
        for f0, name in lost:
            print(f"   {f0!r:22} | {name!r}")
print(f"\nИТОГО строк-продуктов потеряно из-за бага PRODUCT_START_RE: {total}")
