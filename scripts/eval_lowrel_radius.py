# -*- coding: utf-8 -*-
"""Радиус добора процедурной ветки (`P3-1` #131): как часто товарная выдача низкорелевантна.

ЗАЧЕМ. `P3` #121 снизила пропуски гейта `is_procedural`, но остаток вынесла сознательно:
утвердительные формулировки без глагола и без темы регулярками не берутся. Структурный ответ,
названный при закрытии `P3`, — **добор процедурной ветки при НИЗКОЙ релевантности товарной
выдачи**: если товарный поиск не дал уверенного совпадения, попробовать корпус Правил, прежде чем
отвечать «не нашёл». Задача #131 требует СНАЧАЛА узнать радиус такой правки, а не сделать её:
она меняет поведение на ВСЕХ вопросах со слабой товарной выдачей, а не на тех одиннадцати, ради
которых пишется.

ЧТО СЧИТАЕМ. Для каждого вопроса всех приёмочных наборов, ушедшего ТОВАРНОЙ веткой:
  * поднялся ли флаг `low_relevance` — тот самый, на который добор и предлагается вешать;
  * сходство top-1 по корпусу норм (`pp719_rules`) — разделяет ли порог классы вопросов.

⚠⚠ ПОПУЛЯЦИЯ БЕРЁТСЯ ИЗ `eval_routing.collect()`, А НЕ ЗАВОДИТСЯ СВОЯ. Две таблицы про одно
разъезжаются, и побеждает та, которую правят; у свипа маршрута уже есть разметка классов, включая
поимённые исключения (`EXPECTED_DOCUMENTS_ROUTE`, товарные контроли внутри процедурного набора,
кейсы СТ-1 в товарном наборе). Свой список вопросов означал бы свою — и другую — разметку.

⚠⚠ ФЛАГ СПРАШИВАЕТСЯ У КОДА, А НЕ ПОВТОРЯЕТСЯ В СКРИПТЕ. Зовём `pipeline._plan_answer`, то есть
ровно тот путь, который работает в рантайме, и читаем `plan.low_relevance`. Оракул, повторяющий
модель кода, ловит опечатку, но не ошибку модели (урок 30.08) — а здесь вся ценность замера
именно в том, что решение о низкой релевантности принимает продукт.

⚠ РЕРАНКЕР ВЫКЛЮЧАЕТСЯ, И ЭТО НЕ МЕНЯЕТ ИЗМЕРЯЕМОГО. Он единственный платный шаг в `_plan_answer`.
Его контракт — «никогда не роняет и не теряет выдачу (только переставляет)» (`reranker.py`), а
`low_relevance` зависит от НАЛИЧИЯ совпадения по коду в выдаче и от `dense_top1`, то есть от
множества, а не от порядка. Плюс он и так зовётся только когда совпадения по коду НЕТ. Инвариант
проверяется ключом `--check-rerank` на выборке: с реранкером и без него флаг обязан совпасть.

⚠ ТРИ ИСХОДА, А НЕ ДВА. Если коллекция норм или товарная пуста/недоступна, скрипт останавливается
и чисел НЕ печатает: «ноль» инструмента, не видящего популяцию, означает «не считаю».

ЗАПУСК:
    .venv/Scripts/python.exe scripts/eval_lowrel_radius.py
    ... --json out.json          машинно-читаемо (для diff «до»/«после» правки)
    ... --check-rerank 12        проверить инвариант реранкера на N вопросах (ПЛАТНО)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from app.core.console import enable_utf8  # noqa: E402

enable_utf8()  # #107: скрипт печатает значки вне cp1251

from app.core.config import settings  # noqa: E402
from app.rag import pipeline  # noqa: E402
from app.rag.retriever import _client, dense_top1  # noqa: E402

import eval_routing  # noqa: E402

RULES = settings.QDRANT_RULES_COLLECTION
PRODUCT = settings.QDRANT_COLLECTION


def _require_corpora() -> None:
    """Предохранитель «не измерено»: без обеих коллекций числа этого скрипта — фикция."""
    try:
        client = _client()
        for coll in (PRODUCT, RULES):
            if not client.collection_exists(coll):
                raise RuntimeError(f"коллекции «{coll}» нет")
            n = client.get_collection(coll).points_count
            if not n:
                raise RuntimeError(f"коллекция «{coll}» пуста")
    except Exception as exc:  # noqa: BLE001 — сообщаем и останавливаемся, а не печатаем нули
        print("!! НЕ ИЗМЕРЕНО: корпус недоступен —", exc)
        print("   Замер требует ЖИВОГО Qdrant: и товарной коллекции, и корпуса норм.")
        raise SystemExit(3)


def measure(rows: list[dict]) -> list[dict]:
    """Для каждого ТОВАРНОГО маршрута — флаг низкой релевантности и сходство по корпусу норм."""
    out: list[dict] = []
    for i, r in enumerate(rows, 1):
        if r["procedural"]:
            continue  # уже на процедурной ветке — добор к нему не относится
        q = r["query"]
        plan = pipeline._plan_answer(q)
        out.append({
            **r,
            "low_relevance": bool(plan.low_relevance),
            "rules_top1": round(dense_top1(q, collection=RULES), 4),
            "hits": len(plan.hits),
            "cases": len(plan.cases),
        })
        if i % 25 == 0:
            print(f"   … {i}/{len(rows)}", flush=True)
    return out


def _band(vals: list[float]) -> str:
    return f"{min(vals):.4f}–{max(vals):.4f}" if vals else "—"


def report(rows: list[dict]) -> None:
    classes = ("товарный", "документный", "процедурный", "вне сферы")

    print("\nСКОЛЬКО ТОВАРНЫХ МАРШРУТОВ ПОЛУЧАЮТ ФЛАГ НИЗКОЙ РЕЛЕВАНТНОСТИ")
    print("  (это и есть радиус добора: вопросы, на которых он СРАБОТАЛ БЫ)\n")
    print(f"  {'класс':<14} {'товарным':>9} {'low_rel':>8} {'доля':>7}")
    for cls in classes:
        sub = [r for r in rows if r["class"] == cls]
        if not sub:
            continue
        low = [r for r in sub if r["low_relevance"]]
        share = f"{100 * len(low) / len(sub):.0f} %" if sub else "—"
        print(f"  {cls:<14} {len(sub):>9} {len(low):>8} {share:>7}")

    print("\nПОЛОСЫ СХОДСТВА ПО КОРПУСУ НОРМ СРЕДИ ТЕХ, ГДЕ ФЛАГ ПОДНЯТ")
    print("  (разделяет ли порог «кого пускать в добор» — целевых от посторонних)\n")
    for cls in classes:
        sub = [r["rules_top1"] for r in rows if r["class"] == cls and r["low_relevance"]]
        print(f"  {cls:<14} n={len(sub):<4} полоса {_band(sub)}")

    # ⚠ ГЛАВНОЕ ЧИСЛО ЗАМЕРА: пропуски, которые добор МОГ БЫ поймать. Вопрос процедурного класса,
    # ушедший товарной веткой С флагом, — единственный класс, ради которого правка и заводится.
    gain = [r for r in rows if r["class"] == "процедурный" and r["low_relevance"]]
    blind = [r for r in rows if r["class"] == "процедурный" and not r["low_relevance"]]
    risk = [r for r in rows if r["class"] == "вне сферы" and r["low_relevance"]]
    print("\nЧТО ЭТО ЗНАЧИТ ДЛЯ ПРАВКИ")
    print(f"  пропусков, которые добор УВИДЕЛ БЫ (процедурный + флаг): {len(gain)}")
    print(f"  пропусков, к которым он СЛЕП (процедурный, флага нет):   {len(blind)}")
    print(f"  вне-сферы, попадающих под добор (флаг поднят):           {len(risk)}")

    for title, sub in (("ПРОПУСКИ, ВИДИМЫЕ ДОБОРУ", gain),
                       ("ПРОПУСКИ, НЕВИДИМЫЕ ДОБОРУ", blind),
                       ("ВНЕ СФЕРЫ ПОД ДОБОРОМ", risk)):
        if not sub:
            continue
        print(f"\n{title}: {len(sub)}")
        for r in sorted(sub, key=lambda x: -x["rules_top1"]):
            print(f"  {r['set']}#{r['id']:<4} нормы={r['rules_top1']:.4f}  {r['query'][:62]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", type=Path, help="сохранить строки замера машинно-читаемо")
    ap.add_argument("--check-rerank", type=int, default=0, metavar="N",
                    help="проверить инвариант реранкера на N вопросах (ПЛАТНО, зовёт DeepSeek)")
    args = ap.parse_args()

    _require_corpora()

    rows = eval_routing.collect()
    print(f"вопросов в своде: {len(rows)}; из них товарным маршрутом: "
          f"{sum(1 for r in rows if not r['procedural'])}")

    # ⚠ Реранкер выключаем на время замера — единственный платный шаг `_plan_answer`.
    # Обоснование инвариантности — в шапке; проверка — ключом --check-rerank.
    saved = settings.RERANK_ENABLED
    settings.RERANK_ENABLED = False
    try:
        measured = measure(rows)
    finally:
        settings.RERANK_ENABLED = saved

    report(measured)

    if args.check_rerank:
        print(f"\nИНВАРИАНТ РЕРАНКЕРА (ПЛАТНО): сверяю флаг на {args.check_rerank} вопросах")
        settings.RERANK_ENABLED = True
        bad = 0
        try:
            for r in measured[: args.check_rerank]:
                again = pipeline._plan_answer(r["query"]).low_relevance
                if bool(again) != r["low_relevance"]:
                    bad += 1
                    print(f"  !! РАСХОЖДЕНИЕ {r['set']}#{r['id']}: без реранка "
                          f"{r['low_relevance']}, с реранком {again} — {r['query'][:50]}")
        finally:
            settings.RERANK_ENABLED = saved
        print(f"  расхождений: {bad} из {min(args.check_rerank, len(measured))}"
              + ("  → выключение реранкера замер НЕ искажает" if not bad
                 else "  → ⚠ ОБОСНОВАНИЕ В ШАПКЕ НЕВЕРНО, чинить замер"))
        if bad:
            return 1

    if args.json:
        args.json.write_text(json.dumps(measured, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        print(f"\nсохранено: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
