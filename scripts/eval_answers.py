"""Eval-харнес качества ОТВЕТА навигатора (этап 1.0.4 плана 1.0).

В отличие от `eval_retrieval.py` (мерит ПОИСК — нашёлся ли нужный раздел), этот харнес
мерит сам СГЕНЕРИРОВАННЫЙ ОТВЕТ: вызывает полный пайплайн (`pipeline.answer`, т.е. реальный
DeepSeek) на golden set и проверяет ответ ДЕТЕРМИНИРОВАННО, без LLM-судьи — поэтому замер
воспроизводим, бесплатен сверх вызовов DeepSeek и не требует эталонных ответов эксперта
(их пока нет; когда появятся в knowledge_base/cases — добавим слой сравнения).

Что меряем (главный принцип проекта — анти-галлюцинации):
  • FAITHFULNESS (заземление чисел) — КЛЮЧЕВАЯ метрика. Каждое число баллов/порога/процента
    в ОТВЕТЕ обязано присутствовать в КОНТЕКСТЕ, который видела модель (правило 2 промпта).
    Число баллов/%, которого нет в контексте — выдуманное. Считаем долю ответов без выдумок.
  • АТРИБУЦИЯ — упомянут ли в ответе ожидаемый раздел (по русскому наименованию/римской цифре).
  • ДИСКЛЕЙМЕР ЗДЕСЬ НЕ МЕРЯЕТСЯ (снято 12.08.2026). Пометка эксперта осознанно убрана из тела
    ответа (R3, правило 6 промпта: внутренний инструмент, повтор в каждой реплике — шум) и живёт
    в постоянной строке UI и в КАЖДОЙ выгрузке. Функции `_ensure_disclaimer`, на которую метрика
    опиралась, больше нет. Требование не исчезло — оно проверяется там, где теперь применяется:
    `tests/test_export.py` (все форматы выгрузки, 6 проверок). Метрика, которая после R3 всегда
    показывала бы 0/42, вводила бы в заблуждение при чтении отчёта.
  • OUT-OF-SCOPE — на запросах вне 719 ответ должен ОТКАЗАТЬ (а не подгонять «ближайший мусор»).
  • CJK — нет ли утечки иероглифов DeepSeek (известный P2-баг).

ВНИМАНИЕ: вызывает DeepSeek (по кейсу на запрос). ~46 кейсов ≈ ¥0.4-0.6. Нужен поднятый Qdrant
(коллекция pp719) + DEEPSEEK_API_KEY в .env.

Запуск:
  .venv\\Scripts\\python scripts\\eval_answers.py                      # все кейсы
  .venv\\Scripts\\python scripts\\eval_answers.py --cases 5            # первые 5 (дёшево)
  .venv\\Scripts\\python scripts\\eval_answers.py --report docs/eval_answers_report.md
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from app.core.console import enable_utf8  # noqa: E402  (только после sys.path)

enable_utf8()  # #107: скрипт печатает значки вне cp1251 — падал бы в момент печати
import json  # noqa: E402

from app.core.config import settings  # noqa: E402
# Логику faithfulness берём ИЗ пайплайна — рантайм-постпроверка и этот замер меряют одно и то же.
from app.rag.pipeline import answer, claim_numbers, unverified_numbers  # noqa: E402
from app.rag.retriever import _client  # noqa: E402
from eval_documents import (  # noqa: E402  (общий оракул документных кейсов, EV21 #119)
    check_documents_reference,
    documents_row,
)

GOLDEN = ROOT / "scripts" / "eval_golden.json"

# Отказ «вне сферы / не найдено» (правила 1, 1б промпта):
DECLINE_RE = re.compile(
    r"вне сфер|не наш|не найден|не относ|уточнит|не подпад|не попад|не предусмотр|не явля",
    re.IGNORECASE,
)
# Иероглифы (утечка DeepSeek): CJK + хирагана/катакана.
CJK_RE = re.compile(r"[぀-ヿ一-鿿]")
# Инлайн-цитата на позицию-источник: [1], [2]… (P1-промпт).
CITE_RE = re.compile(r"\[\d+\]")


def expected_section_title(hits, expected_roman: str) -> str | None:
    for h in hits:
        if h.section_roman == expected_roman and getattr(h, "section_title", ""):
            return h.section_title
    return None


def attributed(text: str, expected_roman: str, hits) -> bool:
    """АТРИБУЦИЯ СТРОГО: раздел НАЗВАН — римской цифрой или полным названием.

    ⚠⚠ ПОЧЕМУ СТРОГО (`EV17` #109). Прежняя редакция засчитывала атрибуцию, если в ответе есть
    ЛЮБОЕ слово названия раздела длиннее 5 букв. Для XXV «Музыкальные инструменты и звуковое
    оборудование» это в том числе «оборудование» — слово из каждого второго товарного ответа.

    Замер 25.08.2026 поймал это на прогоне уровня 2: 41/42 = 0.98 в одном прогоне из трёх при
    1.00 в двух других. Разбор кейса 43 «микрофоны и громкоговорители» показал, что римская
    цифра не печатается НИ РАЗУ за четыре прямых прогона, то есть строгий путь не срабатывал
    никогда — число всё это время держалось на совпадении случайного слова и дрожало вместе с
    формулировкой. Метрика меряла словоупотребление, а не атрибуцию.

    Тот же класс, что `named_requirements_source` (найден 19.08, исправлен 21.08): величина,
    зависящая от выбора модели между двумя правильными формами ответа, — не метрика.

    Лексика раздела осталась ОТДЕЛЬНОЙ величиной — `topically_coherent`, наблюдаемой, без порога.
    """
    if re.search(rf"\bраздел[а-я]*\s+{expected_roman}\b", text, re.IGNORECASE):
        return True
    title = expected_section_title(hits, expected_roman)
    # Полное название раздела — тоже атрибуция: «Музыкальные инструменты и звуковое
    # оборудование» названо целиком, спутать его не с чем.
    return bool(title) and title.strip().lower() in text.lower()


def topically_coherent(text: str, expected_roman: str, hits) -> bool:
    """Есть ли в ответе ЛЕКСИКА ожидаемого раздела. Наблюдаемая величина, ПОРОГА НЕТ.

    Ровно то, что раньше считалось атрибуцией. Само по себе полезно (ответ хотя бы про ту
    предметную область), но атрибуцией не является: слово «оборудование» в ответе не значит,
    что читатель узнал, к какому разделу приложения относится его продукция.

    ⚠ Порог не назначается намеренно: пока не замерено, КАК ЧАСТО ответ вообще называет раздел,
    неизвестно, чего от него требовать. Это и есть открытый вопрос `EV17`.
    """
    title = expected_section_title(hits, expected_roman)
    if not title:
        return False
    return any(w.lower() in text.lower() for w in re.findall(r"[А-Яа-яЁёA-Za-z]{6,}", title))


def evaluate(limit: int, cases_limit: int, kind: str = "all"):
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    cases = data["cases"]
    if kind != "all":
        cases = [c for c in cases if c.get("kind", "product") == kind]
    if cases_limit:
        cases = cases[:cases_limit]
    check_documents_reference()   # положительный контроль ДО первого платного вызова
    rows = []
    for c in cases:
        kind = c.get("kind", "product")
        ans = answer(c["query"], okpd2=c.get("okpd2") or None, limit=limit)
        text = ans.text or ""
        # ⚠ Заземление берём У ОТВЕТА, а не пересобираем: пересборка — второе место, где
        # решается, что ответ видел, и оно расходится молча. 18.08.2026 так и произошло: контекст
        # стал зависеть от кода, здесь код не передавался, и два кейса с кодом отчитались
        # «выдуманными числами», которых рантайм честно держал в промпте.
        ctx = ans.grounding

        # Та же логика, что в рантайм-постпроверке — включая ВОПРОС как законный источник чисел
        # (16.08.2026). Забудь передать `c["query"]` — и замер начнёт считать выдумкой то, что
        # рантайм выдумкой не считает, то есть мерить не продукт, а расхождение с самим собой.
        hallucinated = unverified_numbers(text, ctx, c["query"])

        row = {
            "id": c["id"],
            "kind": kind,
            "in_scope": c["in_scope"],
            "expected": c["expected_section"] or "—",
            "query": c["query"],
            "n_claims": len(claim_numbers(text)),
            "hallucinated": hallucinated,
            "faithful": not hallucinated,
            "guard_flagged": bool(ans.unverified_numbers),  # пометил ли рантайм-guard
            # ⚠ Атрибуция считается только там, где раздел ОЖИДАЕТСЯ. У документного вопроса раздела
            # нет вовсе, и прежняя форма (`if c["in_scope"]`) искала бы «раздел None»: уверенно
            # возвращала бы False и роняла метрику, ничего при этом не измеряя.
            "attributed": (attributed(text, c["expected_section"], ans.hits)
                           if c["in_scope"] and c["expected_section"] else None),
            "coherent": (topically_coherent(text, c["expected_section"], ans.hits)
                         if c["in_scope"] and c["expected_section"] else None),
            "declined": bool(DECLINE_RE.search(text)),
            "cited": bool(CITE_RE.search(text)),  # есть ли инлайн-ссылка [N] на позицию
            "cjk": bool(CJK_RE.search(text)),
            "low_relevance": ans.low_relevance,
            # Наблюдаемая величина: как ТОТ ЖЕ текст прочитал предохранитель продукта. Расхождение
            # с `term_affirmed` — сигнал сам по себе: одно из двух чтений неверно.
            "guard_phantom": list(ans.phantom_documents or []),
        }
        if kind == "documents":
            row.update(documents_row(c, text))
        rows.append(row)
    return rows


def _rate(items, pred) -> tuple[int, int]:
    n = len(items)
    return (sum(1 for x in items if pred(x)), n)


def summarize(rows, limit: int) -> list[str]:
    # ⚠⚠ ТОВАРНЫЕ И ДОКУМЕНТНЫЕ КЕЙСЫ СЧИТАЮТСЯ РАЗДЕЛЬНО, И ЭТО НЕ КОСМЕТИКА.
    # Числа базы сравнения сняты на 42 товарных вопросах. Сложи с ними четыре документных — и
    # знаменатель поменяется, а дельта к базе перестанет читаться: просадка метрики и смена
    # ПОПУЛЯЦИИ дадут одинаковое движение числа. Правило значимости дельты (`EVAL_GUIDE §1.2`)
    # сравнивает величины, снятые на одном наборе; сравнивать разные наборы оно не умеет.
    ins = [r for r in rows if r["in_scope"] and r.get("kind", "product") != "documents"]
    docs = [r for r in rows if r.get("kind") == "documents"]
    out = [r for r in rows if not r["in_scope"]]
    L = []
    L.append("=" * 78)
    L.append(f"EVAL ОТВЕТА — golden set {len(rows)} кейсов ({len(ins)} товарных in-scope, "
             f"{len(docs)} документных, {len(out)} out-of-scope), limit={limit}, "
             f"модель={settings.DEEPSEEK_MODEL}")
    L.append("=" * 78)
    L.append("")

    if ins:
        f_ok, f_n = _rate(ins, lambda r: r["faithful"])
        a_ok, a_n = _rate(ins, lambda r: r["attributed"])
        co_ok, co_n = _rate(ins, lambda r: r["coherent"])
        cit_ok, cit_n = _rate(ins, lambda r: r["cited"])
        c_ok, c_n = _rate(ins, lambda r: not r["cjk"])
        total_claims = sum(r["n_claims"] for r in ins)
        total_halluc = sum(len(r["hallucinated"]) for r in ins)
        halluc_rows = [r for r in ins if r["hallucinated"]]
        g_ok = sum(1 for r in halluc_rows if r["guard_flagged"])
        L.append("Товарные in-scope (качество ответа) — набор, на котором снята база сравнения:")
        L.append(f"  FAITHFULNESS (нет выдуманных чисел) = {f_ok}/{f_n} = {f_ok / f_n:.2f}")
        L.append(f"      чисел баллов/% в ответах: {total_claims}; из них выдумано (нет в контексте): {total_halluc}")
        if halluc_rows:
            L.append(f"      из них помечено рантайм-guard'ом эксперту: {g_ok}/{len(halluc_rows)} ответов "
                     f"(P0-постпроверка — незаземлённое число не уходит к эксперту незамеченным)")
        L.append(f"  АТРИБУЦИЯ раздела (раздел НАЗВАН)   = {a_ok}/{a_n} = {a_ok / a_n:.2f}")
        L.append(f"      строго: римская цифра раздела либо его полное название")
        L.append(f"  _справочно_ лексика раздела в ответе = {co_ok}/{co_n} = {co_ok / co_n:.2f}"
                 f"   [порога нет — EV17 #109]")
        L.append(f"  Инлайн-цитаты [N] на позицию        = {cit_ok}/{cit_n} = {cit_ok / cit_n:.2f}")
        L.append(f"  Без CJK-иероглифов                  = {c_ok}/{c_n} = {c_ok / c_n:.2f}")
        L.append("")

    if docs:
        d_ok, d_n = _rate(docs, lambda r: r["docs_ok"])
        e_ok, e_n = _rate(docs, lambda r: r["explanation_ok"])
        t_ok, t_n = _rate(docs, lambda r: not r["term_affirmed"])
        src_rows = [r for r in docs if r["want_sources"]]
        s_ok = sum(1 for r in src_rows if len(r["named_sources"]) == len(r["want_sources"]))
        df_ok, df_n = _rate(docs, lambda r: r["faithful"])
        disagree = [r for r in docs if bool(r["term_affirmed"]) != bool(r["guard_phantom"])]
        L.append("Документные кейсы (EV21 #119) — кластер жалоб №1, 31 упоминание в отзывах:")
        L.append(f"  Закрытый перечень доехал до ответа  = {d_ok}/{d_n} = {d_ok / d_n:.2f}"
                 f"   (эталон — CONFIRMING_DOCUMENTS, не вывод сервиса)")
        L.append(f"  Разъяснение ПО УСЛОВИЮ (K15)        = {e_ok}/{e_n} = {e_ok / e_n:.2f}"
                 f"   (спросили → объясни; не спрашивали → термина нет вовсе)")
        L.append(f"  Несуществующий документ не утверждён = {t_ok}/{t_n} = {t_ok / t_n:.2f}")
        L.append(f"  Источник назван                     = "
                 + (f"{s_ok}/{len(src_rows)} = {s_ok / len(src_rows):.2f}" if src_rows
                    else "— (кейсов с ожидаемым источником нет)"))
        L.append(f"  FAITHFULNESS на документных         = {df_ok}/{df_n} = {df_ok / df_n:.2f}")
        if disagree:
            L.append("  ⚠ РАСХОЖДЕНИЕ С РАНТАЙМ-ГАРДОМ (одно из двух чтений текста неверно): "
                     + ", ".join(f"#{r['id']}" for r in disagree))
        for r in docs:
            miss = [d for d in r["want_docs"] if d not in r["named_docs"]]
            bits = []
            if miss:
                bits.append("не назван: " + "; ".join(m.split(" ")[0].lower() for m in miss))
            if not r["explanation_ok"] and r["want_explanation"] and not r["term_affirmed"]:
                bits.append("про документ спросили, а разъяснения в ответе нет")
            elif not r["explanation_ok"] and not r["want_explanation"]:
                bits.append(f"термин приехал в ответ, хотя не спрашивали "
                            f"({r['term_mentions']} упом.)")
            if r["term_affirmed"]:
                bits.append("УТВЕРЖДАЕТ несуществующий: " + ", ".join(r["term_affirmed"]))
            miss_src = [s for s in r["want_sources"] if s not in r["named_sources"]]
            if miss_src:
                bits.append("источник не назван: " + ", ".join(miss_src))
            L.append(f"    #{r['id']:>3} {r['query'][:46]:46} — " + ("; ".join(bits) if bits else "✓"))
        L.append("")

    if out:
        dec_ok, dec_n = _rate(out, lambda r: r["declined"])
        L.append("Out-of-scope (продукция вне 719):")
        L.append(f"  Корректный отказ («вне сферы / уточнить») = {dec_ok}/{dec_n} = {dec_ok / dec_n:.2f}")
        L.append("")

    L.append(f"{'id':>3} {'sc':<3} {'ожид':>5} {'faith':>6} {'attr':>5} {'cjk':>4}  запрос")
    L.append("-" * 78)
    for r in rows:
        sc = "IN" if r["in_scope"] else "OUT"
        faith = "✓" if r["faithful"] else f"✗{len(r['hallucinated'])}"
        attr = "—" if r["attributed"] is None else ("✓" if r["attributed"] else "✗")
        cjk = "!" if r["cjk"] else "·"
        if not r["in_scope"]:
            attr = "↩" if r["declined"] else "✗"  # для OUT: отказал ли
        if r.get("kind") == "documents":
            sc = "ДОК"  # раздела у документного вопроса нет — в колонке итог документных проверок
            attr = "✓" if (r["docs_ok"] and r["explanation_ok"] and not r["term_affirmed"]) else "✗"
        L.append(f"{r['id']:>3} {sc:<3} {r['expected']:>5} {faith:>6} {attr:>5} {cjk:>4}  {r['query'][:30]}")
    L.append("")

    bad_faith = [r for r in rows if not r["faithful"]]
    if bad_faith:
        L.append("ВЫДУМАННЫЕ ЧИСЛА (нет в контексте):")
        for r in bad_faith:
            L.append(f"  #{r['id']} ({r['query'][:40]}): {', '.join(r['hallucinated'])}")
    bad_out = [r for r in out if not r["declined"]]
    if bad_out:
        L.append("OUT-OF-SCOPE без отказа: " + ", ".join(f"#{r['id']}" for r in bad_out))
    L.append("")
    L.append("Примечание: faithfulness — детерминированная сверка чисел баллов/% ответа с контекстом")
    L.append("(консервативна: считает выдумкой только число, которого в контексте НЕТ вовсе).")
    return L


def main() -> None:
    ap = argparse.ArgumentParser(description="Eval-харнес КАЧЕСТВА ОТВЕТА навигатора 719 (вызывает DeepSeek)")
    ap.add_argument("--limit", type=int, default=5, help="глубина выдачи (top-k) для пайплайна")
    ap.add_argument("--cases", type=int, default=0, help="обработать только первые N кейсов (дёшево)")
    # ⚠ Срез нужен по двум причинам сразу. ЦЕНА: `--cases N` берёт ПЕРВЫЕ N, а документные кейсы
    # стоят в конце — проверить их можно было только оплатив весь набор. БАЗА: её числа сняты на
    # 42 товарных, и без команды, воспроизводящей эту популяцию, они становятся нечем пересчитать
    # (правило 6 базы сравнения).
    ap.add_argument("--kind", choices=("all", "product", "documents"), default="all",
                    help="какие кейсы гнать: все · только товарные (популяция базы) · документные")
    ap.add_argument("--report", type=str, default="", help="путь для сохранения отчёта (markdown)")
    args = ap.parse_args()

    try:
        _client().get_collections()
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Qdrant недоступен ({e}). Подними Docker Desktop — коллекция pp719 встанет сама.")
    if not (settings.DEEPSEEK_API_KEY or "").strip():
        sys.exit("Нет DEEPSEEK_API_KEY в .env — этот харнес вызывает DeepSeek.")

    rows = evaluate(args.limit, args.cases, args.kind)
    report = summarize(rows, args.limit)
    print("\n".join(report))

    if args.report:
        path = (ROOT / args.report) if not Path(args.report).is_absolute() else Path(args.report)
        path.write_text("# Eval-отчёт КАЧЕСТВА ОТВЕТА (этап 1.0.4)\n\n```\n" + "\n".join(report) + "\n```\n",
                        encoding="utf-8")
        print(f"\nОтчёт сохранён: {path}")


if __name__ == "__main__":
    main()
