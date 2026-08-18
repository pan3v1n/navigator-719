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
from app.rag import fragments, inheritance  # noqa: E402
from app.rag.pipeline import (  # noqa: E402
    _target_hit,
    answer,
    claim_numbers,
)
from app.rag.retriever import okpd2_match  # noqa: E402
from app.rag.thresholds import lookup_threshold  # noqa: E402

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


def _norm_num(v) -> str | None:
    """Число записи в ТОЙ ЖЕ форме, в какой `claim_numbers` достаёт его из ответа («30», «12.5»)."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return str(int(f)) if f.is_integer() else str(f)
    s = str(v).strip().replace(",", ".")
    return s or None


def _numbers_of(threshold, blocks) -> set[str]:
    """Числа баллов/процентов, ПРИНАДЛЕЖАЩИЕ данным: порог позиции и узлов, баллы операций,
    величины внутри текстов операций и условий блоков."""
    out: set[str] = set()
    texts: list[str] = []
    if threshold:
        texts.append(str(threshold))
    for b in (blocks or []):
        for field in ("min_threshold", "note", "component"):
            if b.get(field):
                texts.append(str(b[field]))
        for op in (b.get("operations") or []):
            if op.get("text"):
                texts.append(str(op["text"]))
            n = _norm_num(op.get("points"))
            if n:
                out.add(n)
    return out | set(claim_numbers("\n".join(texts)))


def record_numbers(h) -> set[str]:
    """Числа записи-хита. Считается из ДАННЫХ, а не из рендера — см. `foreign_numbers`."""
    return _numbers_of(h.min_threshold, h.requirement_blocks)


def _own_numbers(target, hits: list, code: str | None) -> set[str]:
    """Числа, которые ответ вправе назвать: они принадлежат позиции, О КОТОРОЙ идёт речь.

    Сюда входят четыре законных источника, и каждый — факт ДАННЫХ, а не свойство рендера:
      * сама целевая запись;
      * порог, добираемый рантаймом из примечаний-таблиц (`thresholds.lookup_threshold`) — у части
        позиций порог живёт в примечании раздела и попадает в контекст законно;
      * требования ГРУППЫ (`R6`), когда своих у позиции нет: они показываются с атрибуцией;
      * строки ОДНОЙ расколотой ячейки (`R29`/`EV6`) — это не чужая продукция, а один источник,
        разъехавшийся по нескольким строкам кода при конвертации.

    ⚠ ЧЕГО СЮДА СОЗНАТЕЛЬНО НЕ ВХОДИТ: записи, попавшие в окно по ПРЕФИКСУ кода. Пользователь,
    назвавший «28.13», получает окно, где рантайм помечает целевыми ВСЕ 52 подходящие записи
    (531 операция); посчитать их числа «своими» — значит ослепить метрику ровно на том дефекте,
    который она обязана показывать. Точное совпадение кода — другое дело: у одного кода в
    приложении бывает несколько записей с поделёнными между ними требованиями (26.11.22.210),
    и назвавший этот код эксперт спрашивает про них обе."""
    own = record_numbers(target)
    mt = target.min_threshold or lookup_threshold(
        target.okpd2_codes, target.product_name, target.section_roman)
    if mt:
        own |= set(claim_numbers(str(mt)))
    parent = inheritance.lookup(target.section_roman, target.product_name)
    if parent:
        own |= _numbers_of(parent.get("min_threshold"),
                           [{"operations": parent.get("operations") or []}])
    group = fragments.group_of(target.product_name)
    for h in hits:
        if h is target:
            continue
        if (group is not None and fragments.group_of(h.product_name) == group) or _exact_code(h, code):
            own |= record_numbers(h)
    return own


def _exact_code(h, code: str | None) -> bool:
    """Названный пользователем код совпал с кодом записи ТОЧНО (не по префиксу)."""
    return bool(code) and code.strip() in (h.okpd2_codes or [])


def _case_about_target(case: dict, target) -> bool:
    """Кейс эксперта про ТУ ЖЕ продукцию? Тогда его числа — не чужие."""
    code = (case.get("okpd2") or "").strip()
    return bool(code) and okpd2_match(target.okpd2_codes or [], code)


def foreign_numbers(query: str, hits: list, cases: list | None = None,
                    code: str | None = None) -> set[str]:
    """Числа, принадлежащие ЧУЖИМ позициям окна, но не целевой и не её законным источникам.

    ⚠⚠ ПРЕЖНЯЯ ВЕРСИЯ ЭТОЙ ФУНКЦИИ БЫЛА ТАВТОЛОГИЕЙ, и на ней построен отчёт `EV7` от 17.08.2026
    («гейт „без чужих чисел“ 1.00 ×3, ноль утечек из 50»). Эталон «чужих чисел» она добывала так:
    рендерила кандидата через `format_context([target, h])` и вычитала числа целевой. Ровно та же
    правка `EV7` из этого рендера требования кандидатов и УБРАЛА — значит после неё разность пуста
    ПО ПОСТРОЕНИЮ, при любом поведении модели. Проверено прогоном на фикстуре насосов: кандидат
    несёт 110, 450 и порог 750, а `foreign_numbers` возвращает пустое множество. Гейт не мог
    опуститься ниже 1.00 на ветке без кода — а это 8 из 10 запросов набора.

    Урок: **оракул метрики нельзя выводить из артефакта, который она проверяет.** Тот же класс, что
    «эталон нельзя размечать регулярками» (`K12`), — метрика начинает проверять себя. Здесь он был
    замаскирован тем, что число ДВИНУЛОСЬ в успокаивающую сторону (0.90 → 1.00) и совпало с
    ожиданием от правки.

    ТЕПЕРЬ эталон строится из ЗАПИСИ (`record_numbers`): порог позиции и узлов, баллы операций,
    величины в текстах требований и условий блоков. Такой оракул переживает любую правку рендера —
    в том числе обратную — и умеет показывать «плохо». Что считается СВОИМ — в `_own_numbers`.

    ⚠ КАНАЛ КЕЙСОВ ЭКСПЕРТА (`cases`) теперь тоже виден. `_plan_answer` кладёт `format_cases` и в
    промпт, и в строку заземления, а правило 1а даёт кейсу ВЫСШИЙ приоритет; часть кейсов корпуса
    несёт баллы и проценты. После `EV7` это единственный оставшийся канал чужих чисел в промпте, и
    прежняя метрика не смотрела на него вовсе. Кейс про ту же продукцию (совпадение по коду) чужим
    не считается.

    ⚠ ПОПЫТКА СМЯГЧИТЬ ПО ВЕТКЕ КОДА ПРОВЕРЕНА И ОТВЕРГНУТА (17.08.2026) — запись оставлена как
    предупреждение. Идея была считать «своими» числа записей, делящих ветку ОКПД2 с целевой:
      * у светодиодов записи имеют РАЗНЫЕ коды (26.11.22.210 против 26.11.22.216) — правило даже
        не срабатывало, утечка так и оставалась 5/5;
      * у центробежных насосов «Насосы подачи жидкостей прочие» и «Насосы технологические типов
        ВВ1… для крупнотоннажных производств СПГ» делят ОДИН код 28.13.14.110, будучи разной
        продукцией, — и правило замаскировало НАСТОЯЩУЮ утечку (4/5 → 0).
    Поэтому ветка кода «своим» не делает: послабление даёт только ТОЧНОЕ совпадение названного кода
    и принадлежность одной расколотой ячейке — оба факта проверяются по данным.

    ⚠ ОКНО БЕРЁТСЯ ИЗ САМОГО ОТВЕТА (`ans.hits`), а не из отдельного прогона ретрива. Первая версия
    звала `_plan_answer` заново — то есть судила ответ по окну, которого он мог не видеть, в
    скрипте, вся суть которого — мерить нестабильность этого самого окна. Заодно уходит седьмое
    обращение к Qdrant и лишний ПЛАТНЫЙ вызов реранкера на каждый запрос без кода.

    EV1: разброс набора чисел сам по себе — плохая метрика. Он растёт и когда модель просто иначе
    формулирует (упомянула «2 балла» — не упомянула), и когда она тащит в ответ ПОРОГ СОСЕДНЕЙ
    ПОЗИЦИИ. Первое — шум, второе — дефект: эксперт читает число как относящееся к своей продукции.
    Здесь считается именно вредная половина."""
    target = _target_hit(hits)
    if target is None:                     # ранний путь (meta/процедурный) — кандидатов нет
        return set()
    own = _own_numbers(target, hits, code)
    group = fragments.group_of(target.product_name)
    others: set[str] = set()
    for h in hits:
        if h is target:
            continue
        if group is not None and fragments.group_of(h.product_name) == group:
            continue                       # строка той же расколотой ячейки — не чужая продукция
        if _exact_code(h, code):
            continue                       # точное совпадение названного кода
        others |= record_numbers(h)
    for c in (cases or []):
        if not _case_about_target(c, target):
            others |= set(claim_numbers(str(c.get("expert_answer") or "")))
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
            # Атрибуция — по ЦЕЛЕВОЙ позиции, а не по hits[0]: с EV6 это разные записи (у
            # расколотой ячейки опорой становится содержательный сиблинг, а не квалификатор).
            tgt = _target_hit(ans.hits)
            secs.append(tgt.section_roman if tgt else "—")
            flags.append(bool(ans.unverified_numbers))
            # Чужие числа считаем по окну ИМЕННО ЭТОГО прогона — см. докстринг foreign_numbers.
            # Числа, названные САМИМ пользователем, утечкой не считаются (та же поправка, что у
            # гарда 16.08: эхо вопроса — не выдумка и не чужое число).
            leaked = (nums - set(ans.echoed_numbers)) & foreign_numbers(
                q, ans.hits, ans.cases, code)
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
    # с рангов 6–8, а именно там, по докстрингу `_plan_answer`, сидят пограничные кандидаты.
    # (До EV7 их баллы попадали в промпт напрямую; после — окно всё равно должно быть боевым:
    # ветка совпадения по коду показывает требования ВСЕХ совпавших записей.) Замер на узком окне
    # давал бы 1.00 при живой утечке в проде.
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
