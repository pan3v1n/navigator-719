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
from app.rag.thresholds import (  # noqa: E402
    lookup_procurement_threshold,
    lookup_threshold,
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


def decisive_numbers(target, inherited_parent=None) -> set[str]:
    """`EV10` (#91): РЕШАЮЩИЕ числа позиции — порог и баллы операций.

    ЗАЧЕМ ОТДЕЛЬНАЯ ВЕЛИЧИНА. Метрика «стабильный набор чисел» считает ЛЮБОЕ число ответа, а
    значит меряет не воспроизводимость решения, а полноту упоминаний. Разбор нестабильных
    запросов это и показал: у бульдозеров между прогонами расходятся `0.1` и `0.3` — проценты
    затрат на НИОКР, названные в ПРОЗЕ операции, а порог и баллы совпадают во всех прогонах.
    Эксперт по таким числам решения не принимает; он смотрит на порог и на баллы операций.

    Поэтому решающие числа берутся из СТРУКТУРНЫХ полей — `min_threshold` позиции, пороги,
    добираемые рантаймом из примечаний, и `points` операций, — и НЕ включают величины, вкраплённые
    в текст операций. Это ровно те числа, которые печатает код (`pipeline.points_table`), а не
    выбирает модель.

    ⚠ Наследование (`R6`): если своих операций у позиции нет, решающими становятся баллы ГРУППЫ —
    их же показывает контекст с атрибуцией.
    """
    if target is None:
        return set()
    # ⚠ ЗАКУПОЧНЫЙ ПОРОГ В РЕШАЮЩИЕ НЕ ВХОДИТ, и это замер, а не вкусовщина. Он отвечает на
    # ДРУГОЙ вопрос («для целей осуществления закупок»), печатается отдельной строкой со своим
    # условием, и назвать его или нет — выбор полноты, а не воспроизводимости. Разбор показал:
    # у «одноразовых медицинских масок» между прогонами плавали ровно 80 / 100 / 40 / 50 / 60 —
    # весь закупочный график прим. 53, — тогда как ОБЩИЙ порог «не менее 25 баллов» назывался
    # во всех прогонах без исключения. Включая закупочный, метрика штрафовала ответ за то, что
    # он не пересказал ответ на вопрос, которого не задавали. Различение то же, что ввела `K2`.
    out: set[str] = set(claim_numbers(str(target.min_threshold or "")))
    if not (target.min_threshold or "").strip():
        general = lookup_threshold(target.okpd2_codes, target.product_name, target.section_roman)
        if general:
            out |= set(claim_numbers(str(general)))
    blocks = list(target.requirement_blocks or [])
    if not any(b.get("operations") for b in blocks) and inherited_parent:
        out |= set(claim_numbers(str(inherited_parent.get("min_threshold") or "")))
        blocks = [{"operations": inherited_parent.get("operations") or []}]
    for b in blocks:
        for op in (b.get("operations") or []):
            n = _norm_num(op.get("points"))
            if n:
                out.add(n)
    return out


def _runtime_threshold_numbers(own_threshold, codes, name, section) -> set[str]:
    """Числа порогов, которые рантайм ДОБИРАЕТ записи из примечаний, — общего и закупочного.

    ⚠ ЗЕРКАЛИТ `format_context`, а не «всё, что найдётся» (ревью 20.08.2026). Рантайм строит
    общий порог как `h.min_threshold or lookup_threshold(...)` (`pipeline.py:326`): у записи со
    СВОИМ порогом примечание не читается вовсе. Первая редакция звала `lookup_threshold`
    безусловно и вносила в «свои» числа, которых контекст не печатает НИКОГДА, — замер: 56
    записей имеют и свой порог, и совпадение по примечанию, у 54 из них в `own` попадали лишние
    числа. А `foreign_numbers` возвращает `others - own`, то есть настоящая утечка чужого числа
    вычиталась бы и гейт продолжал показывать 1.00. Метрика, расширенная сверх проверяемого
    артефакта, перестаёт его проверять — тот же класс, что тавтология оракула 17.08.

    Закупочный порог, наоборот, печатается ВСЕГДА при наличии (`pipeline.py:409`), независимо
    от собственного порога записи, — поэтому он в «свои» входит безусловно.
    """
    out: set[str] = set()
    if not (own_threshold or "").strip():
        general = lookup_threshold(codes, name, section)
        if general:
            out |= set(claim_numbers(str(general)))
    proc = lookup_procurement_threshold(codes, name, section)
    if proc:
        out |= set(claim_numbers(str(proc)))
    return out


def _hit_numbers_with_thresholds(h) -> set[str]:
    """Числа записи ВМЕСТЕ с порогами, которые рантайм добирает ей из примечаний."""
    return record_numbers(h) | _runtime_threshold_numbers(
        h.min_threshold, h.okpd2_codes, h.product_name, h.section_roman)


def _own_numbers(target, hits: list, code: "str | list[str] | None") -> set[str]:
    """Числа, которые ответ вправе назвать: они принадлежат позиции, О КОТОРОЙ идёт речь.

    Сюда входят пять законных источников, и каждый — факт ДАННЫХ, а не свойство рендера:
      * сама целевая запись;
      * порог, добираемый рантаймом из примечаний-таблиц (`thresholds.lookup_threshold`) — у части
        позиций порог живёт в примечании раздела и попадает в контекст законно;
      * требования ГРУППЫ (`R6`), когда своих у позиции нет: они показываются с атрибуцией;
      * строки ОДНОЙ расколотой ячейки (`R29`/`EV6`) — это не чужая продукция, а один источник,
        разъехавшийся по нескольким строкам кода при конвертации;
      * ЗАКУПОЧНЫЙ порог самой позиции (`lookup_procurement_threshold`, `K2`) — он печатается
        отдельной строкой со своим условием и принадлежит цели, а не соседу.

    ⚠ ЧЕГО СЮДА СОЗНАТЕЛЬНО НЕ ВХОДИТ: записи, попавшие в окно по ПРЕФИКСУ кода. Пользователь,
    назвавший «28.13», получает окно, где рантайм помечает целевыми ВСЕ 52 подходящие записи
    (531 операция); посчитать их числа «своими» — значит ослепить метрику ровно на том дефекте,
    который она обязана показывать. Точное совпадение кода — другое дело: у одного кода в
    приложении бывает несколько записей с поделёнными между ними требованиями (26.11.22.210),
    и назвавший этот код эксперт спрашивает про них обе."""
    own = record_numbers(target)
    # ⚠ ПЯТЫЙ ЗАКОННЫЙ ИСТОЧНИК, найденный замером 20.08.2026. `K2` (#47) стала печатать
    # ЗАКУПОЧНЫЙ порог отдельной строкой — всегда со своим условием («не для подтверждения
    # происхождения»). Это числа ЦЕЛЕВОЙ позиции, но оракул о них не знал, и гейт «без чужих
    # чисел» сел 1.00 → 0.90: «одноразовые медицинские маски» флагались в 5 прогонах из 5
    # числами 40 и 50 из строки прим. 53 своей же позиции. Ответ при этом ВЕРЕН — порог
    # происхождения назван отдельно («не менее 25 баллов»), а 40/50 стоят только внутри
    # обусловленной фразы про закупки.
    #
    # Тот же класс, что уже дважды ловили: оракул и код, который он судит, менялись в разных
    # коммитах и разошлись. Метрика, штрафующая за ВЕРНОЕ поведение, дороже отсутствующей —
    # она заставляет усомниться в правке (урок про предохранитель, бьющий по верному коду).
    own |= _runtime_threshold_numbers(
        target.min_threshold, target.okpd2_codes, target.product_name, target.section_roman)
    parent = inheritance.lookup(target.section_roman, target.product_name)
    if parent:
        own |= _numbers_of(parent.get("min_threshold"),
                           [{"operations": parent.get("operations") or []}])
    group = fragments.group_of(target.product_name)
    for h in hits:
        if h is target:
            continue
        if (group is not None and fragments.group_of(h.product_name) == group) or _exact_code(h, code):
            # ⚠ Не `record_numbers`: сиблингу рантайм тоже ДОБИРАЕТ порог из примечаний, и
            # `format_context` его печатает (ревью 20.08.2026 — та же неполнота, что была у цели).
            own |= _hit_numbers_with_thresholds(h)
    return own


def _codes_of(code: "str | list[str] | None") -> list[str]:
    """Коды к списку. ⚠ Ревью PR #94: сюда стал приходить `ans.codes` (СПИСОК) — прежняя сигнатура
    `str | None` звала `code.strip()` и падала AttributeError, то есть половинчатая миграция была
    ещё и ловушкой для очевидной следующей правки."""
    if not code:
        return []
    return [c.strip() for c in ([code] if isinstance(code, str) else list(code)) if c and c.strip()]


def _exact_code(h, code: "str | list[str] | None") -> bool:
    """Названный пользователем код совпал с кодом записи ТОЧНО (не по префиксу)."""
    codes = set(h.okpd2_codes or [])
    return any(c in codes for c in _codes_of(code))


def _case_about_target(case: dict, target) -> bool:
    """Кейс эксперта про ТУ ЖЕ продукцию? Тогда его числа — не чужие."""
    code = (case.get("okpd2") or "").strip()
    return bool(code) and okpd2_match(target.okpd2_codes or [], code)


def foreign_numbers(query: str, hits: list, cases: list | None = None,
                    code: "str | list[str] | None" = None) -> set[str]:
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
    target = _target_hit(hits, code)
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
        num_sets, dec_sets, secs, flags = [], [], [], []
        alien_runs, alien_seen, alien_pool = 0, set(), set()
        for _ in range(runs):
            ans = answer(q, okpd2=code, limit=limit)
            nums = frozenset(claim_numbers(ans.text))
            num_sets.append(nums)
            # Атрибуция — по ЦЕЛЕВОЙ позиции, а не по hits[0]: с EV6 это разные записи (у
            # расколотой ячейки опорой становится содержательный сиблинг, а не квалификатор).
            tgt = _target_hit(ans.hits, ans.codes)  # ⚠ теми же кодами, что рантайм
            secs.append(tgt.section_roman if tgt else "—")
            # EV10: из чисел ответа оставляем только РЕШАЮЩИЕ — порог и баллы операций.
            parent = (inheritance.lookup(tgt.section_roman, tgt.product_name) if tgt else None)
            dec_sets.append(frozenset(nums & decisive_numbers(tgt, parent)))
            flags.append(bool(ans.unverified_numbers))
            # Чужие числа считаем по окну ИМЕННО ЭТОГО прогона — см. докстринг foreign_numbers.
            # Числа, названные САМИМ пользователем, утечкой не считаются (та же поправка, что у
            # гарда 16.08: эхо вопроса — не выдумка и не чужое число).
            # ⚠ ТЕМИ ЖЕ КОДАМИ, ЧТО РАНТАЙМ (ревью PR #94): строкой выше атрибуция уже считается
            # по `ans.codes`, а оракул утечки продолжал брать одиночный `code` фикстуры и выводить
            # СВОЮ целевую из него. На вопросе с двумя кодами рантайм держит две опоры, оракул —
            # одну, и числа законной второй опоры считались утечкой.
            pool = foreign_numbers(q, ans.hits, ans.cases, ans.codes)
            alien_pool |= pool
            leaked = (nums - set(ans.echoed_numbers)) & pool
            if leaked:
                alien_runs += 1
                alien_seen |= leaked
        rows.append({
            "q": q, "code": code or "",
            "num_variants": len(set(num_sets)),       # 1 = стабильный набор чисел (с прозой)
            "dec_variants": len(set(dec_sets)),       # 1 = стабильны РЕШАЮЩИЕ числа (EV10)
            # ⚠ EV10: ДОЛЯ СТАБИЛЬНЫХ ЧИСЕЛ, а не доля стабильных запросов. Так и было записано
            # в `EVAL_GUIDE` 16.08: «позиция с шестьюдесятью операциями и позиция с двумя весят
            # одинаково». На бинарной метрике из 10 запросов порог ≥0.95 недостижим ПО
            # ПОСТРОЕНИЮ — 9/10 = 0.90, то есть он требует ровно 10 из 10. Числовая доля даёт
            # шкалу, на которой порог что-то значит.
            "dec_union": len(set().union(*dec_sets)) if dec_sets else 0,
            "dec_common": len(set.intersection(*[set(d) for d in dec_sets])) if dec_sets else 0,
            "dec_pool": len(set().union(*dec_sets)) if dec_sets else 0,
            "sec_variants": len(set(secs)),           # 1 = стабильная атрибуция
            "flag_variants": len(set(flags)),         # 1 = стабильный guard-флаг
            "alien_runs": alien_runs,                 # в скольких прогонах утекли ЧУЖИЕ числа
            "alien_seen": sorted(alien_seen),
            # ⚠ СКОЛЬКО ЧУЖИХ ЧИСЕЛ БЫЛО ДОСТУПНО К УТЕЧКЕ. Без этой величины «1.00» не
            # интерпретируется: пустой пул даёт единицу при любом поведении модели — ровно
            # так прежняя версия оракула и показывала «гейт пройден» (см. foreign_numbers).
            "alien_pool": len(alien_pool),
            "example_nums": sorted(set().union(*num_sets)) if num_sets else [],
        })
    return rows


def report(rows, runs: int) -> list[str]:
    n = len(rows)
    num_stable = sum(1 for r in rows if r["num_variants"] == 1)
    dec_stable = sum(1 for r in rows if r.get("dec_variants", 1) == 1)
    dec_pool = sum(r.get("dec_pool", 0) for r in rows)
    scored = [r for r in rows if r.get("dec_union", 0)]
    dec_union = sum(r["dec_union"] for r in scored)
    dec_common = sum(r["dec_common"] for r in scored)
    dec_share = dec_common / dec_union if dec_union else 1.0
    sec_stable = sum(1 for r in rows if r["sec_variants"] == 1)
    alien_total = sum(r["alien_runs"] for r in rows)
    clean_q = sum(1 for r in rows if r["alien_runs"] == 0)
    pool_total = sum(r.get("alien_pool", 0) for r in rows)
    pool_q = sum(1 for r in rows if r.get("alien_pool", 0))
    L = ["=" * 78,
         f"P2 #8 ДЕТЕРМИНИЗМ — {n} запросов × {runs} повторов, модель={settings.DEEPSEEK_MODEL}",
         "=" * 78,
         f"  ДОЛЯ СТАБИЛЬНЫХ РЕШАЮЩИХ ЧИСЕЛ (EV10, главная): {dec_common}/{dec_union} = "
         f"{dec_share:.2f}   [порог гайда ≥0.95]",
         f"      считано по {len(scored)}/{n} запросам, где решающие числа вообще названы"
         + ("  — НИ ОДНОГО, метрика ничего не проверяет!" if not dec_union else ""),
         f"  справочно, запросов без единого расхождения решающих чисел: "
         f"{dec_stable}/{n} = {dec_stable/n:.2f}   [бинарная: один промах = −0.10]",
         f"      ⚠ доступно решающих чисел: {dec_pool}"
         + ("  — ПУСТО, метрика ничего не проверяет!" if not dec_pool else ""),
         f"  справочно, полнота упоминаний (ЛЮБОЕ число ответа одинаково во всех {runs}): "
         f"{num_stable}/{n} = {num_stable/n:.2f}   [порога нет]",
         f"  СТАБИЛЬНАЯ атрибуция (раздел top-1): {sec_stable}/{n} = {sec_stable/n:.2f}",
         f"  БЕЗ ЧУЖИХ ЧИСЕЛ (ни один прогон не привёл баллы непрофильных кандидатов):"
         f" {clean_q}/{n} = {clean_q/n:.2f}   [утечек всего: {alien_total}/{n * runs} прогонов]",
         f"  ⚠ было ДОСТУПНО к утечке: {pool_total} чужих чисел на {pool_q}/{n} запросах"
         + ("  — ПУЛ ПУСТ, метрика ничего не проверяет!" if not pool_total else ""),
         "",
         "  ⚠ EV10: ПОРОГ ГАЙДА ≥0.95 ОТНОСИТСЯ К ПЕРВОЙ МЕТРИКЕ. «Полнота упоминаний» считает",
         "     ЛЮБОЕ число ответа, включая проценты в прозе (напр. 0,1 и 0,3 % затрат на НИОКР",
         "     у бульдозеров), — эксперт по ним решения не принимает, и порогом их мерить нельзя.",
         "  ⚠ Гейт «без чужих чисел» меряет только ДЕФЕКТ: число соседней позиции эксперт читает",
         "     как относящееся к СВОЕЙ продукции.",
         "",
         "Детализация (вариантов из N повторов; 1 = детерминирован):",
         f"  {'реш.':>5} {'все':>4} {'раздел':>7} {'флаг':>5}  запрос"]
    for r in rows:
        mark = "" if r.get("dec_variants", 1) == 1 else "  ⚠ РЕШАЮЩИЕ числа плавают"
        if r.get("dec_variants", 1) == 1 and r["num_variants"] > 1:
            mark = "  · плавает только проза"
        if r["alien_runs"]:
            mark += f"  ⚠ ЧУЖИЕ числа в {r['alien_runs']}/{runs}"
        L.append(f"  {r.get('dec_variants', 1):>5} {r['num_variants']:>4} {r['sec_variants']:>7} "
                 f"{r['flag_variants']:>5}  {r['q'][:40]}{mark}")
    L.append("")
    leaks = [r for r in rows if r["alien_runs"]]
    if leaks:
        L.append("ЧУЖИЕ ЧИСЛА В ОТВЕТЕ (баллы/пороги непрофильных кандидатов — правило 3в промпта):")
        for r in leaks:
            L.append(f"  «{r['q'][:46]}»: {r['alien_runs']}/{runs} прогонов, числа: {r['alien_seen']}")
        L.append("")
    hard = [r for r in rows if r.get("dec_variants", 1) > 1]
    if hard:
        L.append("⚠ НЕСТАБИЛЬНЫ РЕШАЮЩИЕ ЧИСЛА (порог/баллы — эксперт может увидеть разное):")
        for r in hard:
            L.append(f"  «{r['q'][:46]}»: {r['dec_variants']} вариантов")
        L.append("")
    unstable = [r for r in rows if r["num_variants"] > 1]
    if unstable:
        L.append("Плавает полнота упоминаний (справочно, порога нет):")
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
