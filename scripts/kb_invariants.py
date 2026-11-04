"""Инварианты корпуса, НЕЗАВИСИМЫЕ от правил сборки (`d23_groups`, `d25_share_from_cell`): проверяется то, что
увидят модель и эксперт, — плоский список требований позиции `pipeline._hit_operations` (на нём собраны и контекст
модели, и панель «Источник» `pipeline.source_text`) и сам текст панели.

Ревью PR #268, раунд 6: golden D24 снимался с вывода того же кода и закрепил дубль (XXI-87), корпусный тест D25 брал
оракулом собственные функции шага. Инвариант смотрит только на результат:

(а) в позиции нет дубля и обрезанной копии: внутри одной группы (подряд под одной вводной, или обязательные требования
    позиции) — тексты равны или один начинает другой и оборван (`near`); обязательное требование не повторено
    операцией и не стоит целиком внутри вводной (класс IV-32 на `f1d9ec1`). Текст — без баллов и сносок,
    «Российской Федерации» = «РФ»;
(б) нет вводной без содержимого в конце позиции и вводной-обрывка — строки ячейки «▸» короче трёх слов («составе:»).
    Вводная, за которой сразу подпункт («1. …:» → «1.1. …:»), — законная иерархия, не нарушение;
(в) вводная не стоит сразу перед такой же или своим началом — в контексте и в панели «Источник».

Законные исключения — `ALLOWED` в тесте `tests/test_kb_invariants.py`, поимённо, с обоснованием из первоисточника.

python scripts/kb_invariants.py [--root <каталог structured>]   — все нарушения
"""
from __future__ import annotations

import argparse

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

STRUCTURED = ROOT / "knowledge_base" / "pp719" / "structured"
_PTS_RE = re.compile(r"\(\s*\d+(?:[.,]\d+)?\s*балл\w*[^)]*\)|[-–—]\s*\d+(?:[.,]\d+)?\s*балл\w*", re.I)
_FN_RE = re.compile(r"<[\d.()]+>")
MIN_LEN = 25           # короче — общие слова («сборка», «упаковка»): совпадение не дефект


def norm(text: str | None) -> str:
    t = _FN_RE.sub(" ", _PTS_RE.sub(" ", (text or "").lower().replace("ё", "е")))
    t = _RF_RE.sub("рф", t)                          # разборщик пишет «РФ», ячейка — «Российской Федерации»
    return re.sub(r"\s+", " ", re.sub(r"[^\w%]+", " ", t)).strip()


_RF_RE = re.compile(r"российск\w*\s+федераци\w*")
_CUT_END_RE = re.compile(r"(?:[,(\-–—]|\s(?:и|или|в|во|на|с|со|по|для|от|до|к|из|при|о|об|у|за|не|а))\s*$", re.I)


def near(a_raw: str, b_raw: str, required: bool = False) -> bool:
    """Дубль или обрезанная копия: тексты равны (без баллов и сносок) — или один начинает другой и ОБОРВАН: посреди
    слова («… в течение гарантийного» / «… гарантийного срока хранения»), на запятой, скобке, тире или служебном слове
    («… аккредитованных лабораторий,» / «… лабораторий, отвечающих за проверку …» у XXI-87). У обязательных требований
    позиции — любое начало другого. Замерено и отвергнуто: похожесть ≥ 0.92 (2014 срабатываний на `6a511e1` — пункты,
    различные одним словом: «механики правой» / «левой») и любое начало другого у операций (143: «производство
    электромотора» / «производство электромотора устройства очистки» — общий и частный пункт одного перечня)."""
    a, b = norm(a_raw), norm(b_raw)
    if not a or not b:
        return False
    if a == b:
        return len(a) >= MIN_LEN or required
    (s, s_raw), (l, _) = sorted(((a, a_raw), (b, b_raw)), key=lambda x: len(x[0]))
    if len(s) < MIN_LEN or not l.startswith(s):
        return False
    return required or l[len(s)] != " " or bool(_CUT_END_RE.search((s_raw or "").strip()))


def hit_of(r: dict):
    from app.rag import retriever
    return retriever.Hit(score=1.0, section_roman=r["section_roman"], section_title=r.get("section_title") or "",
                         product_name=r["product_name"], okpd2_codes=r.get("okpd2_codes") or [],
                         min_threshold=r.get("min_threshold"), requirement_blocks=r.get("requirement_blocks") or [],
                         source_anchor=r.get("source_anchor"), okpd2_match=True, payload=r)


def record_violations(r: dict) -> list[tuple[str, str]]:
    """[(вид, описание)] для одной записи."""
    from app.rag import pipeline
    h = hit_of(r)
    mt = h.min_threshold or pipeline.lookup_threshold(h.okpd2_codes, h.product_name, h.section_roman)
    ops = pipeline._hit_operations(h, pipeline.NOTE_CAP_TARGET, mt)
    out: list[tuple[str, str]] = []
    # Последовательность, как её рисует контекст (`format_context`): вводная — и строка `_heading`, и смена `_parent`
    # у операций; группа операций — подряд идущие под одной вводной (две вводные с одинаковым текстом у литьевого и у
    # клеевого метода XVII-11 — разные группы); обязательные требования позиции — своя группа.
    seq: list[tuple[str, str]] = []
    prev = object()
    for o in ops:
        t = (o.get("text") or "").strip()
        if o.get("_heading"):
            seq.append(("HL", t)); prev = object()
        elif o.get("_required"):
            seq.append(("REQ", t)); prev = object()
        else:
            if o.get("_parent") != prev:
                if o.get("_parent"):
                    seq.append(("H", o["_parent"].strip()))
                prev = o.get("_parent")
            seq.append(("OP", t))
    groups: dict[str, list[str]] = {}
    run = 0
    for kind, t in seq:
        if kind in ("H", "HL"):
            run += 1
        elif kind == "REQ":
            groups.setdefault("REQ", []).append(t)
        else:
            groups.setdefault(f"G{run}", []).append(t)
    for key, items in groups.items():
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                if near(items[i], items[j], key == "REQ"):
                    out.append(("а", f"почти совпадают: «{items[i]}» / «{items[j]}»"))
    # обязательное требование, повторённое операцией (XXI-87: обрезанная операция и полное требование)
    all_ops = [t for k, t in seq if k == "OP"]
    for req in groups.get("REQ", []):
        for t in all_ops:
            if near(req, t, True):
                out.append(("а", f"требование повторено операцией: «{req}» / «{t}»"))
    # требование или операция целиком внутри вводной, которая длиннее неё хотя бы на три слова (IV-32 на `f1d9ec1`:
    # «наличие … исключительных прав на ПО …» — операцией «Общих требований» и началом вводной перечня)
    # Только требования позиции: обязательные и «наличие …» не короче восьми слов. Пункт перечня внутри вводной
    # (VI-13: разборщик вклеил «а) изготовление корпуса …» в подпись) — другой, давний класс: на `dev` 25 записей.
    heads = [norm(t) for k, t in seq if k in ("H", "HL")]
    for k, t in seq:
        nt = norm(t)
        if (k == "REQ" or (k == "OP" and nt.startswith("наличие ") and len(nt.split()) >= 8)) and len(nt) >= MIN_LEN:
            for hd in heads:
                if hd != nt and len(hd.split()) >= len(nt.split()) + 3 and f" {nt} " in f" {hd} ":
                    out.append(("а", f"требование повторено во вводной: «{t}» / «{hd}»"))
                    break
    for k, (kind, text) in enumerate(seq):
        if kind not in ("H", "HL"):
            continue
        nxt = seq[k + 1] if k + 1 < len(seq) else None
        # вводная, за которой сразу подпункт («1. …:» → «1.1. …:» у IX-62), — законная иерархия
        if nxt is not None and nxt[0] in ("H", "HL") and near(text, nxt[1], True):
            out.append(("в", f"вводная перед такой же: «{text[:70]}»"))
        elif nxt is None:
            out.append(("б", f"вводная без содержимого в конце: «{text[:70]}»"))
        if kind == "HL" and len(re.findall(r"\w+", text)) < 3:      # строка ячейки, не имя узла
            out.append(("б", f"вводная-обрывок: «{text[:70]}»"))
    # панель «Источник»: строка «▸ …» сразу за такой же
    lines = [ln for ln in pipeline.source_text(h, cap=10 ** 7).splitlines() if ln.strip()]
    for a, b in zip(lines, lines[1:]):
        if a.startswith("▸") and b.startswith("▸") and norm(a) == norm(b):
            out.append(("в", f"панель: вводная перед такой же: «{a[:70]}»"))
    return out


def load(root: Path = STRUCTURED) -> list[dict]:
    recs = []
    for f in sorted(root.glob("*.json")):
        recs += [r for r in json.loads(f.read_text(encoding="utf-8")) if r.get("record_type") != "section_methodology"]
    return recs


def key(r: dict) -> str:
    m = re.search(r"позиция (\d+)", r.get("source_anchor") or "")
    return f"{r['section_roman']}-{m.group(1) if m else '?'}"


def violations(root: Path = STRUCTURED) -> dict[str, list[tuple[str, str]]]:
    return {key(r): v for r in load(root) if (v := record_violations(r))}


def main() -> int:
    from app.core.console import enable_utf8
    enable_utf8()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", type=Path, default=STRUCTURED)
    args = ap.parse_args()
    v = violations(args.root)
    for k, items in v.items():
        for kind, text in items:
            print(f"{k:10} ({kind}) {text}")
    kinds = {}
    for items in v.values():
        for kind, _ in items:
            kinds[kind] = kinds.get(kind, 0) + 1
    print(f"записей с нарушениями: {len(v)}; по видам: {kinds}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
