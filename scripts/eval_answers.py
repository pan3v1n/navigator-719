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
  • ДИСКЛЕЙМЕР — есть ли обязательная пометка эксперта (гарантируется `_ensure_disclaimer`).
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

import json  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.prompts import EXPERT_DISCLAIMER  # noqa: E402
from app.rag.pipeline import answer, format_cases, format_context  # noqa: E402
from app.rag.retriever import _client  # noqa: E402

GOLDEN = ROOT / "scripts" / "eval_golden.json"

# Число-претензия в ОТВЕТЕ: величина рядом с единицей «балл» или «процент/%». Именно такие
# числа модель обязана брать из контекста (правило 2 промпта); даты/сроки («5 лет», «2018 г.»)
# сюда НЕ попадают — у них другая единица.
_NUM = r"\d+(?:[.,]\d+)?"
BALL_CLAIM_RE = re.compile(rf"({_NUM})\s*балл", re.IGNORECASE)
PCT_CLAIM_RE = re.compile(rf"({_NUM})\s*(?:процент|%)", re.IGNORECASE)
# Отказ «вне сферы / не найдено» (правила 1, 1б промпта):
DECLINE_RE = re.compile(
    r"вне сфер|не наш|не найден|не относ|уточнит|не подпад|не попад|не предусмотр|не явля",
    re.IGNORECASE,
)
# Иероглифы (утечка DeepSeek): CJK + хирагана/катакана.
CJK_RE = re.compile(r"[぀-ヿ一-鿿]")


def claim_numbers(text: str) -> list[str]:
    """Числа баллов/процентов из ответа (нормализованы: запятая→точка)."""
    nums = BALL_CLAIM_RE.findall(text) + PCT_CLAIM_RE.findall(text)
    return [n.replace(",", ".") for n in nums]


def number_in_context(num: str, ctx: str) -> bool:
    """True, если числовой токен есть в контексте (граница — не-цифра; «,» и «.» эквивалентны)."""
    for v in {num, num.replace(".", ",")}:
        if re.search(rf"(?<!\d){re.escape(v)}(?!\d)", ctx):
            return True
    return False


def expected_section_title(hits, expected_roman: str) -> str | None:
    for h in hits:
        if h.section_roman == expected_roman and getattr(h, "section_title", ""):
            return h.section_title
    return None


def attributed(text: str, expected_roman: str, hits) -> bool:
    """Упомянут ли ожидаемый раздел: по римской цифре ИЛИ по значимому слову его названия."""
    if re.search(rf"\bраздел[а-я]*\s+{expected_roman}\b", text, re.IGNORECASE):
        return True
    title = expected_section_title(hits, expected_roman)
    if title:
        # значимые слова названия раздела (длиннее 5 букв) — хоть одно в ответе
        for w in re.findall(r"[А-Яа-яЁёA-Za-z]{6,}", title):
            if w.lower() in text.lower():
                return True
    return False


def evaluate(limit: int, cases_limit: int):
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    cases = data["cases"]
    if cases_limit:
        cases = cases[:cases_limit]
    rows = []
    for c in cases:
        ans = answer(c["query"], okpd2=c.get("okpd2") or None, limit=limit)
        text = ans.text or ""
        ctx = format_context(ans.hits, c["query"])
        if ans.cases:
            ctx += "\n" + format_cases(ans.cases)

        nums = claim_numbers(text)
        hallucinated = sorted({n for n in nums if not number_in_context(n, ctx)})

        rows.append({
            "id": c["id"],
            "in_scope": c["in_scope"],
            "expected": c["expected_section"] or "—",
            "query": c["query"],
            "n_claims": len(nums),
            "hallucinated": hallucinated,
            "faithful": not hallucinated,
            "attributed": attributed(text, c["expected_section"], ans.hits) if c["in_scope"] else None,
            "declined": bool(DECLINE_RE.search(text)),
            "disclaimer": EXPERT_DISCLAIMER[:40] in text,
            "cjk": bool(CJK_RE.search(text)),
            "low_relevance": ans.low_relevance,
        })
    return rows


def _rate(items, pred) -> tuple[int, int]:
    n = len(items)
    return (sum(1 for x in items if pred(x)), n)


def summarize(rows, limit: int) -> list[str]:
    ins = [r for r in rows if r["in_scope"]]
    out = [r for r in rows if not r["in_scope"]]
    L = []
    L.append("=" * 78)
    L.append(f"EVAL ОТВЕТА — golden set {len(rows)} кейсов "
             f"({len(ins)} in-scope, {len(out)} out-of-scope), limit={limit}, модель={settings.DEEPSEEK_MODEL}")
    L.append("=" * 78)
    L.append("")

    if ins:
        f_ok, f_n = _rate(ins, lambda r: r["faithful"])
        a_ok, a_n = _rate(ins, lambda r: r["attributed"])
        d_ok, d_n = _rate(ins, lambda r: r["disclaimer"])
        c_ok, c_n = _rate(ins, lambda r: not r["cjk"])
        total_claims = sum(r["n_claims"] for r in ins)
        total_halluc = sum(len(r["hallucinated"]) for r in ins)
        L.append("In-scope (качество ответа):")
        L.append(f"  FAITHFULNESS (нет выдуманных чисел) = {f_ok}/{f_n} = {f_ok / f_n:.2f}")
        L.append(f"      чисел баллов/% в ответах: {total_claims}; из них выдумано (нет в контексте): {total_halluc}")
        L.append(f"  Атрибуция раздела                   = {a_ok}/{a_n} = {a_ok / a_n:.2f}")
        L.append(f"  Дисклеймер эксперта                 = {d_ok}/{d_n} = {d_ok / d_n:.2f}")
        L.append(f"  Без CJK-иероглифов                  = {c_ok}/{c_n} = {c_ok / c_n:.2f}")
        L.append("")

    if out:
        dec_ok, dec_n = _rate(out, lambda r: r["declined"])
        L.append("Out-of-scope (продукция вне 719):")
        L.append(f"  Корректный отказ («вне сферы / уточнить») = {dec_ok}/{dec_n} = {dec_ok / dec_n:.2f}")
        L.append("")

    L.append(f"{'id':>3} {'sc':<3} {'ожид':>5} {'faith':>6} {'attr':>5} {'disc':>5} {'cjk':>4}  запрос")
    L.append("-" * 78)
    for r in rows:
        sc = "IN" if r["in_scope"] else "OUT"
        faith = "✓" if r["faithful"] else f"✗{len(r['hallucinated'])}"
        attr = "—" if r["attributed"] is None else ("✓" if r["attributed"] else "✗")
        disc = "✓" if r["disclaimer"] else "✗"
        cjk = "!" if r["cjk"] else "·"
        if not r["in_scope"]:
            attr = "↩" if r["declined"] else "✗"  # для OUT: отказал ли
        L.append(f"{r['id']:>3} {sc:<3} {r['expected']:>5} {faith:>6} {attr:>5} {disc:>5} {cjk:>4}  {r['query'][:30]}")
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
    ap.add_argument("--report", type=str, default="", help="путь для сохранения отчёта (markdown)")
    args = ap.parse_args()

    try:
        _client().get_collections()
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Qdrant недоступен ({e}). Подними Docker Desktop — коллекция pp719 встанет сама.")
    if not (settings.DEEPSEEK_API_KEY or "").strip():
        sys.exit("Нет DEEPSEEK_API_KEY в .env — этот харнес вызывает DeepSeek.")

    rows = evaluate(args.limit, args.cases)
    report = summarize(rows, args.limit)
    print("\n".join(report))

    if args.report:
        path = (ROOT / args.report) if not Path(args.report).is_absolute() else Path(args.report)
        path.write_text("# Eval-отчёт КАЧЕСТВА ОТВЕТА (этап 1.0.4)\n\n```\n" + "\n".join(report) + "\n```\n",
                        encoding="utf-8")
        print(f"\nОтчёт сохранён: {path}")


if __name__ == "__main__":
    main()
