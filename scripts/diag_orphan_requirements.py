"""Диагностика позиций приложения 719 БЕЗ требований (R6, шаг 1 — только анализ, ничего не меняет).

ЗАЧЕМ. Глобальное ревью 10.08.2026 показало: у 349 из 1368 позиций (26 %) в структурированной базе
нет НИ ОДНОЙ операции требований. На каждый четвёртый товарный вопрос сервис физически не может
показать требования. Это задача T11 («наследование требований по группам»), помеченная как «2.0», —
по цифрам она причина ~четверти неответов и блокирует приёмочный гейт.

ЧТО ВЫЯСНИЛА РАЗВЕДКА. В приложении 719 группа видов продукции делит ОДНУ ячейку требований
(в исходном документе — вертикально объединённая ячейка таблицы). После конвертации RTF→текст
объединение теряется и превращается в пустую ячейку:

    28.22.14.125|Краны грузоподъемные стрелкового типа|<полные требования, 11 операций>|
    28.22.14.129|Краны грузоподъемные прочие||          <- пустая ячейка = «те же требования»
    29.10.51    |Автокраны||                            <- пустая
    28.22.14.151|Краны на гусеничном ходу|<свои требования>|   <- новая группа

Промпт парсера (`app/core/prompts.KB_PARSER_SYSTEM_PROMPT`) предупреждён про пустые ПЕРВЫЕ ячейки
(строки-компоненты `||…`), но не про пустую ячейку ТРЕБОВАНИЙ. Модель, честно следуя правилу
«не выдумывать», записала в notes: «Во фрагменте приложения требования … не указаны».

ЗАЧЕМ ИМЕННО ДИАГНОСТИКА, А НЕ СРАЗУ ФИКС. Неверное наследование = неверные баллы, а это ХУЖЕ, чем
«требования не найдены»: эксперт получит правдоподобный, но чужой перечень и не заметит подмены
(faithfulness-гард проверяет заземлённость числа в контексте, а не правильность его привязки).
Поэтому скрипт не чинит данные, а классифицирует позиции и присваивает каждой УРОВЕНЬ УВЕРЕННОСТИ,
чтобы фикс применялся только там, где родитель определён надёжно.

КАТЕГОРИИ:
  INHERIT   — строка есть в исходнике, ячейка требований пуста, выше в той же группе есть родитель
              с требованиями. Кандидат на наследование. Уверенность — см. ниже.
  IN_COMPONENT — требование распознано, но парсер положил его в `component`, а `operations` пуст;
              данные НЕ потеряны, это дефект рендера (`format_context` читает только operations).
  EXCLUDED  — позиция исключена из приложения (строка-заглушка рядом).
  NO_PARENT — ячейка пуста и родителя выше нет: в исходнике требований действительно нет.
  NO_SOURCE — строку в исходном чанке сопоставить не удалось (дефект нарезки или расхождение имён);
              требует ручного разбора, автоматике не отдавать.

УВЕРЕННОСТЬ для INHERIT (складывается из независимых сигналов):
  HIGH   — родитель на той же ветке ОКПД2 ЛИБО текст родителя дословно упоминает наименование
           потомка (как «…на продукцию (…; прицепы и полуприцепы прочие)») — прямая улика.
  MEDIUM — родитель непосредственно предшествует (между ними нет других строк с требованиями),
           но ни ветка, ни упоминание не совпали.
  LOW    — всё остальное: группа могла оборваться. В автофикс НЕ отдавать.

Запуск:
  .venv/Scripts/python.exe scripts/diag_orphan_requirements.py
  .venv/Scripts/python.exe scripts/diag_orphan_requirements.py --section XVIII --limit 20
  .venv/Scripts/python.exe scripts/diag_orphan_requirements.py --json scratchpad/orphans.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.rag import sparse  # noqa: E402  (стем-токенизация — та же, что в поиске)

CHUNKS_DIR = ROOT / "knowledge_base" / "pp719" / "chunks"
STRUCT_DIR = ROOT / "knowledge_base" / "pp719" / "structured"

# Строка-товар: «28.22.14.129|…», «из 28.30.7|…» и — важно — «19.20.32.112 <11>|…»: в ряде
# разделов (XXI, XIV) код несёт маркер сноски приложения. Без учёта сносок такая строка не
# распознавалась как начало товара и «прилипала» к предыдущей — 64 позиции не сопоставлялись.
# IGNORECASE обязателен: в исходнике встречается и «из 28.30.7|», и «Из 20|» (с заглавной).
# Без него строка-родитель группы не распознавалась, и все её потомки уходили в NO_PARENT —
# а следующие за ними позиции цеплялись к ЧУЖОМУ родителю ниже по таблице.
_ROW_START = re.compile(r"^(?:из\s+)?\d{2}(?:\.\d+)*(?:\s*<[^>|\n]{1,16}>)*\s*\|", re.IGNORECASE)
# Маркер сноски внутри ячейки кода — вырезаем до извлечения кодов, иначе «<11>» даёт «код» 11.
_FOOTNOTE = re.compile(r"<[^>|\n]{1,16}>")
# Строка, у которой объединена и ячейка КОДА. Два подвида различаются числом ведущих труб:
#   «||несущая рама: …»            — пусты обе первые ячейки → строка-КОМПОНЕНТ группы;
#   «|Катализаторы гидрокрекинга||» — пуст только код → это ТОВАР, унаследовавший код группы
#                                     (раздел XIV: код «20» объединён на всю группу катализаторов).
# Раньше ловился только первый подвид, и 10 позиций XIV не сопоставлялись с исходником.
_PIPE_START = re.compile(r"^\|")
_EXCLUDED = re.compile(r"Позиция\s*-\s*Исключена", re.IGNORECASE)
_AMENDMENT = re.compile(r"^\(в ред\.")
_CODE = re.compile(r"\d{2}(?:\.\d+)*")
# Раздел чанка: «04_III_specmashinostroenie.txt» → III
_CHUNK_ROMAN = re.compile(r"^\d+_([IVXLC]+)_")


@dataclass
class Row:
    """Строка таблицы приложения из исходного чанка."""
    kind: str            # product | component | excluded | amendment
    codes: list[str]
    name: str
    req: str
    line_no: int
    group: int = -1      # id группы (общая ячейка требований)


@dataclass
class Finding:
    record: dict
    category: str
    confidence: str = ""
    parent: dict | None = None
    signals: list[str] = field(default_factory=list)
    detail: str = ""


# --------------------------------------------------------------------------- #
# Разбор исходного чанка
# --------------------------------------------------------------------------- #
def parse_chunk(text: str) -> list[Row]:
    """Чанк раздела → строки таблицы. Ячейка требований многострочная, поэтому строку
    считаем начатой только по явному маркеру, всё остальное — продолжение предыдущей."""
    rows: list[Row] = []
    buf: list[str] = []
    kind = None
    start_line = 0

    def flush():
        if kind is None:
            return
        block = "\n".join(buf)
        cells = block.split("|")
        k = kind
        if k == "pipe":
            # Ячейка кода объединена: имя во 2-й ячейке → товар; пусто → строка-компонент группы.
            k = "product" if (len(cells) > 1 and cells[1].strip()) else "component"
        if k == "product":
            rows.append(Row(
                kind="product",
                codes=_CODE.findall(_FOOTNOTE.sub(" ", cells[0])) if cells else [],
                name=(cells[1] if len(cells) > 1 else "").strip(),
                req=(cells[2] if len(cells) > 2 else "").strip(),
                line_no=start_line,
            ))
        else:
            rows.append(Row(kind=k, codes=[], name="", req=block.strip(), line_no=start_line))

    for i, ln in enumerate(text.split("\n"), 1):
        new_kind = None
        if _ROW_START.match(ln):
            new_kind = "product"
        elif _PIPE_START.match(ln):
            new_kind = "pipe"  # товар с объединённым кодом ИЛИ компонент — решается в flush()
        elif _EXCLUDED.search(ln) and len(ln) < 120:
            new_kind = "excluded"
        elif _AMENDMENT.match(ln):
            new_kind = "amendment"
        if new_kind:
            flush()
            kind, buf, start_line = new_kind, [ln], i
        elif kind is not None:
            buf.append(ln)
    flush()
    return rows


def assign_groups(rows: list[Row]) -> None:
    """Группа = строка-товар С требованиями + следующие за ней товары с ПУСТОЙ ячейкой
    (+ строки-компоненты). Поправки и заглушки-исключения группу не разрывают."""
    gid = -1
    for r in rows:
        if r.kind == "product" and r.req:
            gid += 1
        r.group = gid


# --------------------------------------------------------------------------- #
# Структурированные записи
# --------------------------------------------------------------------------- #
def load_structured() -> list[dict]:
    recs: list[dict] = []
    for f in sorted(STRUCT_DIR.glob("*.json")):
        recs.extend(json.loads(f.read_text(encoding="utf-8")))
    return [r for r in recs if r.get("record_type") != "section_methodology"]


def n_operations(rec: dict) -> int:
    """Сколько требований позиции РЕАЛЬНО попадёт в контекст ответа.

    Считаем ровно по правилу рантайма (`pipeline._hit_operations`): блок без `operations`, но с
    текстом в `component`, — это требование, а не заголовок узла. Диагностика обязана мерить то,
    что видит пользователь; иначе после фикса рендера отчёт продолжал бы показывать «пусто» там,
    где в ответе уже есть требования.

    ⚠ Правило рантайма шире ровно на один случай: блок ТОЛЬКО с `note` (без `operations` и без
    `component`) пользователю показывается, а здесь даст 0. Сейчас таких блоков в корпусе нет
    (проверено 14.08.2026), поэтому расхождение дремлет. Если парсер их заведёт, позиция уедет
    в «сироты» и попадёт в карту наследования, но прочитана оттуда не будет: `format_context`
    увидит непустые `ops` и мимо `inheritance.lookup` пройдёт — молчаливый no-op того же класса,
    что стоил двух недель в D6."""
    n = 0
    for b in (rec.get("requirement_blocks") or []):
        ops = b.get("operations") or []
        n += len(ops) if ops else (1 if (b.get("component") or "").strip() else 0)
    return n


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _stems(s: str | None) -> set[str]:
    return {t for t in sparse.tokenize(s or "") if len(t) > 3}


def same_branch(a: list[str], b: list[str]) -> bool:
    """Коды лежат на одной ветке ОКПД2 (посегментно один — префикс другого). Для сопоставления
    записи со строкой исходника (там нужен именно префикс: «28.30.7» ↔ «из 28.30.7»)."""
    for x in a:
        for y in b:
            sx, sy = x.split("."), y.split(".")
            n = min(len(sx), len(sy))
            if n and sx[:n] == sy[:n]:
                return True
    return False


def kinship(a: list[str], b: list[str]) -> int:
    """Длина общего префикса кодов в СЕГМЕНТАХ — «насколько близкая родня».

    Отличается от `same_branch` намеренно: родитель и потомок в объединённой ячейке — чаще всего
    БРАТЬЯ («28.22.14.125» и «28.22.14.129»), а не предок/потомок. Строгий префикс их не ловит
    (4-й сегмент различается), поэтому меряем общую часть: ≥3 сегмента = одна группа ОКПД2."""
    best = 0
    for x in a:
        for y in b:
            sx, sy = x.split("."), y.split(".")
            n = 0
            for p, q in zip(sx, sy):
                if p != q:
                    break
                n += 1
            best = max(best, n)
    return best


def match_row(rec: dict, rows: list[Row]) -> Row | None:
    """Строка исходника для записи. Цепочка: код+имя → имя → код (первое однозначное совпадение)."""
    name = _norm(rec.get("product_name"))
    codes = [c.strip() for c in (rec.get("okpd2_codes") or [])]
    prods = [r for r in rows if r.kind == "product"]

    exact = [r for r in prods if _norm(r.name) == name and same_branch(r.codes, codes)]
    if len(exact) == 1:
        return exact[0]
    # ИМЯ приоритетнее кода. Причина конкретная: в разделах с объединённой ячейкой КОДА (XXI,
    # фторопласты) у 15 строк подряд код пуст, и «единственной точной по коду» оказывается ЧУЖАЯ
    # строка выше — сопоставление уезжает на соседний продукт. Имя же там уникально.
    by_name = [r for r in prods if _norm(r.name) == name]
    if len(by_name) == 1:
        return by_name[0]
    # Точное равенство кода — следующая ступень: выручает обратный случай, когда ИМЯ неоднозначно
    # («29.10.44» с требованиями и «29.10.44.000» с пустой ячейкой названы одинаково).
    same_code = [r for r in prods if set(r.codes) & set(codes)]
    if len(same_code) == 1:
        return same_code[0]
    if exact:
        return exact[0]  # несколько одинаковых — берём первую
    by_code = [r for r in prods if same_branch(r.codes, codes)]
    if len(by_code) == 1:
        return by_code[0]
    return None


def find_parent(row: Row, rows: list[Row]) -> Row | None:
    """Родитель группы — ближайшая выше строка-товар С требованиями в ТОЙ ЖЕ группе."""
    idx = rows.index(row)
    for k in range(idx - 1, -1, -1):
        r = rows[k]
        if r.kind == "product" and r.req and r.group == row.group:
            return r
    return None


def classify(rec: dict, rows: list[Row], ops_by_name: dict[str, int] | None = None) -> Finding:
    blocks = rec.get("requirement_blocks") or []
    comp_texts = [(b.get("component") or "").strip() for b in blocks]
    long_comp = [c for c in comp_texts if len(c) > 40]

    row = match_row(rec, rows)
    if row is None:
        return Finding(rec, "NO_MATCH", detail="строка не сопоставлена с исходным чанком")

    # Требование распознано, но лежит в component — данные есть, теряет их рендер.
    if long_comp:
        return Finding(rec, "IN_COMPONENT", confidence="HIGH",
                       detail=f"{len(long_comp)} блок(ов) с текстом требования в поле component")

    if row.req:
        # У строки ЕСТЬ требования в исходнике, но операций в записи нет — потеря при структуризации.
        # Это ДРУГОЙ дефект, чем наследование: чинится перепарсингом позиции, не переносом от родителя.
        return Finding(rec, "PARSER_LOSS", confidence="HIGH",
                       detail=f"в исходнике {len(row.req)} симв. требований, в записи операций нет")

    idx = rows.index(row)
    if any(r.kind == "excluded" for r in rows[max(0, idx - 2):idx + 2]):
        return Finding(rec, "EXCLUDED", detail="рядом строка-заглушка «Позиция - Исключена»")

    parent = find_parent(row, rows)
    if parent is None:
        return Finding(rec, "NO_PARENT", detail="выше в группе нет строки с требованиями")

    # --- сигналы ---------------------------------------------------------------------------
    # Базовый факт (пустая ячейка требований внутри группы = продолжение объединённой ячейки)
    # ДОКАЗАН на устройстве исходника: в RTF ПП №719 1235 меток \clvmrg, а striprtf рендерит
    # такую ячейку пустой (проверено на минимальном RTF). Поэтому остаточный риск здесь — НЕ
    # «а вдруг это не объединение», а «а вдруг МОЙ разбор неверно определил границу группы».
    # Сигналы ниже меряют именно это.
    p_idx = rows.index(parent)
    signals: list[str] = []
    kin = kinship(parent.codes, row.codes)
    if kin >= 3:
        signals.append(f"одна группа ОКПД2 (общих сегментов: {kin})")
    elif kin == 2:
        signals.append("смежные коды ОКПД2 (2 сегмента)")
    if _stems(row.name) and _stems(row.name) <= _stems(parent.req):
        signals.append("родитель дословно упоминает наименование потомка")

    between = rows[p_idx + 1:idx]
    broke = [r for r in between if r.kind == "excluded"]
    prod_between = [r for r in between if r.kind == "product"]
    if not prod_between:
        signals.append("сразу за родителем")
    elif not any(r.req for r in prod_between):
        signals.append(f"внутри непрерывного блока пустых ячеек ({len(prod_between)} перед ней)")

    if broke:
        conf = "MEDIUM"   # между родителем и потомком есть заглушка-исключение — граница группы под вопросом
        signals.append("между родителем и потомком строка «Позиция - Исключена»")
    elif any(r.req for r in prod_between):
        conf = "LOW"      # разбор группы ненадёжен: внутри «пустого» блока попалась строка с требованиями
    else:
        conf = "HIGH"     # непрерывный блок пустых ячеек сразу под родителем — прямой случай объединения

    # ПОРЯДОК РАБОТ ДЛЯ ШАГА 2: наследовать от родителя, чья СОБСТВЕННАЯ запись пуста, бессмысленно —
    # потомок получит те же ноль операций. Такие родители встречаются: напр. «Спасательные жилеты»
    # (XVIII) сам попал в IN_COMPONENT (требование лежит в component). Значит, IN_COMPONENT и
    # PARSER_LOSS чинятся ПЕРВЫМИ, иначе часть наследования отработает вхолостую.
    p_ops = (ops_by_name or {}).get(_norm(parent.name))
    if p_ops == 0:
        signals.append("⚠ запись родителя сама без операций — сначала починить родителя")

    return Finding(
        rec, "INHERIT", confidence=conf, signals=signals,
        parent={"codes": parent.codes, "name": parent.name,
                "req_chars": len(parent.req), "line_no": parent.line_no,
                "rows_above": len(prod_between) + 1, "record_operations": p_ops},
        detail=f"группа {row.group}, родитель на строке {parent.line_no}",
    )


# --------------------------------------------------------------------------- #
# Отчёт
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# R29: расколотая общая ячейка — требования одной группы разлились по строкам
# --------------------------------------------------------------------------- #
# Зачины настоящего требования. Если ячейка следующей строки начинается НЕ с них, а предыдущая
# оборвана двоеточием — перед нами продолжение одного списка, растащенное по строкам таблицы.
_OPENER = re.compile(
    r"^(налич|осуществл|выполн|при производств|до \d|с \d+ января|соблюден|оформлен|применяем|"
    r"расположен|производств|использован|проведен|обеспечен|\d+\.\s)", re.IGNORECASE)


def find_fragmented(rows: list[Row]) -> list[list[Row]]:
    """Группы строк, между которыми расколот ОДИН список требований.

    Сигнатура: ячейка обрывается двоеточием, а следующая строка-товар начинается не как
    требование. Пробег продолжаем, пока строки выглядят продолжением (не начинаются с зачина).

    ВАЖНО про `\\d+\\.` в зачинах: без него ловилось ложное срабатывание — «Средства защиты
    информации» (IX) имеют 27 СОБСТВЕННЫХ требований, просто пронумерованных «1. соответствие
    заявителя…». Это не обрывок."""
    prods = [r for r in rows if r.kind == "product"]
    out: list[list[Row]] = []
    for k, r in enumerate(prods[:-1]):
        if not (r.req and r.req.rstrip().endswith(":")):
            continue
        run = [r]
        for nxt in prods[k + 1:]:
            if not nxt.req or _OPENER.match(nxt.req.strip()):
                break
            run.append(nxt)
        if len(run) > 1:
            out.append(run)
    return out


# --------------------------------------------------------------------------- #
# R6 шаг 3: карта наследования требований от родителя группы
# --------------------------------------------------------------------------- #
def record_operations(rec: dict) -> list[dict]:
    """Требования записи ровно по правилу рантайма (`pipeline._hit_operations`).

    ⚠ Обещание «ровно по правилу рантайма» до 14.08.2026 не выполнялось: функция плющила блоки в
    плоский список текстов, теряя вводную фразу блока (K4) и условие (`note`, D9). Наследник видел
    операции БЕЗ требования, частью которого они являются, — то есть без «осуществление НА
    ТЕРРИТОРИИ РОССИЙСКОЙ ФЕДЕРАЦИИ следующих технологических операций». Это ровно претензия
    июльского теста, ради которой заведена K4, и она обошла стороной 327 из 331 наследника:
    правка применилась к прямому пути и не применилась к унаследованному.

    Поэтому вводную строку блока собираем ТОЙ ЖЕ функцией, что и рантайм, — иначе две половины
    правила снова разойдутся молча (как разошлись половины ключа наследования в D6)."""
    from app.rag.pipeline import NOTE_CAP_TARGET, _block_intro

    out: list[dict] = []
    for b in rec.get("requirement_blocks") or []:
        ops = b.get("operations") or []
        comp = (b.get("component") or "").strip()
        note = (b.get("note") or "").strip()
        # D9: порог блока — часть вводной строки, и наследник обязан видеть его так же, как
        # прямой путь. Порог позиции сюда не передаём: у наследника он свой (или отсутствует),
        # а глушение дубля — забота рантайма, который знает обе величины.
        thr = (b.get("min_threshold") or "").strip()
        if ops:
            dup = comp and any(comp.lower() == (o.get("text") or "").strip().lower() for o in ops)
            parent = _block_intro(comp if not dup else "", note, NOTE_CAP_TARGET, thr)
            for o in ops:
                item = {"text": (o.get("text") or ""), "points": o.get("points")}
                if parent:
                    item["_parent"] = parent
                out.append(item)
            continue
        text = _block_intro(comp, note, NOTE_CAP_TARGET, thr)
        if text:
            out.append({"text": text, "points": None})
    return out


def _map_key(section: str | None, name: str | None) -> str:
    """Ключ позиции: раздел + ПЕРВАЯ строка наименования без маркеров сносок.

    Раздел обязателен — одноимённые позиции встречаются в разных разделах, без него наследование
    уехало бы в чужой раздел. Сноски (`<9>`) снимаем: в исходной таблице имя пишется со сноской
    («Спасательные жилеты <9>»), а в записи базы — без неё, и без нормализации не находился
    родитель у 130 позиций, включая весь раздел XVIII (99 наследников).

    Хвостовая пунктуация — та же история (D6): у имени-перечня из таблицы после снятия сноски
    остаётся « ;» («…на дальнем расстоянии ;»), а наименование записи базы её не содержит.
    Ключи расходились, и родитель уходил в отказ «не найден в базе». ВАЖНО: правило обязано
    совпадать с `app.rag.inheritance._key` — карту строит этот скрипт, а читает рантайм."""
    first = (name or "").strip().split("\n")[0]
    first = _FOOTNOTE.sub(" ", first)
    first = re.sub(r"\s+", " ", first).strip().strip(";,.").strip()
    return f"{section or '?'}|{first.lower()}"


def build_inheritance_map(recs: list[dict], chunks: dict[str, list[Row]], out: Path) -> None:
    """Строит карту «позиция без требований → требования родителя её группы».

    Осознанные ОТКАЗЫ (лучше не показать, чем показать чужое):
      * только INHERIT/HIGH — MEDIUM/LOW и прочие категории не берём;
      * родитель обязан иметь требования сам (иначе наследовать нечего);
      * позиции из R29 (расколотая ячейка) исключаются с обеих сторон: там текст родителя —
        оборванная вводная, потомок получил бы фразу без списка.
    """
    from app.rag import fragments  # локальный импорт: скрипт работает и без поднятого приложения

    by_key = {_map_key(r.get("section_roman"), r.get("product_name")): r for r in recs}
    ops_by_name: dict[str, int] = {}
    for r in recs:
        k = _norm(r.get("product_name"))
        ops_by_name[k] = max(ops_by_name.get(k, 0), n_operations(r))

    parents: dict[str, dict] = {}
    children: dict[str, str] = {}
    skipped: Counter = Counter()

    for rec in [r for r in recs if n_operations(r) == 0]:
        sec = rec.get("section_roman")
        rows = chunks.get(sec or "")
        if not rows:
            skipped["нет чанка раздела"] += 1
            continue
        f = classify(rec, rows, ops_by_name)
        if f.category != "INHERIT" or f.confidence != "HIGH":
            skipped[f"не INHERIT/HIGH ({f.category})"] += 1
            continue
        if fragments.is_fragmented(rec.get("product_name")) or fragments.is_fragmented(f.parent["name"]):
            skipped["R29: расколотая ячейка"] += 1
            continue
        p_rec = by_key.get(_map_key(sec, f.parent["name"]))
        if p_rec is None:
            skipped["родитель не найден в базе"] += 1
            continue
        p_ops = record_operations(p_rec)
        if not p_ops:
            skipped["у родителя самого нет требований"] += 1
            continue
        p_key = _map_key(sec, p_rec.get("product_name"))
        parents.setdefault(p_key, {
            "section": sec,
            "product_name": (p_rec.get("product_name") or "").split("\n")[0],
            "okpd2_codes": p_rec.get("okpd2_codes") or [],
            "min_threshold": p_rec.get("min_threshold"),
            "operations": p_ops,
        })
        children[_map_key(sec, rec.get("product_name"))] = p_key

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "_comment": ("Карта наследования требований по группам приложения 719 (R6). Генерируется "
                     "детерминированно: scripts/diag_orphan_requirements.py --inheritance. "
                     "Правится ТОЛЬКО перегенерацией."),
        "parents": parents, "children": children,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"Карта наследования → {out}")
    print(f"  позиций-наследников: {len(children)}")
    print(f"  различных родителей: {len(parents)}")
    print(f"  требований у родителей суммарно: {sum(len(p['operations']) for p in parents.values())}")
    print("\n  ОТКАЗЫ (осознанные):")
    for k, v in skipped.most_common():
        print(f"    {v:>4}  {k}")
    per_sec = Counter(k.split("|")[0] for k in children)
    print("\n  по разделам:", dict(per_sec.most_common(10)))


def main() -> None:
    ap = argparse.ArgumentParser(description="Диагностика позиций 719 без требований (R6, шаг 1)")
    ap.add_argument("--section", help="ограничить одним разделом (римская цифра, напр. XVIII)")
    ap.add_argument("--limit", type=int, default=0, help="сколько примеров печатать (0 = только сводка)")
    ap.add_argument("--json", help="куда сложить машиночитаемый результат")
    ap.add_argument("--fragments", metavar="PATH",
                    help="R29: выгрузить позиции с РАСКОЛОТОЙ общей ячейкой требований")
    ap.add_argument("--inheritance", metavar="PATH",
                    help="R6 шаг 3: построить карту наследования требований от родителя группы")
    args = ap.parse_args()

    chunks: dict[str, list[Row]] = {}
    for f in sorted(CHUNKS_DIR.glob("*.txt")):
        m = _CHUNK_ROMAN.match(f.name)
        if not m:
            continue
        rows = parse_chunk(f.read_text(encoding="utf-8"))
        assign_groups(rows)
        chunks.setdefault(m.group(1), rows)

    recs = load_structured()

    if args.fragments:
        groups = []
        for sec, rows in chunks.items():
            for run in find_fragmented(rows):
                groups.append({
                    "section": sec,
                    "reason": "требования группы расколоты между строками таблицы (R29)",
                    "positions": [{"okpd2_codes": r.codes, "product_name": r.name,
                                   "req_preview": " ".join(r.req.split())[:120]} for r in run],
                })
        out = Path(args.fragments)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(groups, ensure_ascii=False, indent=1), encoding="utf-8")
        n_pos = sum(len(g["positions"]) for g in groups)
        print(f"R29: групп с расколотой ячейкой {len(groups)}, позиций в них {n_pos} → {out}")
        for g in groups:
            print(f"\n  [{g['section']}]")
            for p in g["positions"]:
                print(f"     {','.join(p['okpd2_codes']) or '(код объединён)':<16} "
                      f"{p['product_name'][:46]:<46} {p['req_preview'][:60]}")
        return

    if args.inheritance:
        build_inheritance_map(recs, chunks, Path(args.inheritance))
        return

    orphans = [r for r in recs if n_operations(r) == 0]
    if args.section:
        orphans = [r for r in orphans if r.get("section_roman") == args.section]

    # Сколько операций в ЗАПИСИ по её наименованию — чтобы поймать «родитель сам пуст».
    ops_by_name: dict[str, int] = {}
    for r in recs:
        ops_by_name[_norm(r.get("product_name"))] = max(
            ops_by_name.get(_norm(r.get("product_name")), 0), n_operations(r))

    findings: list[Finding] = []
    for rec in orphans:
        rows = chunks.get(rec.get("section_roman") or "")
        if not rows:
            findings.append(Finding(rec, "NO_MATCH", detail="чанк раздела не найден"))
            continue
        findings.append(classify(rec, rows, ops_by_name))

    total_prod = len(recs)
    print("=" * 78)
    print("ДИАГНОСТИКА ПОЗИЦИЙ БЕЗ ТРЕБОВАНИЙ (R6, шаг 1)")
    print("=" * 78)
    print(f"Всего позиций в базе: {total_prod} | без единой операции: {len(orphans)} "
          f"({100 * len(orphans) / total_prod:.0f} %)")
    if args.section:
        print(f"Фильтр: раздел {args.section}")

    by_cat = Counter(f.category for f in findings)
    print("\n--- КАТЕГОРИИ ---")
    for cat, n in by_cat.most_common():
        print(f"  {cat:<13} {n:>4}  ({100 * n / len(findings):.0f} %)")

    inherit = [f for f in findings if f.category == "INHERIT"]
    if inherit:
        conf = Counter(f.confidence for f in inherit)
        print("\n--- УВЕРЕННОСТЬ НАСЛЕДОВАНИЯ ---")
        for c in ("HIGH", "MEDIUM", "LOW"):
            if conf.get(c):
                print(f"  {c:<7} {conf[c]:>4}")
        sig = Counter(s for f in inherit for s in f.signals)
        print("\n--- СИГНАЛЫ ---")
        for s, n in sig.most_common():
            print(f"  {n:>4}  {s}")

    print("\n--- ПО РАЗДЕЛАМ (все категории) ---")
    per_sec: dict[str, Counter] = defaultdict(Counter)
    for f in findings:
        per_sec[f.record.get("section_roman") or "?"][f.category] += 1
        if f.category == "INHERIT":
            per_sec[f.record.get("section_roman") or "?"]["_" + f.confidence] += 1
    hdr = f"  {'разд.':<7}{'всего':>6}{'INHERIT':>9}{'(HIGH)':>8}{'PARSER_LOSS':>13}{'NO_MATCH':>10}{'проч.':>7}"
    print(hdr)
    for sec, c in sorted(per_sec.items(), key=lambda kv: -sum(
            v for k, v in kv[1].items() if not k.startswith("_")))[:14]:
        tot = sum(v for k, v in c.items() if not k.startswith("_"))
        other = tot - c["INHERIT"] - c["PARSER_LOSS"] - c["NO_MATCH"]
        print(f"  {sec:<7}{tot:>6}{c['INHERIT']:>9}{c['_HIGH']:>8}{c['PARSER_LOSS']:>13}"
              f"{c['NO_MATCH']:>10}{other:>7}")

    if args.limit:
        print(f"\n--- ПРИМЕРЫ (до {args.limit}) ---")
        for f in findings[:args.limit]:
            rec = f.record
            head = f"[{rec.get('section_roman')}] {(rec.get('product_name') or '')[:46]}"
            code = (rec.get("okpd2_codes") or ["—"])[0]
            print(f"\n  {head:<58} {code}")
            print(f"      {f.category} {f.confidence}  — {f.detail}")
            if f.parent:
                print(f"      родитель: {', '.join(f.parent['codes']) or '—'} «{f.parent['name'][:44]}» "
                      f"({f.parent['req_chars']} симв. требований)")
            if f.signals:
                print(f"      сигналы: {'; '.join(f.signals)}")

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps([{
            "section": f.record.get("section_roman"),
            "product_name": f.record.get("product_name"),
            "okpd2_codes": f.record.get("okpd2_codes"),
            "source_anchor": f.record.get("source_anchor"),
            "category": f.category,
            "confidence": f.confidence,
            "signals": f.signals,
            "parent": f.parent,
            "detail": f.detail,
        } for f in findings], ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n[OK] машиночитаемый результат → {out}")

    blocked = [f for f in inherit if any(s.startswith("⚠") for s in f.signals)]
    print("\n" + "=" * 78)
    auto = sum(1 for f in inherit if f.confidence == "HIGH")
    print(f"ВЫВОД: к автонаследованию готовы {auto} позиций (INHERIT/HIGH) из {len(findings)}.")
    if blocked:
        print(f"  ⚠ из них {len(blocked)} ждут починки РОДИТЕЛЯ (его запись сама без операций) —")
        print(f"    поэтому порядок шага 2: сперва IN_COMPONENT/PARSER_LOSS, затем наследование.")
    rest = len(findings) - auto
    print(f"  Ручной разбор: {rest} (IN_COMPONENT / PARSER_LOSS / NO_MATCH / NO_PARENT / MEDIUM / LOW).")
    print("=" * 78)


if __name__ == "__main__":
    main()
