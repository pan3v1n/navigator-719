"""P2 #8 — детерминизм: воспроизводим ли ответ при повторных вызовах (temp=0.1 ≠ 0).

Прод-инструмент должен давать стабильный ответ: эксперт, переспросив то же, не должен видеть
ДРУГИЕ баллы/атрибуцию. Гоняем каждый запрос N раз и меряем дисперсию: набор чисел баллов/%,
раздел top-1, флаг незаземлённых чисел. Ретрив/реранкер детерминированы (temp=0) — дисперсия,
если есть, идёт от ГЕНЕРАЦИИ (temp=0.1).

ВНИМАНИЕ: DeepSeek, N×|queries| вызовов (по умолч. 5×10=50 ≈ ¥2-3).
  .venv\\Scripts\\python scripts\\eval_determinism.py --runs 5 --report docs/eval_determinism_report.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings  # noqa: E402
from app.rag.pipeline import (  # noqa: E402
    _target_hit,
    answer,
    claim_numbers,
    format_context,
)

# Представительные запросы (числоёмкие, разные разделы, с кодом и без)
QUERIES = [
    ("сколько баллов нужно для производства городских автобусов", None),
    ("требования и баллы для шариковых и роликовых подшипников", "28.15.10"),
    ("производим светодиоды белого диапазона, какие требования", None),
    ("какие баллы для гусеничных бульдозеров", None),
    ("требования к локализации центробежных насосов", None),
    ("сколько баллов для металлорежущих станков с ЧПУ", None),
    ("требования для производства фармацевтических субстанций", None),
    ("баллы для одноразовых медицинских масок", None),
    ("какие требования для башенных грузоподъёмных кранов", "29.22.14.400"),
    ("требования к производству интегральных микросхем", None),
]


def foreign_numbers(query: str, hits: list) -> set[str]:
    """Числа, которые есть ТОЛЬКО у ЧУЖИХ кандидатов окна, но не у целевой позиции и её сиблингов.

    ⚠ ПОПЫТКА СМЯГЧИТЬ ПО ВЕТКЕ КОДА ПРОВЕРЕНА И ОТВЕРГНУТА (17.08.2026). Идея была считать
    «своими» числа записей, делящих ветку ОКПД2 с целевой: у «светодиодов белого диапазона»
    `_target_hit` берёт `hits[0]` — запись «Светодиоды (в части светодиодов белого диапазона)» с
    НУЛЁМ чисел, — а требования лежат в отдельной записи «Светодиоды белого диапазона», и ответ
    опирается на неё законно. Замер показал, что поправка не работает НИ В ОДНУ сторону:
      * у светодиодов записи имеют РАЗНЫЕ коды (26.11.22.210 против 26.11.22.216) — правило даже
        не срабатывает, утечка так и осталась 5/5;
      * у центробежных насосов «Насосы подачи жидкостей прочие» [1] и «Насосы технологические типов
        ВВ1… для крупнотоннажных производств СПГ» [6] делят ОДИН код 28.13.14.110, будучи разной
        продукцией, — и правило замаскировало НАСТОЯЩУЮ утечку (4/5 → 0).
    Метрика оставлена строгой: числа любой нецелевой записи считаются чужими. Ложное срабатывание
    на светодиодах — не шум метрики, а другой дефект: пустая запись-дубль обгоняет содержательную
    (заведено отдельно), и метрика правильно делает его видимым.

    ⚠ ОКНО БЕРЁТСЯ ИЗ САМОГО ОТВЕТА (`ans.hits`), а не из отдельного прогона ретрива. Первая версия
    звала `_plan_answer` заново — то есть судила ответ по окну, которого он мог не видеть, в
    скрипте, вся суть которого — мерить нестабильность этого самого окна. Заодно уходит седьмое
    обращение к Qdrant и лишний ПЛАТНЫЙ вызов реранкера на каждый запрос без кода.

    EV1: разброс набора чисел сам по себе — плохая метрика. Он растёт и когда модель просто иначе
    формулирует (упомянула «2 балла» — не упомянула), и когда она тащит в ответ ПОРОГ СОСЕДНЕЙ
    ПОЗИЦИИ. Первое — шум, второе — дефект: эксперт читает число как относящееся к своей продукции.
    Замер 17.08 показал, что это разные вещи: у «центробежных насосов» все 12 плавающих чисел были
    чужими (у целевой позиции чисел не было вовсе), а у «светодиодов» плавало число самой целевой.
    Здесь считается именно вредная половина."""
    target = _target_hit(hits)
    if target is None:                     # ранний путь (meta/процедурный) — кандидатов нет
        return set()
    own = set(claim_numbers(format_context([target], query)))
    others: set[str] = set()
    for h in hits:
        if h is not target:
            # ⚠ Форматируем ВМЕСТЕ с целевой, а не в одиночку. `format_context` считает целевым
            # первый хит (или совпавший по коду), поэтому одиночный кандидат получал кап целевого
            # (60 операций вместо 12) и рантайм-добор порога из примечаний — в «чужие» попадали
            # числа, которых в боевом промпте не было вовсе, и гейт ловил ложные утечки.
            pair = set(claim_numbers(format_context([target, h], query)))
            others |= pair
    return others - own


def evaluate(runs: int, limit: int, progress: bool):
    rows = []
    it = QUERIES
    if progress:
        try:
            from tqdm import tqdm
            it = tqdm(QUERIES, unit="зап", desc="детерминизм")
        except ImportError:
            pass
    for q, code in it:
        num_sets, secs, flags = [], [], []
        alien_runs, alien_seen = 0, set()
        for _ in range(runs):
            ans = answer(q, okpd2=code, limit=limit)
            nums = frozenset(claim_numbers(ans.text))
            num_sets.append(nums)
            secs.append(ans.hits[0].section_roman if ans.hits else "—")
            flags.append(bool(ans.unverified_numbers))
            # чужие числа считаем по окну ИМЕННО ЭТОГО прогона — см. докстринг foreign_numbers
            leaked = nums & foreign_numbers(q, ans.hits)
            if leaked:
                alien_runs += 1
                alien_seen |= leaked
        rows.append({
            "q": q, "code": code or "",
            "num_variants": len(set(num_sets)),       # 1 = стабильный набор чисел
            "sec_variants": len(set(secs)),           # 1 = стабильная атрибуция
            "flag_variants": len(set(flags)),         # 1 = стабильный guard-флаг
            "alien_runs": alien_runs,                 # в скольких прогонах утекли ЧУЖИЕ числа
            "alien_seen": sorted(alien_seen),
            "example_nums": sorted(set().union(*num_sets)) if num_sets else [],
        })
    return rows


def report(rows, runs: int) -> list[str]:
    n = len(rows)
    num_stable = sum(1 for r in rows if r["num_variants"] == 1)
    sec_stable = sum(1 for r in rows if r["sec_variants"] == 1)
    alien_total = sum(r["alien_runs"] for r in rows)
    clean_q = sum(1 for r in rows if r["alien_runs"] == 0)
    L = ["=" * 78,
         f"P2 #8 ДЕТЕРМИНИЗМ — {n} запросов × {runs} повторов, модель={settings.DEEPSEEK_MODEL}",
         "=" * 78,
         f"  СТАБИЛЬНЫЙ набор чисел баллов/% (одинаков во всех {runs}): {num_stable}/{n} = {num_stable/n:.2f}",
         f"  СТАБИЛЬНАЯ атрибуция (раздел top-1): {sec_stable}/{n} = {sec_stable/n:.2f}",
         f"  БЕЗ ЧУЖИХ ЧИСЕЛ (ни один прогон не привёл баллы непрофильных кандидатов):"
         f" {clean_q}/{n} = {clean_q/n:.2f}   [утечек всего: {alien_total}/{n * runs} прогонов]",
         "",
         "  ⚠ Первая метрика меряет и шум формулировки, и дефект; третья — только дефект:",
         "     число соседней позиции эксперт читает как относящееся к СВОЕЙ продукции.",
         "",
         "Детализация (вариантов из N повторов; 1 = детерминирован):",
         f"  {'числа':>6} {'раздел':>7} {'флаг':>5}  запрос"]
    for r in rows:
        mark = "" if r["num_variants"] == 1 else "  ⚠ числа плавают"
        if r["alien_runs"]:
            mark += f"  ⚠ ЧУЖИЕ числа в {r['alien_runs']}/{runs}"
        L.append(f"  {r['num_variants']:>6} {r['sec_variants']:>7} {r['flag_variants']:>5}  {r['q'][:40]}{mark}")
    L.append("")
    leaks = [r for r in rows if r["alien_runs"]]
    if leaks:
        L.append("ЧУЖИЕ ЧИСЛА В ОТВЕТЕ (баллы/пороги непрофильных кандидатов — правило 3в промпта):")
        for r in leaks:
            L.append(f"  «{r['q'][:46]}»: {r['alien_runs']}/{runs} прогонов, числа: {r['alien_seen']}")
        L.append("")
    unstable = [r for r in rows if r["num_variants"] > 1]
    if unstable:
        L.append("НЕСТАБИЛЬНЫЕ ЧИСЛА (объединение по повторам — эксперт может увидеть разное):")
        for r in unstable:
            L.append(f"  «{r['q'][:46]}»: {r['num_variants']} вариантов, числа из всех прогонов: {r['example_nums']}")
    L.append("")
    return L


def main() -> None:
    ap = argparse.ArgumentParser(description="P2 #8 детерминизм (вызывает DeepSeek)")
    ap.add_argument("--runs", type=int, default=5)
    # ⚠ Окно = боевое (8), а не прежние 5. При 5 метрика «чужие числа» структурно НЕ ВИДИТ утечек
    # с рангов 6–8, а именно там, по докстрингу `_plan_answer`, сидят пограничные кандидаты — и
    # `MAX_OPS_OTHER` кладёт их баллы в реальный промпт. Замер на узком окне давал бы 1.00 при
    # живой утечке в проде.
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--report", type=str, default="")
    ap.add_argument("--no-progress", action="store_true")
    args = ap.parse_args()

    try:
        from app.rag.retriever import _client
        _client().get_collections()
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Qdrant недоступен ({e}).")
    if not (settings.DEEPSEEK_API_KEY or "").strip():
        sys.exit("Нет DEEPSEEK_API_KEY в .env.")

    rows = evaluate(args.runs, args.limit, not args.no_progress)
    out = report(rows, args.runs)
    text = "\n".join(out)
    print("\n" + text)
    if args.report:
        path = (ROOT / args.report) if not Path(args.report).is_absolute() else Path(args.report)
        path.write_text("# P2 #8 детерминизм\n\n```\n" + text + "\n```\n", encoding="utf-8")
        print(f"\nОтчёт сохранён: {path}")


if __name__ == "__main__":
    main()
