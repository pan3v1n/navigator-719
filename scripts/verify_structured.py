"""Объективная сверка структурированного JSON с исходным текстом раздела.

Проверяет два главных риска LLM-парсинга:
  1. Галлюцинации — баллы в JSON, которых НЕТ в тексте закона.
  2. Потери — баллы из текста закона, пропавшие из JSON полностью
     (не как число, и не сохранённые в тексте операции).

Запуск:
  .venv/Scripts/python.exe scripts/verify_structured.py            # все разделы
  .venv/Scripts/python.exe scripts/verify_structured.py III X      # конкретные
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHUNKS = ROOT / "knowledge_base" / "pp719" / "chunks"
STRUCT = ROOT / "knowledge_base" / "pp719" / "structured"

POINTS_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*балл")


def _num(x) -> float:
    return float(str(x).replace(",", "."))


def chunk_for(roman: str) -> Path | None:
    # приложения с продукцией — файлы 02..10
    for f in sorted(CHUNKS.glob(f"*_{roman}_*.txt")):
        prefix = f.name[:2]
        if prefix.isdigit() and 2 <= int(prefix) <= 10:
            return f
    return None


def struct_for(roman: str) -> Path | None:
    for f in sorted(STRUCT.glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        if data and data[0].get("section_roman") == roman:
            return f
    return None


def check(roman: str) -> None:
    src_f, js_f = chunk_for(roman), struct_for(roman)
    if not src_f or not js_f:
        print(f"=== Раздел {roman}: файлы не найдены (chunk={src_f}, json={js_f})")
        return

    src = src_f.read_text(encoding="utf-8")
    data = json.loads(js_f.read_text(encoding="utf-8"))
    src_points = {_num(x) for x in POINTS_RE.findall(src)}

    json_int: set[float] = set()
    texts: list[str] = []
    total = nulls = 0
    halluc_ops = 0  # операции, чей балл вообще не встречается в законе как "N балл"
    for p in data:
        # текст всех полей JSON — для проверки «потерь» (вкл. порог и примечания)
        texts.append(p.get("min_threshold") or "")
        texts.append(p.get("notes") or "")
        texts.append(p.get("product_name") or "")
        for b in p["requirement_blocks"]:
            texts.append(b.get("note") or "")
            for o in b.get("operations") or []:
                total += 1
                texts.append(o.get("text", "") or "")
                if o.get("points") is None:
                    nulls += 1
                else:
                    pv = _num(o["points"])
                    json_int.add(pv)
                    if pv not in src_points:
                        halluc_ops += 1
    alltext = " ".join(texts)

    def fmt(v: float) -> str:
        return str(int(v)) if v == int(v) else str(v)

    hallucinated = sorted(json_int - src_points)
    missing = sorted(src_points - json_int)
    lost = [
        m for m in missing
        if not re.search(rf"{fmt(m)}\s*балл", alltext) and fmt(m) not in alltext
        and fmt(m).replace(".", ",") not in alltext
    ]

    flag = "⚠" if (hallucinated or lost) else "✅"
    print(f"=== Раздел {roman} {flag}: {len(data)} продуктов, {total} операций (null={nulls}) ===")
    print(
        f"  Выдуманные баллы (нет в законе): {hallucinated or 'НЕТ'}"
        + (f"  → в {halluc_ops} операциях ({halluc_ops*100//max(total,1)}%)" if hallucinated else "")
    )
    print(f"  Потеряно полностью (нет нигде в JSON): {lost or 'НЕТ'}")
    print(
        f"  уник.баллов: закон={len(src_points)}, JSON-int={len(json_int)}; "
        f"из {len(missing)} недостающих сохранено в тексте/порогах: {len(missing) - len(lost)}"
    )


def main() -> None:
    romans = sys.argv[1:] or ["I", "II", "III", "IV", "VI", "VII", "VIII", "IX", "X"]
    for r in romans:
        check(r)
        print()


if __name__ == "__main__":
    main()
