r"""Свип гейта смыслов «заключения»: документ или действие. `K15-3` #132.

⚠⚠ ЗАЧЕМ ОТДЕЛЬНЫЙ ИНСТРУМЕНТ. Радиус правок этого гейта ЧЕТЫРЕ раунда ревью подряд показывал
НОЛЬ по 213 вопросам приёмочных наборов — и это означало не «правка безопасна», а «класс не
покрыт». Каждый раз дефект приходилось показывать конструированным вопросом, то есть замер был
слеп ровно там, где шла работа. Здесь класс покрыт явно.

⚠ Стоит НОЛЬ: гейт детерминирован (регулярки + роутер тем), ни DeepSeek, ни Qdrant не нужны.

ЗАПУСК:
    .venv\Scripts\python.exe scripts\eval_conclusion_gate.py
    .venv\Scripts\python.exe scripts\eval_conclusion_gate.py --show-fail
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.console import enable_utf8  # noqa: E402
from app.rag import documents_ref  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval_conclusion_gate.json")


def evaluate() -> list[dict]:
    cases = json.load(open(FIXTURE, encoding="utf-8"))["cases"]
    rows = []
    for c in cases:
        q = c["query"]
        mentions = documents_ref.mentions_conclusion(q)
        want = c["sense"] == "document"
        rows.append({**c, "mentions": mentions, "ok": mentions == want,
                     "narrow": documents_ref.asks_about_conclusion(q),
                     "explained": documents_ref.NONEXISTENT_EXPLANATION
                                  in documents_ref.documents_context_block(q)})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show-fail", action="store_true", help="печатать только расхождения")
    args = ap.parse_args()
    enable_utf8()
    rows = evaluate()

    # ⚠⚠ ПОЛОЖИТЕЛЬНЫЙ КОНТРОЛЬ. «Ноль расхождений» и «инструмент ничего не смотрел» снаружи
    # неразличимы — правило проекта. Набор обязан содержать оба смысла и оба быть распознаны
    # хоть где-то, иначе печатать числа нельзя.
    assert len(rows) >= 20, f"набор подозрительно мал: {len(rows)}"
    assert any(r["mentions"] for r in rows), "гейт не признал документом НИ ОДИН вопрос — он слеп"
    assert any(not r["mentions"] for r in rows), "гейт признал документом ВСЁ — он не различает"

    by = {}
    for r in rows:
        by.setdefault(r["sense"], []).append(r)
    print()
    print(f"СВИП ГЕЙТА СМЫСЛОВ «ЗАКЛЮЧЕНИЯ» — {len(rows)} вопросов, офлайн")
    print()
    print(f"{'смысл':10} {'верно':>8} {'всего':>6}   доля")
    print("-" * 40)
    for sense in ("document", "act", "none"):
        g = by.get(sense, [])
        ok = sum(1 for r in g if r["ok"])
        share = f"{ok / len(g):.2f}" if g else "—"
        print(f"{sense:10} {ok:>8} {len(g):>6}   {share}")
    print("-" * 40)
    # ⚠⚠ ИЗВЕСТНЫЕ ПРЕДЕЛЫ СЧИТАЮТСЯ ОТДЕЛЬНО И НЕ КРАСЯТ СВИП. Скрывать предел механизма
    # (выкинуть форму из набора) хуже, чем показывать: набор затем и нужен, чтобы предел был
    # виден числом. Регрессией считается только НОВОЕ расхождение.
    bad = [r for r in rows if not r["ok"] and not r.get("known_limit")]
    known = [r for r in rows if not r["ok"] and r.get("known_limit")]
    # ⚠ Обратный контроль: известный предел, который ПЕРЕСТАЛ быть пределом, тоже сигнал —
    # иначе запись о нём тихо устареет и будет врать про механизм.
    fixed = [r for r in rows if r["ok"] and r.get("known_limit")]
    print(f"{'ИТОГО':10} {len(rows) - len(bad) - len(known):>8} {len(rows):>6}   "
          f"{1 - (len(bad) + len(known)) / len(rows):.2f}")
    print()
    if known:
        print(f"ИЗВЕСТНЫЕ ПРЕДЕЛЫ ({len(known)}) — записаны в наборе, регрессией НЕ считаются:")
        for r in known:
            print(f"  #{r['id']:<3} {r['query'][:74]}")
            print(f"       {r.get('why', '')[:96]}")
        print()
    if fixed:
        print(f"⚠ ПРЕДЕЛ БОЛЬШЕ НЕ ПРЕДЕЛ ({len(fixed)}) — снять пометку known_limit в наборе:")
        for r in fixed:
            print(f"  #{r['id']:<3} {r['query'][:74]}")
        print()
    if bad:
        print("РАСХОЖДЕНИЯ (ждали / получили):")
        for r in bad:
            print(f"  #{r['id']:<3} ждали {r['sense']:9} получили "
                  f"{'document' if r['mentions'] else 'act/none':9} [{r['source']}]")
            print(f"       {r['query'][:88]}")
    elif not args.show_fail:
        print("новых расхождений нет")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
