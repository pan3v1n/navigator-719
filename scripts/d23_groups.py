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
from app.rag.sparse import _stemmer  # noqa: E402
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
        report[sec] = {"segments": len(segs), "shared": shared, "fragments": fragments, "all": segs,
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
            # порог записи — тоже текст ячейки, который доходит до модели («Порог: …»): график «до 31 декабря 2023 г. -
            # не менее 60 баллов …» разборщик кладёт в поле порога, и D24 его в блоки не дублирует (ревью D24, M1)
            covers = [_cover(own_text({"min_threshold": r.get("min_threshold"),
                                       "requirement_blocks": r.get("requirement_blocks")}), ft) for r in recs]
            # Числа — отдельно: `toks` их выбрасывает, и перечень без «не менее 75 процентов … трех четвертей»
            # проходил как полный (ревью PR D23, раунд 2, H2: аддитивные установки I).
            cell_nums = _cell_numbers(x["text"])
            lack = Counter()
            for r in recs:                                       # худший член: максимум недостачи по каждому числу
                lack |= cell_nums - _record_numbers(r) - Counter(_numbers(r.get("min_threshold")))
            missing = sorted(lack.elements(), key=lambda n: (len(n), n))
            # один перечень — и когда у вводной снята оговорка порога, расходящаяся с порогом позиции (`_d24_unstale`):
            # у XXVII-11 порог позиции «не менее 50 баллов» — оговорка остаётся, у XXVII-12…14 (прим. 32) — снята
            same = len({json.dumps([{**b, "component": _D24_STALE_THR_RE.sub("", b.get("component") or "")}
                                    for b in r.get("requirement_blocks") or []], ensure_ascii=False, sort_keys=True)
                        for r in recs}) == 1
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


# ---------------------------------------------------------------------------------------------------------- #
# D24 #267 — строки ячейки, выпавшие из записи при разборе
# ---------------------------------------------------------------------------------------------------------- #
# Разборщик терял строки ячейки целиком: у «Бульдозеров» III-2 на бою нет прав на КД/ТД, сервисного центра и условия
# <18>; вводную «осуществление на территории РФ следующих операций …» он заменял ярлыком («Компоненты и технологические
# операции»); у «Автомобилей для гольфа» III разобран один блок из всей ячейки. Ячейка выравнивается с элементами
# записи ПО ПОРЯДКУ (`_d24_align`); строка без пары — потерянная: требование встаёт после последнего совпавшего
# элемента, вводная списка — над тем, что идёт за ней в ячейке.
_D24_PTS_RE = re.compile(r"\s*(?:\(\s*=?\s*(?P<a>\d+(?:[.,]\d+)?)\s*балл\w*(?P<tail>(?:\s+дополнительно)?"
                         r"(?:\s*,[^()]*?обязательн[^()]*)?)\s*\)|[-–—]\s*(?P<b>\d+(?:[.,]\d+)?)\s*балл\w*"
                         r"|^(?P<c>\d+(?:[.,]\d+)?)\s*балл\w*\s*[-–—])", re.I)    # «20 баллов - при изготовлении» IX-21
# «(Втоп = 14 баллов)» — наибольшие баллы операции с расчётом по формуле: баллы операции, но из текста не снимаются
# (на них ссылается формула), — так их пишет и разборщик: «… (Втоп = 14 баллов), расчет баллов …» [14] у IX-111.
_D24_VTOP_RE = re.compile(r"\(\s*[ВB]\s*топ\s*=\s*(\d+(?:[.,]\d+)?)\s*балл\w*\s*\)", re.I)
_D24_PTS_ONLY_RE = re.compile(r"^\(\s*=?\s*(\d+(?:[.,]\d+)?)\s*балл\w*\s*\)\s*[;.,]?\s*$", re.I)
# Строки формул IX («где К - количество …», «K - …», «- общее количество …») без самой формулы (в HTML — картинка)
# смысла не несут — не восстанавливаются, а пишутся в журнал.
_D24_FORMULA_RE = re.compile(r"^(?:где\b|[-–—]\s|[A-Za-zА-Яа-я]{1,3}\s*[-–—=]\s)", re.I)
_D24_REQ_START_RE = re.compile(r"^(?:(?:до|с|по)\s+\d{1,2}\s+\w+\s+\d{4}\s*г\.?:?\s*)?(?:или\s+)?(?:наличие|выполнение|"
                               r"соблюдение|соответствие|использование|применение|обеспечение|осуществление|производство|"
                               r"при\s+производстве)\b", re.I)
# Пометки «(обязательное требование)» и «Российской Федерации» (разборщик пишет «РФ») при сравнении не считаются.
_D24_DECOR = {"обязательное", "обязательная", "обязательный", "требование", "условие", "операция",
              "российской", "федерации"}
_D24_TRIVIAL_RE = re.compile(r"^\W*(?:или)?\W*$", re.I)        # «,», «или» — связка вариантов, не строка требования


def _d24_toks(s: str | None) -> list[str]:
    """Слова строки для сверки — и её числа без баллов: «до 31 декабря 2027 г. - не более 35 процентов» и «с 1 января
    2029 г. - не более 20 процентов» — разные строки (XX-12: график повторён у каждой из четырёх установок, а
    словами строки графика одинаковы)."""
    t = _D24_VTOP_RE.sub(" ", _D24_PTS_RE.sub(" ", (s or "").strip()))
    words = toks(t)
    words = [w for w in words if w not in _D24_DECOR] or words
    return (_stemmer().stem_words(words) if words else []) + _numbers(t)


def _d24_points(ln: str) -> list[float | int]:
    pts = [_num(m.group("a") or m.group("b") or m.group("c")) for m in _D24_PTS_RE.finditer(ln)]
    return pts or [_num(m.group(1)) for m in _D24_VTOP_RE.finditer(ln)]


def _d24_items(r: dict) -> list[tuple[int, int | None, set, float | int | None, set | None, Counter, bool,
                                      set | None]]:
    """Элементы записи по порядку: (блок, операция | None для подписи блока, слова и числа, баллы операции, слова
    подзаголовка, который разборщик вклеил в операцию: «применение печатных плат, произведенных на территории РФ: не
    менее одной печатной платы» — у IX-43 строки «2.1. применение печатных плат …:» и «2.1.1. не менее одной …»), те же
    слова мультимножеством, подпись — вводная списка на «:», слова пункта после вклеенного подзаголовка). Note блока —
    при подписи: туда разборщик кладёт порог
    узла из строки подписи («крио-шарнир (с 1 января 2024 г. - не менее 120 баллов, …):» у XXVI-8)."""
    out = []
    for bi, b in enumerate(r.get("requirement_blocks") or []):
        head = " ".join(x for x in (b.get("component"), b.get("note"), b.get("min_threshold")) if x)
        if head:
            w = _d24_toks(head)
            out.append((bi, None, set(w), None, None, Counter(w), (b.get("component") or "").rstrip().endswith(":"),
                        None))
        for oi, o in enumerate(b.get("operations") or []):
            t = o.get("text") or ""
            pre, suf = ((set(_d24_toks(t.split(":", 1)[0])), set(_d24_toks(t.split(":", 1)[1])))
                        if ":" in t.rstrip(": ") else (None, None))
            w = _d24_toks(t)
            out.append((bi, oi, set(w), o.get("points"), pre, Counter(w), False, suf))
    return out


def _d24_cands(lt: list[str], pts: list, items: list, thr: set = frozenset(), head: bool = False,
               key: tuple = ()) -> list[tuple[int, int, float, str]]:
    """Кандидаты строки ячейки: (первый элемент, последний элемент, оценка). Строку покрывает:
    — один элемент на ≥ 0.75; однословную («раскрой (10 баллов);») — элемент с этим словом: разборщик собирает такие
      строки в одну («полимеры, электроды, сепараторы»), а порядок выравнивания не даёт слову уйти к чужому узлу;
    — элемент от 4 слов почти целиком внутри строки (обрезанная разборщиком строка: «применение электронных модулей
      <33> российского производства, при этом расчет баллов …» без хвоста формулы); слова и числа порога записи `thr`
      в остатке строки не считаются — разборщик унёс их в поле порога («… покраски несущей рамы, тяговой рамы,
      металлоконструкций кабины, а также до 31 декабря 2017 г. не менее 6, … из следующих операций:» у III-143);
    — два соседних элемента на ≥ 0.8 (разборщик делит «литье, ковка (4 балла), раскрой, сварка (4 балла) …» на две
      операции);
    — окно из трёх-шести соседних элементов на ≥ 0.85, если каждая операция окна почти целиком в строке: у HTML
      пропал перенос, и строки склеились («… монтаж чип-компонентов на печатную плату выполнение … дополнительных
      требований: установка программного обеспечения … (20 баллов);» у IX-111 — три элемента записи, не четвёртый).
    — подзаголовок списка («…:»), который разборщик вклеил в каждую операцию списка («pre»): строка есть в записи, но
      операцию не занимает — она остаётся пункту списка, который идёт следом.
    Баллы строки и операции, если указаны у обеих, должны совпасть: «функциональное тестирование (15 баллов)» — не
    операция на 10 баллов с теми же словами; числа после «не менее»/«не более» строки (`key`) — быть в элементе: график
    «с 1 января 2024 г. - не менее 1050 баллов, …» порога позиции XXVI-8 словами и датами совпадал с графиком узла."""
    n, lset = len(lt), set(lt)
    p = pts[0] if len(pts) == 1 else None

    def cov(s):
        return sum(w in s for w in lt) / n

    def inside(s):
        return sum(w in lset for w in s) / len(s) if s else 0.0
    rest = [w for w in lt if w not in thr] or lt
    out = []
    for j, it in enumerate(items):
        if (p is not None and it[3] is not None and it[3] != p) or not all(k in it[2] for k in key):
            continue
        s = it[2]
        c = cov(s)
        if head and it[4] and cov(it[4]) >= 0.75:
            out.append((j, j, 1 + cov(it[4]), "pre"))
        if c >= 0.75 or (it[7] and len(it[7]) >= 2 and inside(it[7]) >= 0.9 and c >= 0.6):
            # и пункт вклеенного подзаголовка, когда разборщик сократил его хвост: «2.2.1. не менее одной интегральной
            # схемы, … предъявляемым в целях ее отнесения к продукции …» против «применение … схем …: не менее одной
            # интегральной схемы» у IX-62 — иначе пункт уходил к операции следующего пункта
            out.append((j, j, 1 + c, "one"))
        elif n >= 2 and len(s) >= 4 and inside(s) >= 0.9 and sum(w in s for w in rest) / len(rest) >= 0.5:
            out.append((j, j, 1 + c, "sub"))
    if n >= 2:
        for j in range(len(items) - 1):
            u = set(items[j][2])
            for k in range(j + 1, min(j + 6, len(items))):
                u = u | items[k][2]
                c = cov(u)
                if c < (0.8 if k == j + 1 else 0.85) or not all(q in u for q in key) or any(
                        items[q][6] for q in range(j, k + 1)):   # вводная «…:» в окно не входит: она — своя строка
                    continue
                if k > j + 1 and not all(inside(items[q][2]) >= 0.8 for q in range(j, k + 1)
                                         if items[q][1] is not None):
                    continue
                if p is not None and not any(items[q][3] in (None, p) for q in range(j, k + 1)
                                             if items[q][1] is not None):
                    continue                    # баллы строки — у одной из операций окна (иначе это чужая операция)
                # окно дороже пары, пара — одного элемента: подпись на краю, не несущая слов строки, окна не меняет —
                # выигрывает меньшее (у XVIII-73 пара «сервисный центр» + следующая подпись «Технологические операции»
                # съедала подпись, и вводная вставала за ней; подпись ВНУТРИ окна может не нести ничего —
                # «Дополнительные требования» у IX-111)
                out.append((j, k, 1 + c - 0.002 * (k - j), "win"))
    return out


def _d24_align(info: list[dict | None], items: list) -> list[tuple[int, int, str] | None]:
    """Выравнивание строк ячейки с элементами записи ПО ПОРЯДКУ: наибольшее число совпавших строк, при равенстве —
    ближайший следующий элемент. Жадный выбор лучшего элемента где угодно в записи ставил строку к ПОСЛЕДНЕМУ из
    равных (четыре одинаковых «выполнение заготовительных операций (10 баллов)» узлов XXVI-8 — к узлу муфты) и
    засчитывал общее требование по похожему требованию узла (ревью D24, M2, M4). Строка может встать к тому же
    элементу, что ПРЕДЫДУЩАЯ строка, если элемент — склейка нескольких строк ячейки (`can_stay`); после
    строки без пары — нет: иначе вторая копия графика «до 31 декабря 2027 г. - не более 35 процентов …» XX-12 вставала
    к той же note, что первая. Результат на строку — (первый, последний элемент, вид пары) или None."""
    m = len(items)
    NEG = float("-inf")

    def can_stay(i: int, e: int) -> bool:
        """Строку i покрывают слова элемента e, которые НЕ заняли идущие подряд перед ней строки с той же парой
        (мультимножеством): склейка разборщиком «операция: расчет баллов по формуле: где К - количество …» (IX-28)
        вмещает три строки ячейки, а пункт «контейнер для передачи высокого давления (60 баллов)» — не вторую строку
        после вводной «компоненты контейнера высокого давления:», чьи слова он уже отдал (XX-11)."""
        used = Counter()
        for k in range(i - 1, -1, -1):
            if info[k] is None:
                continue
            if not any(c[0] == c[1] == e and c[3] != "pre" for c in info[k]["cands"]):
                break
            used += Counter(info[k]["lt"])
        left, hit = items[e][5] - used, 0
        for w in info[i]["lt"]:
            if left[w] > 0:
                left[w] -= 1
                hit += 1
        return hit / info[i]["n"] >= 0.75

    f = [0.0, NEG] + [NEG] * (2 * m)     # состояние 2·s + fresh: s — последний занятый элемент + 1, fresh — строка
    back = []                            # перед этой совпала с ним же
    for i, x in enumerate(info):
        if x is None:                    # «(2 балла);», «или» — не разрывают
            back.append(None)
            continue
        g, ch = [NEG] * (2 * m + 2), [None] * (2 * m + 2)
        for st in range(m + 1):          # строка без пары
            k = 2 * st + (f[2 * st + 1] > f[2 * st])
            g[2 * st], ch[2 * st] = f[k], (k, None, None, None)
        pre, best, arg = [], NEG, 0
        for st in range(m + 1):
            for fr in (0, 1):
                v = f[2 * st + fr] + 0.001 * st
                if v > best:
                    best, arg = v, 2 * st + fr
            pre.append((best, arg))
        for js, je, sc, kind in x["cands"]:
            if kind == "pre":                # не занимает элемент: состояние то же, элемент — впереди
                for st in range(js + 1):
                    for fr in (0, 1):
                        v = f[2 * st + fr] + sc
                        if v > NEG and v > g[2 * st]:
                            g[2 * st], ch[2 * st] = v, (2 * st + fr, js, je, kind)
                continue
            bv, bs = pre[js]
            t = 2 * (je + 1) + 1
            if bv > NEG and bv - 0.001 * js + sc > g[t]:
                g[t], ch[t] = bv - 0.001 * js + sc, (bs, js, je, kind)
            if js == je and can_stay(i, js):
                t = 2 * (js + 1) + 1
                if f[t] > NEG and f[t] + sc > g[t]:
                    g[t], ch[t] = f[t] + sc, (t, js, je, kind)
        back.append(ch)
        f = g
    st = max(range(2 * m + 2), key=lambda k: f[k])
    res: list[tuple[int, int, str] | None] = [None] * len(info)
    for i in range(len(info) - 1, -1, -1):
        if back[i] is None:
            continue
        c = back[i][st]
        if c[1] is not None:
            res[i] = (c[1], c[2], c[3])
        st = c[0]
    return res


# Строка ячейки, оборванная на служебном слове («по 31 декабря 2019 г. осуществление на территории Российской Федерации
# не» + «менее трех из следующих операций (при наличии):» у IX-36), — перенос HTML, а не две строки.
_D24_WRAP_END_RE = re.compile(r"(?:^|\s)(?:не|и|или|в|во|на|с|со|по|для|от|до|к|ко|а|из|при|без|о|об|у|за|под|над|"
                              r"между|через|[-–—])$", re.I)
_D24_FORMULA_HEAD_RE = re.compile(r"по\s+формул\w*(?:\s*<[^>]*>)*\s*:?$", re.I)
_D24_DATE_HEAD_RE = re.compile(r"^(?:до|с|по)\s+\d{1,2}\s+\w+\s+\d{4}\s*г\.?\s*:$", re.I)
_D24_BOUND_RE = re.compile(r"не\s+(?:менее|более)\s+(\d+(?:[.,]\d+)?)", re.I)
_D24_STALE_THR_RE = re.compile(r",?\s*(?:котор\w+\s+)?(?:в\s+совокупности\s+)?оценива\w*\s+не\s+менее\s+(\d+)\s+"
                               r"балл\w*", re.I)


def _d24_lines(text: str) -> list[str]:
    out: list[str] = []
    for ln in (text or "").split("\n"):
        ln = ln.strip()
        if not ln or AMEND_RE.match(ln):
            continue
        if out and ((_D24_WRAP_END_RE.search(out[-1]) and ln[:1].islower())
                    or (out[-1].endswith("Российской") and ln.startswith("Федерац"))):       # V-2
            out[-1] += " " + ln
        else:
            out.append(ln)
    return out


def _d24_info(ln: str, items: list, thr: set = frozenset()) -> dict | None:
    if _D24_PTS_ONLY_RE.match(ln) or _D24_TRIVIAL_RE.match(ln):
        return None
    lt = _d24_toks(ln)
    if not lt:
        return None
    pts = _d24_points(ln)
    key = tuple(n for m in _D24_BOUND_RE.finditer(_D24_PTS_RE.sub(" ", ln)) for n in _numbers(m.group(1)))
    return {"n": len(lt), "lt": lt, "pts": pts, "key": key,
            "cands": _d24_cands(lt, pts, items, thr, ln.rstrip().rstrip(";,.").endswith(":"), key)}


def _d24_split(ln: str, items: list, thr: set = frozenset()) -> list[str] | None:
    """Склеенная строка без пары — по частям, если склейка доказана. По «;»: в строке несколько баллов, или часть
    строки есть в записи, или каждая часть — своё требование («выполнение одного из условий <18> …; осуществление … :»
    у III-2); требование с перечнем внутри («прав на: КД; ТД») не делится — части после первой не требования. По «:»:
    после вводной в той же строке — пункт, который есть в записи («осуществление … в полном объеме (…) следующих
    технологических операций: изготовление, сборка … тележек грузовых вагонов;» у XII-5)."""
    def found(p):
        x = _d24_info(p, items, thr)
        return bool(x) and x["n"] >= 2 and any(c[3] == "one" for c in x["cands"])
    parts = [p.strip() for p in ln.split(";") if p.strip()]
    if len(parts) >= 2 and not any(len(_d24_points(p)) > 1 for p in parts):
        if (len(_d24_points(ln)) > 1 or all(_D24_REQ_START_RE.match(p) for p in parts)
                or any(found(p) for p in parts)):
            return [p + ";" for p in parts]
    depth, cut = 0, None
    for i, ch in enumerate(ln):
        depth += (ch == "(") - (ch == ")")
        if ch == ":" and depth == 0:
            cut = i
            break
    if cut is not None:
        head, tail = ln[:cut + 1].strip(), ln[cut + 1:].strip()
        if len(toks(head)) >= 2 and tail and found(tail.rstrip(";,. ")):
            return [head, tail]
    return None


def _num(s: str) -> float | int:
    v = float(s.replace(",", "."))
    return int(v) if v.is_integer() else v


def _d24_unstale(t: str, position_threshold: str | None) -> tuple[str, str | None]:
    """Порог в вводной расходится с порогом позиции: «…, которые в совокупности оцениваются не менее 50 баллов:» у
    XXVII-12…14 при пороге прим. 32 «2024 год — не менее 55 …» — по правилу 2б промпта модель показала бы 50 баллов
    порогом узла (ревью D24, M5). Вводная остаётся без оговорки; (текст, снятая оговорка | None)."""
    m = _D24_STALE_THR_RE.search(t) if position_threshold else None
    if m and m.group(1) not in re.findall(r"\d+", position_threshold):
        return (t[:m.start()] + t[m.end():]).strip(), m.group(0).strip(" ,")
    return t, None


def _d24_resolve(items: list, text: str, threshold: str | None = None, position_threshold: str | None = None
                 ) -> tuple[list[str], list[dict | None], list[tuple[int, str] | None]]:
    """Строки ячейки (склеенные без пары — по частям), их разбор и пара каждой: (индекс последнего элемента пары, вид
    пары: "one", "sub" — элемент внутри строки, "win" — окно элементов, "thr" — строка порога записи, индекс -2) или
    None — пары нет. Строка сверяется без оговорки порога, расходящейся с `position_threshold` (`_d24_unstale`): так
    её вставил первый прогон, и повторный обязан её узнать."""
    lines = _d24_lines(text)
    thr = set(_d24_toks(threshold)) if threshold else set()
    for rnd in range(4):                                 # склеенные строки без пары — по частям, и снова выравнивание
        info = [_d24_info(_d24_unstale(ln, position_threshold)[0], items, thr) for ln in lines]
        match = _d24_align(info, items)
        new, split = [], False
        for ln, x, mt in zip(lines, info, match):
            parts = (_d24_split(ln, items, thr) if x is not None and (mt is None or mt[2] == "sub") and rnd < 3
                     else None)
            new += parts or [ln]
            split |= bool(parts)
        if not split:
            break
        lines = new
    taken: dict[int, Counter] = defaultdict(Counter)      # элемент → слова совпавших с ним строк
    for x, mt in zip(info, match):
        for k in range(mt[0], mt[1] + 1) if mt and mt[2] != "pre" else ():
            taken[k] += Counter(x["lt"])

    def free(c, lt):
        """Элемент не занят выравниванием — или занят, но строку покрывают его НЕзанятые слова (подпись разборщика
        склеила две строки ячейки: «изготовление основных корпусных деталей: изготовление станины …, рамы» у I-13);
        повтор строки, уже засчитанной элементу (график XX-12 у второй установки), пары не находит."""
        if c[3] == "pre":
            return True
        if c[0] == c[1] and c[0] in taken:
            left, hit = items[c[0]][5] - taken[c[0]], 0
            for w in lt:
                if left[w] > 0:
                    left[w] -= 1
                    hit += 1
            return hit / len(lt) >= 0.75
        return all(k not in taken for k in range(c[0], c[1] + 1))
    res: list[tuple[int, str] | None] = []
    for x, mt in zip(info, match):
        if mt is not None:
            res.append((mt[1], mt[2]))
            continue
        if x is None:
            res.append(None)
            continue
        # без пары по порядку: среди незанятых элементов — переставленный блок
        ok = [c for c in x["cands"] if free(c, x["lt"])]
        if ok:
            js, je, _, kind = max(ok, key=lambda c: c[2])
            for k in range(js, je + 1) if kind != "pre" else ():
                taken[k] += Counter(x["lt"])
            res.append((je, kind))
        elif thr and sum(w in thr for w in x["lt"]) / x["n"] >= 0.75 and all(k in thr for k in x["key"]):
            res.append((-2, "thr"))
        else:
            res.append(None)
    return lines, info, res


def lost_units(r: dict, text: str, threshold: str | None = None,
               position_threshold: str | None = None) -> tuple[list[dict], list[tuple[str, str]]]:
    """Потерянные строки ячейки → единицы вставки и журнал пропущенного.

    Строки выравниваются с элементами записи по порядку (`_d24_align`); строка без пары по порядку ищет пару среди
    элементов, которые выравнивание не заняло (разборщик переставил блок: «Общие требования» у XXI-110 стоят после
    операций), и в пороге записи `threshold` (график «до 31 декабря 2023 г. - не менее 60 баллов» — строки ячейки,
    которые разборщик положил в поле порога, VI-5). Строка без пары и там — потерянная.

    Единица: {"after": индекс последнего совпавшего элемента (-1 — до всех), "component": текст | None, "ops": [...],
    "into": True — операции в блок этого элемента, "next": элемент следующей совпавшей строки}. Заголовок с «:» и
    следующие строки — новый блок (так у «Автомобилей для гольфа»: «несущая рама (корпус):» + операции с баллами);
    одиночное требование — блок без операций (рантайм выводит его в «Обязательные требования», R6); операция с баллами
    без заголовка — в блок последнего совпавшего элемента; «(2 балла);» отдельной строкой — баллы предыдущей операции.
    Строка с НЕСКОЛЬКИМИ баллами без «;» («стрелы (3 балла), рукояти (3 балла) …») не делится — части оторвались бы
    от глагола; пишется в журнал. Подпись блока без операций, у строки которой в ячейке есть баллы («функциональное
    тестирование продукции (15 баллов)» у VII-33), — операция, а не обязательное требование: единица {"op_of": блок}.
    Элемент — обрывок строки (`sub`): подпись с операциями — на место вводной ячейки {"relabel"}, обрезанная операция
    или требование — текстом строки {"retext"}. Пояснение (не требование: «подтверждением наличия … является …») — в
    note блока {"note_of"}. `position_threshold` — порог, который покажет рантайм: оговорка «оцениваются не менее N
    баллов» вводной, расходящаяся с ним, снимается."""
    items = _d24_items(r)
    blocks = r.get("requirement_blocks") or []
    # Ячейка из вариантов по датам («до 1 января 2019 г.:» … «с 1 января 2019 г.:» … — VII), а в записи их меток нет:
    # разборщик оставил один вариант, и строки другого выровнялись бы с ним (у VII-15 «не более 60 процентов» варианта
    # до 2019 г. — к «не более 50 процентов» действующего). Такая запись не сверяется — в журнал.
    bt = norm(own_text({"requirement_blocks": blocks}))
    miss = [ln for ln in _d24_lines(text) if _D24_DATE_HEAD_RE.match(ln) and norm(ln) not in bt]
    if miss:
        return [], [("варианты по датам", "; ".join(miss) + " — метки нет в записи, ячейка не сверяется")]
    lines, info, res = _d24_resolve(items, text, threshold, position_threshold)
    known = set().union(*(it[2] for it in items), set(_d24_toks(threshold)) if threshold else set())

    def under_header(j: int) -> bool:
        """Совпавший элемент — заголовок списка или операция под ним: подпись блока кончается «:» в записи — или в
        ячейке, если разборщик двоеточие снял («…в уставном капитале доли резидентов Российской Федерации» у I-26)."""
        if j < 0:
            return False
        comp = (blocks[items[j][0]].get("component") or "").rstrip()
        return comp.endswith(":") or last_raw.endswith(":")

    last, units, skipped, cur, in_formula, raw_end, last_raw, pending = -1, [], [], None, False, "", "", []

    def unstale(t: str) -> str:
        t2, cut = _d24_unstale(t, position_threshold)
        if cut:
            skipped.append(("порог вводной ≠ порогу позиции", cut))
        return t2

    prev: list[int | None] = []                      # предыдущая строка ячейки со словами
    for i in range(len(lines)):
        prev.append(next((k for k in range(i - 1, -1, -1) if info[k] is not None), None))
    for i, (ln, x, mt) in enumerate(zip(lines, info, res)):
        j, kind = mt if mt is not None else (None, None)
        if x is None:
            m = _D24_PTS_ONLY_RE.match(ln)
            if m and cur is not None and cur["ops"] and cur["ops"][-1]["points"] is None:
                cur["ops"][-1]["points"] = _num(m.group(1))
            continue
        if j is not None:
            if j >= 0 and kind != "pre":
                it = items[j]
                pts = x["pts"]
                b = blocks[it[0]]
                if (it[1] is None and len(pts) == 1 and not b.get("operations") and not b.get("note")
                        and not b.get("min_threshold") and not (b.get("component") or "").rstrip().endswith(":")):
                    units.append({"after": j, "op_of": it[0], "points": pts[0], "ops": []})
                elif kind == "sub":
                    # элемент — обрывок строки ячейки: подпись разборщика пересказывает вводную («Комплектующие и
                    # материалы» + note «если предусмотрено …» у XVIII-10 против «производство на территориях …
                    # следующих комплектующих и материалов (если предусмотрено …):»), операцию или требование он
                    # обрезал («… в полном объеме (в том числе в рамках внутренней кооперации …)» у XII-5) — на место
                    # встаёт строка первоисточника (ревью D24, M4: «элемент внутри строки» засчитывал строку целиком)
                    head = ln.rstrip().rstrip(";,.").strip()
                    text = (head if head.endswith(":")
                            else re.sub(r"\s+", " ", _D24_PTS_RE.sub("", ln)).strip().rstrip(";,.").strip())
                    lost_w = [w for w in x["lt"] if w not in known]
                    if it[1] is None and b.get("operations") and text.endswith(":"):
                        units.append({"after": j, "relabel": it[0], "component": unstale(text), "ops": []})
                    elif len(lost_w) >= 3 and it[1] is None and not b.get("operations"):
                        units.append({"after": j, "retext": (it[0], None), "text": text, "ops": []})
                    elif len(lost_w) >= 3 and it[1] is not None:
                        units.append({"after": j, "retext": (it[0], it[1]), "text": text, "ops": [],
                                      "points": pts[0] if len(pts) == 1 else None})
                last = j
                for u in pending:
                    u["next"] = j
                pending = []
            elif (j == -2 and cur is not None and units and units[-1] is cur and not cur["ops"]
                  and (cur.get("component") or "").endswith(":")):
                # вводная порога записи, а не списка: «конвейеры скребковые … могут быть отнесены к продукции,
                # произведенной на территории РФ, при обеспечении:» + график из поля порога у VI-25 — висела бы в конце
                units.pop()
                if cur in pending:
                    pending.remove(cur)
                skipped.append(("вводная порога", cur["component"]))
            cur, in_formula, last_raw = None, False, ln.rstrip()
            continue
        pts = list(_D24_PTS_RE.finditer(ln))
        vtop = None if pts else _D24_VTOP_RE.search(ln)
        is_req = bool(_D24_REQ_START_RE.match(ln))
        if not is_req and not pts and _D24_FORMULA_HEAD_RE.search(ln.rstrip().rstrip(";,.")):
            # заголовок формулы без самой формулы (в HTML — картинка): «количество баллов (Yt), начисляемых за … по
            # формуле:» у IX-23, «при этом расчет баллов осуществляется по формуле:» у IX-29 — не требование
            skipped.append(("формула (заголовок)", ln))
            cur, in_formula = None, True
            continue
        k = prev[i]
        if (k is not None and res[k] is not None and ln[:1].islower() and x["n"] <= 3 and not pts
                and not lines[k].rstrip().endswith((";", ":", "."))):
            # хвост переноса строки, которая в записи есть («… на срок не менее 5 лет в следующем» + «составе:» у
            # IX-72): не заголовок и не требование — разборщик хвост потерял, вставка дала бы висячую строку
            skipped.append(("хвост переноса", ln))
            continue
        if _D24_FORMULA_RE.match(ln) or (in_formula and ln[:1].islower() and not is_req and not pts
                                          and not ln.rstrip().endswith(":")):
            skipped.append(("формула", ln))     # и её продолжение («деленное на общее количество …»)
            # заголовок формулы без операций («расчет баллов по формуле:» — шесть раз у принтеров IX-16) без самой
            # формулы — висячая фраза, а не требование: уходит вместе с ней
            if (cur is not None and units and units[-1] is cur and not cur["ops"]
                    and (cur.get("component") or "").endswith(":")):
                units.pop()
                if cur in pending:
                    pending.remove(cur)
                skipped.append(("формула (заголовок)", cur["component"]))
                cur = None
            in_formula = True
            continue
        in_formula = False
        if len(pts) > 1:
            skipped.append(("несколько баллов", ln))     # «литье (2 балла), мех. обработка (2 балла) картера» — одна
            cur = None
            continue
        points = (_num(pts[0].group("a") or pts[0].group("b") or pts[0].group("c")) if pts
                  else _num(vtop.group(1)) if vtop else None)
        if pts:
            tail = (pts[0].group("tail") or "").strip(" ,")
            body = (ln[:pts[0].start()] + (f" ({tail})" if tail else "") + ln[pts[0].end():])
        else:
            body = ln
        body = body.strip().rstrip(";,.").strip()
        header = body.endswith(":")
        comp = (cur.get("component") or "") if cur is not None else ""
        if (cur is not None and comp and not cur["ops"] and not cur.get("into") and not pts and not is_req
                and ln[:1].islower() and comp.count("(") > comp.count(")")):
            # перенос строки внутри скобки — и с «;» перед ним: «осуществление … (при неприменении компонента баллы …
            # не учитываются при расчете максимально возможного количества баллов;» + «при отсутствии технологической
            # операции … баллов):» у III-65 — одна вводная, а не обязательное требование и заголовок (ревью D24, M3)
            cur["component"] += (";" if raw_end.endswith(";") else "") + " " + body
        elif header:
            # заголовок списка — и с баллами за весь список («… (при наличии) (13 баллов):» XXIII-1, «… предоставляющих
            # заявителю (20 баллов):» IX-10): баллы группы — часть подписи, а не операция (иначе их сложили бы дважды —
            # рядом условие блока «13 баллов за выполнение всех операций»)
            cur = {"after": last, "component": ln.rstrip().rstrip(";,.").strip() if (pts or vtop) else body, "ops": []}
            cur["component"] = unstale(cur["component"])
            units.append(cur)
            pending.append(cur)
        elif cur is not None and comp.endswith(":"):
            cur["ops"].append({"text": body, "points": points})        # строка под заголовком — его операция
        elif points is not None or (not is_req and under_header(last)
                                    and (cur is None or (cur.get("into") and cur["after"] == last))):
            # операция с баллами — или пункты списка под совпавшим заголовком («…доли резидентов РФ:» → «для
            # акционерных обществ - не менее 75 процентов», «для обществ с ограниченной ответственностью …») — в блок
            # последнего совпавшего элемента. Пункты без баллов под ОПЕРАЦИЕЙ на «:» — часть её текста, как пишет и
            # разборщик: «соблюдение процентной доли … колтюбинговой установки …:» + график «до 31 декабря 2027 г. -
            # не более 35 процентов; …» у XX-12 (отдельными операциями график вытеснял за кап операции с баллами)
            append = (points is None and last >= 0 and items[last][1] is not None and last_raw.endswith(":")
                      and not (blocks[items[last][0]].get("component") or "").rstrip().endswith(":"))
            if cur is not None and cur.get("into") and cur["after"] == last and bool(cur.get("append")) == append:
                cur["ops"].append({"text": body, "points": points})
            else:
                cur = {"after": last, "component": None, "ops": [{"text": body, "points": points}], "into": True,
                       "list": under_header(last), "append": append}
                units.append(cur)
        elif (cur is not None and comp and not cur["ops"] and ln[:1].islower()
              and not is_req and not raw_end.endswith((";", ":"))):
            cur["component"] += " " + body                              # перенос строки одного требования
        elif not is_req and last >= 0:
            # пояснение, а не требование («подтверждением наличия конструкторской документации является …» у I-13,
            # «Допускается применение лифтов … иностранного производства» у XXIII-1): note блока, к которому оно
            # относится в ячейке, — обязательным требованием оно вставало бы при усечении первым (ревью D24)
            cur = {"after": last, "component": body, "ops": [], "note_of": True}
            units.append(cur)
        else:
            cur = {"after": last, "component": body, "ops": []}
            units.append(cur)
            pending.append(cur)
        raw_end = ln.rstrip()
    return units, skipped


def apply_units(r: dict, units: list[dict]) -> int:
    """Вставляет единицы в блоки записи по месту. Возвращает число вставленных строк.

    Заголовок списка, за которым в ячейке идут операции ТОГО ЖЕ блока записи, делит блок: операции с этой встают под
    него («корпус насоса, работающий под давлением:» у XXVI-3 — над своими четырьмя операциями, а не после них)."""
    if not units:
        return 0
    items = _d24_items(r)
    blocks = list(r.get("requirement_blocks") or [])
    n = 0
    for u in units:                         # обрывки — текстом первоисточника на своём месте
        if "relabel" in u:
            blocks[u["relabel"]] = {**blocks[u["relabel"]], "component": u["component"]}
        elif "retext" in u:
            bi, oi = u["retext"]
            if oi is None:
                blocks[bi] = {**blocks[bi], "component": u["text"]}
            else:
                ops = list(blocks[bi]["operations"])
                ops[oi] = {**ops[oi], "text": u["text"]}
                if ops[oi].get("points") is None and u.get("points") is not None:
                    ops[oi]["points"] = u["points"]
                blocks[bi] = {**blocks[bi], "operations": ops}
        else:
            continue
        n += 1
    units = [u for u in units if "relabel" not in u and "retext" not in u]
    new_after: dict[int, list[dict]] = defaultdict(list)          # индекс блока → новые блоки после него
    into: dict[int, list[tuple[int, list[dict]]]] = defaultdict(list)
    starts: dict[int, dict[int, list[dict]]] = defaultdict(lambda: defaultdict(list))   # блок → операция → заголовки
    as_op: dict[int, float | int] = {}
    before: dict[int, list[dict]] = defaultdict(list)             # индекс блока → новые блоки перед ним
    notes_add: dict[int, list[str]] = defaultdict(list)           # индекс блока → пояснения в его note
    appended: dict[tuple[int, int], list[str]] = defaultdict(list)  # (блок, операция) → пункты в её текст

    def pos(bi: int, oi: int | None) -> int:
        """Место элемента в блоке: -1 — подпись, иначе номер операции."""
        return -1 if bi < 0 or oi is None else oi
    for u in units:
        bi, oi = (items[u["after"]][0], items[u["after"]][1]) if u["after"] >= 0 else (-1, None)
        ap = pos(bi, oi)
        if "op_of" in u:
            as_op[u["op_of"]] = u["points"]
            n += 1
            continue
        n += len(u["ops"]) + (1 if u.get("component") else 0)
        if u.get("note_of"):
            notes_add[bi].append(u["component"])
            continue
        nxt = items[u["next"]] if u.get("next") is not None else None
        # в блок — если у него есть операции или это заголовок списка («…доли резидентов РФ:»); блок одиночного
        # требования («наличие … прав …») вводной для чужой операции не становится
        np_ = pos(nxt[0], nxt[1]) if nxt is not None else None
        if u.get("append"):
            appended[(bi, oi)] += [o["text"] for o in u["ops"]]
        elif u.get("into") and bi >= 0 and (blocks[bi].get("operations") or u.get("list")
                                          or (blocks[bi].get("component") or "").rstrip().endswith(":")):
            into[bi].append((min(ap, len(blocks[bi].get("operations") or []) - 1), u["ops"]))
        elif (u.get("component") or "").endswith(":") and nxt is not None and not (nxt[0] == bi and np_ <= ap):
            # вводная списка — над тем, что идёт за ней в ячейке: в том же блоке — делит его; в другом — встаёт перед
            # ним (разборщик переставил блок: «Общие требования» XXI-110 после операций, и вводная после них висела
            # в конце записи, ревью D24, M2) или делит его, если за ней — не первая операция блока
            blk = {"component": u["component"], "operations": list(u["ops"]), "note": None}
            if np_ < 0 or (nxt[0] != bi and np_ == 0):
                before[nxt[0]].append(blk)
            else:
                starts[nxt[0]][np_].append(blk)
        else:
            new_after[bi].append({"component": u.get("component"), "operations": u["ops"], "note": None})
    out = list(new_after.get(-1, []))
    for i, b in enumerate(blocks):
        out += before.get(i, [])
        if i in notes_add:
            note = (b.get("note") or "").strip()
            for t in notes_add[i]:
                note = f"{note}{'' if not note or note.endswith(('.', ';', ':')) else ';'} {t}".strip()
            b = {**b, "note": note}
        if i in as_op:
            # подпись без операций, у строки которой есть баллы, — операция предыдущего блока с операциями
            host = next((k for k in range(len(out) - 1, -1, -1) if out[k].get("operations")), None)
            if host is not None:
                out[host] = {**out[host], "operations": list(out[host]["operations"])
                             + [{"text": b["component"], "points": as_op[i]}]}
                out += new_after.get(i, [])
                continue
        ops = list(b.get("operations") or [])
        for k, o in enumerate(ops):
            if (i, k) in appended:
                t = (o.get("text") or "").rstrip().rstrip(";")
                t = (t if t.endswith(":") else t + ":") + " " + "; ".join(appended[(i, k)])   # строка ячейки — на «:»
                ops[k] = {**o, "text": t}
        ins: dict[int, list[dict]] = defaultdict(list)
        for pos, add in into.get(i, []):
            ins[pos] += add
        seg = {**b, "operations": list(ins.get(-1, []))}
        segs = [seg]
        for k, o in enumerate(ops):
            for nb in starts.get(i, {}).get(k, []):
                seg = nb
                segs.append(seg)
            seg["operations"].append(o)
            seg["operations"] += ins.get(k, [])
        if i not in into and i not in starts:
            segs = [{**b, "operations": ops} if any(key[0] == i for key in appended) else b]   # и note-пояснения
        out += segs
        out += new_after.get(i, [])
    r["requirement_blocks"] = out
    return n


def _stems(s: str | None) -> set[str]:
    """Основы слов (Snowball): «блоки» и «блоков» — одна основа, пятибуквенный префикс их разводил (IX-23)."""
    w = toks(s)
    return set(_stemmer().stem_words(w)) if w else set()


def fold_headings(r: dict, cell_text: str) -> int:
    """Вводная списка без операций («осуществление на территории РФ следующих технологических операций:») встаёт НА
    МЕСТО подписи следующего блока, если та — ярлык разборщика, пересказывающий её же («Технологические операции»,
    «Комплектующие и материалы», «Обязательные требования (20 баллов)»), или пуста. Ярлык — по факту: его нет в
    ячейке, а все основы его слов есть во вводной. Подпись из ячейки («для пластмассовых и резиновых игр …:»,
    «наличие управляющего программно-аппаратного комплекса …») не трогается — вводная остаётся отдельным блоком,
    рантайм печатает её строкой «▸» (`pipeline._hit_operations`, `_heading`). Возвращает число вставших вводных."""
    blocks = r.get("requirement_blocks") or []
    cell = norm(cell_text)
    out, n, i = [], 0, 0
    while i < len(blocks):
        b = blocks[i]
        head = (b.get("component") or "").rstrip()
        nxt = blocks[i + 1] if i + 1 < len(blocks) else None
        if (nxt is not None and not b.get("operations") and head.endswith(":") and not b.get("note")
                and nxt.get("operations")):
            label = (nxt.get("component") or "").strip()
            if not label or (norm(label) not in cell and _stems(label) and _stems(label) <= _stems(head)):
                out.append({**nxt, "component": head})
                n += 1
                i += 2
                continue
        out.append(b)
        i += 1
    if n:
        r["requirement_blocks"] = out
    return n


# Порог записи, который на деле — требование ячейки («для акционерных обществ - не менее 75 процентов …» у I-26,
# «20 баллов» — заголовок группы требований у IX-11): рантайм берёт `min_threshold` ПЕРВЫМ и перекрывает им верный
# порог примечания (прим. 56, прим. 26). Снимается, только если это не формулировка порога, текст есть в ячейке и у
# записи есть порог примечания.
_THRESHOLD_STMT_RE = re.compile(r"не\s+менее\s+\d+[^;]{0,60}?балл|суммарн\w*\s+количеств\w*\s+баллов|всех\s+следующих|"
                                r"не\s+менее\s+\d+\s+(?:из|технологич|операц)", re.I)
_BARE_POINTS_RE = re.compile(r"\(?\s*(\d+(?:[.,]\d+)?)\s*балл\w*\s*\)?\.?")


def _threshold_is_cell_requirement(t: str | None, cell: str, cell_t: set) -> bool:
    """Порог записи — строка требования ячейки, а не порог: её слова есть в ячейке, и это не формулировка порога. Или
    голые «20 баллов» — баллы заголовка группы требований («выполнение обязательных требований, в совокупности
    предоставляющих заявителю 20 баллов:» у IX-6, 10, 11, 16, 18, 28, 71): у `toks` такой строки слов нет вовсе, и
    прежняя проверка её не видела (ревью D24, H1)."""
    if not t or _THRESHOLD_STMT_RE.search(t):
        return False
    tt = toks(t)
    if tt:
        return sum(w in cell_t for w in tt) / len(tt) >= 0.9
    m = _BARE_POINTS_RE.fullmatch(t.strip())
    return bool(m) and any(re.search(rf"(?<![\d,.]){re.escape(m.group(1))}\s*балл", ln) and ln.rstrip().endswith(":")
                           and not _THRESHOLD_STMT_RE.search(ln) for ln in _d24_lines(cell))


def restore_lost_lines(report: dict) -> tuple[list[dict], list[dict]]:
    """D24: каждой записи со своими операциями — строки её ячейки, которых в записи нет. (журнал, пропуски)."""
    from app.rag import thresholds as T
    log, skipped, seen = [], [], set()
    for sec, rep in report.items():
        for x in rep.get("all") or []:
            if not x["text"].strip():
                continue
            cell_t = set(toks(x["text"]))
            for r in x["_recs"]:
                if id(r) in seen or not any(b.get("operations") for b in r.get("requirement_blocks") or []):
                    continue                    # без своих операций — требования через карту наследования
                seen.add(id(r))
                t = r.get("min_threshold")
                drop = (_threshold_is_cell_requirement(t, x["text"], cell_t)
                        and T.lookup_threshold(r.get("okpd2_codes") or [], r["product_name"], sec))
                # порог, который остаётся, — тоже текст ячейки: его строки не потеряны (VI-5: «до 31 декабря 2023 г. -
                # не менее 60 баллов» разборщик положил в поле порога — вставка дала бы их второй раз, требованиями)
                units, skip = lost_units(r, x["text"], None if drop else t,
                                         (None if drop else t)
                                         or T.lookup_threshold(r.get("okpd2_codes") or [], r["product_name"], sec))
                skipped += [{"section": sec, "anchor": r.get("source_anchor"), "why": w, "line": ln[:200]}
                            for w, ln in skip]
                what = []
                n = apply_units(r, units)
                if n:
                    what.append(f"строк ячейки восстановлено: {n}")
                    for key, label in (("relabel", "подпись разборщика → вводная ячейки"),
                                       ("retext", "обрывок → строка ячейки"),
                                       ("op_of", "подпись с баллами → операция")):
                        k = sum(1 for u in units if key in u)
                        if k:
                            what.append(f"{label}: {k}")
                    folded = fold_headings(r, x["text"])
                    if folded:
                        what.append(f"вводных на месте ярлыка: {folded}")
                    has_pts = (any(o.get("points") is not None for u in units for o in u["ops"])
                               or any("op_of" in u for u in units))
                    if has_pts and r.get("requirement_type") not in ("points", "mixed"):
                        r["requirement_type"] = "mixed"
                        what.append("тип")
                if drop:
                    r["min_threshold"] = None
                    what.append("снят порог-требование (есть порог примечания)")
                nt = toks(r.get("notes"))
                if nt and n:
                    bt = set(toks(own_text({"requirement_blocks": r["requirement_blocks"]})))
                    # и числа notes — в блоках мультимножеством: у XX-12 график «до 31 декабря 2027 г. - не более 35
                    # процентов …» повторён для трёх установок, и по словам notes «были в блоках» после первой копии
                    bt |= set(toks(r.get("min_threshold")))
                    if (sum(w in cell_t for w in nt) / len(nt) >= 0.9 and sum(w in bt for w in nt) / len(nt) >= 0.9
                            and not (Counter(_numbers(r.get("notes"))) - _record_numbers(r)
                                     - Counter(_numbers(r.get("min_threshold"))))):
                        r["notes"] = None
                        what.append("notes (текст ячейки) перенесён в блоки")
                if what:
                    log.append({"section": sec, "code": x["code"], "product": r["product_name"][:100],
                                "anchor": r.get("source_anchor"), "ops": [0, n], "what": what, "source": "D24"})
    return log, skipped


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
    ap.add_argument("--skipped", type=Path, help="D24: строки ячеек, которые НЕ восстанавливаются (формулы, …)")
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
        clean = {s: {**{k: v for k, v in r.items() if k != "all"},
                     "shared": [{k: v for k, v in x.items() if k != "_recs"} for x in r["shared"]],
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
    d24_log, d24_skipped = restore_lost_lines(report)
    n_lines = sum(int(w.split(": ")[1]) for e in d24_log for w in e["what"] if w.startswith("строк ячейки"))
    why = Counter(s["why"] for s in d24_skipped)
    print(f"D24: записей {len(d24_log)}, восстановлено строк ячейки {n_lines}; пропущено {len(d24_skipped)} "
          f"({', '.join(f'{k} {v}' for k, v in why.most_common()) or '—'})")
    log += d24_log
    if args.skipped:
        args.skipped.write_text(json.dumps(d24_skipped, ensure_ascii=False, indent=1), encoding="utf-8")
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
