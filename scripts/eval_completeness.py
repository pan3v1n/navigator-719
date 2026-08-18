"""Полнота ответа — пара-ограничитель к faithfulness (уровень 2 EVAL_GUIDE).

ЗАЧЕМ. Faithfulness меряет «ничего лишнего». В одиночку эта метрика бессмысленна: поднять её до
1.00 тривиально, отвечая «данных недостаточно» на всё. Гайд поэтому требует читать её только в
паре с полнотой — но метрики полноты в проекте не было, то есть пара была РАЗОМКНУТА, и рост
faithfulness 0.98 → 1.00 (замер 16.08) формально нельзя было отличить от «система научилась молчать».

ПОЧЕМУ БЕЗ РУЧНОЙ РАЗМЕТКИ. Гайд предлагает набор `gs_answers` с эталонными фактами — его нет, и
размечать 120 кейсов руками дорого. Но эталон уже есть и он машинный: КОНТЕКСТ мы строим сами
(`format_context`), значит точно знаем, что обязано доехать до ответа. Полнота считается
ОТНОСИТЕЛЬНО КОНТЕКСТА, а не относительно внешнего списка фактов:

  * порог целевой позиции есть в контексте → обязан быть в ответе;
  * баллы операций есть в контексте → какая доля названа в ответе;
  * в контексте стоит маркер неполноты → ответ обязан предупредить;
  * требования унаследованы → ответ обязан назвать позицию-источник (иначе подмена).

Побочно считаются детерминированные проверки §2.2 гайда — «семь строк, которые ловят больше
дефектов, чем любая LLM-метрика, и стоят ноль»: запрет вердикта, номеров правил промпта, термина
«заключение ТПП», эмодзи и подстановки порога УЗЛА в строку «Порог».

Запуск (нужны Qdrant и DEEPSEEK_API_KEY):
  .venv/Scripts/python.exe scripts/eval_completeness.py --report docs/eval_completeness_report.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.rag import fragments, inheritance  # noqa: E402
from app.rag.pipeline import (  # noqa: E402
    _target_hit as target_hit,
    answer,
    claim_numbers,
    number_in_context,
)

GOLDEN = ROOT / "scripts" / "eval_golden.json"

# --- §2.2: чего в ответе быть НЕ должно ------------------------------------------------------
# Вердикт — главный запрет продукта: решение «российская/не российская» принимает эксперт ТПП.
VERDICT_RE = re.compile(
    r"будет призна\w+ российск|призна\w+ российск\w+ продукц|вы пройд[её]те|порог наберётся|"
    r"порог набирается|вы наберёте|соответствует требованиям 719", re.I)
# Номера правил инструкции («[2б]», «правило 4») эксперт прочитает как ссылку на источник.
RULE_REF_RE = re.compile(r"\[\s*\d+\s*[а-яё]\s*\]|правил[оа]\s+\d+[а-яё]?\b", re.I)
ZAKL_RE = re.compile(r"заключени\w*\s+ТПП", re.I)
EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️]")
CITE_RE = re.compile(r"\[(\d+)\]")
# Строка «Порог:» в ответе — в неё нельзя подставлять порог отдельного УЗЛА (правило 2б).
THRESHOLD_LINE_RE = re.compile(r"\*\*Порог:?\*\*[^\n]*", re.I)
WARN_RE = re.compile(r"неполн|не пол(ный|ного|ностью)|показаны не все|больше, чем показано|"
                     r"полный перечень|сверьте с первоисточник|не дела\w+ вывод", re.I)
GROUP_RE = re.compile(r"групп\w+|у позиции\s+«|приведены у позиции|общие требования", re.I)

# --- УТОЧНЯЮЩИЙ ОТВЕТ: полнота к нему НЕ применима ------------------------------------------
# Правило 1г промпта прямо ТРЕБУЕТ не расписывать баллы, когда уверенного совпадения нет: вместо
# этого выдать 2–4 позиции-кандидата с кодами и попросить подтвердить. Первая версия этой метрики
# считала такие ответы провалом полноты («0 из 4 баллов доехало») — то есть штрафовала за
# предписанное поведение и, если бы по ней оптимизировали, толкала бы систему называть баллы по
# НЕПОДТВЕРЖДЁННОЙ позиции. Это ровно тот класс «правдоподобно, но неверно», против которого
# построен весь продукт.
#
# Проверено на бою (16.08.2026): «делаем ручной слесарный инструмент» и «собираем городские
# автобусы» — оба без совпадения по коду, оба выдали список кандидатов. Дефекта нет.
CLARIFY_RE = re.compile(
    r"ближайшие позиции|укажите код|уточните\s+(наименование|код)|подтвердите\s+(код|нужную)|"
    r"точного совпадения|совпадение не уверенное|какая из них|кандидат", re.I)

# ⚠ Просьба о коде — ещё НЕ уточняющий ответ (калибровка 17.08.2026, `EV7`). Правило 1г описывает
# ФОРМУ: «Точного совпадения не нашёл. Ближайшие позиции: …» — и прямо запрещает расписывать
# требования кандидатов. Значит уточняющий ответ НЕ опирается ни на одну позицию, то есть в нём нет
# блока «**Позиция:**» из шаблона правила 4. Прежняя версия классификатора считала уточняющим любой
# ответ с фразой «укажите код» — и после `EV7`, где контекст сам велит просить код у кандидата,
# в исключённые уехали ответы, ДОВЕЗШИЕ все баллы целевой позиции: #8 бульдозеры 15 из 15,
# #9 экскаваторы 11 из 11, #12 светодиоды 8 из 8. Доля полноты оставалась 1.00, но метрика
# перестала мерить четверть набора (11 из 42 против 5) — то есть тихо насыщалась.
_COMMIT_RE = re.compile(r"\*\*\s*Позиция\s*:", re.I)


def is_clarifying(text: str, hits) -> bool:
    """Ответ-уточнение: позиция ещё не подтверждена, баллы называть нельзя (правило 1г).

    Три признака, все обязательны: нет совпадения по коду, ответ НЕ опирается на позицию (нет блока
    «**Позиция:**») и просит подтвердить. Одной фразы мало — «уточните» встречается и в обычном
    разборе («уточните у заявителя наличие сервисного центра», «уточните код, если продукция —
    одна из соседних позиций»)."""
    if any(h.okpd2_match for h in hits):
        return False
    if _COMMIT_RE.search(text or ""):   # ответ назвал целевую позицию → это разбор, а не уточнение
        return False
    return bool(CLARIFY_RE.search(text))


# ⚠ «Кто целевой» берётся ИЗ ПАЙПЛАЙНА (`_target_hit`), своей копии здесь нет. Копия была, и с
# `EV6` она разошлась с рантаймом: на расколотой ячейке метрика бралась за строку-квалификатор,
# у которой после `EV7` в контексте нет ни порога, ни баллов, — кейс молча выпадал из всех
# знаменателей, а отчёт продолжал печатать 1.00. Это тот же класс насыщения, что чинился в
# `is_clarifying` двадцатью строками выше.


def context_facts(hit, ctx: str) -> dict:
    """Что контекст ОБЯЗЫВАЕТ показать в ответе (эталон полноты, машинный)."""
    block = ctx.split("\n\n")[0] if ctx else ""
    for b in (ctx or "").split("\n\n"):  # блок целевой позиции, а не обязательно первый
        if hit and hit.product_name and hit.product_name[:40] in b:
            block = b
            break
    m = re.search(r"^\s*Порог:\s*(.+)$", block, re.M)
    threshold = (m.group(1).strip() if m else "")
    # Баллы операций именно ЦЕЛЕВОГО блока: числа перед «балл.»
    points = [n for n in re.findall(r"—\s*(\d+(?:[.,]\d+)?)\s*балл", block)]
    return {
        "threshold": threshold,
        "threshold_numbers": claim_numbers(threshold),
        "points": sorted(set(points), key=lambda x: -float(x.replace(",", "."))),
        "has_incomplete_marker": ("СПИСОК ОПЕРАЦИЙ НЕПОЛНЫЙ" in block
                                  or "ПОРОГИ ПОКАЗАНЫ НЕ ПОЛНОСТЬЮ" in block),
        "inherited": "ТРЕБОВАНИЯ ГРУППЫ" in block,
    }


def evaluate(limit: int, cases_limit: int) -> list[dict]:
    cases = json.loads(GOLDEN.read_text(encoding="utf-8"))["cases"]
    cases = [c for c in cases if c["in_scope"]]  # полноту меряем там, где есть что показывать
    if cases_limit:
        cases = cases[:cases_limit]

    rows = []
    for c in cases:
        ans = answer(c["query"], okpd2=c.get("okpd2") or None, limit=limit)
        text = ans.text or ""
        ctx = ans.grounding  # ⚠ заземление ответа, а не пересборка (см. eval_answers)
        hit = target_hit(ans.hits)
        want = context_facts(hit, ctx)

        # --- ПОЛНОТА -------------------------------------------------------------------------
        # Порог: достаточно, чтобы его ЧИСЛА доехали (форма записи у модели своя).
        thr_expected = bool(want["threshold_numbers"])
        thr_shown = (all(number_in_context(n, text) for n in want["threshold_numbers"])
                     if thr_expected else None)
        pts = want["points"]
        pts_shown = sum(1 for n in pts if number_in_context(n, text))
        warned = bool(WARN_RE.search(text)) if want["has_incomplete_marker"] else None
        attributed = bool(GROUP_RE.search(text)) if want["inherited"] else None

        # --- §2.2 ДЕТЕРМИНИРОВАННЫЕ ЗАПРЕТЫ --------------------------------------------------
        cites = {int(x) for x in CITE_RE.findall(text)}
        thr_line = " ".join(THRESHOLD_LINE_RE.findall(text))
        # Порог УЗЛА в строке «Порог» — числа, которых нет в пороге позиции, но есть в баллах узлов.
        node_leak = [n for n in claim_numbers(thr_line)
                     if n not in want["threshold_numbers"] and n in pts]

        clarifying = is_clarifying(text, ans.hits)
        # Просьба подтвердить код при ОПОРЕ на позицию — не уточнение, а страховка (см. is_clarifying).
        # Считаем отдельно: если однажды такие ответы начнут терять баллы целевой, это будет видно.
        asks_code = bool(CLARIFY_RE.search(text)) and not clarifying
        if clarifying:  # полнота неприменима: позиция не подтверждена, баллы называть нельзя
            thr_expected, thr_shown, pts, pts_shown = False, None, [], 0

        rows.append({
            "id": c["id"], "query": c["query"], "section": c["expected_section"],
            "clarifying": clarifying, "asks_code": asks_code,
            "thr_expected": thr_expected, "thr_shown": thr_shown,
            "n_points": len(pts), "points_shown": pts_shown,
            "warned": warned, "attributed": attributed,
            "verdict": bool(VERDICT_RE.search(text)),
            "rule_ref": bool(RULE_REF_RE.search(text)),
            "zakl": bool(ZAKL_RE.search(text)),
            "emoji": bool(EMOJI_RE.search(text)),
            "cites_ok": (not cites) or max(cites) <= max(len(ans.hits), 1),
            "node_leak": node_leak,
            "unverified": ans.unverified_numbers,
        })
    return rows


def report(rows: list[dict]) -> list[str]:
    n = len(rows)
    thr_rows = [r for r in rows if r["thr_expected"]]
    thr_ok = sum(1 for r in thr_rows if r["thr_shown"])
    pt_rows = [r for r in rows if r["n_points"]]
    pt_cov = ([r["points_shown"] / r["n_points"] for r in pt_rows] or [0])
    warn_rows = [r for r in rows if r["warned"] is not None]
    attr_rows = [r for r in rows if r["attributed"] is not None]

    def pct(a, b):
        return f"{a}/{b} = {a / b:.2f}" if b else "— (случаев нет)"

    clarifying = [r for r in rows if r.get("clarifying")]
    out = ["=" * 78,
           f"ПОЛНОТА ОТВЕТА — {n} in-scope кейсов; эталон = КОНТЕКСТ, который построил сам сервис",
           "=" * 78,
           "",
           f"Уточняющих ответов (правило 1г, полнота неприменима): {len(clarifying)} из {n}"
           + (" — " + ", ".join(f"#{r['id']}" for r in clarifying) if clarifying else ""),
           "  Позиция не подтверждена — называть по ней баллы ЗАПРЕЩЕНО, поэтому из полноты исключены.",
           f"  Ответов, опирающихся на позицию И просящих подтвердить код: "
           f"{sum(1 for r in rows if r.get('asks_code'))} из {n} — это разбор, а не уточнение, "
           f"в полноту ВКЛЮЧЕНЫ.",
           "",
           "ПОЛНОТА (пара-ограничитель к faithfulness), по подтверждённым позициям:",
           f"  Порог доехал до ответа        = {pct(thr_ok, len(thr_rows))}   (порог гайда ≥0.90)",
           f"  Баллы операций доехали        = {sum(pt_cov) / len(pt_cov):.2f} в среднем по кейсу "
           f"({sum(r['points_shown'] for r in pt_rows)} из {sum(r['n_points'] for r in pt_rows)} чисел)",
           f"  Предупредил о неполноте       = {pct(sum(1 for r in warn_rows if r['warned']), len(warn_rows))}   (порог 1.00)",
           f"  Назвал источник требований    = {pct(sum(1 for r in attr_rows if r['attributed']), len(attr_rows))}   (порог 1.00)",
           "",
           "ЗАПРЕТЫ §2.2 (порог 1.00 = ни одного нарушения):",
           f"  Нет вердикта «признана российской»  = {pct(n - sum(r['verdict'] for r in rows), n)}",
           f"  Нет номеров правил промпта          = {pct(n - sum(r['rule_ref'] for r in rows), n)}",
           f"  Нет термина «заключение ТПП»        = {pct(n - sum(r['zakl'] for r in rows), n)}",
           f"  Нет эмодзи                          = {pct(n - sum(r['emoji'] for r in rows), n)}",
           f"  Все [N] существуют                  = {pct(sum(r['cites_ok'] for r in rows), n)}",
           f"  Порог узла не подставлен в «Порог»  = {pct(n - sum(1 for r in rows if r['node_leak']), n)}",
           ""]

    bad = [r for r in rows if (r["thr_expected"] and not r["thr_shown"]) or r["verdict"]
           or r["rule_ref"] or r["zakl"] or r["emoji"] or r["node_leak"] or not r["cites_ok"]]
    if bad:
        out.append("ПРОБЛЕМНЫЕ КЕЙСЫ:")
        for r in bad:
            why = []
            if r["thr_expected"] and not r["thr_shown"]:
                why.append("порог не доехал")
            if r["verdict"]:
                why.append("ВЕРДИКТ")
            if r["rule_ref"]:
                why.append("номер правила промпта")
            if r["zakl"]:
                why.append("«заключение ТПП»")
            if r["emoji"]:
                why.append("эмодзи")
            if r["node_leak"]:
                why.append(f"порог узла в строке «Порог»: {r['node_leak']}")
            if not r["cites_ok"]:
                why.append("ссылка [N] на несуществующую позицию")
            out.append(f"  #{r['id']:>3} [{r['section']:>5}] {r['query'][:52]:52} — {', '.join(why)}")
        out.append("")

    out.append("ПОКРЫТИЕ БАЛЛОВ ПО КЕЙСАМ (худшие сверху):")
    for r in sorted(pt_rows, key=lambda x: x["points_shown"] / x["n_points"])[:12]:
        out.append(f"  #{r['id']:>3} {r['points_shown']:>3}/{r['n_points']:<3} "
                   f"{r['points_shown'] / r['n_points']:.2f}  {r['query'][:56]}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Полнота ответа относительно контекста (уровень 2)")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--cases", type=int, default=0, help="только первые N кейсов (дёшево)")
    ap.add_argument("--report", type=str, default="")
    args = ap.parse_args()

    rows = evaluate(args.limit, args.cases)
    text = "\n".join(report(rows))
    print(text)
    if args.report:
        p = (ROOT / args.report) if not Path(args.report).is_absolute() else Path(args.report)
        p.write_text("# Полнота ответа (уровень 2 EVAL_GUIDE)\n\n```\n" + text + "\n```\n",
                     encoding="utf-8")
        print(f"\nОтчёт сохранён: {p}")


if __name__ == "__main__":
    main()
