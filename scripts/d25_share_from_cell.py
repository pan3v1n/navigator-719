"""D25 #285: требование «соблюдение процентной доли стоимости … иностранных товаров» — из `notes` в блоки записи.

Поле `notes` идёт только в текст индекса (`scripts/load_kb.py`), в контекст модели не попадает
(`app/rag/pipeline.py`). У 27 позиций раздела XXV (музыкальные инструменты, звуковая аппаратура) обязательное
требование о доле иностранных товаров — «… - не более 30 процентов цены товара <4>» — жило ТОЛЬКО там: эксперт
получал операции и порог в баллах, но не предел доли. `D24` (`scripts/d23_groups.py`) этот раздел не сверяет вовсе:
таблица XXV не открывается зачином «наличие у … резидент» (`OPENER_RE`), и раздел стоит в списке «вне сверки»
вместе с II, VIII, X. Допуск XXV в общий инструмент замерен (03.11.2026) и отвергнут: строки ячейки XXV переносятся
посреди фразы, а вводная с порогом («… не менее 90 баллов:») встала бы блоком, у двух позиций дважды.

Ещё у 10 позиций XXV (аккордеоны, духовые) той же фразы не было в записи вовсе — ни в блоках, ни в пороге, ни в
`notes`: разборщик её выронил. Шаг закрывает оба случая.

Что делает шаг. Текст — из ячейки источника (чанк `knowledge_base/pp719/chunks/*`, разбор `structure_kb`, та же
вёрстка Контура, из которой собран корпус), а не пересказ из `notes`: строки ячейки склеиваются до «;» / «.» / «:»,
берутся фразы, открытые «[до|с|по <дата>] соблюдение процентной доли». Фраза, которой нет ни в блоках, ни в пороге
записи, встаёт блоком без операций в НАЧАЛО записи (в ячейке она стоит первой; рантайм выводит такой блок
обязательным требованием, как «наличие …»). Только разделы вне сверки `D24` (`OUTSIDE_D24`). Кусок `notes`, все
числа которого — в перенесённых фразах, из `notes` снимается; остальное (оговорки ко всей позиции, переходные
положения) остаётся. Повторный прогон — 0 правок.

Порядок пересборки данных: `scripts/d23_groups.py <html> --write`, затем этот шаг с `--write`.

python scripts/d25_share_from_cell.py            — план
python scripts/d25_share_from_cell.py --write    — записать structured/*.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from app.core.console import enable_utf8  # noqa: E402

STRUCTURED = ROOT / "knowledge_base" / "pp719" / "structured"
# Разделы ВНЕ сверки `D23`/`D24` — строка «разделы ВНЕ сверки …» вывода `scripts/d23_groups.py` на вёрстке Контура
# 508574 (03.11.2026). В остальных строки ячейки на место ставит `D24`, и там доля лежит операцией с баллами
# («соблюдение процентной доли … (100 баллов): до 31 декабря 2027 г. …», XX-12): буквального совпадения с фразой
# ячейки нет, и шаг поставил бы ДУБЛЬ — замер плана по всем разделам: +37 записей вне XXV, все такого вида.
# Сверка начнёт видеть раздел — убрать его отсюда.
OUTSIDE_D24 = ("II", "VIII", "X", "XXV")
_DATE = r"(?:до|с|по)\s+\d{1,2}\s+[а-яё]+\s+\d{4}\s*г\.?"
SHARE_RE = re.compile(rf"^(?:{_DATE}\s*,?\s*)?соблюдение\s+процентной\s+доли\b", re.I)
_END_RE = re.compile(r"[;.:]\s*$")
_MID_END_RE = re.compile(r"(?<=[;.])\s+(?=[А-ЯЁA-Z])")
_FOOTNOTE_RE = re.compile(r"<[\d.()]+>")


def norm(text: str | None) -> str:
    """Сравнение текстов: без сносок `<N>`, регистра, «ё», пунктуации и лишних пробелов."""
    t = _FOOTNOTE_RE.sub(" ", (text or "").lower().replace("ё", "е").replace("\xa0", " "))
    return re.sub(r"\s+", " ", re.sub(r"[^\w%]+", " ", t)).strip()


def numbers(text: str | None) -> Counter:
    return Counter(re.findall(r"\d+", _FOOTNOTE_RE.sub(" ", text or "")))


def cell_sentences(cells: list[str]) -> list[str]:
    """Строки ячейки → фразы: перенос внутри фразы склеивается до «;», «.» или «:» в конце строки, а фраза,
    кончающаяся ВНУТРИ строки («… цены товара <4>. Выполнение на территории Российской» — XXV, Фортепиано),
    делится по «;» / «.» перед заглавной буквой: иначе к доле приклеилась бы вводная перечня с порогом."""
    out, cur = [], []
    for cell in cells:
        for line in cell.split("\n"):
            s = line.strip()
            if not s:
                continue
            if cur and SHARE_RE.match(s):
                # новая фраза о доле — всегда новая фраза: у «Альтов» XXV-30 источник не поставил «;» после «<4>»
                out.append(" ".join(cur))
                cur = []
            cur.append(s)
            if _END_RE.search(s):
                out.append(" ".join(cur))
                cur = []
    if cur:
        out.append(" ".join(cur))
    return [p for s in out for p in _MID_END_RE.split(s) if p.strip()]


def share_sentences(cells: list[str]) -> list[str]:
    """Фразы о доле иностранных товаров — текстом ячейки, без знака конца («;», «.»), сноска `<N>` остаётся."""
    return [re.sub(r"\s*[;.]\s*$", "", s) for s in cell_sentences(cells) if SHARE_RE.match(s)]


def blocks_text(rec: dict) -> str:
    parts = []
    for b in rec.get("requirement_blocks") or []:
        parts += [b.get("component") or "", b.get("note") or ""]
        parts += [o.get("text") or "" for o in b.get("operations") or []]
    return norm(" ".join(parts))


def notes_mentions_share(rec: dict) -> bool:
    return bool(re.search(r"процентн\w*\s+доли", rec.get("notes") or "", re.I))


def plan_record(rec: dict, cells: list[str]) -> tuple[list[str], str | None, list[str]]:
    """(фразы к вставке, новые notes, замечания). Пустой список фраз — запись не трогается.

    Фраза ячейки «есть в записи», если она в блоках или в пороге записи (`min_threshold` рантайм печатает строкой
    «Порог» как есть — так у XXV-3, 11, 13–15; тот же довод у `D24`, VI-5)."""
    have = blocks_text(rec) + " " + norm(rec.get("min_threshold"))
    add = [s for s in share_sentences(cells) if norm(s) not in have]
    if not add:
        return [], rec.get("notes"), []
    if not notes_mentions_share(rec):
        return add, rec.get("notes"), []
    pool = numbers(" ".join(add))
    keep, issues = [], []
    for piece in re.split(r";\s*", rec["notes"].strip()):
        if not piece.strip():
            continue
        moved = re.search(r"процент", piece, re.I) and not (numbers(piece) - pool)
        if not moved:
            keep.append(piece.strip())
            if re.search(r"процентн\w*\s+доли|не более \d+ процент", piece, re.I):
                issues.append(f"кусок notes о доле не подтверждён фразами ячейки: «{piece[:90]}»")
    new_notes = "; ".join(keep) or None
    return add, new_notes, issues


def product_cells(roman: str) -> dict[tuple[str, tuple[str, ...]], list[list[str]]]:
    """(наименование, коды) → ячейки требований позиций раздела по чанку источника."""
    import structure_kb as sk
    from restructure_changed import clean_name
    _, prods = sk.parse_section(sk.find_chunk(roman, None).read_text(encoding="utf-8"))
    out: dict[tuple[str, tuple[str, ...]], list[list[str]]] = {}
    for p in prods:
        out.setdefault((clean_name(p), tuple(p.okpd2_codes)), []).append(p.component_cells)
    return out


def sections_with_share(roman: str) -> bool:
    """Есть ли в чанке раздела фраза о доле вообще (дешёвый отбор до разбора раздела)."""
    import structure_kb as sk
    return bool(re.search(r"соблюдение\s+процентной\s+доли", sk.find_chunk(roman, None).read_text(encoding="utf-8"),
                          re.I))


def find_cells(rec: dict, cells_by: dict) -> list[str] | None:
    name, codes = rec.get("product_name") or "", tuple(rec.get("okpd2_codes") or [])
    same = [v for (n, c), vs in cells_by.items() if n == name for v in vs]
    if len(same) > 1:
        same = cells_by.get((name, codes), [])
    return same[0] if len(same) == 1 else None


def run(write: bool, root: Path = STRUCTURED) -> tuple[list[dict], list[str]]:
    from restructure_changed import json_indent
    log, issues = [], []
    for f in sorted(root.glob("*.json")):
        old = f.read_text(encoding="utf-8")
        data = json.loads(old)
        todo = [r for r in data if r.get("record_type") != "section_methodology"]
        if (not todo or todo[0]["section_roman"] not in OUTSIDE_D24
                or not sections_with_share(todo[0]["section_roman"])):
            continue
        cells_by = product_cells(todo[0]["section_roman"])
        changed = False
        for rec in todo:
            cells = find_cells(rec, cells_by)
            anchor = rec.get("source_anchor") or rec["product_name"]
            if cells is None:
                if notes_mentions_share(rec):
                    issues.append(f"{anchor}: позиция не сопоставлена с ячейкой источника — не тронута")
                continue
            add, new_notes, why = plan_record(rec, cells)
            issues += [f"{anchor}: {w}" for w in why]
            if not add:
                continue
            rec["requirement_blocks"] = ([{"component": s, "operations": [], "note": None} for s in add]
                                         + (rec.get("requirement_blocks") or []))
            rec["notes"] = new_notes
            changed = True
            log.append({"section": rec["section_roman"], "anchor": anchor, "product": rec["product_name"][:80],
                        "added": add, "notes_left": new_notes})
        if changed and write:
            new = json.dumps(data, ensure_ascii=False, indent=json_indent(old)) + ("\n" if old.endswith("\n") else "")
            f.write_text(new, encoding="utf-8")
    return log, issues


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--write", action="store_true", help="записать правку (без флага — план)")
    ap.add_argument("--log", type=Path, help="журнал правок (JSON)")
    args = ap.parse_args()
    enable_utf8()
    log, issues = run(args.write)
    for e in log:
        print(f"{e['anchor']} «{e['product'][:50]}»: +{len(e['added'])} фраз о доле"
              + ("" if e["notes_left"] is None else f"; в notes осталось: «{e['notes_left'][:60]}»"))
    for i in issues:
        print("  ⚠", i)
    print(f"D25: записей {len(log)}, фраз {sum(len(e['added']) for e in log)}, замечаний {len(issues)}"
          + ("" if args.write else " — ПЛАН, файлы не тронуты (--write)"))
    if args.log:
        args.log.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
