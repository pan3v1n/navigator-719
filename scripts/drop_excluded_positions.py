"""Чистка structured/*.json от пустых записей-заглушек (исключённые позиции приложения).

Раньше парсер (scripts/structure_kb.py) порождал «продукт» из строки-кода, у которой нет
ни наименования, ни требований — это исключённые из приложения позиции
(«из 28.30.31 - Позиции исключены.», «28.99.39.190 - Позиция исключена.») и осиротевшие
подкоды ОКПД2. В индексе они бесполезны и вредны: на запрос по коду всплывает хит с
ПУСТЫМ именем. Парсер исправлен (фильтр пустых строк-кодов после привязки компонентов);
этот скрипт применяет ТО ЖЕ правило к уже сгенерированным JSON — БЕЗ повторного вызова
DeepSeek (записи детерминированные, LLM для них не вызывался).

Правило выброса (ровно empty_stub с пустым именем): record_type отсутствует И
product_name пуст И нет requirement_blocks И нет min_threshold. Реальные продукты с
пустой ячейкой имени, но требованиями ниже (напр. 29.10.52.190) НЕ затрагиваются —
у них непустые requirement_blocks.

Идемпотентно: повторный запуск удаляет 0. Запуск:
  .venv/Scripts/python.exe scripts/drop_excluded_positions.py            # применить
  .venv/Scripts/python.exe scripts/drop_excluded_positions.py --dry-run  # только показать
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STRUCT = ROOT / "knowledge_base" / "pp719" / "structured"


def is_excluded_stub(rec: dict) -> bool:
    """True для пустой записи-заглушки исключённой позиции (см. модуль-докстринг)."""
    if rec.get("record_type"):  # методички (section_methodology) не трогаем
        return False
    if (rec.get("product_name") or "").strip():
        return False
    if rec.get("requirement_blocks"):
        return False
    if rec.get("min_threshold"):
        return False
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description="Удаление пустых записей исключённых позиций из structured/*.json")
    ap.add_argument("--dry-run", action="store_true", help="не записывать, только показать что удалится")
    args = ap.parse_args()

    grand_total = 0
    for jf in sorted(STRUCT.glob("*.json")):
        data = json.loads(jf.read_text(encoding="utf-8"))
        kept = [r for r in data if not is_excluded_stub(r)]
        removed = len(data) - len(kept)
        if removed:
            grand_total += removed
            print(f"  {jf.name}: {len(data)} → {len(kept)}  (−{removed})")
            if not args.dry_run:
                jf.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")

    verb = "удалится" if args.dry_run else "удалено"
    print(f"\nИТОГО записей {verb}: {grand_total}")
    if args.dry_run and grand_total:
        print("(--dry-run: файлы не изменены)")


if __name__ == "__main__":
    main()
