"""Замер маршрута второго ключа на НЕЗАВИСИМОЙ популяции (`scripts/eval_route_st1_population.json`).

ЗАЧЕМ ОТДЕЛЬНЫЙ ИНСТРУМЕНТ. `eval_routing.py` считает маршрут по всем наборам репозитория и
отвечает на вопрос «что изменилось у СОСЕДЕЙ». Здесь другой вопрос: «работает ли правило на самом
классе». Для него нужна популяция, построенная НЕ в форме правила, — иначе замер меряет правило.

⚠⚠⚠ ПОВОД — РАУНД 8 РЕВЬЮ PR #137. Механизм раунда 7 («ствол + закрытый список дисквалификаторов»)
был выбран замером трёх состояний на наборе `eval_offdomain_tnved.json`, у которого ВСЕ 12 кейсов
ставят дисквалификатор ВПЛОТНУЮ за стволом. Правило сверяет ровно следующее слово — то есть набор
был построен в форме кандидата и не умел его опровергнуть. Раунд 8 показал: стоит вставить одно
прилагательное, и 14 из 16 реалистичных таможенных вопросов текут по-прежнему, а «утечка 0/12»
описывала двенадцать строк, а не класс.

ЧТО СЧИТАЕМ. Две ошибки, обе обязательны:
    leak — вопрос вне сферы ушёл ПРОЦЕДУРНОЙ веткой (дефект КОРРЕКТНОСТИ: ответ синтезируется
           из Правил СНГ и Приказа №14, снаружи неотличим от верного);
    loss — вопрос по СТ-1 на процедурную ветку НЕ попал (дефект ПОЛЕЗНОСТИ: заявитель получает
           требования локализации по ОКПД2 вместо условий происхождения).
Набор только из утечек красит любое сужающее правило, набор только из целевых — любое
расширяющее. Считать надо обе стороны и печатать разбивку по `gap`: именно эта ось прятала HIGH-3.

⚠ ПРЕДОХРАНИТЕЛЬ. При сравнении с базой (`--baseline`) скрипт ОСТАНАВЛИВАЕТСЯ, если ни один кейс
не сменил ответ: «ноль расхождений» тогда означает не «правка безопасна», а «популяция не
различает состояния» — та же ошибка, что дала «220 вопросов побайтно те же» при двух живых HIGH.

ЗАПУСК:
    .venv/Scripts/python.exe scripts/eval_route_population.py
    ... --json out.json              машинно-читаемо, для сравнения состояний
    ... --baseline before.json       дельта к сохранённому состоянию + предохранитель
    ... --show leak                  печать поимённо: leak | loss | all | none
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.console import enable_utf8  # noqa: E402

enable_utf8()  # #107: скрипт печатает значки вне cp1251

from app.rag import procedural, topics  # noqa: E402

POPULATION = ROOT / "scripts" / "eval_route_st1_population.json"


def _revision() -> str:
    """Ревизия рабочего дерева — чтобы сохранённый замер нельзя было спутать с другим состоянием."""
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True, timeout=10)
        rev = out.stdout.strip() or "?"
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                               capture_output=True, text=True, timeout=10).stdout.strip()
        return rev + ("+грязное дерево" if dirty else "")
    except Exception:
        return "?"


def measure() -> dict:
    """Прогон популяции через ГОРЯЧИЙ путь маршрута — `is_procedural`, как его зовёт пайплайн."""
    data = json.loads(POPULATION.read_text(encoding="utf-8"))
    rows = []
    for case in data["cases"]:
        q = case["query"]
        # ⚠ Тот же вызов, что в рантайме (`pipeline.answer` → `procedural.is_procedural`).
        # Тест, зовущий компонент НАПРЯМУЮ, не проверяет путь до него — урок PR #137.
        routed = bool(procedural.is_procedural(q, has_code=False))
        rows.append({
            "id": case["id"],
            "query": q,
            "expect": case["expect"],
            "axis": case["axis"],
            "gap": case["gap"],
            "note": case.get("note", ""),
            "routed": routed,
            "topic": topics.classify(q),
            # leak: вне сферы, а уехал процедурно. loss: целевой, а не уехал.
            "leak": case["expect"] == "offdomain" and routed,
            "loss": case["expect"] == "st1" and not routed,
        })
    return {"revision": _revision(), "rows": rows}


def _summary(rows: list[dict]) -> dict:
    st1 = [r for r in rows if r["expect"] == "st1"]
    off = [r for r in rows if r["expect"] == "offdomain"]
    return {
        "st1_total": len(st1), "loss": sum(r["loss"] for r in st1),
        "off_total": len(off), "leak": sum(r["leak"] for r in off),
    }


def _print_report(res: dict, show: str) -> None:
    rows = res["rows"]
    s = _summary(rows)
    print(f"\nПОПУЛЯЦИЯ МАРШРУТА ВТОРОГО КЛЮЧА — ревизия {res['revision']}")
    print("=" * 78)
    print(f"  СТ-1 (обязаны уйти процедурно):   {s['st1_total'] - s['loss']:>3} / {s['st1_total']}"
          f"   потеряно: {s['loss']}")
    print(f"  Вне сферы (обязаны НЕ уйти):      {s['off_total'] - s['leak']:>3} / {s['off_total']}"
          f"   утечек:   {s['leak']}")
    print(f"  ИТОГО ошибок: {s['loss'] + s['leak']} из {len(rows)}")

    print("\n  По осям:")
    by_axis: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        by_axis[r["axis"]]["n"] += 1
        by_axis[r["axis"]]["err"] += int(r["leak"] or r["loss"])
    for axis in sorted(by_axis, key=lambda a: -by_axis[a]["err"]):
        c = by_axis[axis]
        flag = "  ← " + ("ЧИСТО" if not c["err"] else f"ОШИБОК {c['err']}")
        print(f"    {axis:<16} {c['n']:>3} вопросов{flag}")

    # ⚠ Разбивка по РАССТОЯНИЮ — ось, на которой прежний набор был однороден и потому слеп.
    print("\n  По расстоянию между стволом и определяющим словом (`gap`):")
    by_gap: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        by_gap[r["gap"]]["n"] += 1
        by_gap[r["gap"]]["err"] += int(r["leak"] or r["loss"])
    for gap in sorted(by_gap, key=lambda g: -by_gap[g]["err"]):
        c = by_gap[gap]
        print(f"    {gap:<10} {c['n']:>3} вопросов   ошибок {c['err']}")

    if show in ("leak", "all"):
        bad = [r for r in rows if r["leak"]]
        if bad:
            print(f"\n  УТЕЧКИ ({len(bad)}) — вне сферы, ушли процедурной веткой:")
            for r in bad:
                print(f"    #{r['id']:<3} [{r['gap']:<9}] {r['query']}")
                print(f"          тема={r['topic']}  · {r['note']}")
    if show in ("loss", "all"):
        bad = [r for r in rows if r["loss"]]
        if bad:
            print(f"\n  ПОТЕРИ ({len(bad)}) — СТ-1, не дошли до процедурной ветки:")
            for r in bad:
                print(f"    #{r['id']:<3} [{r['gap']:<9}] {r['query']}")
                print(f"          · {r['note']}")
    print()


def _compare(res: dict, baseline_path: Path, show: str) -> int:
    base = json.loads(baseline_path.read_text(encoding="utf-8"))
    prev = {r["id"]: r for r in base["rows"]}
    changed = [r for r in res["rows"]
               if r["id"] in prev and prev[r["id"]]["routed"] != r["routed"]]

    print(f"\nСРАВНЕНИЕ С БАЗОЙ  {base.get('revision', '?')}  →  {res['revision']}")
    print("=" * 78)

    # ⚠⚠ ПРЕДОХРАНИТЕЛЬ НА ПУТИ ДЕЙСТВИЯ. «Ничего не изменилось» на популяции, которая обязана
    # различать сравниваемые состояния, означает, что мерили не то: либо база снята на том же
    # коде, либо популяция к правке нечувствительна. Молча напечатать «0 расхождений» здесь —
    # ровно тот отчёт («220 вопросов побайтно те же»), под которым уехали две живые HIGH.
    if not changed:
        print("  ⚠⚠ НИ ОДИН КЕЙС НЕ СМЕНИЛ МАРШРУТ.")
        print("  Это НЕ вывод «правка безопасна». Это отказ измерять: популяция не различает")
        print("  сравниваемые состояния. Проверьте, что база снята на ДРУГОМ коде.")
        return 2

    b, n = _summary(base["rows"]), _summary(res["rows"])
    print(f"  утечек:   {b['leak']:>3} → {n['leak']:<3}   ({n['leak'] - b['leak']:+d})")
    print(f"  потеряно: {b['loss']:>3} → {n['loss']:<3}   ({n['loss'] - b['loss']:+d})")
    print(f"  сменили маршрут: {len(changed)}")
    if show != "none":
        for r in changed:
            was, now = prev[r["id"]]["routed"], r["routed"]
            verdict = "ОК" if not (r["leak"] or r["loss"]) else "ОШИБКА"
            print(f"    #{r['id']:<3} {'товарн→процед' if now else 'процед→товарн'}  "
                  f"[{verdict}] {r['query']}")
    print()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", type=Path, help="сохранить результат для сравнения состояний")
    ap.add_argument("--baseline", type=Path, help="сравнить с сохранённым состоянием")
    ap.add_argument("--show", choices=["leak", "loss", "all", "none"], default="all")
    args = ap.parse_args()

    res = measure()
    _print_report(res, args.show)
    if args.json:
        args.json.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  сохранено: {args.json}")
    if args.baseline:
        return _compare(res, args.baseline, args.show)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
