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
from collections import defaultdict
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


def _join_continuations(groups: list[dict]) -> list[dict]:
    """Группа, чей перечень — ПРОДОЛЖЕНИЕ перечня предыдущей, присоединяется к ней.

    Вёрстка рвёт общую ячейку и через строку с кодом, если между ними стоит поправка во всю ширину
    («Аддитивные установки экструзии материала» начинаются с «для акционерных обществ - не менее 75
    процентов» — середины перечня «Аддитивных установок фотополимеризации»). Признак тот же, что у
    генератора списка `R29` (`diag_orphan_requirements.find_fragmented`): предыдущий перечень оборван
    двоеточием, а этот начинается не с зачина требования (`_OPENER` генератора).

    После «;» — только продолжение перечня БЕЗ баллов (документы у «Светодиодов белого диапазона»:
    «…схемы принципиальные электрические <51>;» → «сборочные чертежи на изделие <51>;»). Балльный перечень
    законно начинается с операции («монтаж судовой системы хранения сжиженного газа (… баллов)» у
    газовозов XVIII — свой полный перечень), и склейка по «;» раздала бы его соседям: замер на 508574 —
    перечни судов, которые корпус несёт целиком, превращались в «обрывки» общего."""
    out: list[dict] = []
    for grp in groups:
        first = next((x["req"] for x in grp["rows"] if x["req"]), "").strip()
        prev_text = [x["req"] for x in out[-1]["rows"] if x["req"]] if out else []
        tail = prev_text[-1].rstrip() if prev_text else ""
        cont = tail.endswith(":") or (tail.endswith(";") and not POINTS_RE.search(first.split("\n")[0]))
        if (out and first and cont and not grp["excluded"] and not out[-1]["excluded"]
                and not LIST_OPENER_RE.match(first)):
            out[-1]["rows"] += grp["rows"]
            out[-1]["codes"] += grp["codes"]
            out[-1]["continued"] = True
            continue
        out.append(grp)
    return out


def groups_of(g: list[dict]) -> list[dict]:
    """Строки таблицы раздела → группы «код → [перечни: текст + продукты]».

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
    out = _join_continuations(out)
    for grp in out:
        segs: list[dict] = []
        seg = None
        for x in grp["rows"]:
            if seg is None or (x["name"] and x["req"] and OPENER_RE.match(x["req"]) and seg["text"]):
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
        groups = [grp for g in by_sec[sec] for grp in groups_of(g)]
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
            if min(covers) >= top - gap and top >= min_cover:
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
    union, seen = [], set()
    for r in recs:
        for b in r.get("requirement_blocks") or []:
            k = _block_key(b)
            if k not in seen:
                seen.add(k)
                union.append(b)
    best = max(recs, key=lambda r: _cover(own_text(r), ft))
    # Разбор одного члена — только если у остальных нет операций, которых в нём нет. Покрытия по словам мало:
    # у станков I (28.41.1–28.41.4) ячейку начинают «наличие управляющего программно-аппаратного комплекса
    # (25 баллов)» и «изготовление станины» — 8 операций у I-13, остальные 204 у I-19, и разбор I-19 покрывал
    # ячейку не хуже объединения (слова перечня повторяются), а первые 8 операций терялись.
    pool = _op_texts(best)
    extra = [op for r in recs if r is not best for op in _op_texts(r) if not _op_covered(op, pool)]
    if not extra and _cover(own_text(best), ft) >= _cover(" ".join(own_text(r) for r in recs), ft) - 0.02:
        return copy.deepcopy(best.get("requirement_blocks") or []), f"разбор «{best['product_name'][:60]}»"
    return copy.deepcopy(union), "объединение блоков членов"


def _op_texts(r: dict) -> list[str]:
    return [norm(o.get("text")) for b in r.get("requirement_blocks") or [] for o in b.get("operations") or []]


def _op_covered(op: str, pool: list[str]) -> bool:
    """Операция есть в разборе: тем же текстом или пересказом (≥ 80 % её слов в одной операции разбора)."""
    words = set(op.split())
    return any(op == p or (words and len(words & set(p.split())) / len(words) >= 0.8) for p in pool)


def _rtype(blocks: list[dict], recs: list[dict]) -> str | None:
    """Тип требований перечня: у члена, давшего больше всего операций; есть баллы, а тип не балльный — mixed."""
    donor = max(recs, key=lambda r: sum(len(b.get("operations") or []) for b in r.get("requirement_blocks") or []))
    t = donor.get("requirement_type")
    has_pts = any(o.get("points") is not None for b in blocks for o in b.get("operations") or [])
    return "mixed" if has_pts and t not in ("points", "mixed") else t


def reconcile(report: dict) -> list[dict]:
    """Правка расколотых перечней: каждому члену — полный перечень его ячейки. Возвращает журнал правок."""
    log = []
    for sec, rep in report.items():
        for x in rep["shared"]:
            if x["verdict"] != "расколот":
                continue
            recs = x["_recs"]
            blocks, how = full_list(recs, x["text"])
            rtype = _rtype(blocks, recs)
            thresholds = {r.get("min_threshold") for r in recs if r.get("min_threshold")}
            for r in recs:
                before = sum(len(b.get("operations") or []) for b in r.get("requirement_blocks") or [])
                changed = []
                if [_block_key(b) for b in r.get("requirement_blocks") or []] != [_block_key(b) for b in blocks]:
                    r["requirement_blocks"] = copy.deepcopy(blocks)
                    changed.append("перечень")
                if rtype and r.get("requirement_type") != rtype:
                    r["requirement_type"] = rtype
                    changed.append("тип")
                # Порог в общей ячейке общий; разные пороги у членов — порог примечания по коду, не трогаем.
                if not r.get("min_threshold") and len(thresholds) == 1:
                    r["min_threshold"] = next(iter(thresholds))
                    changed.append("порог")
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
    if args.json:
        clean = {s: {**r, "shared": [{k: v for k, v in x.items() if k != "_recs"} for x in r["shared"]],
                     "into_excluded": [{k: v for k, v in x.items() if k != "_recs"} for x in r["into_excluded"]]}
                 for s, r in report.items()}
        args.json.write_text(json.dumps(clean, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"разбор: {args.json}")
    log = reconcile(report)                           # правит записи в памяти; на диск — только с --write
    print(f"правка: записей {len(log)}, перечней {len({(e['section'], e['code']) for e in log})}"
          + ("" if args.write else " — ПЛАН, файлы не тронуты (--write)"))
    if args.log:
        args.log.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.write:
        write_files(files)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
