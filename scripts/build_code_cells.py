"""D16 #173: ячейка КОДА первоисточника для каждой позиции товарного корпуса — пометка «из» и
«за исключением» у каждого кода. Только данные, без Qdrant и LLM.

ЗАЧЕМ. Прим. 3 к приложению: если коду предшествует «из», требования применяются ТОЛЬКО к товарам,
названным в графе «Наименование товара». `structure_kb` разбирал «из 32.50.22.110» в
`okpd2_codes: ["32.50.22.110"]` — пометка терялась, и модель читала частичную позицию как
охватывающую весь код («Бедренная ножка SL-Stem» → позиция «Протезы верхних конечностей», отчёт
22.09, случай ⑤). Тем же путём терялось «за исключением»: у четырёх позиций
(«26.20.11 (за исключением 26.20.11.120, 26.20.11.130)») ИСКЛЮЧЁННЫЕ коды записаны в
`okpd2_codes` наравне со своими.

КАНАЛ — РАНТАЙМ, БЕЗ ПЕРЕИНДЕКСАЦИИ: файл `knowledge_base/pp719/code_cells.json` читает
`app/rag/code_cells.py` по разделу, наименованию и кодам позиции — теми же строками, что лежат в
payload (они берутся из тех же `structured/*.json`). Правится ТОЛЬКО перегенерацией:
    .venv\\Scripts\\python scripts/build_code_cells.py            # пишет файл, печатает радиус
    .venv\\Scripts\\python scripts/build_code_cells.py --check    # код 1, если файл отстал
Отставание файла от корпуса ловит тест (`tests/test_code_cells.py`), а не память: при
актуализации редакции его перегенерируют вместе с `structured/`.

КАК СОПОСТАВЛЯЕТСЯ. Чанк раздела (`chunks/<NN>_<stem>.txt`) ↔ файл раздела
(`structured/<stem>.json`). Строка таблицы — ячейка кода (бывает многострочной:
«из 32.50.13.130,\\nиз 32.50.13.190,\\n…\\nиз 32.50.50.190|Имплантаты…», бывает
«из 28.13.24 из 28.29.22.190|…» и «26.20.11\\n(за исключением 26.20.11.120, 26.20.11.130)|…»),
затем наименование. Запись ищется по ступеням: тот же список кодов + то же наименование → тот же
список + начало наименования → тот же список → то же множество кодов (с исключёнными) +
наименование → только наименование. Несколько строк на ступени — допустимо, если их пометки
СОВПАДАЮТ (одноимённые позиции с тем же кодом в одном разделе); расходятся — «не определено».

⚠ Прежний разбор `diag_orphan_requirements.parse_chunk` многострочную ячейку кода не видел: строки
«из 32.50.13.130,» без трубы уходили в хвост ПРЕДЫДУЩЕЙ строки, и строка «Имплантаты…» получала
один код из девяти. Здесь ячейка собирается целиком.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from app.core.console import enable_utf8  # noqa: E402
from app.rag.code_cells import EXCEPT, IZ, PLAIN, key  # noqa: E402  (ключ — один на рантайм и сборку)

CHUNKS = ROOT / "knowledge_base" / "pp719" / "chunks"
STRUCTURED = ROOT / "knowledge_base" / "pp719" / "structured"
OUT = ROOT / "knowledge_base" / "pp719" / "code_cells.json"

_FOOTNOTE = re.compile(r"<[^>|\n]{1,16}>")
_EXCEPT = re.compile(r"\(\s*за\s+исключением\s+([^)]*)\)", re.I)
_CODE = re.compile(r"\d{2}(?:\.\d+)*")
_CODE_IZ = re.compile(r"(?:(из)\s+)?(\d{2}(?:\.\d+)*)", re.I)
# Ячейка кода целиком: коды, «из», запятые. IGNORECASE: в первоисточнике есть и «Из 20|».
_CELL = re.compile(r"^(?:\s*(?:из\s+)?\d{2}(?:\.\d+)*\s*,?)+\s*$", re.I)
_CHUNK_STEM = re.compile(r"^\d+_(.+)$")


def _is_cell(text: str) -> bool:
    """Текст — содержимое ячейки кода (коды, «из», сноски, «(за исключением …)»), и только оно."""
    rest = _EXCEPT.sub(" ", _FOOTNOTE.sub(" ", text))
    return not rest.strip() or bool(_CELL.match(rest))


def parse_cell(cell: str) -> tuple[list[tuple[str, bool]], list[str]]:
    """Ячейка кода → ([(код, есть ли «из»)…] в порядке первоисточника, [коды «за исключением»])."""
    cell = _FOOTNOTE.sub(" ", cell)
    excepted = [c for m in _EXCEPT.finditer(cell) for c in _CODE.findall(m.group(1))]
    cell = _EXCEPT.sub(" ", cell)
    return [(m.group(2), bool(m.group(1))) for m in _CODE_IZ.finditer(cell)], excepted


def parse_rows(text: str) -> list[dict]:
    """Строки-товары таблицы раздела: line, codes [(код, из)], excepted, name, merged_from.

    Строка без трубы, целиком состоящая из ячейки кода, — начало многострочной ячейки; её копим
    до строки с трубой. Всё прочее (требования, поправки «(в ред. …)», заглушки «Исключено»)
    накопленное сбрасывает.

    Строка с ПУСТОЙ ячейкой кода и непустым наименованием («|Жидкость Б-1|…», фторпродукты XXI;
    «|Катализаторы гидрокрекинга||», XIV) — товар, у которого ячейка кода объединена по вертикали с
    ближайшей строкой выше, где код есть: код и его «из» — те же (`merged_from` — строка-владелец
    ячейки). Без этого такая запись сопоставлялась бы с ПЕРВОЙ строкой раздела с тем же кодом, а не
    со своей ячейкой: пометки сейчас совпадают, но привязка была бы чужой."""
    rows: list[dict] = []
    pending: list[str] = []
    owner: dict | None = None
    for i, raw in enumerate(text.split("\n"), 1):
        s = raw.strip()
        if "|" in s:
            head, rest = s.split("|", 1)
            name = rest.split("|", 1)[0].strip()
            if head.strip() and _is_cell(head):
                codes, excepted = parse_cell(" ".join(pending + [head]))
                if codes:   # «(за исключением продукции … кодом X) <46>|» — продолжение ИМЕНИ
                    owner = {"line": i, "codes": codes, "excepted": excepted, "name": name,
                             "merged_from": None}
                    rows.append(owner)
            elif not head.strip() and not pending and name and owner is not None:
                rows.append({"line": i, "codes": owner["codes"], "excepted": owner["excepted"],
                             "name": name, "merged_from": owner["line"]})
            pending = []
        elif s and _is_cell(s) and (pending or _CODE_IZ.match(s)):
            pending.append(s)
        else:
            pending = []
    return rows


def _norm(s: str | None) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", _FOOTNOTE.sub(" ", s or "").lower()).split())


def _marks(rec_codes: list[str], row: dict) -> list[str] | None:
    """Пометка на КАЖДЫЙ код записи по строке первоисточника; None — код не выводится однозначно.

    Тот же список кодов → пометки по позиции (у «Шлюпок спасательных» один код стоит в ячейке
    ДВАЖДЫ — без «из» и с «из», — и только порядок их различает). Иначе — по коду: код есть в
    ячейке с одной пометкой → она; в «(за исключением …)» → EXCEPT; иначе / с разными — None."""
    row_codes = [c for c, _ in row["codes"]]
    if row_codes == rec_codes:
        return [IZ if iz else PLAIN for _, iz in row["codes"]]
    out = []
    for c in rec_codes:
        seen = {iz for rc, iz in row["codes"] if rc == c}
        if len(seen) == 1:
            out.append(IZ if seen.pop() else PLAIN)
        elif not seen and c in row["excepted"]:
            out.append(EXCEPT)
        else:
            return None
    return out


def _tiers(rec: dict):
    codes = [c.strip() for c in rec.get("okpd2_codes") or []]
    name = _norm(rec.get("product_name"))

    def same(r):
        return [c for c, _ in r["codes"]] == codes

    def prefix(r):
        n = _norm(r["name"])
        return bool(n) and (n.startswith(name[:40]) or name.startswith(n[:40]))

    return codes, [
        ("код и наименование", lambda r: same(r) and _norm(r["name"]) == name),
        ("код и начало наименования", lambda r: same(r) and prefix(r)),
        ("код", same),
        ("коды с исключёнными и наименование",
         lambda r: {c for c, _ in r["codes"]} | set(r["excepted"]) == set(codes)
         and _norm(r["name"]) == name),
        ("наименование", lambda r: _norm(r["name"]) == name),
    ]


def resolve(rec: dict, rows: list[dict]) -> dict:
    """Запись → {"marks": [...]} либо {"marks": None, "reason": "…"}; + "via", "lines" для отчёта."""
    codes, tiers = _tiers(rec)
    if not codes:
        return {"marks": None, "reason": "у позиции нет кода ОКПД2", "via": "—", "lines": []}
    for via, test in tiers:
        found = [r for r in rows if test(r)]
        if not found:
            continue
        variants = {tuple(m) if m is not None else None for m in (_marks(codes, r) for r in found)}
        lines = [r["line"] for r in found]
        if None in variants:
            return {"marks": None, "via": via, "lines": lines,
                    "reason": "код позиции не выводится из ячейки кода найденной строки однозначно"}
        if len(variants) > 1:
            return {"marks": None, "via": via, "lines": lines,
                    "reason": f"строк первоисточника {len(found)}, и пометки у них расходятся"}
        if via == "код":
            # ⚠ Ревью D16, LOW-2. Наименование записи со строкой не совпало ничем — у трёх записей
            # это заголовок раздела («Продукция радиоэлектроники» при пустой графе наименования,
            # XV «Отдельные виды технологического оборудования…» при настоящем перечне «Реакторы…;
            # коксовые камеры…»). Пояснение «только товары, названные в её наименовании» отсылало
            # бы модель к имени, которого в первоисточнике нет: ограничение стало бы видимостью.
            return {"marks": None, "via": via, "lines": lines,
                    "reason": "наименование позиции не совпадает с графой наименования строки "
                              "первоисточника (сопоставлено только по коду)"}
        return {"marks": list(variants.pop()), "via": via, "lines": lines}
    return {"marks": None, "reason": "строка первоисточника не найдена", "via": "—", "lines": []}


def load_sections() -> list[tuple[str, list[dict], list[dict]]]:
    """[(stem раздела, записи, строки чанка)] — чанк сопоставлен файлу раздела по имени."""
    chunks = {}
    for f in CHUNKS.glob("*.txt"):
        m = _CHUNK_STEM.match(f.stem)
        if m:
            chunks[m.group(1)] = f
    out = []
    for f in sorted(STRUCTURED.glob("*.json")):
        recs = [r for r in json.loads(f.read_text(encoding="utf-8"))
                if r.get("record_type") != "section_methodology"]
        if f.stem not in chunks:
            raise SystemExit(f"нет чанка для раздела {f.stem} — сопоставлять не с чем")
        out.append((f.stem, recs, parse_rows(chunks[f.stem].read_text(encoding="utf-8"))))
    return out


def build() -> tuple[dict, list[dict]]:
    """(содержимое файла, подробности по каждой записи для отчёта)."""
    positions: dict[str, dict] = {}
    details: list[dict] = []
    for stem, recs, rows in load_sections():
        for rec in recs:
            res = resolve(rec, rows)
            k = key(rec.get("section_roman"), rec.get("product_name"), rec.get("okpd2_codes"))
            entry = ({"marks": res["marks"]} if res["marks"] is not None
                     else {"marks": None, "reason": res["reason"]})
            prev = positions.get(k)
            if prev is not None and prev != entry:
                # Две записи с одинаковыми разделом, наименованием и кодами, а пометки разные:
                # рантайм их не различит — честнее не утверждать ни одну.
                entry = {"marks": None, "reason": "одноимённые позиции с теми же кодами расходятся "
                                                  "в пометке"}
            positions[k] = entry
            details.append({"section": stem, "key": k, **res})
    data = {
        "_about": "D16 #173: пометка «из» (прим. 3 к приложению) и «за исключением» у каждого кода "
                  "позиции, по строке первоисточника. Генерируется scripts/build_code_cells.py, "
                  "руками не править.",
        "positions": dict(sorted(positions.items())),
    }
    return data, details


def dump(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1) + "\n"


def report(data: dict, details: list[dict]) -> None:
    pos = data["positions"]
    kinds = Counter()
    for e in pos.values():
        m = e["marks"]
        if m is None:
            kinds["не определено"] += 1
        elif EXCEPT in m:
            kinds["есть «за исключением»"] += 1
        elif all(x == IZ for x in m):
            kinds["все коды с «из»"] += 1
        elif not any(x == IZ for x in m):
            kinds["без «из»"] += 1
        else:
            kinds["часть кодов с «из»"] += 1
    print(f"записей корпуса: {len(details)}, ключей: {len(pos)}")
    for k, n in kinds.most_common():
        print(f"   {k:28} {n}")
    print("ступень сопоставления:", dict(Counter(d["via"] for d in details)))
    multi = sum(1 for d in details if len(d["lines"]) > 1)
    print(f"записей, нашедших НЕСКОЛЬКО строк (пометки совпали): {multi}")
    for d in details:
        if d["marks"] is None:
            print(f"   НЕ ОПРЕДЕЛЕНО {d['key'][:90]} — {d['reason']}")
        elif EXCEPT in d["marks"]:
            print(f"   ЗА ИСКЛЮЧЕНИЕМ {d['key'][:90]}")


def main() -> int:
    enable_utf8()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="не писать: код 1, если файл отстал")
    args = ap.parse_args()
    data, details = build()
    text = dump(data)
    if args.check:
        same = OUT.exists() and OUT.read_text(encoding="utf-8") == text
        print("файл актуален" if same else f"ФАЙЛ ОТСТАЛ от корпуса: перегенерировать {OUT.name}")
        return 0 if same else 1
    report(data, details)
    OUT.write_text(text, encoding="utf-8")
    print(f"записано: {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
