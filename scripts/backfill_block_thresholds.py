"""D9: возвращает в записи пороги, которые не поместились в единственное поле `min_threshold`.

ЗАЧЕМ. В схеме записи одно поле порога, а приложение задаёт для части позиций НЕСКОЛЬКО:
отдельно по узлам изделия («криогенный насос низкого давления (не менее 100 баллов):») и
отдельно по видам работ («Изготовление смычков» — своя шкала 80→110 при шкале инструментов
170→200). Всё, что не поместилось, разбор молча отбрасывал: пользователь видел ОДИН порог и
читал его как порог всей позиции, а недобор по конкретному узлу оставался незамеченным — сумма
по изделию при этом сходится. Класс «правдоподобно, но неверно»: показанное число дословно
верно, поэтому дефекта не видят ни faithfulness-гард, ни глаз эксперта.

ПОЧЕМУ НЕ ПЕРЕПАРС МОДЕЛЬЮ. Перегенерация раздела — обмен, а не улучшение (урок `D6`): LLM
недетерминирована, радиус правки — все записи раздела, и потери прячутся среди улучшений. Здесь
же данные лежат в первоисточнике ДОСЛОВНО, поэтому четыре раздела чинятся регулярками, без
единого обращения к модели и без риска задеть соседние записи.

ПРАВИЛА (каждое опирается на дословную строку источника):
  A. Порог узла: строка «<узел> (не менее N баллов):» → `min_threshold` блока с тем же
     `component`.
  B. Шкала блока: заголовок «<Блок>:» и следующая за ним вводная «…оцениваемых в совокупности
     суммарным количеством баллов, <шкала>:» → `min_threshold` этого блока.
  C. Вытесненный порог позиции: в `min_threshold` записи нет ни одного числа-балла, а во
     фрагменте есть РОВНО ОДНА ничейная вводная со шкалой → она занимает `min_threshold`, а
     вытесненный ею текст сохраняется операцией без баллов (дословно из источника).

ПРЕДОХРАНИТЕЛИ (уроки `D6`/`D8`). После правки запись не имеет права потерять ни одного
числа-балла и ни одной операции. Нарушение → правка ЭТОЙ записи откатывается целиком и
печатается громко. Скрипт идемпотентен: блок с уже проставленным порогом не трогается.

⚠ После записи обязательны (см. чек-лист §1а): перегенерация карты наследования
(`diag_orphan_requirements.py --inheritance …`), пересборка списка неполных порогов
(`verify_structured.py --json …`) и переиндексация Qdrant — порог блока едет в payload.

Запуск:
  .venv/Scripts/python.exe scripts/backfill_block_thresholds.py             # показать, не писать
  .venv/Scripts/python.exe scripts/backfill_block_thresholds.py --write     # записать в корпус
  .venv/Scripts/python.exe scripts/backfill_block_thresholds.py XXIV --write
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import verify_structured as vs          # noqa: E402
from scripts.structure_kb import parse_section       # noqa: E402

# Разделы, где сверка по записи (`D8`) нашла потерянные пороги. Ограничение — ради скорости и
# узкого радиуса правки, а не потому, что остальные разделы небезопасны: скрипт идемпотентен.
DEFAULT_SECTIONS = ("IX", "XXIV", "XXV", "XXVI")

_FOOTNOTE = re.compile(r"<[^>]{0,12}>")
# «криогенный насос низкого давления (не менее 100 баллов):» — порог узла в скобке после имени.
NODE_RE = re.compile(r"^(?P<name>.+?)\s*\(\s*(?P<thr>не менее\s+\d[^)]*балл[^)]*)\)\s*:?\s*$", re.I)
# «…оцениваемых в совокупности суммарным количеством баллов, до 31 декабря 2020 г. - не менее …»
SCALE_RE = re.compile(r"суммарным количеством баллов[,]?\s*(?P<thr>.+?)\s*:?\s*$", re.I)


def _norm(s: str | None) -> str:
    """Нормализация для сопоставления: без сносок, пробелов и хвостовой пунктуации."""
    return re.sub(r"\s+", " ", _FOOTNOTE.sub(" ", s or "")).strip(" .,;:").lower()


def _detect_indent(text: str, default: int = 2) -> int:
    """Отступ, которым файл записан сейчас.

    В корпусе он РАЗНЫЙ: часть разделов записана `structure_kb` с отступом 2, часть переписана
    другими скриптами с отступом 1. Пишем тем же — иначе диффом становится весь файл целиком,
    и правка на четыре строки выглядит как перезапись раздела: такую не прочитать глазами и не
    отличить от подмены данных."""
    for ln in text.split("\n")[1:]:
        if ln.strip().startswith("{"):
            return len(ln) - len(ln.lstrip(" "))
    return default


def _lines(product) -> list[str]:
    return [ln.strip() for ln in re.sub(r"[ \t]+", " ", product.raw_block()).split("\n") if ln.strip()]


def _node_thresholds(lines: list[str]) -> dict[str, str]:
    """Правило A: {нормализованное имя узла → дословный порог}."""
    out: dict[str, str] = {}
    for ln in lines:
        m = NODE_RE.match(ln)
        if m:
            out.setdefault(_norm(m.group("name")), m.group("thr").strip())
    return out


def _scale_thresholds(lines: list[str]) -> tuple[dict[str, str], list[str]]:
    """Правило B: ({заголовок блока → шкала}, ничейные шкалы).

    Ничейная шкала — вводная, над которой нет заголовка-блока: это шкала всей позиции, и она
    нужна правилу C. Заголовком считаем короткую строку с двоеточием на конце («Изготовление
    смычков:»), а не любую предыдущую строку: иначе шкала прилипнет к случайному тексту."""
    by_head: dict[str, str] = {}
    orphan: list[str] = []
    for i, ln in enumerate(lines):
        m = SCALE_RE.search(ln)
        if not m or "не менее" not in m.group("thr").lower():
            continue
        thr = m.group("thr").strip(" ,;:")
        head = lines[i - 1] if i else ""
        if head.endswith(":") and len(head) < 120 and not SCALE_RE.search(head):
            by_head[_norm(head)] = thr
        else:
            orphan.append(thr)
    return by_head, orphan


def _with_threshold(block: dict, thr: str) -> dict:
    """Кладёт порог сразу после `component` — чтобы диффы корпуса читались глазами."""
    out: dict = {}
    for k, v in block.items():
        out[k] = v
        if k == "component":
            out["min_threshold"] = thr
    if "min_threshold" not in out:
        out["min_threshold"] = thr
    return out


def _all_ops(rec: dict) -> list[str]:
    return [_norm(o.get("text")) for b in (rec.get("requirement_blocks") or [])
            for o in (b.get("operations") or [])]


def _preserved(old: dict, new: dict) -> tuple[bool, str]:
    """Правила сохранности D6: не терять ни баллов, ни операций."""
    lost = vs.record_points(old) - vs.record_points(new)
    if lost:
        return False, f"потеряны баллы {sorted(lost)}"
    if not set(_all_ops(old)) <= set(_all_ops(new)):
        return False, "потерян текст операции"
    if len(_all_ops(new)) < len(_all_ops(old)):
        return False, "операций стало меньше"
    return True, ""


def fix_record(rec: dict, product) -> tuple[dict, list[str], list[str]]:
    """Возвращает (новая запись, изменения, предупреждения). Вход не мутируется.

    Предупреждения отделены от изменений намеренно: отказ тронуть запись — не правка, и если
    считать его правкой, скрипт перестаёт быть идемпотентным (каждый прогон «что-то менял»),
    а в отчёте появляется работа, которой не было."""
    new = deepcopy(rec)
    lines = _lines(product)
    nodes = _node_thresholds(lines)
    by_head, orphan = _scale_thresholds(lines)
    changes: list[str] = []
    warnings: list[str] = []

    blocks = new.get("requirement_blocks") or []
    for i, b in enumerate(blocks):
        if (b.get("min_threshold") or "").strip():
            continue                                   # уже проставлен — идемпотентность
        key = _norm(b.get("component"))
        thr = nodes.get(key) or by_head.get(key)
        if thr:
            blocks[i] = _with_threshold(b, thr)
            changes.append(f"блок «{(b.get('component') or '')[:44]}» → порог «{thr[:64]}»")

    # Правило C — порог позиции вытеснен другим требованием.
    if not vs._points_in(new.get("min_threshold")) and len(orphan) == 1:
        old_thr = (new.get("min_threshold") or "").strip()
        src = next((ln for ln in lines if _norm(ln) == _norm(old_thr)), None) if old_thr else None
        if old_thr and not src:
            # Вытесненного текста нет в источнике дословно — трогать нельзя: перезапись стёрла бы
            # требование, которого мы не умеем восстановить.
            warnings.append(f"⚠ порог позиции не тронут: «{old_thr[:60]}» не найден в источнике")
        else:
            new["min_threshold"] = orphan[0]
            changes.append(f"порог позиции → «{orphan[0][:64]}»")
            if src:
                known = set(_all_ops(new)) | {_norm(b.get("component")) for b in blocks}
                if _norm(src) not in known and blocks:
                    blocks[0].setdefault("operations", []).append({"text": src, "points": None})
                    changes.append(f"вытесненное требование сохранено операцией: «{src[:56]}»")
    return new, changes, warnings


def process_section(roman: str, write: bool) -> tuple[int, int, list[str]]:
    """Возвращает (записей изменено, записей откачено, строки отчёта)."""
    src_f, js_f = vs.chunk_for(roman), vs.struct_for(roman)
    if not src_f or not js_f:
        return 0, 0, [f"  раздел {roman}: нет чанка или JSON — пропуск"]

    header, products = parse_section(src_f.read_text(encoding="utf-8"))
    original = js_f.read_text(encoding="utf-8")
    data = json.loads(original)
    payload = [r for r in data if r.get("record_type") != "section_methodology"]
    if len(products) != len(payload):
        # Та же честность, что в verify_structured: пары строим только когда состав совпадает.
        return 0, 0, [f"  раздел {roman}: продуктов {len(products)} ≠ записей {len(payload)} — "
                      f"сверка невозможна, раздел пропущен"]

    report: list[str] = []
    changed = rolled = 0
    idx = 0
    for pos, rec in enumerate(data):
        if rec.get("record_type") == "section_methodology":
            continue
        product = products[idx]
        idx += 1
        new, changes, warns = fix_record(rec, product)
        name = (rec.get("product_name") or "").split("\n")[0][:56]
        for w in warns:
            report.append(f"  [{roman}] {name}: {w}")
        if not changes:
            continue
        ok, why = _preserved(rec, new)
        if not ok:
            rolled += 1
            report.append(f"  ⚠ ОТКАТ [{roman}] {name}: {why}")
            continue
        data[pos] = new
        changed += 1
        report.append(f"  [{roman}] {name}")
        report.extend(f"      · {c}" for c in changes)

    if changed and write:
        text = json.dumps(data, ensure_ascii=False, indent=_detect_indent(original))
        js_f.write_text(text + "\n" if original.endswith("\n") else text, encoding="utf-8")
        report.append(f"  → записан {js_f.name}")
    return changed, rolled, report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sections", nargs="*", default=list(DEFAULT_SECTIONS),
                    help=f"разделы римскими цифрами (по умолчанию {' '.join(DEFAULT_SECTIONS)})")
    ap.add_argument("--write", action="store_true", help="записать изменения в корпус")
    args = ap.parse_args()

    total_changed = total_rolled = 0
    for roman in (args.sections or DEFAULT_SECTIONS):
        changed, rolled, report = process_section(roman.upper(), args.write)
        total_changed += changed
        total_rolled += rolled
        for line in report:
            print(line)

    print(f"\nИтого: записей изменено {total_changed}, откачено {total_rolled}"
          f"{'' if args.write else '  (ПРОБНЫЙ ПРОГОН — файлы не тронуты, нужен --write)'}")
    if args.write and total_changed:
        print("Дальше обязательно: --inheritance (карта), verify_structured --json (список D9), "
              "переиндексация Qdrant.")


if __name__ == "__main__":
    main()
