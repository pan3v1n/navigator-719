"""`D23` #251 — сверка с первоисточником: группы «код → перечень требований» по HTML Контура.

ЗАЧЕМ. Пять сверок, назначенных эксперту (#53, #69, #52, #65, #95) и `D19` #210, решаются первоисточником
силами проекта (решение владельца 28.10.2026). RTF Контура теряет структуру объединённых ячеек (метки
`\\clvmrg` есть не везде), а HTML той же редакции раскладывает исходную строку таблицы на строки-абзацы
регулярно, и это читается детерминированно:

  * ГРУППА — строка с непустым кодом и следующие строки с ПУСТЫМ кодом до следующего кода. В первоисточнике
    это одна строка таблицы: код, перечень наименований (по абзацу на строку HTML) и одна ячейка требований
    (тоже по абзацу на строку);
  * внутри группы новый ПЕРЕЧЕНЬ требований начинается только с зачина «наличие у <юрлица / субъекта
    деятельности / производителя> … резидента» — продукты между зачинами делят один перечень;
  * строки «Позиция исключена» / «Код исключен» и строки поправок «(в ред. …)» — не продукты.

Разбор корпуса шёл по строкам и мог раздать продуктам группы ОБРЫВКИ общего перечня («Хладон-218» получил
«пиролиз, абсорбция…», «Хладон-125» — «конденсация, ректификация»). Инструмент находит такие записи: у
каждой продукции общего перечня «эффективный» текст (свои блоки, а без своих операций — требования группы
через наследование, как в рантайме) сверяется с полным перечнем, и члены перечня сравниваются МЕЖДУ СОБОЙ —
у записей с баллами общего зачина нет по формату, поэтому абсолютное покрытие одно ничего не доказывает.

Карта наследования (#69, #52) проверяется тем же разбором: ребёнок обязан стоять в ТОМ ЖЕ перечне, что и
его родитель.

Без `--write` данные не правит: печатает сводку, пишет разбор (`--json`) и журнал плана правки (`--log`)
для поимённого чтения. С `--write` — каждому члену расколотого перечня записывается полный перечень его ячейки.

⚠ Вёрстка — та, из которой собран корпус: сейчас 508574 (N 1297 + N 1257). На 508767 раздел VII свёрстан
иначе (`ACTUALIZATION.md` п. 11), и сверка даёт 39 ложных «споров» наследования против 2.

ЗАПУСК (HTML — не в репозитории, 9 МБ; sha256 печатается в выходе, у Контура он меняется от скачивания к
скачиванию — в странице есть служебные куски, — а таблицы нет):
    curl -L "https://normativ.kontur.ru/document?moduleId=1&documentId=508574" -o <файл>.html
    .venv/Scripts/python.exe scripts/d23_groups.py <файл>.html --json <выход>.json --log <план>.json
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from app.core.console import enable_utf8  # noqa: E402
from app.rag import inheritance  # noqa: E402
from diag_orphan_requirements import _OPENER as LIST_OPENER_RE  # noqa: E402

STRUCTURED = ROOT / "knowledge_base" / "pp719" / "structured"
FRAGMENTS = ROOT / "knowledge_base" / "pp719" / "fragmented_requirements.json"

# Зачин перечня требований: «наличие у юридического лица - налогового резидента…», «наличие у субъекта
# деятельности в сфере промышленности - налогового резидента…», «наличие у производителя … резидента».
# Перед зачином бывает срок действия перечня: «с 1 января 2017 г. наличие у юридического лица …».
OPENER_RE = re.compile(r"^(?:(?:до|с)\s+\d{1,2}\s+\w+\s+\d{4}\s*г\.?:?\s*)?"
                       r"наличие\s+у\s+(?:юридического\s+лица|субъекта\s+деятельности|производителя)"
                       r"[^\n]{0,80}резидент", re.I)
# «Позиция исключена», «Код исключен», «- Исключено.»; но НЕ «за исключением продукции …» в наименовании.
EXCLUDED_RE = re.compile(r"исключен[оаы]?\b", re.I)
AMEND_RE = re.compile(r"^\(\s*(?:в\s+ред\.|введ|позиция\s+введена|абзац\s+введен)", re.I)
POINTS_RE = re.compile(r"\d+\s*балл", re.I)
# Слова, которых в записи нет по построению формата: баллы записаны числом, а не словом.
_NOISE = {"балл", "балла", "баллов"}


class _Tables(HTMLParser):
    """Таблицы HTML с ROWSPAN/COLSPAN: список таблиц, каждая — список строк, строка — список ячеек."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[dict]]] = []
        self._stack: list[dict] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "table":
            self._stack.append({"rows": [], "row": None, "cell": None})
        elif not self._stack:
            return
        elif tag == "tr":
            t = self._stack[-1]
            t["row"] = []
            t["rows"].append(t["row"])
        elif tag in ("td", "th"):
            t = self._stack[-1]
            if t["row"] is None:
                t["row"] = []
                t["rows"].append(t["row"])
            t["cell"] = {"rs": int(a.get("rowspan") or 1), "cs": int(a.get("colspan") or 1), "buf": []}
            t["row"].append(t["cell"])
        elif tag == "br" and self._stack[-1]["cell"] is not None:
            self._stack[-1]["cell"]["buf"].append("\n")

    def handle_endtag(self, tag):
        if not self._stack:
            return
        if tag in ("td", "th"):
            self._stack[-1]["cell"] = None
        elif tag == "table":
            self.tables.append(self._stack.pop()["rows"])

    def handle_data(self, data):
        if self._stack and self._stack[-1]["cell"] is not None:
            self._stack[-1]["cell"]["buf"].append(data)


def grid(rows: list[list[dict]]) -> list[dict]:
    """Строки таблицы → [{"r", "code", "name", "req", "ncells"}]: текст колонок 0/1/2 в строке НАЧАЛА ячейки."""
    occupied: dict[tuple[int, int], dict] = {}
    for r, row in enumerate(rows):
        c = 0
        for cell in row:
            while (r, c) in occupied:
                c += 1
            cell["col"], cell["r0"] = c, r
            cell["text"] = re.sub(r"[ \t ]+", " ", "".join(cell["buf"])).strip()
            for dr in range(cell["rs"]):
                for dc in range(cell["cs"]):
                    occupied[(r + dr, c + dc)] = cell
            c += cell["cs"]
    out = []
    for r in range(len(rows)):
        def at(col, r=r):
            cell = occupied.get((r, col))
            return cell["text"] if cell is not None and cell["r0"] == r else ""
        rc = occupied.get((r, 2))
        wide = len(rows[r]) == 1 and rows[r][0]["cs"] >= 3       # исключение, поправка, общее примечание
        out.append({"r": r, "code": at(0), "name": at(1), "req": at(2), "ncells": len(rows[r]), "wide": wide,
                    "req_cell": id(rc) if rc is not None else None,
                    "req_spans_in": rc is not None and rc["r0"] < r})
    return out


def norm(s: str | None) -> str:
    s = re.sub(r"<[\d.()]+>", " ", s or "")
    s = s.replace(" ", " ").replace("ё", "е").lower()
    return re.sub(r"[^a-zа-я0-9]+", " ", s).strip()


def toks(s: str | None) -> list[str]:
    return [w for w in norm(s).split() if len(w) > 2 and w not in _NOISE and not w.isdigit()]


def load_files(root: Path = STRUCTURED) -> dict[Path, list[dict]]:
    return {f: json.loads(f.read_text(encoding="utf-8")) for f in sorted(root.glob("*.json"))}


def load_records(files: dict[Path, list[dict]] | None = None) -> dict[str, list[dict]]:
    """Раздел → записи. С `files` — те же объекты, что в файлах: правка записи попадает в файл."""
    recs: dict[str, list[dict]] = defaultdict(list)
    for data in (files if files is not None else load_files()).values():
        for r in data:
            if r.get("record_type") != "section_methodology":
                recs[r["section_roman"]].append(r)
    return recs


def own_text(r: dict) -> str:
    parts = [str(r[k]) for k in ("min_threshold", "notes") if r.get(k)]
    for b in r.get("requirement_blocks") or []:
        parts += [str(b[k]) for k in ("component", "note", "min_threshold") if b.get(k)]
        parts += [str(o.get("text") or "") for o in b.get("operations") or []]
    return " ".join(parts)


def effective_text(r: dict, find=None) -> tuple[str, str]:
    """(текст, откуда): свои блоки, а без своих операций — требования группы через наследование, как в
    рантайме. Запись родителя берётся целиком (компоненты, примечания), если `find` её находит: карта несёт
    только операции, и наследник верного родителя иначе выглядел бы обрывком."""
    own = own_text(r)
    if any(b.get("operations") for b in r.get("requirement_blocks") or []):
        return own, "свои"
    parent = inheritance.lookup(r["section_roman"], r["product_name"])
    if parent:
        prec = find.parent(r["section_roman"], parent) if find else None
        body = own_text(prec) if prec else " ".join(str(o.get("text") or "") for o in parent.get("operations") or [])
        return own + " " + body, "наследование"
    return own, "пусто"


def section_tables(tables, recs) -> dict[str, list[list[dict]]]:
    """Раздел → строки его таблиц. Таблица отходит разделу, чьих наименований в ней больше всего (не меньше
    трёх): у раздела их бывает несколько (подразделы, редакции), а таблицы примечаний совпадают с разделом
    лишь отдельными именами и уходят по тому же правилу к одному разделу — их строки без кода не образуют
    групп и в сверку не попадают."""
    names = {s: {norm(r["product_name"]) for r in rr} for s, rr in recs.items()}
    out: dict[str, list[list[dict]]] = defaultdict(list)
    for rows in tables:
        g = grid(rows)
        if not any(OPENER_RE.match(x["req"]) for x in g):
            continue                                  # таблица порогов из примечания, не перечни требований
        nm = [norm(x["name"]) for x in g if x["name"]]
        n, sec = max((sum(1 for x in nm if x in ns), s) for s, ns in names.items())
        if n >= 3:
            out[sec].append(g)
    return out


_AMEND_TAIL_RE = re.compile(r"\s*\((?:в\s+ред|введ)[^)]*\)\s*$", re.I)
_QUOTE_END_RE = re.compile(r"[\"»]\s*[;.]?$")
# Голова ячейки, начатой с «наличие у …»: права или копии ДОКУМЕНТАЦИИ («наличие у юридического лица … прав на
# конструкторскую …», XXII «наличие у изготовителя учтенных копий конструкторской документации»). Тот же зачин
# с другим предметом — середина перечня («… в уставном капитале доли резидентов РФ:» у аддитивных установок I).
_HAVE_RE = re.compile(r"^(?:(?:до|с)\s+\d{1,2}\s+\w+\s+\d{4}\s*г\.?:?\s*)?наличие\s+у\b", re.I)
_DOCS_RIGHTS_RE = re.compile(r"\b(?:прав\w*|копи\w*)\b[^;\n]{0,120}?документац", re.I)
_FOOTNOTE_TAIL_RE = re.compile(r"<[\d.()]+>\s*$")
_DATE_LABEL = r"(?:до|с)\s+\d{1,2}\s+\w+\s+\d{4}\s*г\.?:?"
_PREAMBLE_LINE_RE = re.compile(rf"^(?:{_DATE_LABEL}\s*$|(?:{_DATE_LABEL}\s*)?(?:или\s+)?(?:наличие|соблюдение)\b)", re.I)
_OR_START_RE = re.compile(r"^или(?:\s|$)", re.I)


def _first_line(grp: dict) -> str:
    return next((x["req"] for x in grp["rows"] if x["req"]), "").strip().split("\n")[0]


def _tail_state(grp: dict) -> str:
    """Чем кончается текст ячейки группы: «end» — законченная ячейка, «;» / «:» / «,», «letter» — оборвана
    на слове. Хвост «(в ред. …)» знаком конца не считается; закрывающая кавычка вставки поправки («…(3 балла)";»)
    — конец ячейки (так кончается текст, вставленный актом)."""
    text = [x["req"] for x in grp["rows"] if x["req"]]
    t = _AMEND_TAIL_RE.sub("", text[-1].rstrip()) if text else ""
    if not t:
        return "end"
    if _QUOTE_END_RE.search(t):
        return "quote"
    if t[-1] in ";:,":
        return t[-1]
    if _FOOTNOTE_TAIL_RE.search(t):
        return "fn"                     # «… на срок не менее 5 лет <6>»: знак конца не виден за сноской
    return "letter" if t[-1].isalpha() else "end"


def _preamble_only(grp: dict) -> bool:
    """Кусок из одних вводных требований («наличие … прав …», «наличие … сервисного центра», «соблюдение … доли»,
    метка «до 1 января 2019 г.:») — без единой операции. Ячейка из одних прав почти не встречается: такой кусок
    на хвосте без знака — оборванная ячейка (ревью PR D23, раунд 2, M2: мебель XI, бельё XVII, краны VI)."""
    lines = [_AMEND_TAIL_RE.sub("", ln.strip()) for x in grp["rows"] if x["req"] for ln in x["req"].split("\n")]
    lines = [ln for ln in lines if ln and not AMEND_RE.match(ln)]
    return bool(lines) and all(_PREAMBLE_LINE_RE.match(ln) for ln in lines)


def head_key(line: str) -> str:
    return " ".join(norm(line).split()[:6])


def section_heads(grids: list[list[dict]]) -> dict[str, int]:
    """Как в разделе начинаются ячейки: первые шесть слов куска, который открывает таблицу или идёт после
    ЗАКОНЧЕННОЙ ячейки, и сколько раз. В IX 19 ячеек открывает «1. соответствие заявителя следующим
    обязательным условиям», в XXII 15 — «наличие у изготовителя учтенных копий конструкторской документации»."""
    heads: dict[str, int] = defaultdict(int)
    for g in grids:
        prev = None
        for grp in groups_of(g, heads=None):
            if not _first_line(grp):
                continue
            if prev is None or _tail_state(prev) in ("end", "quote", "fn"):
                heads[head_key(_first_line(grp))] += 1
            prev = grp
    return dict(heads)


def _is_head(line: str, heads: dict[str, int]) -> bool:
    if _HAVE_RE.match(line):
        return bool(_DOCS_RIGHTS_RE.search(line))
    return heads.get(head_key(line), 0) >= 2


def _join_continuations(groups: list[dict], heads: dict[str, int] | None = None) -> list[dict]:
    """Группа, чей текст — ПРОДОЛЖЕНИЕ ячейки предыдущей, присоединяется к ней.

    Контур режет длинную ячейку на куски по строкам кода (свой ROWSPAN у каждого куска, в RTF — `\\clvmgf`
    заново), а ячейку рвут ещё и поправки во всю ширину. Признак — ТЕКСТ: законченная ячейка на 508574 кончается
    без знака (1027 стыков) или точкой (9), а оборванная — на «;», «:» или «,» (55). Поэтому:
      * хвост «,» — продолжение («…права использования товарного знака,» → «правообладателем которого…», IV);
      * хвост «;» или «:» — продолжение, если следующий кусок не голова ячейки (`_is_head`): перечень судов
        XVIII — ОДНА ячейка в 7 кусках, от «наличие у юридического лица …» до «сдача судна (обязательное
        условие)» — единственной строки без «;»; куски начинаются операцией с баллами («монтаж судовой системы
        хранения сжиженного газа (250 баллов)» продолжает «… монтаж электрооборудования (95 баллов);»);
      * хвост на слове + строчная буква в начале, не зачин — оборванная фраза («…комплексное предотвращение» →
        «и (или) минимизацию …», XXI);
      * хвост `";` (закрывающая кавычка вставки поправки) — ячейка закончена, следом идёт новая;
      * хвост на слове или на сноске `<N>`, а кусок — одни вводные требования без операций («… на срок не менее
        5 лет» → «осуществление на территории РФ …», мебель XI) — продолжение, если следом не голова;
      * кусок с «или» — альтернатива предыдущему, продолжение («… <4>» → «или наличие …», VII).
    ⚠ Неряшливость источника: «Смолы эпоксидные» и «Герметики» XXI кончаются на «;», а следом открывается своя
    ячейка с правами на документацию — её и ловит `_is_head`. Без `heads` (сбор голов раздела) — только «,» и
    оборванная фраза."""
    out: list[dict] = []
    for grp in groups:
        first = next((x["req"] for x in grp["rows"] if x["req"]), "").strip()
        if not (out and first) or grp["excluded"] or out[-1]["excluded"]:
            out.append(grp)
            continue
        state, line = _tail_state(out[-1]), first.split("\n")[0]
        if state == "quote":
            cont = False
        elif state == "," or _OR_START_RE.match(line):
            cont = True                    # «или …» — альтернатива предыдущему (капнографы VII после «… <4>»)
        elif state in ";:":
            cont = heads is not None and not _is_head(line, heads)
        elif state in ("letter", "fn"):
            cont = ((state == "letter" and first[:1].islower() and not LIST_OPENER_RE.match(first))
                    or (heads is not None and _preamble_only(out[-1]) and not _is_head(line, heads)))
        else:
            cont = False
        if cont:
            # на стыке новый перечень не открывается (см. `groups_of`); копия строки — сетка таблицы общая
            k = next(i for i, x in enumerate(grp["rows"]) if x["req"])
            grp["rows"][k] = {**grp["rows"][k], "cont": True}
            out[-1]["rows"] += grp["rows"]
            out[-1]["codes"] += grp["codes"]
            out[-1]["continued"] = True
            continue
        out.append(grp)
    return out


def groups_of(g: list[dict], heads: dict[str, int] | None = None) -> list[dict]:
    """Строки таблицы раздела → группы «код → [перечни: текст + продукты]». `heads` — головы ячеек раздела
    (`section_heads`): без них склейка продолжений по «;» и «:» не делается.

    Новый код открывает новую группу, КРОМЕ строки, куда ячейка требований заходит сверху (ROWSPAN): такие
    коды делят одну ячейку, то есть один перечень («Краны … стрелкового типа» / «прочие» / «Автокраны»).
    Если при этом группа открыта исключённой строкой, ячейка заходит с исключённой позиции на живую (`D19`)
    — это помечается, а не исправляется."""
    out: list[dict] = []
    cur = None
    for x in g:
        line = x["code"] or x["name"]
        if x["wide"] or AMEND_RE.match(line or "") or (x["ncells"] <= 2 and not x["req"] and not x["req_spans_in"]):
            continue                                  # строка во всю ширину / поправка «(в ред. …)»
        if x["code"] and not (x["req_spans_in"] and cur is not None and cur["req_cell"] == x["req_cell"]):
            cur = {"code": x["code"], "rows": [], "req_cell": x["req_cell"], "codes": [x["code"]],
                   "excluded": bool(EXCLUDED_RE.search(x["code"] + " " + x["name"])), "into_excluded": False}
            out.append(cur)
        elif x["code"] and cur is not None:
            cur["codes"].append(x["code"])
            if cur["excluded"] and not EXCLUDED_RE.search(x["code"] + " " + x["name"]):
                cur["into_excluded"] = True
        if cur is not None:
            if x["req_cell"] is not None and not x["req_spans_in"]:
                cur["req_cell"] = x["req_cell"]
            cur["rows"].append(x)
    out = _join_continuations(out, heads)
    for grp in out:
        segs: list[dict] = []
        seg = None
        for x in grp["rows"]:
            if seg is None or (x["name"] and x["req"] and not x.get("cont") and OPENER_RE.match(x["req"])
                               and seg["text"]):
                seg = {"text": [], "products": []}
                segs.append(seg)
            if x["req"]:
                seg["text"].append(x["req"])
            if x["name"] and not EXCLUDED_RE.search(x["name"]):
                seg["products"].append(x["name"])
        grp["segments"] = segs
    return out


def _position(r: dict) -> int:
    m = re.search(r"позиция\s+(\d+)", r.get("source_anchor") or "")
    return int(m.group(1)) if m else 10 ** 6


def match_records(recs: list[dict]):
    """Наименование из таблицы → запись: сначала полное имя, затем ключ наследования (первая строка).

    Одноимённые записи раздела — РАЗНЫЕ позиции, различимые только порядком в таблице (на 508574 пять пар:
    «Шасси с установленными двигателями…» III-116/117, «Устройства наведения промышленные» IV-11/34,
    «Трансмиттеры и транспондеры для оптических сетей» IV-43/45, «Сополимеры стирола…» XXI-169/177, «Альты»
    XXV-14/30). Прежде имя брало последнюю из пары: у «Трансмиттеров» в перечень вместо позиции 43 с
    требованиями дважды попадала пустая 45, и перечень выглядел «низким покрытием», а не расколом. Поэтому
    `find` такое имя НЕ разрешает (None — родитель по имени неоднозначен), а `find.claim` раздаёт одноимённые
    записи членам перечней по порядку позиций: таблица обходится сверху вниз, как нумерованы позиции."""
    by_name: dict[str, list[dict]] = defaultdict(list)
    for r in sorted(recs, key=_position):
        by_name[norm(r["product_name"])].append(r)
    first: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        first[inheritance._key(r["section_roman"], r["product_name"])].append(r)

    def find(sec: str, name: str) -> dict | None:
        cand = by_name.get(norm(name)) or []
        if cand:
            return cand[0] if len(cand) == 1 else None
        cand = first.get(inheritance._key(sec, name)) or []
        if len(cand) == 1:
            return cand[0]
        n = norm(name)                                # имя в таблице длиннее или короче имени записи
        cand = [rs[0] for k, rs in by_name.items()
                if len(rs) == 1 and len(n) >= 12 and (k.startswith(n) or n.startswith(k))]
        return cand[0] if len(cand) == 1 else None

    def parent(sec: str, p: dict) -> dict | None:
        """Запись родителя из карты наследования. Одноимённого родителя различают операции: карта хранит
        их вместе с именем («Устройства наведения промышленные» IV-11 и IV-34 — у наследника IV-35 родитель
        34, по ROWSPAN это одна ячейка)."""
        cand = by_name.get(norm(p.get("product_name"))) or []
        if len(cand) <= 1:
            return find(sec, p.get("product_name") or "")
        want = [norm(o.get("text")) for o in p.get("operations") or []]
        hit = [c for c in cand
               if [norm(o.get("text")) for b in c.get("requirement_blocks") or [] for o in b.get("operations") or []] == want]
        return hit[0] if len(hit) == 1 else None

    claimed: set[int] = set()

    def claim(sec: str, name: str) -> dict | None:
        cand = by_name.get(norm(name)) or []
        r = next((c for c in cand if id(c) not in claimed), cand[-1]) if len(cand) > 1 else find(sec, name)
        if r is not None:
            claimed.add(id(r))
        return r

    find.claim = claim
    find.parent = parent
    return find


def analyse(by_sec: dict, recs: dict, min_cover: float, gap: float) -> dict:
    report: dict = {}
    for sec in [s for s in recs if s in by_sec]:
        find = match_records(recs[sec])
        segs, seen, where = [], set(), {}
        heads = section_heads(by_sec[sec])
        groups = [grp for g in by_sec[sec] for grp in groups_of(g, heads)]
        for gi, grp in enumerate(groups):
            for si, seg in enumerate(grp["segments"]):
                full = "\n".join(seg["text"])
                ft = toks(full)
                members, objs = [], []
                for name in seg["products"]:
                    r = find.claim(sec, name)
                    if r is None:
                        members.append({"product": name[:100], "anchor": None, "cover": None, "source": "нет в корпусе"})
                        continue
                    if all(o is not r for o in objs):
                        objs.append(r)
                    seen.add(id(r))
                    where.setdefault(id(r), (gi, si))
                    eff, src = effective_text(r, find)
                    et = set(toks(eff))
                    parent = inheritance.lookup(sec, r["product_name"]) if src == "наследование" else None
                    prec = find.parent(sec, parent) if parent else None
                    members.append({"product": r["product_name"][:100], "anchor": r.get("source_anchor"),
                                    "parent_id": id(prec) if prec is not None else None,
                                    "cover": round(sum(1 for w in ft if w in et) / len(ft), 3) if ft else 1.0,
                                    "source": src, "parent": parent.get("product_name") if parent else None})
                union = set(toks(" ".join(own_text(r) for r in objs)))
                segs.append({"group": gi, "seg": si, "code": " | ".join(grp["codes"])[:120],
                             "group_segments": len(grp["segments"]),
                             "union_cover": round(sum(1 for w in ft if w in union) / len(ft), 3) if ft else 1.0,
                             "excluded": grp["excluded"], "into_excluded": grp["into_excluded"],
                             "chars": len(full), "text": full, "members": members, "_recs": objs})
        shared = [x for x in segs if len(x["_recs"]) >= 2]
        fragments = []
        for x in shared:
            covers = [m["cover"] for m in x["members"] if m["cover"] is not None]
            top = max(covers)
            # Вердикт перечня: «целый» — члены несут его одинаково; «расколот» — кто-то несёт заметно меньше
            # лучшего, то есть разбор раздал перечень по строкам; «низкое покрытие» — все одинаково плохи, а
            # объединение не лучше: это формат записи (балльная без общего зачина), не раскол.
            # Объединение членов покрывает ячейку заметно лучше ЛУЧШЕГО из них — каждый несёт кусок (раунд 3 ревью,
            # H-1: мебель XI 31.01.12…31.09.13 — у членов 0.56 и 0.53, у объединения 0.88; дробилки VI 0.70/0.73 →
            # 0.98). Без этой ветки перечень уходил в «низкое покрытие» или «целый» и не правился.
            if x["union_cover"] >= top + gap and x["union_cover"] >= min_cover:
                x["verdict"] = "расколот"
            elif min(covers) >= top - gap and top >= min_cover:
                x["verdict"] = "целый"
            elif min(covers) < top - gap:
                x["verdict"] = "расколот"
            else:
                x["verdict"] = "низкое покрытие"
            for m in x["members"]:
                if x["verdict"] == "расколот" and m["cover"] is not None and m["cover"] < top - gap:
                    fragments.append({**m, "code": x["code"], "seg_top": top, "seg_size": len(covers)})
        # Наследование (#69, #52). Три исхода:
        #   «тот же перечень»   — родитель и наследник в одной ячейке первоисточника (объединение видно);
        #   «пустая ячейка»     — у наследника своей ячейки нет, родитель — ближайший выше перечень с текстом:
        #                         вёрстка потеряла объединение (XXI), карта с первоисточником согласна;
        #   «свой перечень» / «не ближайший» — карта с первоисточником НЕ согласна: наследнику показывают
        #                         чужие требования.
        texted = [i for i, x in enumerate(segs) if x["text"].strip()]
        inh_ok, inh_empty, inh_bad = 0, 0, []
        for i, x in enumerate(segs):
            for m in x["members"]:
                if not m.get("parent"):
                    continue
                at = where.get(m["parent_id"]) if m.get("parent_id") is not None else None
                if at == (x["group"], x["seg"]):
                    inh_ok += 1
                    continue
                above = [k for k in texted if k < i]
                nearest = (segs[above[-1]]["group"], segs[above[-1]]["seg"]) if above else None
                if not x["text"].strip() and at is not None and at == nearest:
                    inh_empty += 1
                    continue
                inh_bad.append({"product": m["product"], "parent": m["parent"], "code": x["code"],
                                "why": "свой перечень" if x["text"].strip() else
                                       ("родитель не найден" if at is None else "не ближайший"),
                                "own_text": x["text"][:300], "child_at": [x["group"], x["seg"]],
                                "parent_at": at})
        report[sec] = {"segments": len(segs), "shared": shared, "fragments": fragments,
                       "into_excluded": [x for x in segs if x["into_excluded"]],
                       "inherit_ok": inh_ok, "inherit_empty": inh_empty, "inherit_bad": inh_bad,
                       "unmatched": [r["product_name"][:80] for r in recs[sec] if id(r) not in seen]}
    return report


_EMPTY_NOTE_RE = re.compile(r"\s*Во фрагменте приложения требования[^.]*не указаны\.?", re.I)
# Пометка разборщика у пустого члена группы (35 записей, форма одна). После правки она ложна: требования у
# записи есть, а «группа», которую она называет, бывает чужой (у «Трансмиттеров» IV-44/45 — «Агрегирующие
# транспондеры», хотя ячейка по ROWSPAN — позиции 43).
_GROUP_NOTE_RE = re.compile(r"\s*Вариант продукции в группе «(?:[^«»]|«[^»]*»)*» под общим кодом «[^»]*»\.\s*"
                            r"Отдельные требования к локализации во фрагменте не выделены[^.]*\(доразбор\)\.?")


def _block_key(b: dict) -> tuple:
    return (norm(b.get("component")), norm(b.get("note")), norm(b.get("min_threshold")),
            tuple((norm(o.get("text")), o.get("points")) for o in b.get("operations") or []))


def _cover(text: str, ft: list[str]) -> float:
    et = set(toks(text))
    return sum(1 for w in ft if w in et) / len(ft) if ft else 1.0


def full_list(recs: list[dict], text: str) -> tuple[list[dict], str]:
    """Полный перечень общей ячейки: (блоки, откуда взяты).

    Разбор раздал перечень по строкам ПОДРЯД, поэтому блоки членов в порядке таблицы и есть перечень.
    Повторы снимаются целым блоком (компонент + операции + баллы): операция законно повторяется в разных
    компонентах («использование российского металлопроката …»), и снимать её поштучно нельзя. Если один
    член уже несёт перечень целиком (покрытие не хуже объединения), берётся его разбор: два полных разбора
    одной ячейки разнятся формулировками, и объединение задвоило бы операции."""
    ft = toks(text)
    names = {norm(r["product_name"]) for r in recs} | {norm(r["product_name"].split("\n")[0]) for r in recs}
    union, seen = [], set()
    for r in recs:
        for b in _unlabel(r.get("requirement_blocks") or [], names):
            k = _block_key(b)
            if k not in seen:
                seen.add(k)
                union.append(b)
    best = max(recs, key=lambda r: _cover(own_text(r), ft))
    # Разбор одного члена — только если у остальных нет операций, которых в нём нет. Покрытия по словам мало:
    # у станков I (28.41.1–28.41.4) ячейку начинают «наличие управляющего программно-аппаратного комплекса
    # (25 баллов)» и «изготовление станины» — 8 операций у I-13, остальные 204 у I-19, и разбор I-19 покрывал
    # ячейку не хуже объединения (слова перечня повторяются), а первые 8 операций терялись.
    # M1 ревью: сравниваются и подписи блоков — в них стоят даты вариантов («до 1 января 2019 г.: … не более
    # 50 процентов» у VII-35 «Холодильники комбинированные лабораторные» терялся: его операции «покрывал» блок
    # «с 1 января 2019 г.» донора VII-37).
    pool = _req_texts(best, names)
    extra = [op for r in recs if r is not best for op in _req_texts(r, names) if not _op_covered(op, pool)]
    if not extra and _cover(own_text(best), ft) >= _cover(" ".join(own_text(r) for r in recs), ft) - 0.02:
        return copy.deepcopy(_unlabel(best.get("requirement_blocks") or [], names)), f"разбор «{best['product_name'][:60]}»"
    return copy.deepcopy(union), "объединение блоков членов"


def _unlabel(blocks: list[dict], names: set[str]) -> list[dict]:
    """Блоки без подписи-имени члена перечня. Разборщик ставил в `component` имя СВОЕЙ позиции («Хладон-318
    (октафторциклобутан)»), а в общем перечне такая подпись читается как «требования к Хладону-318» у
    «Жидкости Б-1» (37 блоков в 23 перечнях на 508574; в ячейке этих подписей нет). Блок, у которого кроме
    имени ничего нет, выпадает — требования в нём нет."""
    out = []
    for b in blocks:
        if norm(b.get("component")) in names:
            if not (b.get("operations") or b.get("note") or b.get("min_threshold")):
                continue
            b = {**b, "component": None}
        out.append(b)
    return out


def _op_texts(r: dict) -> list[str]:
    return [norm(o.get("text")) for b in r.get("requirement_blocks") or [] for o in b.get("operations") or []]


def _req_texts(r: dict, names: set[str]) -> list[str]:
    """Операции записи и подписи её блоков (`component`, `note`), кроме подписи-имени члена перечня."""
    out = []
    for b in r.get("requirement_blocks") or []:
        for k in ("component", "note"):
            t = norm(b.get(k))
            if t and t not in names:
                out.append(t)
        out += [norm(o.get("text")) for o in b.get("operations") or []]
    return [t for t in out if t]


def _op_covered(op: str, pool: list[str]) -> bool:
    """Операция есть в разборе: тем же текстом или пересказом (≥ 80 % её слов в одной операции разбора) — и
    с ТЕМИ ЖЕ числами. Без чисел пересказом считалось «не более 50 процентов цены товара» при «не более
    70 процентов»: ячейка гамма-камер и МР-томографов VII несёт два варианта через «или», и вариант с 50 %
    (разбор гамма-камер) выпадал из перечня."""
    words = set(op.split())
    nums = set(re.findall(r"\d+", op))
    dates = set(_DATE_MARK_RE.findall(op))
    # Метки дат «до/с 1 января 2019 г.» обязаны совпасть: «до» и «с» укладываются в допуск 20 %, и вариант «до 1
    # января 2019 г.» у VII-35 «покрывался» вариантом «с 1 января 2019 г.» донора (ревью PR D23, раунд 2, M1).
    return any(op == p or (words and len(words & set(p.split())) / len(words) >= 0.8
                           and nums <= set(re.findall(r"\d+", p)) and dates == set(_DATE_MARK_RE.findall(p)))
               for p in pool)


_DATE_MARK_RE = re.compile(r"\b(до|с|со)\s+(\d{1,2})\s+([а-я]+)\s+(\d{4})\b")


def _rtype(blocks: list[dict], recs: list[dict]) -> str | None:
    """Тип требований перечня: у члена, давшего больше всего операций; есть баллы, а тип не балльный — mixed."""
    donor = max(recs, key=lambda r: sum(len(b.get("operations") or []) for b in r.get("requirement_blocks") or []))
    t = donor.get("requirement_type")
    has_pts = any(o.get("points") is not None for b in blocks for o in b.get("operations") or [])
    if not has_pts:
        # Без баллов тип не переносится: у ОФЭКТ VII-6 тип донора «mixed» снимал строку R7 «Порог: не
        # предусмотрен» (ревью PR D23, раунд 2, L3); типы в корпусе расставлены не по одному правилу, и
        # переписывать их сверх нужного правка не должна.
        return None
    return "mixed" if t not in ("points", "mixed") else t


# Порог в `min_threshold` члена — «не менее N …» / «всех следующих …», а не операция с баллами в скобках
# («с 1 января 2024 г. - на уровне не менее 70 процентов (25 баллов)» у III-61 — это операция ячейки).
_THRESHOLDISH_RE = re.compile(r"не\s+менее\s+\d+|всех\s+следующих", re.I)
_POINTS_OP_RE = re.compile(r"\(\s*\d+[,.]?\d*\s*балл", re.I)


def cell_threshold(recs: list[dict], cell_text: str) -> str | None:
    """Порог САМОЙ ячейки, который разборщик положил в `min_threshold` одного из членов, или None.

    Условия (раунды 1–3 ревью PR D23): это порог, а не операция; его слова и ЧИСЛА есть в тексте ячейки; кандидат
    один. Кому его отдавать, решает `reconcile`: только члену без своего порога — ни в записи, ни в примечаниях
    (`lookup_threshold`): чужой порог записи перекрыл бы верный порог примечания (IX-12/13, XXVII-12…14, I-25).
    Случаи: «не менее 8 / 10 / 12 из следующих операций» шасси III-116 → III-117 (без него у III-117 стояла ложная
    строка R7 «порог не предусмотрен»); «не менее 4 / 5 / всех технологических операций» насосов XIX-1 → XIX-2…10;
    «не менее 70 баллов» XV-7 → XV-6."""
    cell_t = set(toks(cell_text))
    cell_n = Counter(_numbers(cell_text))
    cand = set()
    for r in recs:
        t = r.get("min_threshold")
        if not t or not _THRESHOLDISH_RE.search(t) or _POINTS_OP_RE.search(t):
            continue
        tt = toks(t)
        if tt and sum(w in cell_t for w in tt) / len(tt) >= 0.95 and not (Counter(_numbers(t)) - cell_n):
            cand.add(t)
    return next(iter(cand)) if len(cand) == 1 else None


def reconcile(report: dict) -> list[dict]:
    """Правка расколотых перечней: каждому члену — полный перечень его ячейки. Возвращает журнал правок."""
    from app.rag import thresholds as T
    log = []
    for sec, rep in report.items():
        for x in rep["shared"]:
            if x["verdict"] != "расколот":
                continue
            recs = x["_recs"]
            blocks, how = full_list(recs, x["text"])
            rtype = _rtype(blocks, recs)
            # ⚠ Порог записи (`min_threshold`) НЕ переносится между членами: рантайм берёт его ПЕРВЫМ
            # (`min_threshold or lookup_threshold`), и чужое значение перекрыло бы верный порог примечания. Ревью
            # PR D23: «20 баллов» (балл строки ячейки) у IX-12/13 вместо порогов прим. 26, «не менее 50 баллов» у
            # XXVII-12…14 вместо прим. 32, требование о доле в уставном капитале у I-25 вместо прим. 56. Порог по
            # ячейке — только `share_cell_thresholds`, с доказательством.
            cell_thr = cell_threshold(recs, x["text"])
            for r in recs:
                before = sum(len(b.get("operations") or []) for b in r.get("requirement_blocks") or [])
                changed = []
                if [_block_key(b) for b in r.get("requirement_blocks") or []] != [_block_key(b) for b in blocks]:
                    r["requirement_blocks"] = copy.deepcopy(blocks)
                    changed.append("перечень")
                if rtype and r.get("requirement_type") != rtype:
                    r["requirement_type"] = rtype
                    changed.append("тип")
                if (cell_thr and not r.get("min_threshold")
                        and not T.lookup_threshold(r.get("okpd2_codes") or [], r["product_name"], sec)):
                    r["min_threshold"] = cell_thr
                    changed.append("порог ячейки")
                for rx, what in ((_EMPTY_NOTE_RE, "снята пометка «требования не указаны»"),
                                 (_GROUP_NOTE_RE, "снята пометка «доразбор»")):
                    if r.get("notes") and rx.search(r["notes"]):
                        r["notes"] = rx.sub("", r["notes"]).strip() or None
                        changed.append(what)
                if changed:
                    after = sum(len(b.get("operations") or []) for b in r["requirement_blocks"])
                    log.append({"section": sec, "code": x["code"], "product": r["product_name"][:100],
                                "anchor": r.get("source_anchor"), "ops": [before, after], "what": changed,
                                "source": how})
    return log


# Примечание о целом разделе («Заключения, выданные до 10.05.2024 в отношении продукции, включенной в раздел
# VII …, действительны до окончания их срока действия»). Разбор оставлял его строкой-продолжением у позиции,
# под которой оно стоит в таблице: на 508574 — у VII-22 (плазмаферез) и VII-37 (морозильники), то есть у
# случайных позиций (`D19` #210 п. 2). Место ему — запись методики раздела.
_SECTION_NOTE_RE = re.compile(r"Заключения, выданные до \S+ в отношении продукции, включенной в раздел\s+"
                              r"(?P<sec>[IVXL]+)\b.*?действительны до окончания их срока действия"
                              r"(?:\s*\([^()]*\))?\.?")


def move_section_notes(files: dict[Path, list[dict]]) -> list[dict]:
    """Примечание о целом разделе — из `notes` позиций в `notes` записи методики этого раздела (один раз)."""
    meth = {r["section_roman"]: r for data in files.values() for r in data
            if r.get("record_type") == "section_methodology"}
    log = []
    for data in files.values():
        for r in data:
            if r.get("record_type") == "section_methodology" or not r.get("notes"):
                continue
            for m in list(_SECTION_NOTE_RE.finditer(r["notes"])):
                note, sec = m.group(0).strip(), m.group("sec")
                if sec != r["section_roman"] or sec not in meth:
                    continue                          # чужой раздел или методики нет — не трогаем
                r["notes"] = (r["notes"].replace(m.group(0), "").strip() or None)
                target = meth[sec]
                if note not in (target.get("notes") or ""):
                    target["notes"] = ((target.get("notes") or "").rstrip() + "\n" + note).strip()
                log.append({"section": sec, "from": r.get("source_anchor"), "product": r["product_name"][:80],
                            "note": note[:120]})
    return log


NOTES_CHUNK = ROOT / "knowledge_base" / "pp719" / "chunks" / "130_PRIMECHANIYA_prilozheniya.txt"
_NOTE_CODE_RE = re.compile(r"(из\s+)?(?<![\d.])(\d{2}(?:\.\d+)+)(?![\d.])", re.I)
_DATE_RE = re.compile(r"\d{1,2}\.\d{1,2}\.\d{4}")


def _note_codes(note: str, text: str) -> list[str] | None:
    """Коды КОДОВОГО примечания («Продукция, классифицируемая кодами … из 26.11.22.200, из 26.11.22.210,
    26.11.22.216 и включенная в раздел IV …») с пометками «из», или None — если примечание называет продукцию
    в кавычках (тогда порог адресован ИМЕНИ, и по ячейке его переносить нельзя)."""
    m = re.search(rf"^{re.escape(note)}\.\s+(.*)$", text, re.M)
    if not m:
        return None
    head = re.split(r"\s+и\s+включенн", m.group(1))[0]
    if any(q in head for q in "\"«»"):
        return None
    head = _DATE_RE.sub(" ", head)
    return sorted(("из " if a else "") + c for a, c in _NOTE_CODE_RE.findall(head))


def share_cell_thresholds(report: dict, notes_text: str | None = None) -> list[dict]:
    """Порог кодового примечания — членам ячейки, которым рантайм его не нашёл, ТОЛЬКО при доказательстве:
    список кодов примечания (с «из») В ТОЧНОСТИ равен кодам общей ячейки первоисточника.

    Светодиоды IV: прим. 71 «из 26.11.22.200, из 26.11.22.210, 26.11.22.216» — ровно коды ячейки белых
    (IV-1…3), прим. 72 «из 26.11.22.200, 26.11.22.210, 26.11.22.211–.215» — ровно ячейка небелых (IV-4…10).
    Коды .200 и .210 стоят в ОБОИХ примечаниях, и `thresholds._lookup` у строк-квалификаторов IV-1, 2, 4, 5
    честно не угадывает; различает только ячейка. ⚠ В общем случае члены одной ячейки законно имеют РАЗНЫЕ
    пороги (V «из 27.12»: реле, выключатели, зажимы — у каждого свой или никакого), поэтому без равенства
    списков — ничего. Сторож устаревания (при новой редакции прим. 71/72) — тест данных."""
    from app.rag import thresholds as T
    text = notes_text if notes_text is not None else NOTES_CHUNK.read_text(encoding="utf-8")
    log = []
    for sec, rep in report.items():
        for x in rep["shared"]:
            recs = x["_recs"]
            got = [(r, r.get("min_threshold") or T.lookup_threshold(r.get("okpd2_codes") or [],
                                                                     r["product_name"], sec)) for r in recs]
            have = {t for _, t in got if t}
            miss = [r for r, t in got if not t]
            # L4 ревью: коды сравниваются с кодами ВСЕЙ группы — доказательство только для группы из одного перечня
            if len(have) != 1 or not miss or x.get("group_segments", 1) != 1:
                continue
            thr = next(iter(have))
            refs = re.findall(r"\[прим\. ([^\]]+)\]", thr)
            cell = sorted(re.sub(r"\s+", " ", c.strip()).lower() for c in re.split(r"\s*\|\s*|\n", x["code"])
                          if c.strip())
            if len(refs) != 1 or [c.lower() for c in (_note_codes(refs[0], text) or [])] != cell:
                continue
            for r in miss:
                r["min_threshold"] = thr
                log.append({"section": sec, "code": x["code"], "product": r["product_name"][:100],
                            "anchor": r.get("source_anchor"), "ops": [0, 0], "what": [f"порог прим. {refs[0]}"],
                            "source": "коды примечания = коды ячейки"})
    return log


MANIFEST = ROOT / "knowledge_base" / "pp719" / "shared_cells.json"
MANIFEST_MIN_RECALL = 0.9
_AMEND_PAREN_RE = re.compile(r"\((?:в\s+ред|введ)[^)]*\)", re.I)


_NUMBERING_RE = re.compile(r"(?<![\d,])\d+(?:\.\d+){1,}(?![\d,])")


def _numbers(text: str | None) -> list[str]:
    """Числа текста — СПИСКОМ (мультимножество): без дат и номеров актов в «(в ред. …)», без сносок `<N>` (их
    снимает `norm`), без многоуровневой нумерации и кодов вида «1.1.1.11» / «26.20.22» (раунд 3 ревью, L-1)."""
    t = _NUMBERING_RE.sub(" ", _AMEND_PAREN_RE.sub(" ", text or ""))
    return re.findall(r"\d+", norm(t))


def _cell_numbers(text: str) -> Counter:
    return Counter(_numbers(text))


def _record_numbers(r: dict) -> Counter:
    """Числа записи: в тексте блоков (операции, подписи, пороги блоков) и в баллах операций — мультимножеством:
    одно «2018» в операции о числе операций не «покрывает» потерянную метку даты «с 1 января 2018 г.» у доли
    (раунд 3 ревью, M-2: XIX «Комплектующие», XIII, VII-41…43)."""
    nums = Counter(_numbers(own_text({"requirement_blocks": r.get("requirement_blocks")})))
    for b in r.get("requirement_blocks") or []:
        for o in b.get("operations") or []:
            p = o.get("points")
            if p is not None:
                nums.update(re.findall(r"\d+", str(int(p)) if float(p).is_integer() else str(p)))
    return nums


def shared_manifest(report: dict, sha: str, source: str) -> dict:
    """Манифест сверки: каждый общий перечень (≥ 2 записи), его члены и насколько текст записей покрывает
    ячейку первоисточника — ПОСЛЕ правки. Из него генератор R29 решает, снимать ли пометку «перечень неполный»
    (ревью PR D23, H3: признак «у всех позиций один перечень» правка обеспечивает сама и полноты не доказывает)."""
    lists = []
    for sec, rep in report.items():
        for x in rep["shared"]:
            recs = x["_recs"]
            ft = toks(x["text"])
            covers = [_cover(own_text({"requirement_blocks": r.get("requirement_blocks")}), ft) for r in recs]
            # Числа — отдельно: `toks` их выбрасывает, и перечень без «не менее 75 процентов … трех четвертей»
            # проходил как полный (ревью PR D23, раунд 2, H2: аддитивные установки I).
            cell_nums = _cell_numbers(x["text"])
            lack = Counter()
            for r in recs:
                lack |= cell_nums - _record_numbers(r)           # худший член: максимум недостачи по каждому числу
            missing = sorted(lack.elements(), key=lambda n: (len(n), n))
            same = len({json.dumps(r.get("requirement_blocks"), ensure_ascii=False, sort_keys=True) for r in recs}) == 1
            lists.append({
                "section": sec, "code": x["code"],
                "members": [{"product_name": r["product_name"], "anchor": r.get("source_anchor")} for r in recs],
                "same_list": same,
                # полнота — у ХУДШЕГО члена: блоки записи (без notes — до модели они не доходят) против ячейки
                "recall": round(min(covers), 3) if covers else 0.0,
                "numbers_missing": missing,
                "ops": [sum(len(b.get("operations") or []) for b in r.get("requirement_blocks") or []) for r in recs],
            })
    return {"_comment": ("D23 #251: общие ячейки требований по сетке HTML Контура и полнота текста записей против "
                         "ячейки. Генерируется `scripts/d23_groups.py <HTML> --write`; правится ТОЛЬКО перегенерацией."),
            "source": source, "sha256_16": sha, "min_recall": MANIFEST_MIN_RECALL, "lists": lists}


def write_files(files: dict[Path, list[dict]]) -> None:
    """Обратно в файлы тем же отступом: чужой отступ превратил бы правку позиции в дифф на весь раздел."""
    from restructure_changed import json_indent
    for f, data in files.items():
        old = f.read_text(encoding="utf-8")
        new = json.dumps(data, ensure_ascii=False, indent=json_indent(old)) + ("\n" if old.endswith("\n") else "")
        if new != old:
            f.write_text(new, encoding="utf-8")


def main() -> int:
    enable_utf8()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("html", type=Path)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--min-cover", type=float, default=0.6, help="ниже — член общего перечня несёт ОБРЫВОК")
    ap.add_argument("--gap", type=float, default=0.1, help="отставание от лучшего члена перечня — тоже обрывок")
    ap.add_argument("--log", type=Path, help="журнал правок расколотых перечней (JSON)")
    ap.add_argument("--write", action="store_true", help="записать правку в structured/*.json (без флага — план)")
    args = ap.parse_args()

    raw = args.html.read_bytes()
    print(f"источник: {args.html.name}  sha256 {hashlib.sha256(raw).hexdigest()[:16]}…  ({len(raw)} байт)")
    p = _Tables()
    p.feed(raw.decode("utf-8", errors="replace"))
    files = load_files()
    recs = load_records(files)
    frag_names = {norm(pos["product_name"]) for grp in json.loads(FRAGMENTS.read_text(encoding="utf-8"))
                  for pos in grp["positions"]}
    report = analyse(section_tables(p.tables, recs), recs, args.min_cover, args.gap)
    tot = defaultdict(int)
    for sec, r in report.items():
        nf = len(r["fragments"])
        r29 = sum(1 for f in r["fragments"] if norm(f["product"]) in frag_names)
        nsplit = sum(1 for x in r["shared"] if x["verdict"] == "расколот")
        print(f"{sec:6} перечней {r['segments']:4} | общих {len(r['shared']):3}, расколото {nsplit:2} | "
              f"обрывков {nf:3} (R29 {r29}) | "
              f"наслед.: тот же {r['inherit_ok']:3}, пустая {r['inherit_empty']:3}, СПОР {len(r['inherit_bad']):2} | "
              f"вне таблицы {len(r['unmatched']):3} | ячейка с исключённой {len(r['into_excluded'])}")
        tot["shared"] += len(r["shared"])
        tot["split"] += nsplit
        tot["fragments"] += nf
        tot["ok"] += r["inherit_ok"]
        tot["empty"] += r["inherit_empty"]
        tot["bad"] += len(r["inherit_bad"])
        tot["unmatched"] += len(r["unmatched"])
    print(f"ВСЕГО: общих перечней {tot['shared']}, расколото {tot['split']}, обрывков {tot['fragments']}, "
          f"наследование: тот же перечень {tot['ok']}, пустая ячейка {tot['empty']}"
          f" / не в том перечне {tot['bad']}, записей вне таблицы {tot['unmatched']}")
    outside = sorted(set(recs) - set(report))
    print(f"разделы ВНЕ сверки (таблица без зачина «наличие у …» или не узнана): {', '.join(outside) or 'нет'}")
    if args.json:
        clean = {s: {**r, "shared": [{k: v for k, v in x.items() if k != "_recs"} for x in r["shared"]],
                     "into_excluded": [{k: v for k, v in x.items() if k != "_recs"} for x in r["into_excluded"]]}
                 for s, r in report.items()}
        args.json.write_text(json.dumps(clean, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"разбор: {args.json}")
    log = reconcile(report)                           # правит записи в памяти; на диск — только с --write
    print(f"правка: записей {len(log)}, перечней {len({(e['section'], e['code']) for e in log})}"
          + ("" if args.write else " — ПЛАН, файлы не тронуты (--write)"))
    shared_thr = share_cell_thresholds(report)
    for e in shared_thr:
        print(f"  {e['what'][0]}: «{e['product']}» ({e['anchor']}) — {e['source']}")
    print(f"порогов кодового примечания по ячейке: {len(shared_thr)}")
    log += shared_thr
    moved = move_section_notes(files)
    for e in moved:
        print(f"  примечание раздела {e['section']}: из «{e['product']}» ({e['from']}) → методика раздела")
    print(f"примечаний о целом разделе перенесено в методику: {len(moved)}")
    log += [{"section": e["section"], "code": "", "product": e["product"], "anchor": e["from"], "ops": [0, 0],
             "what": ["примечание раздела → методика"], "source": "D19 п. 2"} for e in moved]
    if args.log:
        args.log.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    manifest = shared_manifest(report, hashlib.sha256(raw).hexdigest()[:16], args.html.name)
    weak = [m for m in manifest["lists"] if m["recall"] < MANIFEST_MIN_RECALL]
    print(f"манифест общих перечней: {len(manifest['lists'])}, полнота против ячейки < {MANIFEST_MIN_RECALL}: "
          f"{len(weak)} (у них пометка R29 «перечень неполный» остаётся)")
    if args.write:
        write_files(files)
        MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
