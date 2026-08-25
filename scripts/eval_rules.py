"""K9: замер процедурной ветки — АТРИБУЦИЯ ИСТОЧНИКА.

ЗАЧЕМ. Процедурный корпус — самая быстрорастущая часть базы (242 → 244 пункта, впереди волна D
с ещё пятью документами), и до сих пор он не был покрыт НИ ОДНОЙ метрикой. Добавление документа
могло тихо ухудшить ответы по соседнему, и заметить это было нечем.

Насколько это не теория: 12.08.2026 замером обнаружилось, что Приказ ТПП №52 (172 пункта из 244,
71 % корпуса) забирал 24 места из 30 в окне контекста, а на вопросе «сроки рассмотрения
заявления» — все 6, вытесняя Правила ведения реестра, которые эти сроки и устанавливают.
Дефект жил с момента появления корпуса и не был виден ни одной существующей метрике.

ЧТО МЕРИМ — не текст ответа, а ИСТОЧНИК, из которого он собран:
  * ТОЧНОСТЬ ТЕМЫ    — `rules_topic` определил тот документ, которому вопрос адресован;
  * АТРИБУЦИЯ@1      — первый пункт в окне принадлежит ожидаемому документу;
  * ПРЕДСТАВЛЕННОСТЬ — ожидаемый документ вообще есть в окне (это чинила квота K10);
  * ДОЛЯ ОКНА        — сколько мест из `limit` занял ожидаемый документ.

Эталон — `doc_type`, а НЕ текст пункта: формулировка меняется от редакции к редакции, а зона
ответственности документа — нет. Замер детерминированный и бесплатный: дёргает `search_rules`
напрямую, без DeepSeek.

ЗАПУСК (нужен поднятый Qdrant с коллекцией pp719_rules):
    PYTHONUTF8=1 .venv/Scripts/python.exe scripts/eval_rules.py
    PYTHONUTF8=1 .venv/Scripts/python.exe scripts/eval_rules.py --report docs/eval_rules_report.md
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from app.core.console import enable_utf8  # noqa: E402  (только после sys.path)

enable_utf8()  # #107: скрипт печатает значки вне cp1251 — падал бы в момент печати
from app.rag import retriever, topics  # noqa: E402

GOLDEN = ROOT / "scripts" / "eval_golden_rules.json"
NAMES = {"decree_body": "тело ПП №719", "rules_registry": "Правила реестра",
         "tpp_order_52": "Приказ ТПП №52"}


def evaluate(limit: int) -> list[dict]:
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    rows = []
    for c in data["cases"]:
        # K12: мерим ПРОДАКШЕН-путь. Пайплайн передаёт в окно предпочтение документов по ТЕМЕ
        # вопроса (`topics.classify`), а не только по лексике документа (`rules_topic`) — замер без
        # этого проверял бы уже не тот отбор, что работает в ответе.
        hits = retriever.search_rules(c["query"], limit=limit,
                                      primary_docs=topics.doc_types(topics.classify(c["query"])))
        docs = [h.get("doc_type") for h in hits]
        # P2: атрибуция по ДОКУМЕНТУ слепа к тому, какие пункты внутри него попали в окно.
        # На кейсе 17 она давала ✓, хотя раздела 4 (где и лежит состав документов) в окне не было
        # вовсе, и ответ честно писал «перечень в контексте не представлен».
        want = c.get("expect_points") or []
        got_points = [str(h.get("point") or "") for h, d in zip(hits, docs) if d == c["expect"]]
        rows.append({
            "id": c["id"], "query": c["query"], "expect": c["expect"], "why": c.get("why", ""),
            "topic": retriever.rules_topic(c["query"]),
            "top1": docs[0] if docs else None,
            "present": c["expect"] in docs,
            "share": sum(1 for d in docs if d == c["expect"]),
            "n": len(docs),
            "want_points": want,
            "point_ok": any(p.startswith(w) for p in got_points for w in want) if want else None,
            "got_points": got_points,
        })
    return rows


def report(rows: list[dict], limit: int) -> list[str]:
    n = len(rows)
    topic_ok = sum(1 for r in rows if r["topic"] == r["expect"])
    attr_ok = sum(1 for r in rows if r["top1"] == r["expect"])
    present = sum(1 for r in rows if r["present"])
    L = ["=" * 78,
         f"EVAL ПРОЦЕДУРНОЙ ВЕТКИ — {n} кейсов, окно top-{limit}",
         "=" * 78, "",
         "Эталон — ДОКУМЕНТ-ИСТОЧНИК (doc_type), а не текст пункта.", "",
         f"  ТОЧНОСТЬ ТЕМЫ    = {topic_ok}/{n} = {topic_ok / n:.2f}   (rules_topic угадал документ)",
         f"  АТРИБУЦИЯ@1      = {attr_ok}/{n} = {attr_ok / n:.2f}   ← ГЛАВНАЯ: чей пункт первый в окне",
         f"  ПРЕДСТАВЛЕННОСТЬ = {present}/{n} = {present / n:.2f}   (документ вообще попал в окно)"]

    scoped = [r for r in rows if r["point_ok"] is not None]
    if scoped:
        pt_ok = sum(1 for r in scoped if r["point_ok"])
        L.append(f"  НУЖНЫЙ ПУНКТ     = {pt_ok}/{len(scoped)} = {pt_ok / len(scoped):.2f}   "
                 f"(в окне есть пункт из ожидаемого раздела — P2)")
    L.append("")

    by_doc = Counter(r["expect"] for r in rows)
    L.append("По документам:")
    for dt, total in by_doc.most_common():
        sub = [r for r in rows if r["expect"] == dt]
        a = sum(1 for r in sub if r["top1"] == dt)
        p = sum(1 for r in sub if r["present"])
        share = sum(r["share"] for r in sub) / (len(sub) * limit)
        L.append(f"  {NAMES.get(dt, dt):18} кейсов {total:>2}  атрибуция@1 {a}/{total}  "
                 f"в окне {p}/{total}  доля окна {share:.0%}")
    L.append("")

    miss_pt = [r for r in rows if r["point_ok"] is False]
    if miss_pt:
        L.append(f"ПРОМАХИ ПО ПУНКТУ ({len(miss_pt)}):")
        for r in miss_pt:
            L.append(f"  #{r['id']:>2} ждали пункт из {r['want_points']}, в окне: "
                     f"{r['got_points'] or '— (документа нет в окне)'}  ← {r['query'][:46]}")
        L.append("")

    bad = [r for r in rows if r["top1"] != r["expect"]]
    if bad:
        L.append(f"ПРОМАХИ АТРИБУЦИИ ({len(bad)}):")
        for r in bad:
            L.append(f"  #{r['id']:>2} ждём [{NAMES.get(r['expect'], r['expect'])}], "
                     f"первым [{NAMES.get(r['top1'], r['top1'])}]  ← {r['query'][:52]}")
            L.append(f"      в окне: {'да' if r['present'] else 'НЕТ'}, мест {r['share']}/{limit}"
                     f" · причина кейса: {r['why']}")
        L.append("")

    L.append(f"{'id':>3} {'тема':>16} {'ждём':>16} {'первый':>16} {'мест':>5}  запрос")
    L.append("-" * 78)
    for r in rows:
        mark = "✓" if r["top1"] == r["expect"] else "✗"
        L.append(f"{r['id']:>3} {NAMES.get(r['topic'], '—'):>16} {NAMES.get(r['expect'], ''):>16} "
                 f"{NAMES.get(r['top1'], '—'):>16} {r['share']:>3}/{limit} {mark} {r['query'][:30]}")
    return L


def main() -> None:
    ap = argparse.ArgumentParser(description="Замер атрибуции источника в процедурной ветке (K9)")
    ap.add_argument("--limit", type=int, default=6, help="окно контекста (как RULES_TOP_K)")
    ap.add_argument("--report", type=str, default="", help="путь для markdown-отчёта")
    args = ap.parse_args()

    try:
        retriever._client().get_collections()
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Qdrant недоступен ({e}). Подними Docker Desktop + контейнер Qdrant.")

    rows = evaluate(args.limit)
    lines = report(rows, args.limit)
    print("\n".join(lines))
    if args.report:
        p = ROOT / args.report
        p.write_text("# Eval процедурной ветки — атрибуция источника (K9)\n\n```\n"
                     + "\n".join(lines) + "\n```\n", encoding="utf-8")
        print(f"\nОтчёт сохранён: {p}")


if __name__ == "__main__":
    main()
