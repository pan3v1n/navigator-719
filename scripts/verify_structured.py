"""Объективная сверка структурированного JSON с исходным текстом раздела.

Два слоя проверки — по РАЗДЕЛУ и по ЗАПИСИ.

A. По разделу (исходный слой). Ловит два главных риска LLM-парсинга:
  1. Галлюцинации — баллы в JSON, которых НЕТ в тексте закона.
  2. Потери — баллы из текста закона, пропавшие из JSON полностью
     (не как число, и не сохранённые в тексте операции).

B. По записи (D8). Тот же вопрос, но для каждой позиции отдельно: числа-баллы её
   строки-исходника против чисел её записи. Нужен потому, что слой A сверяет МНОЖЕСТВА
   РАЗДЕЛА и потому слеп к самому вероятному дефекту разбора — ПЕРЕНОСУ балла между
   позициями: если «45 баллов» пропали у «Платформ», но есть у соседней записи, множество
   раздела не изменится и слой A скажет ✅. На перегенерации XVIII (D6) из 15 потерянных
   чисел слой A увидел 2 — остальные поймал только ручной дифф со старым JSON.

   Слой B заодно закрывает второе слепое пятно: текст записи собирается ВКЛЮЧАЯ `component`.
   Разбор местами кладёт перечни и требования именно туда, и слой A считал такой текст
   потерянным, хотя он на месте.

Запуск:
  .venv/Scripts/python.exe scripts/verify_structured.py            # все разделы
  .venv/Scripts/python.exe scripts/verify_structured.py III X      # конкретные
  .venv/Scripts/python.exe scripts/verify_structured.py --records XVIII   # только слой B, подробно
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # запуск как скрипта: нужен корень для `scripts.structure_kb`
    sys.path.insert(0, str(ROOT))

from app.core.console import enable_utf8  # noqa: E402  (только после sys.path)

# Windows-консоль по умолчанию cp1251 и не знает «⚠», «✅», «→»: без этого печать
# предупреждения роняет скрипт UnicodeEncodeError'ом. Подробности — в app/core/console.py.
enable_utf8()
CHUNKS = ROOT / "knowledge_base" / "pp719" / "chunks"
STRUCT = ROOT / "knowledge_base" / "pp719" / "structured"

# Скобка перед словом — не опечатка сверки, а форма самого приложения: «производство
# силового генератора (20 баллов), мотор-генератора 36 (баллов)». Таких мест в корпусе шесть
# против 13 902 обычных, но без них сверка объявляла верно разобранный балл выдуманным (D8).
POINTS_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*\(?\s*балл")


def _num(x) -> float:
    return float(str(x).replace(",", "."))


def chunk_for(roman: str) -> Path | None:
    # приложения с продукцией: старые корректные чанки 02..10 ИЛИ перечанкованные
    # (rechunk_appendix.py) с префиксом 1NN (105, 110..129). Процедурные части — пропуск.
    for f in sorted(CHUNKS.glob(f"*_{roman}_*.txt")):
        m = re.match(r"^(\d+)_", f.name)
        if m and (2 <= int(m.group(1)) <= 10 or int(m.group(1)) >= 100):
            return f
    return None


def struct_for(roman: str) -> Path | None:
    for f in sorted(STRUCT.glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        if data and data[0].get("section_roman") == roman:
            return f
    return None


# Величина технологической операции из формулы приложения. Источник пишет «(Втоп = 30 баллов)»,
# а разбор сохраняет саму формулу — «Расчет баллов по формуле: Втоп = 30 * К». Число то же самое,
# но без слова «баллов» рядом, и сверка объявляла верно сохранённую величину ПОТЕРЯННОЙ: ровно так
# в список D9 попала позиция IX «Устройства автоматической обработки данных прочие».
FORMULA_POINTS_RE = re.compile(r"\bВтоп\s*=\s*(\d+(?:[.,]\d+)?)", re.I)


def _points_in(text: str | None) -> set[float]:
    """Числа, записанные в тексте как «N баллов» или как величина формулы «Втоп = N»."""
    t = text or ""
    return {_num(x) for x in POINTS_RE.findall(t)} | {_num(x) for x in FORMULA_POINTS_RE.findall(t)}


def record_points(rec: dict) -> set[float]:
    """Все баллы записи: и проставленные в `points`, и оставшиеся числом в любом её тексте.

    `component` включён намеренно: разбор кладёт туда и перечни изделий, и целые требования,
    и без него балл, «переехавший» в компонент, выглядел бы потерянным."""
    pts: set[float] = set()
    for field in ("min_threshold", "notes", "product_name"):
        pts |= _points_in(rec.get(field))
    for b in rec.get("requirement_blocks") or []:
        pts |= _points_in(b.get("component"))
        pts |= _points_in(b.get("note"))
        pts |= _points_in(b.get("min_threshold"))  # D9: у блока свой порог (узел / вид работ)
        for o in b.get("operations") or []:
            pts |= _points_in(o.get("text"))
            if o.get("points") is not None:
                pts.add(_num(o["points"]))
    return pts


def source_points(product) -> set[float]:
    """Баллы строки-исходника позиции (наименование + все её ячейки требований)."""
    return _points_in(product.raw_block())


def _notes_chunk() -> str:
    """Блок «Примечания:» приложения — общий для всех разделов."""
    f = next(iter(CHUNKS.glob("*PRIMECHANIYA*.txt")), None)
    return f.read_text(encoding="utf-8") if f else ""


def section_points(roman: str, header: str) -> set[float]:
    """Баллы, законные для записи, но живущие ВНЕ её строки: шапка раздела и примечания.

    Два реальных источника таких чисел:
      * вводные абзацы раздела («баллы суммируются, но не более 100 баллов в совокупности»);
      * пороги из блока примечаний — их парсер не видит, и для оптоволокна (примечания 72.2/72.3)
        `min_threshold` проставлялся вручную. Без этой поблажки сверка объявляла бы ручную
        и верную простановку «лишним баллом»."""
    return _points_in(header) | _points_in(_notes_chunk())


def collect_incomplete(romans: list[str]) -> list[dict]:
    """Позиции, где числа-баллы строки-исходника не покрыты записью, — машинный список для рантайма.

    Из этого файла `app.rag.fragments` ставит пометку неполноты порогов: показать один порог там,
    где в законе их несколько (по узлам изделия или по видам работ), опаснее, чем не показать
    ничего, — ответ выглядит полным, а недобор по узлу остаётся незамеченным (`D9`)."""
    from scripts.structure_kb import parse_section

    out: list[dict] = []
    for roman in romans:
        src_f, js_f = chunk_for(roman), struct_for(roman)
        if not src_f or not js_f:
            continue
        header, products = parse_section(src_f.read_text(encoding="utf-8"))
        outside = section_points(roman, header)
        data = [r for r in json.loads(js_f.read_text(encoding="utf-8"))
                if r.get("record_type") != "section_methodology"]
        if len(products) != len(data):
            continue
        for p, rec in zip(products, data):
            src, got = source_points(p), record_points(rec)
            lost = sorted(x for x in src - got if x != 0)
            extra = sorted(got - src - outside)
            if not lost and not extra:
                continue
            out.append({
                "section": roman,
                "product_name": (rec.get("product_name") or "").split("\n")[0],
                "okpd2_codes": rec.get("okpd2_codes") or [],
                "lost_points": [_fmt(x) for x in lost],
                "extra_points": [_fmt(x) for x in extra],
            })
    return out


def check_records(roman: str, verbose: bool = False) -> tuple[int, int]:
    """Слой B: сверка баллов ПО ЗАПИСИ. Возвращает (записей проверено, записей с расхождением).

    Сопоставление позиционное — оно корректно ровно тогда, когда состав JSON совпадает с
    составом чанка; это отдельно доказывает `reconcile_kb.py`, и при расхождении длин мы
    честно отказываемся сверять, а не подгоняем пары."""
    from scripts.structure_kb import parse_section  # локально: тянет app.core.config

    src_f, js_f = chunk_for(roman), struct_for(roman)
    if not src_f or not js_f:
        return 0, 0

    header, products = parse_section(src_f.read_text(encoding="utf-8"))
    outside = section_points(roman, header)
    data = [r for r in json.loads(js_f.read_text(encoding="utf-8"))
            if r.get("record_type") != "section_methodology"]
    if len(products) != len(data):
        print(f"  ⚠ сверка по записи невозможна: чанк={len(products)} строк, JSON={len(data)} "
              f"записей — сначала reconcile_kb.py")
        return 0, 0

    bad = 0
    for p, rec in zip(products, data):
        src, got = source_points(p), record_points(rec)
        # «0 баллов» — не значение, а отметка «эта градация баллов не даёт» («менее 25 процентов -
        # 0 баллов»). Разбор её законно опускает, ругаться не на что.
        lost = sorted(x for x in src - got if x != 0)
        extra = sorted(got - src - outside)
        if not lost and not extra:
            continue
        bad += 1
        if verbose or bad <= 5:
            name = (rec.get("product_name") or p.name_raw or "").split("\n")[0][:52]
            codes = ",".join(rec.get("okpd2_codes") or []) or "—"
            parts = []
            if lost:
                parts.append(f"потеряно {[_fmt(x) for x in lost]}")
            if extra:
                parts.append(f"чужие/лишние {[_fmt(x) for x in extra]}")
            print(f"    ⚠ [{codes}] {name!r}: {'; '.join(parts)}")
    return len(data), bad


def _fmt(v: float) -> str:
    return str(int(v)) if v == int(v) else str(v)


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
    metho_count = 0  # прозовые пороги из секционной записи-методички (раздельный учёт)
    halluc_ops = 0  # операции, чей балл вообще не встречается в законе как "N балл"
    for p in data:
        # Секционная запись-методичка: прозовые пороги раздела (потолки НИОКР, ступенчатые
        # минимумы). Учитываем их тексты, но считаем ОТДЕЛЬНО — не как операции продукта.
        if p.get("record_type") == "section_methodology":
            metho_lines = p.get("methodology_thresholds") or []
            texts.extend(metho_lines)
            texts.append(p.get("notes") or "")
            metho_count += len(metho_lines)
            continue
        # текст всех полей JSON — для проверки «потерь» (вкл. порог и примечания)
        texts.append(p.get("min_threshold") or "")
        texts.append(p.get("notes") or "")
        texts.append(p.get("product_name") or "")
        for b in p["requirement_blocks"]:
            texts.append(b.get("note") or "")
            # component — тоже текст закона (D8): разбор кладёт туда перечни изделий и целые
            # требования, и без него «переехавший» балл считался бы потерянным
            texts.append(b.get("component") or "")
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

    hallucinated = sorted(json_int - src_points)
    missing = sorted(src_points - json_int)
    lost = [
        m for m in missing
        if not re.search(rf"{_fmt(m)}\s*балл", alltext) and _fmt(m) not in alltext
        and _fmt(m).replace(".", ",") not in alltext
    ]

    flag = "⚠" if (hallucinated or lost) else "✅"
    n_products = sum(1 for p in data if p.get("record_type") != "section_methodology")
    print(f"=== Раздел {roman} {flag}: {n_products} продуктов, {total} операций (null={nulls}) ===")
    print(
        f"  Выдуманные баллы (нет в законе): {hallucinated or 'НЕТ'}"
        + (f"  → в {halluc_ops} операциях ({halluc_ops*100//max(total,1)}%)" if hallucinated else "")
    )
    print(f"  Потеряно полностью (нет нигде в JSON): {lost or 'НЕТ'}")
    if metho_count:
        print(f"  Прозовые пороги раздела в записи-методичке (V2-доразбор): {metho_count}")
    print(
        f"  уник.баллов: закон={len(src_points)}, JSON-int={len(json_int)}; "
        f"из {len(missing)} недостающих сохранено в тексте/порогах: {len(missing) - len(lost)}"
    )

    checked, bad = check_records(roman)
    if checked:
        mark = "⚠" if bad else "✅"
        print(f"  Сверка ПО ЗАПИСИ {mark}: расхождений {bad} из {checked} позиций")


ALL_ROMANS = [
    "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII", "XIII",
    "XIV", "XV", "XVI", "XVII", "XVIII", "XIX", "XX", "XXI", "XXII", "XXIII", "XXIV",
    "XXV", "XXVI", "XXVII", "XXVIII", "XXIX",
]


def main() -> None:
    argv = sys.argv[1:]
    only_records = "--records" in argv
    dump_to = None
    if "--json" in argv:
        i = argv.index("--json")
        dump_to = argv[i + 1] if i + 1 < len(argv) else None
        if not dump_to:
            sys.exit("--json требует путь к файлу")
        argv = argv[:i] + argv[i + 2:]
    args = [a for a in argv if a != "--records"]
    romans = args or ALL_ROMANS

    if dump_to:
        items = collect_incomplete(romans)
        out = Path(dump_to)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "_comment": ("Позиции, у которых числа-баллы строки-исходника не покрыты записью "
                         "(D9: в записи одно поле min_threshold, а в законе порогов несколько — "
                         "по узлам изделия или по видам работ). Генерируется детерминированно: "
                         "scripts/verify_structured.py --json <путь>. Правится ТОЛЬКО перегенерацией."),
            "positions": items,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"Позиции с неполными порогами → {out}")
        print(f"  найдено: {len(items)}")
        for it in items:
            print(f"    [{it['section']}] {','.join(it['okpd2_codes']) or '—'} "
                  f"{it['product_name'][:46]!r}: потеряно {it['lost_points']}")
        return
    if only_records:
        total = bad_total = 0
        for r in romans:
            print(f"=== Раздел {r} — сверка по записи ===")
            checked, bad = check_records(r, verbose=True)
            print(f"  расхождений {bad} из {checked} позиций\n")
            total += checked
            bad_total += bad
        print(f"ИТОГО: {bad_total} расхождений на {total} позиций")
        return
    for r in romans:
        check(r)
        print()


if __name__ == "__main__":
    main()
