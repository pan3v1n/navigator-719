"""Пороги баллов из примечаний к приложению ПП №719 (таблицы «код | наименование | по годам»).

Порог (min_threshold) для многих позиций живёт НЕ в самой позиции, а в нумерованных примечаниях к
разделам: напр. прим. 77 к разд. XVI — «Чиллеры | 270 | 320 | 480 | 542 балла» по годам. В структуру
продукта эти пороги не попали (примечания не структурируются, см. память об актуализации), поэтому
`format_context` показывал «порог не указан». Здесь мы парсим эти таблицы из чанка примечаний
(закоммичен в git → есть и на VM) и отдаём порог для позиции — БЕЗ переиндексации pp719.

Числа берутся ДОСЛОВНО из первоисточника, поэтому при вставке в контекст ответа они заземлены
(faithfulness-гард `unverified_numbers` их пропускает). Матчим порог по НАИМЕНОВАНИЮ позиции (у одного
кода бывает несколько строк: общая категория и конкретный продукт «Чиллеры»), в рамках раздела.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

_CHUNK = (Path(__file__).resolve().parents[2] / "knowledge_base" / "pp719" / "chunks"
          / "130_PRIMECHANIYA_prilozheniya.txt")

# Интро примечания-таблицы: «77. Продукция … в раздел XVI настоящего приложения …»
_INTRO_SIMPLE = re.compile(r"^\s*(\d+)\.\s+Продукция,.*?в\s+раздел\s+([IVXLC]+)\s+настоящего приложения")
_YEAR_RE = re.compile(r"с 1 января (\d{4})")
_CODE_RE = re.compile(r"\d{2}\.\d{2}(?:\.\d+)*")


@lru_cache(maxsize=1)
def _tables() -> list[dict]:
    """Парсит все таблицы-пороги из чанка примечаний. Кэш на процесс."""
    if not _CHUNK.exists():
        return []
    lines = _CHUNK.read_text(encoding="utf-8").split("\n")
    tables: list[dict] = []
    i, n = 0, len(lines)
    while i < n:
        m = _INTRO_SIMPLE.match(lines[i])
        if not m:
            i += 1
            continue
        note_no, section = m.group(1), m.group(2)
        # найти строку-заголовок таблицы (со «с 1 января» и разделителями «|»)
        j = i + 1
        while j < n and "с 1 января" not in lines[j]:
            if _INTRO_SIMPLE.match(lines[j]):
                break
            j += 1
        if j >= n or "с 1 января" not in lines[j]:
            i += 1
            continue
        years = _YEAR_RE.findall(lines[j])
        rows: list[dict] = []
        pending = ""  # строка-продолжение многострочной ячейки кода («из 28.25.12»)
        k = j + 1
        while k < n:
            raw = lines[k]
            if not raw.strip() or _INTRO_SIMPLE.match(raw):
                break
            cells = raw.split("|")
            if len([c for c in cells if c.strip()]) < 3:  # ячейка кода на отдельной строке
                pending = (pending + " " + raw.strip()).strip()
                k += 1
                continue
            code_cell = (pending + " " + cells[0]).strip()
            pending = ""
            name = cells[1].strip()
            thr = [c.strip() for c in cells[2:] if c.strip()]
            codes = _CODE_RE.findall(code_cell)
            by_year = {years[x]: thr[x] for x in range(min(len(years), len(thr)))}
            if codes and name and by_year:
                rows.append({"codes": codes, "name": name, "by_year": by_year})
            k += 1
        if rows:
            tables.append({"note": note_no, "section": section, "years": years, "rows": rows})
        i = k
    return tables


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _fmt(row: dict, note: str, section: str) -> str:
    steps = "; ".join(f"с 1 января {yr} г. — {val}" for yr, val in row["by_year"].items())
    return f"{steps} [прим. {note} к разд. {section}]"


# --- Простые пороги-списки из примечаний (прим. 7/11/31/52/53…) --------------------------
# Формат строки: «[из ]КОД "Наименование"[, [из ]КОД "…"…] - не менее N баллов[…]; (в ред. …)».
# Отличается от таблиц-по-годам (те через «|»); порог привязан к КОДУ (у строки бывает несколько
# кодов, у кода — несколько строк с разными порогами → дизамбигуация по наименованию). Описательные
# примечания (прим. 8(1): «…не менее N баллов для лопастей…», без кода) сюда НЕ попадают — покрыты
# verified_cases / отдельной доработкой.
_NOTE_HDR_RE = re.compile(r"^(\d+(?:\(\d+\))?)\.\s")            # «7.», «8(1).», «52.»
# Строка-порог начинается с кода (кодов) и кавычки. Два реальных усложнения корпуса, из-за которых
# раньше терялись десятки порогов (R7):
#   * СНОСКА между кодом и наименованием: «19.20.31 <11> "Пропан и бутан сжиженные" - не менее 300…»
#   * НЕСКОЛЬКО КОДОВ через запятую: «из 20.13.43.110, из 20.13.43.111, из 20.13.43.119 "Сода…"»
# Прежняя регулярка требовала кавычку сразу за первым кодом и не брала ни то, ни другое.
_CODE_TOKEN = r"(?:из\s+)?\d{2}(?:\.\d+)*(?:\s*<[^>\n]{1,16}>)?"
_FLAT_ROW_RE = re.compile(rf'^\s*{_CODE_TOKEN}(?:\s*,\s*{_CODE_TOKEN})*\s*"', re.IGNORECASE)
_FOOTNOTE_RE = re.compile(r"<[^>\n]{1,16}>")
_CODE_ONLY_RE = re.compile(r"\d{2}(?:\.\d+)*")
_NAME_Q_RE = re.compile(r'"([^"]+)"')


def _codes_before_quote(line: str) -> list[str]:
    """Все коды в левой части строки — ДО первой кавычки.

    Отсечка по кавычке обязательна: иначе в «коды» попадают числа из наименования продукции
    («чистотой менее 95 процентов»). Сноски вырезаем до поиска, иначе «<11>» даёт «код» 11."""
    q = line.find('"')
    left = line[:q] if q > 0 else line
    return _CODE_ONLY_RE.findall(_FOOTNOTE_RE.sub(" ", left))
_AMEND_STRIP_RE = re.compile(r"\s*\(в ред\.(?:[^()]|\([^()]*\))*\)")


@lru_cache(maxsize=1)
def _flat_thresholds() -> list[dict]:
    """Строки-пороги «код "имя" - не менее N баллов» из примечаний-списков (прим. 7/8/9/31/52/53…).
    Два формата: ИНЛАЙН — порог на той же строке, что и код («…"имя": … - не менее N баллов…»);
    МНОГОСТРОЧНЫЙ (прим. 9) — строка «код "имя":» и ниже отдельные строки-ступени «- не менее N
    баллов;» до следующего кода/примечания. Кэш на процесс."""
    if not _CHUNK.exists():
        return []
    lines = _CHUNK.read_text(encoding="utf-8").split("\n")
    rows: list[dict] = []
    note = None
    i, n = 0, len(lines)
    while i < n:
        ln = lines[i]
        h = _NOTE_HDR_RE.match(ln)
        if h:
            note = h.group(1)
        if _FLAT_ROW_RE.match(ln):
            if "не менее" in ln:  # инлайн: код и порог на одной строке
                j = ln.find("не менее")
                left = ln[:j]
                thr = _AMEND_STRIP_RE.sub("", ln[j:]).strip().rstrip(";. ").strip()
                codes = _codes_before_quote(ln)
                if codes and thr:
                    rows.append({"codes": codes, "names": _NAME_Q_RE.findall(left),
                                 "threshold": thr, "note": note})
            elif _AMEND_STRIP_RE.sub("", ln).rstrip().endswith(":"):  # многостроч. (прим.9): «код "имя":» + ступени
                codes = _codes_before_quote(ln)
                names = _NAME_Q_RE.findall(ln)
                steps: list[str] = []
                k = i + 1
                while (k < n and lines[k].strip()
                       and not _FLAT_ROW_RE.match(lines[k]) and not _NOTE_HDR_RE.match(lines[k])):
                    if "не менее" in lines[k]:
                        steps.append(_AMEND_STRIP_RE.sub("", lines[k]).strip().rstrip(";. ").strip())
                    k += 1
                if codes and steps:
                    rows.append({"codes": codes, "names": names,
                                 "threshold": "; ".join(steps), "note": note})
                i = k
                continue
        i += 1
    return rows


# --- ⚠ ОБЛАСТЬ ДЕЙСТВИЯ ПРИМЕЧАНИЯ (K2, #47) -------------------------------------------
# Часть примечаний задаёт порог НЕ для подтверждения российского происхождения, а «для целей
# осуществления закупок … для обеспечения государственных и муниципальных нужд». Это ДРУГОЙ вопрос
# и другой порог: у одной позиции они могут различаться, а «Порог:» в ответе означает первое.
#
# ⚠ ЗАМЕР 19.08.2026: 57 позиций корпуса получали закупочный порог как СВОЙ ОБЩИЙ — с настоящей
# ссылкой на первоисточник («не менее 75 баллов [прим. 53]»). Неверный порог с подлинной ссылкой
# опаснее отсутствующего: faithfulness-гард молчит (число дословно), а эксперт видит подтверждение.
# Тот же класс, что утечка порога вверх по иерархии в `_code_applies`: гард проверяет
# заземлённость, а не правильность привязки.
#
# Признак берётся из ТЕКСТА примечания, а не из списка номеров: список устареет на следующей
# редакции, а формулировка живёт в первоисточнике.
_PROCUREMENT_RE = re.compile(r"[Дд]ля целей осуществления закупок")


@lru_cache(maxsize=1)
def _note_scopes() -> dict[str, str]:
    """Номер примечания → 'procurement' | 'general' (по его вводной фразе)."""
    if not _CHUNK.exists():
        return {}
    lines = _CHUNK.read_text(encoding="utf-8").splitlines()
    out: dict[str, str] = {}
    bounds = [(m.group(1), i) for i, ln in enumerate(lines) if (m := _NOTE_HDR_RE.match(ln))]
    for k, (num, i) in enumerate(bounds):
        j = bounds[k + 1][1] if k + 1 < len(bounds) else len(lines)
        head = " ".join(lines[i:j])[:900]
        out.setdefault(num, "procurement" if _PROCUREMENT_RE.search(head) else "general")
    return out


def note_scope(note: str | None) -> str:
    """Область действия примечания. Неизвестное примечание считаем общим — это прежнее поведение."""
    return _note_scopes().get(str(note or ""), "general")


def _segs(code: str) -> list[str]:
    return [s for s in str(code).strip().split(".") if s]


def _code_applies(note_code: str, pos_code: str) -> bool:
    """Применим ли порог примечания к позиции. НАПРАВЛЕНИЕ ПРИНЦИПИАЛЬНО (R8).

    Условие: код примечания — ПРЕДОК ИЛИ РАВЕН коду позиции. Тогда позиция лежит внутри ветки,
    на которую распространяется примечание, и порог к ней относится.

    Раньше матч был симметричным, и порог «утекал» ВВЕРХ по иерархии — примечание для узкого кода
    применялось ко всей группе. Живые последствия: «Система электродвижения» (27.11) получала порог
    «Детандер-генераторов жидкостных для СПГ» (27.11.32.120); «Обувь с верхом из текстильных
    материалов» (15.20.14) — порог «Обуви валяной» (15.20.14.130). Число дословно из первоисточника,
    поэтому faithfulness-гард молчит: он проверяет заземлённость, а не правильность привязки.
    Порог — самый дорогой факт продукта, поэтому здесь лучше не показать, чем показать чужой."""
    n, p = _segs(note_code), _segs(pos_code)
    return bool(n) and len(n) <= len(p) and p[:len(n)] == n


def _name_overlap(product_name: str | None, names: list[str]) -> int:
    pt = set(re.findall(r"\w{4,}", _norm(product_name)))
    return max((len(pt & set(re.findall(r"\w{4,}", _norm(nm)))) for nm in names), default=0)


def _fmt_flat(r: dict, group: bool = False) -> str:
    out = f"{r['threshold']} [прим. {r['note']}]" if r.get("note") else r["threshold"]
    if group:
        # Порог задан для ветки-предка, а не для самой позиции: показываем, но честно называем
        # уровень — иначе групповой порог читается как собственный порог позиции.
        out += (f" — порог задан для группы кодов {', '.join(r['codes'])}; "
                f"проверьте применимость к вашей позиции по первоисточнику")
    return out


def lookup_threshold(codes: list[str], product_name: str, section: str | None = None) -> str | None:
    """ОБЩИЙ порог позиции из примечаний (для подтверждения российского происхождения), либо None.

    ⚠ Закупочные примечания сюда НЕ входят — см. `_note_scopes`. Их порог отдаёт
    `lookup_procurement_threshold`, и показывать его надо ВМЕСТЕ с условием, при котором он читается."""
    return _lookup(codes, product_name, section, "general")


def lookup_procurement_threshold(codes: list[str], product_name: str,
                                 section: str | None = None) -> str | None:
    """Порог «для целей осуществления закупок» — ДРУГОЙ вопрос и другой ответ, чем `lookup_threshold`."""
    return _lookup(codes, product_name, section, "procurement")


def _lookup(codes: list[str], product_name: str, section: str | None, scope: str) -> str | None:
    """Порог по годам для позиции (из примечаний), либо None. Матч по НАИМЕНОВАНИЮ в рамках раздела
    (у одного кода бывают строки «общая категория» и «конкретный продукт» — имя различает)."""
    name = _norm(product_name)
    # 1) таблицы-пороги по годам (прим.77 и т.п.) — матч по НАИМЕНОВАНИЮ
    if name:
        tables = _tables()
        for t in tables:  # точный матч имени в нужном разделе
            if section and t["section"] != section:
                continue
            if note_scope(t["note"]) != scope:
                continue
            for r in t["rows"]:
                if _norm(r["name"]) == name:
                    return _fmt(r, t["note"], t["section"])
        if section is None:
            # Матч имени без ограничения раздела — ТОЛЬКО когда раздел неизвестен (R8). Раньше этот
            # проход выполнялся всегда, и одноимённая позиция из ДРУГОГО раздела могла отдать свой
            # порог. Сейчас на корпусе это не срабатывает, но защита нужна: данные меняются.
            for t in tables:
                if note_scope(t["note"]) != scope:
                    continue
                for r in t["rows"]:
                    if _norm(r["name"]) == name:
                        return _fmt(r, t["note"], t["section"])
    # 2) простые пороги-списки (прим. 7/11/31/52/53…) — матч по КОДУ; при неоднозначности (у кода
    #    несколько строк с разными порогами) разрешаем по наименованию, иначе НЕ гадаем.
    if codes:
        cands: list[tuple[dict, bool]] = []  # (строка примечания, точное ли совпадение кода)
        for r in _flat_thresholds():
            if note_scope(r.get("note")) != scope:
                continue
            exact = None
            for c in codes:
                for rc in r["codes"]:
                    if _code_applies(rc, c):
                        exact = bool(exact) or (_segs(rc) == _segs(c))
            if exact is not None:
                cands.append((r, exact))
        if len(cands) == 1:
            row, is_exact = cands[0]
            return _fmt_flat(row, group=not is_exact)
        if len(cands) > 1:
            # При равном пересечении имён точное совпадение кода приоритетнее группового.
            row, is_exact = max(cands, key=lambda rc: (_name_overlap(product_name, rc[0]["names"]), rc[1]))
            if _name_overlap(product_name, row["names"]) >= 2:
                return _fmt_flat(row, group=not is_exact)
    return None
