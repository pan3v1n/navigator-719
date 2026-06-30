"""P1 (под-ось) — устойчивость ретрива к ПОВЕРХНОСТНОМУ ШУМУ: опечатки и аббревиатуры.

`eval_paraphrase.py` мерил СЕМАНТИЧЕСКИЙ перефраз (другие слова, тот же смысл). Этот харнес
мерит другую ось — когда слова ТЕ ЖЕ, но искажены на уровне символов/сокращений:
  • ОПЕЧАТКИ — перестановка соседних, пропуск, удвоение, замена на соседнюю клавишу ЙЦУКЕН;
  • АББРЕВИАТУРЫ — доменные сокращения («оборудование»→«обор.», «автоматический»→«автомат.»).

Зачем: dense e5 обычно устойчив к опечаткам (сабворд), а sparse BM25 ХРУПОК (опечатка → другой
стем → нет совпадения; «обор.» не стеммится в «оборудование»). Тест показывает, держит ли ГИБРИД
(в основном dense) живой косноязычный ввод эксперта. Детерминированно (seed), без DeepSeek на
генерацию (реранкер — опц., как продакшен-путь). Выборка та же, что в eval_paraphrase (сопоставимо).

Запуск (нужен Qdrant; e5 свободен после других прогонов):
  .venv\\Scripts\\python scripts\\eval_perturbation.py --per-section 5 --report docs/eval_perturbation_report.md
"""

from __future__ import annotations

import argparse
import random
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.rag.retriever import search  # noqa: E402
from scripts.eval_paraphrase import _rank, stratified  # noqa: E402
from scripts.load_kb import load_records  # noqa: E402

# ЙЦУКЕН — горизонтальные соседи (замена «промахнулся по клавише»)
_ROWS = ["йцукенгшщзхъ", "фывапролджэ", "ячсмитьбю"]
_ADJ: dict[str, str] = {}
for _row in _ROWS:
    for _i, _ch in enumerate(_row):
        _nb = (_row[_i - 1] if _i > 0 else "") + (_row[_i + 1] if _i < len(_row) - 1 else "")
        _ADJ[_ch] = _nb

# доменные сокращения: (стем-префикс слова → аббревиатура)
_ABBR = [
    ("оборудован", "обор."), ("установк", "уст."), ("устройств", "устр."),
    ("автоматическ", "автомат."), ("электрическ", "эл."), ("электронн", "эл."),
    ("промышленн", "пром."), ("производствен", "произв."), ("производств", "пр-во"),
    ("транспортн", "трансп."), ("аппаратур", "аппар."), ("аппарат", "аппар."),
    ("двигател", "двиг."), ("медицинск", "мед."), ("комплекс", "компл."),
    ("температур", "темп."), ("напряжен", "напр."), ("грузоподъёмн", "г/п"),
    ("грузоподъемн", "г/п"), ("систем", "сист."), ("машин", "маш."),
    ("инструмент", "инстр."), ("измерительн", "измер."), ("гидравлическ", "гидр."),
    ("радиоэлектронн", "р/э"), ("кондиционер", "кондиц."), ("прецизионн", "прециз."),
]


def make_typo(text: str, rng: random.Random, k: int) -> str:
    chars = list(text)
    alpha = [i for i, c in enumerate(chars) if c.isalpha()]
    if len(alpha) < 3:
        return text
    k = min(max(1, k), len(alpha))
    for i in sorted(rng.sample(alpha, k), reverse=True):
        t = rng.choice(["swap", "del", "dup", "sub"])
        c = chars[i].lower()
        if t == "swap" and i + 1 < len(chars) and chars[i + 1].isalpha():
            chars[i], chars[i + 1] = chars[i + 1], chars[i]
        elif t == "del" and len(alpha) > 3:
            del chars[i]
        elif t == "dup":
            chars.insert(i, chars[i])
        elif t == "sub" and _ADJ.get(c):
            chars[i] = rng.choice(_ADJ[c])
        elif i + 1 < len(chars):  # фоллбэк — перестановка
            chars[i], chars[i + 1] = chars[i + 1], chars[i]
    return "".join(chars)


def make_abbrev(text: str) -> str:
    def repl(w: str) -> str:
        lw = w.lower()
        for stem, ab in _ABBR:
            if lw.startswith(stem):
                return ab
        return w

    parts = re.split(r"(\W+)", text)
    res = "".join(repl(p) if (p and p[0].isalpha()) else p for p in parts)
    if res == text:  # ни одного совпадения — укорачиваем самое длинное слово
        words = sorted(set(re.findall(r"[А-Яа-яЁё]{6,}", text)), key=len, reverse=True)
        if words:
            res = text.replace(words[0], words[0][:4] + ".", 1)
    return res


def _eval_query(q, anc, sec, limit, rerank_on):
    ex = lambda h: h.source_anchor == anc   # noqa: E731
    se = lambda h: h.section_roman == sec   # noqa: E731
    hits = search(q, okpd2=None, limit=limit)
    e_raw, s_raw = _rank(hits, ex), _rank(hits, se)
    if rerank_on and e_raw != 1:
        from app.rag.reranker import rerank
        rr = rerank(q, hits)
        e_rr, s_rr = _rank(rr, ex), _rank(rr, se)
    else:
        e_rr, s_rr = e_raw, s_raw
    top1 = f"[{hits[0].section_roman}] {hits[0].product_name}" if hits else "—"
    return {"q": q, "sec": sec, "e_raw": e_raw, "s_raw": s_raw, "e_rr": e_rr, "s_rr": s_rr, "top1": top1}


def evaluate(recs, limit, rerank_on, seed, progress):
    vb, typo, abbr = [], [], []
    it = recs
    if progress:
        try:
            from tqdm import tqdm
            it = tqdm(recs, unit="поз", desc="ретрив")
        except ImportError:
            pass
    for idx, r in enumerate(it):
        anc, sec, name = r["source_anchor"], r.get("section_roman", "?"), r["product_name"]
        rng = random.Random(seed + idx)
        k = min(3, max(1, sum(c.isalpha() for c in name) // 12))
        vb.append({**_eval_query(name, anc, sec, limit, rerank_on), "name": name})
        typo.append({**_eval_query(make_typo(name, rng, k), anc, sec, limit, rerank_on), "name": name})
        abbr.append({**_eval_query(make_abbrev(name), anc, sec, limit, rerank_on), "name": name})
    return vb, typo, abbr


def _r(rows, key, k):
    return sum(1 for x in rows if x[key] and x[key] <= k) / len(rows) if rows else 0.0


def report(vb, typo, abbr, limit, rerank_on):
    L = ["=" * 80,
         f"P1 ПОВЕРХНОСТНЫЙ ШУМ — {len(vb)} позиций (опечатки + аббревиатуры), top-{limit}",
         "=" * 80, ""]

    def block(tag, rows):
        L.append(f"  {tag:<26} exact: r@1={_r(rows,'e_raw',1):.3f} r@3={_r(rows,'e_raw',3):.3f}"
                 + (f"  →реранк r@1={_r(rows,'e_rr',1):.3f} r@3={_r(rows,'e_rr',3):.3f}" if rerank_on else "")
                 + f"   раздел r@3(raw)={_r(rows,'s_raw',3):.3f}"
                 + (f"→{_r(rows,'s_rr',3):.3f}" if rerank_on else ""))

    L.append("РЕТРИВ (exact=своя позиция; раздел=верный section_roman):")
    block("ВЕРБАТИМ", vb)
    block("ОПЕЧАТКИ", typo)
    block("АББРЕВИАТУРЫ", abbr)
    L.append("")
    L.append(f"ПРОСАДКА exact r@1 (raw): опечатки −{_r(vb,'e_raw',1)-_r(typo,'e_raw',1):.3f}   "
             f"аббревиатуры −{_r(vb,'e_raw',1)-_r(abbr,'e_raw',1):.3f}")
    L.append(f"ПРОСАДКА раздел r@3 ({'+реранк' if rerank_on else 'raw'}): "
             f"опечатки −{_r(vb,'s_rr' if rerank_on else 's_raw',3)-_r(typo,'s_rr' if rerank_on else 's_raw',3):.3f}   "
             f"аббревиатуры −{_r(vb,'s_rr' if rerank_on else 's_raw',3)-_r(abbr,'s_rr' if rerank_on else 's_raw',3):.3f}")
    L.append("")

    for tag, rows in (("ОПЕЧАТКИ", typo), ("АББРЕВИАТУРЫ", abbr)):
        key = "s_rr" if rerank_on else "s_raw"
        fails = [x for x in rows if not x[key] or x[key] > limit]
        L.append(f"{tag}: верный раздел не в top-{limit} (реальные провалы): {len(fails)}/{len(rows)}")
        for x in fails[:12]:
            L.append(f"  ✗ [{x['sec']:>4}] {x['name'][:30]:30} → «{x['q'][:34]}»  top1: {x['top1'][:24]}")
        L.append("")

    L.append("QC — образцы искажений:")
    for i in range(min(6, len(vb))):
        L.append(f"  [{vb[i]['sec']}] {vb[i]['name'][:30]:30} | опеч: «{typo[i]['q'][:34]}» | аббр: «{abbr[i]['q'][:34]}»")
    L.append("")
    return L


def main() -> None:
    ap = argparse.ArgumentParser(description="P1 устойчивость к опечаткам/аббревиатурам")
    ap.add_argument("--per-section", type=int, default=5)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--no-rerank", action="store_true")
    ap.add_argument("--report", type=str, default="")
    ap.add_argument("--no-progress", action="store_true")
    args = ap.parse_args()

    try:
        from app.rag.retriever import _client
        _client().get_collections()
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Qdrant недоступен ({e}). Подними Docker + Qdrant (:6533).")

    recs = stratified(load_records(), args.per_section, args.seed)
    print(f"Выборка: {len(recs)} позиций (та же, что в eval_paraphrase: per-section={args.per_section}, seed={args.seed})")
    vb, typo, abbr = evaluate(recs, args.limit, not args.no_rerank, args.seed, not args.no_progress)
    out = report(vb, typo, abbr, args.limit, not args.no_rerank)
    text = "\n".join(out)
    print("\n" + text)
    if args.report:
        path = (ROOT / args.report) if not Path(args.report).is_absolute() else Path(args.report)
        path.write_text("# P1 perturbation (опечатки/аббревиатуры) eval\n\n```\n" + text + "\n```\n", encoding="utf-8")
        print(f"\nОтчёт сохранён: {path}")


if __name__ == "__main__":
    main()
