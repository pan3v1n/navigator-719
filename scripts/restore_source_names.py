"""Наименование записи — из ячейки таблицы первоисточника, а не из пересказа модели.

ЗАЧЕМ (актуализация под ред. 29.09.2026). LLM-разбор `structure_kb.py` местами ПЕРЕСКАЗЫВАЕТ
наименование позиции вместо того, чтобы перенести его: перечень из десяти видов «Швартовные
лебедки; траловые лебедки; гиневые лебедки; …» стал «Лебедки судовые», «Краны со складной стрелой
судовые; краны с прямой стрелой судовые; …» — «Краны судовые (со складной стрелой, …)», а из
ячейки с двумя наименованиями («Шлюпки спасательные» / «Шлюпки спасательные (пассажирские); …»)
осталось одно. Такого наименования в законе нет, а эксперт читает его как цитату. Сверки
`verify_structured` и `reconcile_kb` этого не видят: баллы и число позиций сходятся.

ЧТО ДЕЛАЕТ. Сопоставляет запись раздела с кандидатом-строкой `structure_kb.parse_section` по
номеру позиции (`source_anchor` → `position`) и, если имена расходятся ПО СУЩЕСТВУ, ставит имя
из ячейки источника: построчно, без маркеров сносок. Различие ФОРМЫ (переводы строк против «; »,
пробелы, регистр, сноски) правкой не считается. Требования, коды и прочие поля не трогает.

⚠ ГОДИТСЯ ТОЛЬКО ДЛЯ РАЗДЕЛОВ, РАЗОБРАННЫХ ТЕКУЩЕЙ ВЕРСИЕЙ ПАРСЕРА. У старых JSON нумерация
позиций разошлась с нынешним разбором (восстановление бескодовых строк-вариантов, июль 2026), и
сопоставление по номеру даёт сотни ложных «расхождений» (III, V, VII, XXI). Поэтому раздел
задаётся явно, а скрипт отказывается работать, если доля расхождений выше `--max-share`.

    .venv/Scripts/python scripts/restore_source_names.py XVIII             # показать
    .venv/Scripts/python scripts/restore_source_names.py XVIII --write     # записать
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from app.core.console import enable_utf8  # noqa: E402

_FOOTNOTE = re.compile(r"\s*<[^>|\n]{1,16}>")


def source_name(name_raw: str) -> str:
    """Имя из ячейки: построчно, без сносок и висячих пробелов; переводы строк сохраняются."""
    lines = [_FOOTNOTE.sub("", ln).strip() for ln in (name_raw or "").split("\n")]
    return "\n".join(ln for ln in lines if ln)


def same_name(a: str | None, b: str | None) -> bool:
    """Одно и то же наименование с точностью до ФОРМЫ записи."""
    def norm(s):
        s = _FOOTNOTE.sub(" ", s or "")
        s = re.sub(r"\s*;\s*", "; ", s)
        return re.sub(r"\s+", " ", s).strip(" ;,.").lower()
    return norm(a) == norm(b)


def plan(roman: str) -> tuple[Path, list[dict], list[tuple[int, str, str]]]:
    """(файл, записи, [(индекс записи, было, станет)])."""
    import structure_kb as skb

    chunk = skb.find_chunk(roman, None)
    _, products = skb.parse_section(chunk.read_text(encoding="utf-8"))
    by_pos = {p.position: p for p in products}
    path = next((ROOT / "knowledge_base" / "pp719" / "structured").glob(f"{roman}_*.json"))
    recs = json.loads(path.read_text(encoding="utf-8"))
    fixes = []
    for i, r in enumerate(recs):
        m = re.search(r"позиция (\d+)", r.get("source_anchor") or "")
        p = by_pos.get(int(m.group(1))) if m else None
        if p is None or same_name(r.get("product_name"), p.name_raw):
            continue
        fixes.append((i, r.get("product_name") or "", source_name(p.name_raw)))
    return path, recs, fixes


def main() -> None:
    enable_utf8()
    ap = argparse.ArgumentParser(description="Наименования записей — из ячеек первоисточника")
    ap.add_argument("section", help="римский номер раздела, разобранного текущим парсером")
    ap.add_argument("--write", action="store_true", help="записать (без флага — показать)")
    ap.add_argument("--max-share", type=float, default=0.1,
                    help="предохранитель: доля расхождений, выше которой сопоставление не верят")
    args = ap.parse_args()

    path, recs, fixes = plan(args.section)
    share = len(fixes) / max(1, len(recs))
    print(f"{args.section}: записей {len(recs)}, имя расходится с источником у {len(fixes)} ({share:.1%})")
    for i, was, new in fixes:
        print(f"  [{i}] было «{was[:80]}»\n       станет «{new.replace(chr(10), ' / ')[:120]}»")
    if share > args.max_share:
        sys.exit(f"⚠ доля расхождений {share:.1%} > {args.max_share:.0%} — нумерация позиций, видимо, "
                 f"разошлась с разбором (старый JSON). Ничего не записано.")
    if args.write and fixes:
        for i, _was, new in fixes:
            recs[i]["product_name"] = new
        path.write_text(json.dumps(recs, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"записано: {path}")
    elif fixes:
        print("(пробный прогон — нужен --write)")


if __name__ == "__main__":
    main()
