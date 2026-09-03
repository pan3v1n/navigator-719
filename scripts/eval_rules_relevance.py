# -*- coding: utf-8 -*-
"""Порог релевантности для ПРОЦЕДУРНОЙ ветки: замер разделяющей способности и выбор порога.

ЗАЧЕМ. Раунд 7 ревью PR #137 записал диагноз — «у процедурной ветки НЕТ гейта вне-сферы» — и
лечил его СЛОВАРЁМ маршрута. Раунд 8 показал, что словарь лечит симптом: три HIGH из шести имели
один корень (расширенная тема, кормящая гейт), а замер на независимой популяции дал 22 → 23, то
есть правка не улучшила НИЧЕГО. Причина не тронута: `search_rules` не имеет порога релевантности,
`_answer_procedural` держит `low_relevance=False` жёстко, и за восемью якорями гейта не стоит
ничего. Здесь меряется структурная замена: щедрый маршрут + порог по плотному сходству.

⚠⚠⚠ ГЛАВНОЕ ТРЕБОВАНИЕ К ЭТОМУ ЗАМЕРУ — НЕЗАВИСИМОСТЬ ПОПУЛЯЦИИ ОТ ПОРОГА. Кандидат 0.851 был
подобран на `eval_route_st1_population.json`; проверять его там же — ровно та ошибка, которую
раунд 8 и разбирает («победитель выиграл у популяции, которая не умела его опровергнуть»).
Поэтому порог сверяется на наборах, заведённых ЗАДОЛГО до него и под другие вопросы:
    eval_golden_rules.json      25 вопросов — приёмочная ось `K9`, ВСЕ обязаны быть отвечены
                                (чистая популяция ПОТЕРЬ: порог не имеет права их погасить);
    eval_golden_negative.json   39 вопросов вне сферы (чистая популяция УТЕЧЕК).
Оба набора не знают ни про ТН ВЭД, ни про порог, ни про правку маршрута.

⚠ ТРИ ИСХОДА, А НЕ ДВА. Если коллекция норм пуста или недоступна, скрипт НЕ печатает «0 утечек»
— он останавливается: «не измерено». Ноль инструмента, не видящего популяцию, означает «не считаю».

ЗАПУСК:
    .venv/Scripts/python.exe scripts/eval_rules_relevance.py
    ... --cache out.json     сохранить сходства (свип порогов потом бесплатный)
    ... --show 0.851         печать поимённо на выбранном пороге
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.console import enable_utf8  # noqa: E402

enable_utf8()

from app.core.config import settings  # noqa: E402
from app.rag import procedural  # noqa: E402
from app.rag.retriever import dense_top1  # noqa: E402

RULES = settings.QDRANT_RULES_COLLECTION

# (файл, как достать вопросы, ожидание: True — обязан быть отвечен процедурно)
POPULATIONS = [
    ("eval_golden_rules.json",    "K9 приёмка",       True),
    ("eval_golden_negative.json", "вне сферы (без ТН ВЭД)", False),
    # ⚠⚠ БЕЗ ЭТОГО НАБОРА ЗАМЕР СЛЕП НА СТОРОНУ УТЕЧЕК. В `eval_golden_negative` РОВНО НОЛЬ
    # вопросов с ТН ВЭД (проверено: 0 из 39) — то есть его «0 утечек» означает «не считаю», а не
    # «чисто». Это записанный урок проекта, и на нём же построен весь раунд 8.
    # ⚠ Набор смещён по оси МАРШРУТА (все 12 ставят дисквалификатор вплотную), но эта ось
    # ОРТОГОНАЛЬНА плотному сходству, которым работает гейт: порог не смотрит на соседнее слово.
    # Для проверки ГЕЙТА он независим, для проверки СЛОВАРЯ — нет.
    ("eval_offdomain_tnved.json", "вне сферы + ТН ВЭД", False),
    ("eval_route_st1_population.json", "маршрут СТ-1 (порог подбирался ЗДЕСЬ)", None),
]

THRESHOLDS = [0.0, 0.830, 0.840, 0.845, 0.851, 0.855, 0.860, 0.870]


def _cases(path: Path, expect: bool | None) -> list[dict]:
    d = json.loads(path.read_text(encoding="utf-8"))
    raw = d.get("cases", d) if isinstance(d, dict) else d
    out = []
    for c in raw:
        if expect is None:                       # популяция маршрута несёт своё поле
            want = c["expect"] == "st1"
        elif expect is False:                    # golden_negative: in_scope=False → вне сферы
            want = bool(c.get("in_scope", False))
        else:
            want = True
        out.append({"query": c["query"], "want": want,
                    "id": c.get("id"), "axis": c.get("axis", c.get("category", ""))})
    return out


def collect(cache: Path | None) -> list[dict]:
    """Сходство + маршрут для каждого вопроса. Дорогая часть (эмбеддинг), поэтому кэшируется."""
    if cache and cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))["rows"]

    rows = []
    for fname, label, expect in POPULATIONS:
        for c in _cases(ROOT / "scripts" / fname, expect):
            # ⚠ Маршрут — тем же вызовом, что в рантайме: тест, зовущий компонент напрямую,
            # не проверяет путь до него (урок PR #137).
            routed = bool(procedural.is_procedural(c["query"], has_code=False))
            score = dense_top1(c["query"], collection=RULES)
            rows.append({**c, "pop": label, "routed": routed, "score": round(score, 4)})
    if cache:
        cache.write_text(json.dumps({"rows": rows}, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    return rows


def _errors(rows: list[dict], thr: float) -> tuple[int, int]:
    """(утечки, потери) при пороге: отвечаем процедурно, если маршрут ПУСТИЛ и порог пройден."""
    leak = loss = 0
    for r in rows:
        answered = r["routed"] and r["score"] >= thr
        if r["want"] and not answered:
            loss += 1
        if not r["want"] and answered:
            leak += 1
    return leak, loss


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", type=Path, default=ROOT / "scripts" / "_rules_relevance.json")
    ap.add_argument("--show", type=float, help="печать поимённо на этом пороге")
    args = ap.parse_args()

    rows = collect(args.cache)

    # ⚠⚠ ПОЛОЖИТЕЛЬНЫЙ КОНТРОЛЬ НА ПУТИ ДЕЙСТВИЯ: если сходства вырождены (все нули или все
    # одинаковы), инструмент НИЧЕГО не измеряет, и печатать пороги нельзя.
    scores = [r["score"] for r in rows]
    if not scores or max(scores) - min(scores) < 0.01:
        print("!! НЕ ИЗМЕРЕНО: сходства вырождены — коллекция норм пуста или недоступна.")
        print(f"   получено {len(scores)} значений, разброс "
              f"{(max(scores) - min(scores)) if scores else 0:.4f}")
        return 2

    print(f"\nПОРОГ РЕЛЕВАНТНОСТИ ПРОЦЕДУРНОЙ ВЕТКИ — коллекция {RULES}")
    print("=" * 78)

    print("\n  Распределение сходства по популяциям:")
    for _f, label, _e in POPULATIONS:
        sub = [r for r in rows if r["pop"] == label]
        for want, name in ((True, "обязаны быть отвечены"), (False, "обязаны получить отказ")):
            s = sorted(r["score"] for r in sub if r["want"] is want)
            if not s:
                continue
            p10 = s[max(0, len(s) // 10)]
            print(f"    {label:<38} {name:<22} n={len(s):>3}  "
                  f"min {s[0]:.4f}  p10 {p10:.4f}  медиана {s[len(s)//2]:.4f}  max {s[-1]:.4f}")

    print("\n  Свип порога (ошибки НА ВСЕХ популяциях, маршрут — как сейчас в дереве):")
    print(f"    {'порог':<9} {'утечек':>7} {'потерь':>7} {'итого':>7}    "
          f"{'K9 погашено':>12}")
    k9 = [r for r in rows if r["pop"] == "K9 приёмка"]
    for thr in THRESHOLDS:
        leak, loss = _errors(rows, thr)
        k9_lost = sum(1 for r in k9 if not (r["routed"] and r["score"] >= thr))
        mark = "  ← порог из отчёта" if abs(thr - 0.851) < 1e-9 else ""
        print(f"    {thr:<9.3f} {leak:>7} {loss:>7} {leak + loss:>7}    {k9_lost:>12}{mark}")

    print("\n  ⚠ «K9 погашено» — сколько приёмочных вопросов порог отправил бы в отказ.")
    print("    Эта ось не двигалась ни за один из восьми раундов ревью; сдвиг здесь —")
    print("    цена, которую надо назвать вслух, а не побочный эффект.")

    if args.show is not None:
        thr = args.show
        print(f"\n  ПОИМЁННО на пороге {thr}:")
        for r in rows:
            answered = r["routed"] and r["score"] >= thr
            if r["want"] != answered:
                kind = "ПОТЕРЯ" if r["want"] else "УТЕЧКА"
                print(f"    [{kind}] {r['score']:.4f} routed={int(r['routed'])} "
                      f"({r['pop']}) {r['query'][:64]}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
