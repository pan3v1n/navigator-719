"""EV8-1 #178 — пробы сужения целевых позиций ТОЧНОГО кода по наименованию в вопросе. Офлайн, без Qdrant.

ЗАЧЕМ. Под одним кодом ОКПД2 в приложении лежат РАЗНЫЕ изделия (у 32.50.13.190 — 37 позиций), и все совпавшие
с кодом становились целевыми с полными требованиями. Сужение (`pipeline._narrow_by_name`) оставляет позиции,
о которых вопрос говорит. Его цена — потерять позицию, о которой спросили: первая версия (27.10) теряла её
на оговорках («за исключением вакуумных пробирок»), синонимах и вопросах о двух изделиях, а её замер был
тавтологичен (вопрос — полное имя самой позиции). Этот набор строится из ДАННЫХ, а не из правила, и бьёт
туда, где первая версия ломалась.

ОКНО. Как в рантайме — до 8 позиций кода (`okpd2_match`): ожидаемые позиции и прочие позиции кода по числу
общих с вопросом слов. Сравнение — с выбором БЕЗ запроса (тот же `target_hits`), поэтому подмена строки-
квалификатора содержательным сиблингом (`EV6`) нарушением не считается.

СЕМЕЙСТВА (ожидание — в скобках):
  own_head   «Какие требования к <голова позиции> <код>?»           (позиция остаётся целевой)
  own_name   «<полное наименование> <код>»                           (остаётся)
  prefix1–3  «требования к <первые 1–3 слова> <код>»                 (остаётся)
  pair       «<голова А> и <голова Б> <код>»                         (обе остаются)
  paraphrase перефразы `eval_paraphrases.json` + код                 (остаётся)
  exclusion  «<пункт оговорки позиции X> <код> требования»           (если сужено — X НЕ целевая)
  group      «<официальное имя кода> <код>»                          (не сужается)
  manual     случаи ревью 27.10 (синонимы, общие определения)        (по списку)

Выход: по семействам — проб, сужено, нарушений; нарушения поимённо (до --show). Код возврата 1 при нарушениях.
  .venv\\Scripts\\python scripts\\eval_ev8_probes.py
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.console import enable_utf8  # noqa: E402

enable_utf8()
from app.rag import okpd2_ref, pipeline  # noqa: E402
from app.rag.retriever import Hit  # noqa: E402

WINDOW = 8
STRUCT = ROOT / "knowledge_base" / "pp719" / "structured"
PARA = ROOT / "scripts" / "eval_paraphrases.json"

# Случаи ревью 27.10 (первая версия выбирала чужое изделие): (код, вопрос, обязаны остаться, обязаны уйти) —
# начала наименований.
MANUAL = [
    ("28.30.86.110", "Кормораздатчики для крупного рогатого скота 28.30.86.110 — какие требования?",
     ["Раздатчики кормов"], []),
    ("28.30.86.110", "Мобильный кормораздатчик-смеситель для ферм крупного рогатого скота, код 28.30.86.110",
     ["Раздатчики кормов"], []),
    ("28.30.86.110", "Автопоилки для крупного рогатого скота 28.30.86.110 требования", ["Поилки животноводческие"], []),
    ("28.30.86.110", "Навозоуборочный транспортер (удаление навоза) 28.30.86.110",
     ["Установки для удаления навоза"], []),
    ("28.30.86.110", "Раздатчики кормов для КРС 28.30.86.110 — сколько баллов?",
     ["Раздатчики кормов"], ["Автоматические чесалки", "Поилки"]),
    ("32.50.13.190", "Вакуумные пробирки для взятия крови, одноразовые стерильные, 32.50.13.190",
     ["Вакуумные одноразовые пробирки"], ["Контейнеры для биопроб"]),
    ("32.50.13.190", "Капельницы (системы для вливания инфузионных растворов) одноразовые 32.50.13.190",
     ["Устройства для инфузионной терапии"], []),
    ("32.50.13.190", "Шприцы инъекционные однократного применения 32.50.13.190", ["Иглы для инъекций"], []),
]


# Якоря `eval_paraphrases.json`, устаревшие после перенумерации разделов редакциями, — проверены вручную:
# перефразы описывают другую позицию. Ожидание «позиция остаётся» для них неверно; набор чинится в `EV24` #244.
STALE_ANCHORS = {
    "Приложение к ПП №719, Раздел IV, позиция 46":
        "перефразы описывают «Оптические приемники когерентного сигнала», а якорь ведёт к лазерам",
}


def _records() -> list[dict]:
    out = []
    for f in sorted(STRUCT.glob("*.json")):
        out += [r for r in json.loads(f.read_text(encoding="utf-8")) if r.get("record_type") != "section_methodology"]
    return out


def _groups(recs: list[dict]) -> dict[str, tuple[str, list[dict]]]:
    """{ключ кода: (код, позиции)} — коды, под которыми ≥ EV8_MANY позиций."""
    g: dict[str, list[dict]] = defaultdict(list)
    code_of: dict[str, str] = {}
    for r in recs:
        for c in r.get("okpd2_codes") or []:
            k = okpd2_ref.okpd2_key(c)
            if k and r not in g[k]:
                g[k].append(r)
                code_of.setdefault(k, c)
    return {k: (code_of[k], v) for k, v in g.items() if len(v) >= pipeline.EV8_MANY}


def _hit(r: dict) -> Hit:
    return Hit(score=1.0, section_roman=r["section_roman"], section_title=r.get("section_title") or "",
               product_name=r["product_name"], okpd2_codes=list(r.get("okpd2_codes") or []), min_threshold=None,
               requirement_blocks=[], source_anchor=r["source_anchor"], okpd2_match=True,
               payload={"record_type": r.get("record_type")})


def _window(group: list[dict], query: str, first: list[dict]) -> list[Hit]:
    qs = pipeline._ev8_stems(query)
    rest = sorted((r for r in group if r not in first),
                  key=lambda r: (-len(qs & pipeline._ev8_stems(r["product_name"])), r["source_anchor"]))
    return [_hit(r) for r in (first + rest)[:WINDOW]]


def _anchors(hits: list[Hit]) -> set[str]:
    return {h.source_anchor for h in hits}


def _head_texts(name: str) -> list[str]:
    text = pipeline._ev8_base(name)
    while pipeline._EV8_PAREN_RE.search(text):
        text = pipeline._EV8_PAREN_RE.sub(" ", text)
    return [t for part in text.split(";") if (t := " ".join(part.split("(")[0].split()))]


def _words(name: str) -> list[str]:
    return [w for w in re.findall(r"[А-Яа-яЁёA-Za-z-]+", pipeline._ev8_base(name))
            if len(w) > 3 and pipeline._ev8_stems(w)]


def _exclusion_items(name: str) -> list[str]:
    out = []
    for m in re.finditer(r"(?:за\s+исключением|кроме)\s+([^);]+)", name or "", re.IGNORECASE):
        out += [p.strip() for p in re.split(r"[,;]", m.group(1)) if len(pipeline._ev8_stems(p)) >= 2]
    return out


def run(show: int = 15) -> int:
    recs = _records()
    groups = _groups(recs)
    names = okpd2_ref._okpd2_names()
    stat: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0, 0])  # проб, сужено, нарушений, Σ до, Σ после
    bad: dict[str, list[str]] = defaultdict(list)

    def probe(fam: str, code: str, group: list[dict], query: str, keep: list[dict], drop_if_narrowed=()):
        win = _window(group, query, keep)
        base = pipeline.target_hits(win, code)
        got = pipeline.target_hits(win, code, query)
        st = stat[fam]
        st[0] += 1
        narrowed = len(got) < len(base)
        st[1] += narrowed
        st[3] += len(base)
        st[4] += len(got)
        lost = [r for r in keep if r["source_anchor"] in _anchors(base) and r["source_anchor"] not in _anchors(got)]
        wrong = [r for r in drop_if_narrowed if narrowed and r["source_anchor"] in _anchors(got)]
        if lost or wrong:
            st[2] += 1
            what = "; ".join([f"потеряна «{r['product_name'][:50]}»" for r in lost]
                             + [f"оставлена исключающая «{r['product_name'][:50]}»" for r in wrong])
            bad[fam].append(f"{query[:90]} → {what}")

    paras = json.loads(PARA.read_text(encoding="utf-8")) if PARA.exists() else {}
    stale: list[str] = []
    for key, (code, group) in groups.items():
        for p in group:
            heads = _head_texts(p["product_name"])
            if heads:
                probe("own_head", code, group, f"Какие требования к {heads[0]} {code}?", [p])
            probe("own_name", code, group, f"{p['product_name']} {code}", [p])
            w = _words(p["product_name"])
            for n in (1, 2, 3):
                if len(w) >= n:
                    probe(f"prefix{n}", code, group, f"требования к {' '.join(w[:n])} {code}", [p])
            for item in _exclusion_items(p["product_name"]):
                probe("exclusion", code, group, f"{item} {code} требования", [], drop_if_narrowed=[p])
            for para in paras.get(p["source_anchor"], []):
                if p["source_anchor"] in STALE_ANCHORS:
                    stale.append(f"{p['source_anchor'].replace('Приложение к ПП №719, ', '')}: «{para[:60]}» — "
                                 f"{STALE_ANCHORS[p['source_anchor']]}")
                    continue
                probe("paraphrase", code, group, f"{para} {code}", [p])
        for a, b in zip(group, group[1:]):
            ha, hb = _head_texts(a["product_name"]), _head_texts(b["product_name"])
            if ha and hb:
                probe("pair", code, group, f"{ha[0]} и {hb[0]} {code}", [a, b])
        official = names.get(okpd2_ref.okpd2_key(code)) or names.get(code)
        if official:
            q = f"{official} {code}"
            win = _window(group, q, [])
            st = stat["group"]
            st[0] += 1
            nb, ng = len(pipeline.target_hits(win, code)), len(pipeline.target_hits(win, code, q))
            st[3] += nb
            st[4] += ng
            if ng < nb:
                st[1] += 1
                st[2] += 1
                bad["group"].append(f"{q[:90]} → сужено")
    for code, q, keep_starts, drop_starts in MANUAL:
        key = okpd2_ref.okpd2_key(code)
        group = groups[key][1]
        keep = [r for r in group if any(r["product_name"].startswith(s) for s in keep_starts)]
        drop = [r for r in group if any(r["product_name"].startswith(s) for s in drop_starts)]
        assert len(keep) == len(keep_starts), (code, keep_starts)
        win = _window(group, q, keep + drop)
        got = _anchors(pipeline.target_hits(win, code, q))
        st = stat["manual"]
        st[0] += 1
        nb = len(pipeline.target_hits(win, code))
        st[1] += len(got) < nb
        st[3] += nb
        st[4] += len(got)
        miss = [r for r in keep if r["source_anchor"] not in got] + [r for r in drop if r["source_anchor"] in got]
        if miss:
            st[2] += 1
            bad["manual"].append(f"{q[:90]} → не так: {[r['product_name'][:40] for r in miss]}")

    print(f"кодов с ≥ {pipeline.EV8_MANY} позициями: {len(groups)}; позиций под ними: "
          f"{len({r['source_anchor'] for _c, g in groups.values() for r in g})}")
    print(f"{'семейство':12} {'проб':>6} {'сужено':>8} {'нарушений':>10} {'целевых до → после (среднее)':>30}")
    total = 0
    for fam in ("own_head", "own_name", "prefix1", "prefix2", "prefix3", "pair", "paraphrase", "exclusion",
                "group", "manual"):
        n, nar, viol, sb, sg = stat[fam]
        total += viol
        avg = f"{sb / n:.1f} → {sg / n:.1f}" if n else "—"
        print(f"{fam:12} {n:6} {nar:8} {viol:10} {avg:>30}")
    for fam, lines in bad.items():
        print(f"\n— нарушения [{fam}]:")
        for ln in lines[:show]:
            print("   ", ln)
    if stale:
        print(f"\n— перефразы с устаревшим якорем (`eval_paraphrases.json`, не нарушения): {len(stale)}")
        for ln in stale[:show]:
            print("   ", ln)
    print(f"\nнарушений всего: {total}")
    return 1 if total else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--show", type=int, default=15, help="сколько нарушений печатать на семейство")
    sys.exit(run(ap.parse_args().show))
