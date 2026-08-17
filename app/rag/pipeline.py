"""RAG-пайплайн навигатора: запрос → гибрид-поиск → контекст → DeepSeek → ответ.

Пометка «ИИ — черновик, вердикт за экспертом ТПП» в ТЕЛО ответа не добавляется (решение R3):
в интерфейсе она висит постоянной строкой, а маркируется каждая ВЫГРУЗКА диалогов.
Запуск как смоук (нужен поднятый Qdrant с коллекцией и DEEPSEEK_API_KEY в .env):
  .venv/Scripts/python.exe -m app.rag.pipeline "производим прицепы для легковых авто" 29.20.23
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from functools import lru_cache

from loguru import logger

from app.core.config import settings
from app.core.prompts import (
    NAVIGATOR_SYSTEM_PROMPT,
    PROCEDURAL_SYSTEM_PROMPT,
    build_navigator_user_prompt,
    build_procedural_user_prompt,
)
from app.rag import fragments, inheritance, okpd2_ref, sparse
from app.rag.embeddings import embed_query
from app.rag.retriever import Hit, dense_top1, search, search_cases, search_rules
from app.rag.thresholds import lookup_threshold

# Адаптивный кап операций (вариант A, 2026-07-05). Целевой хит (совпадение по коду ОКПД2 / top-1)
# показываем ПОЛНЕЕ — MAX_OPS_TARGET, — чтобы не резать умеренные продукты (напр. чиллеры XVI,
# 38 операций); прочие кандидаты [2]-[5] — кратко (MAX_OPS_OTHER). Контекст остаётся ограниченным
# (~60 + 4×12 ≈ прежние 5×25), а «СПИСОК ОПЕРАЦИЙ НЕПОЛНЫЙ» помечается только там, где список реально
# усечён (истинный хвост). История: плоский кап 15→25 (INTERIM-фикс P2, faithfulness 1.000 при temp=0
# + пометке), но 25 резал 38-оп чиллеры так же, как 676-оп автобусы. Экстремальный хвост (676/220 опер)
# лечится parent/child auto-merge — отложено в 2.0.
MAX_OPS_TARGET = 60
MAX_OPS_OTHER = 12
MAX_CASES = 3  # сколько подтверждённых кейсов подмешивать в контекст
RULES_TOP_K = 6  # сколько пунктов Правил реестра тянуть для процедурного ответа (проза → синтез из нескольких)
# K12: сколько пунктов Приказа №52 донести до ТОВАРНОГО ответа на смешанном вопросе. Три — по числу
# частей перечня раздела 4 (заявка · приложения к заявке · документы под критерии 719); больше
# разбавляет товарный контекст, из-за которого пользователь и пришёл.
RULES_DOC_POINTS = 3
# Out-of-scope guard: порог dense top-1 cosine. Ниже — подозрение, что продукция вне 719.
# Калибровка на golden set (docs/eval_report.md): out-of-scope ≤ 0.822, in-scope ≥ 0.808 —
# полоса перекрытия узкая, поэтому порог НЕ режет жёстко, а лишь поднимает флаг для модели
# (финальное решение «вне сферы» принимает LLM по смыслу контекста, см. правило 1б промпта).
RELEVANCE_SOFT = 0.83


@dataclass
class Answer:
    text: str
    hits: list[Hit]
    cases: list[dict] = field(default_factory=list)
    low_relevance: bool = False  # сработал ли сигнал out-of-scope guard
    unverified_numbers: list[str] = field(default_factory=list)  # числа баллов/% в ответе, не найденные в контексте
    # Числа, которых нет в контексте, но которые назвал САМ пользователь: не выдумка, в приёмочный
    # флаг не идут (см. `echoed_numbers`), но класс остаётся наблюдаемым — пишется в журнал.
    echoed_numbers: list[str] = field(default_factory=list)
    prompt_tokens: int = 0  # токены DeepSeek за ответ (учёт затрат в админ-логах)
    completion_tokens: int = 0
    # Пункты первоисточников процедурного ответа (Правила/тело ПП №719/Приказ №52) в порядке [n] —
    # для кликабельных источников. Товарный путь их не заполняет (там источники строятся из hits).
    rule_sources: list[dict] = field(default_factory=list)
    # U5: что показать подсказкой в поле ввода ПОСЛЕ этого ответа — готовый текст, «» = пусто.
    # Считается по ветке ответа (`app/rag/followup.py`), а не выдёргивается регуляркой из текста:
    # предложение внутри ответа пишет модель, и подсказка ходила бы за её формулировкой.
    input_hint: str = ""


# Условие блока (`note`) — норма, а не комментарий, поэтому режем его щедро и только по границе
# предложения. 600 символов покрывают 493 из 503 условий корпуса; кандидатам (не целевому хиту)
# хватает 200 — там задача лишь показать, что условие есть.
NOTE_CAP_TARGET = 600
NOTE_CAP_OTHER = 200


def _clip_note(note: str, limit: int) -> str:
    """Обрезает условие блока по границе предложения и ГОВОРИТ об усечении.

    Молча обрезанное условие опаснее показанного не полностью: «до 1 января 2024 г. - не менее 510
    баллов, с 1 января 2024 г. - не менее 780» после тихого реза превращается в один порог, и это
    ровно тот класс «правдоподобно, но неверно», ради которого заведена D9."""
    note = " ".join(note.split())
    if len(note) <= limit:
        return note
    cut = max(note.rfind("; ", 0, limit), note.rfind(". ", 0, limit))
    if cut < limit // 2:
        # Границы предложения нет — режем по лимиту, чем терять условие целиком, но ТОЛЬКО по
        # границе слова: рез внутри числа рождает число, которого в первоисточнике нет
        # («ГОСТ 3.1129-93» → «…3.1»), а постпроверка `unverified_numbers` сверяет ответ
        # с КОНТЕКСТОМ и такую подделку молча признает заземлённой.
        cut = note.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
    return (note[:cut].rstrip(" ;.")
            + " … (условие показано не полностью — полный текст в первоисточнике)")


def _same_threshold(a: str | None, b: str | None) -> bool:
    """Один и тот же порог, записанный с разными пробелами/регистром."""
    norm = lambda s: " ".join((s or "").split()).strip(" .;:").lower()  # noqa: E731
    return bool(norm(a)) and norm(a) == norm(b)


def _block_intro(component: str, note: str, note_cap: int, threshold: str | None = None) -> str | None:
    """Вводная строка блока: узел/вводная фраза + ЕГО порог и условие, при котором блок читается.

    Порог узла (D9) и условие приклеиваем к вводной, а не выводим отдельными строками, чтобы их
    нельзя было прочитать как порог всей позиции: они стоят вплотную к названию узла, к которому
    относятся. Ровно эту форму разбирает правило 2б промпта («криогенный насос низкого давления —
    не менее 100 баллов» → порог УЗЛА, никогда не в строку «Порог»)."""
    tail = "; ".join(x for x in ((threshold or "").strip(),
                                 _clip_note(note, note_cap) if note else "") if x)
    note = tail  # ниже вводная собирается из «имя — хвост»
    if component and note:
        return f"{component} — {note}"
    if component:
        return component
    # Условие БЕЗ имени узла (60 блоков корпуса: 59 — «обязательное требование», один — шкала
    # баллов по узлам разд. XVIII) выводилось строкой «▸ …» ровно там, где правило 2б промпта
    # велит читать «▸» как УЗЕЛ ИЗДЕЛИЯ вместе с условием. Для «100 баллов для главной
    # энергетической установки; 40 баллов для вспомогательной» это значит, что баллы блока могут
    # уехать в ответ порогами узлов — тот самый класс «правдоподобно, но неверно», ради которого
    # заведена D9. Поэтому условие без узла подписываем явно, а не выдаём за название узла.
    return f"Условие блока: {note}" if note else None


def _hit_operations(h: Hit, note_cap: int = NOTE_CAP_TARGET,
                    position_threshold: str | None = None) -> list[dict]:
    """Плоский список требований хита в порядке первоисточника (из всех requirement_blocks).

    R6: блок БЕЗ `operations`, но с текстом в `component` — это ТРЕБОВАНИЕ, а не заголовок узла.
    По схеме парсера `component` = «название компонента/узла ИЛИ ОПЕРАЦИИ ВЕРХНЕГО УРОВНЯ»: когда
    операций в блоке нет, весь смысл лежит именно там. Раньше такие блоки не показывались вообще —
    из контекста молча выпадало 56 449 символов требований по 84 позициям, причём у 77 из них есть
    другие операции, поэтому потеря была незаметна ни в ответе, ни в метриках.

    Класс потерянного — ровно тот, на который жаловались эксперты (отчёт за июль, «обязательные
    требования не показаны»): права на конструкторскую/техническую документацию, наличие сервисного
    центра, регистрационное удостоверение. Проверено на корпусе: все 168 таких блоков несут
    полноценный текст требования (ни одной висячей вводной фразы, ни одного дубля существующей
    операции, ни одного короткого ярлыка), поэтому правило безусловное.

    Баллы им НЕ приписываем (`points=None`) — промпт выведет их в блок «Обязательные требования
    (без балльной оценки)», как и положено требованию без балльной оценки.

    D9: поле `note` блока до 14.08.2026 не читалось ВООБЩЕ — ни здесь, ни где-либо ещё в рантайме,
    хотя `load_kb` кладёт запись в payload целиком. А там лежит условие, при котором блок читается:
    503 блока у 259 позиций (19 % корпуса), из них 305 — рядом с балльными операциями, где условие
    прямо меняет прочтение баллов. Внутри: пороги отдельных узлов изделия («криогенный насос низкого
    давления — не менее 100 баллов»), пометки «обязательное требование» (54), правила начисления
    («при неприменении компонента баллы за него не начисляются»), периоды действия.

    Хуже всего был случай «Оборудование для многостадийного ГРП»: девять узлов, у каждого свой порог
    по годам, а `min_threshold` записи — null. Пользователь видел баллы вообще без порога. В список
    D9 позиция не попала, потому что `verify_structured` сверяет ЗАПИСЬ, а числа в записи есть —
    слепая зона проверки ровно там же, где слепая зона рантайма."""
    ops: list[dict] = []
    for b in h.requirement_blocks:
        block_ops = b.get("operations") or []
        comp = (b.get("component") or "").strip()
        note = (b.get("note") or "").strip()
        # D9: у блока может быть СВОЙ порог — по узлу изделия («криогенный насос низкого
        # давления (не менее 100 баллов)») или своя шкала по годам у вида работ («Изготовление
        # смычков» — 80→110 отдельно от 170→200 у инструментов). В схеме записи он лежит в
        # `requirement_blocks[].min_threshold`; повтор порога позиции глушим, чтобы не удваивать.
        thr = (b.get("min_threshold") or "").strip()
        if thr and _same_threshold(thr, position_threshold):
            thr = ""
        if block_ops:
            # K4: вводная фраза блока — ЧАСТЬ требования, а не украшение, и до 12.08.2026 она
            # молча терялась: `ops.extend(block_ops)` брал только подпункты. А формулируется
            # требование именно в ней — «ОСУЩЕСТВЛЕНИЕ НА ТЕРРИТОРИИ РОССИЙСКОЙ ФЕДЕРАЦИИ
            # следующих технологических операций: …». Из 3959 блоков корпуса вводную имеют
            # 3733 (94 %), и у 484 из них она называет территорию — это ровно претензия
            # июльского теста «отсутствует отсылка на обязательность осуществления операций
            # на территории РФ» (15 упоминаний). Без вводной ответ показывает подпункты, не
            # говоря, ЧАСТЬЮ ЧЕГО они являются.
            #
            # Дубли отсекаем: у 8.3 % блоков вводная дословно повторяет одну из своих операций —
            # там она не добавляет смысла, только шум.
            dup = comp and any(
                comp.lower() == (o.get("text") or "").strip().lower() for o in block_ops)
            parent = _block_intro(comp if not dup else "", note, note_cap, thr)
            for o in block_ops:
                ops.append({**o, "_parent": parent} if parent else o)
            continue
        # Блок без операций: весь смысл в `component`, а условие уточняет, как его читать.
        text = _block_intro(comp, note, note_cap, thr)
        if text:
            ops.append({"text": text, "points": None})
    return ops


def _rank_operations(ops: list[dict], query: str | None) -> list[dict]:
    """Переставляет операции так, чтобы релевантные запросу шли первыми.

    Нужно для мега-продуктов (сотни операций): усечение до MAX_OPS_TARGET иначе режет
    нужное, оставляя первые попавшиеся. Скоринг — пересечение стем-токенов операции и
    запроса (локальный токенизатор BM25, без сети/модели). Сортировка стабильна: при
    равной релевантности исходный порядок сохраняется. Без запроса/совпадений — без изменений."""
    qtok = set(sparse.tokenize(query)) if query else set()
    if not qtok:
        return ops
    return sorted(ops, key=lambda o: -len(qtok & set(sparse.tokenize(o.get("text", "")))))


def format_context(hits: list[Hit], query: str | None = None) -> str:
    # Целевой хит (совпадение по коду ОКПД2, иначе top-1) показываем полнее прочих кандидатов.
    has_match = any(h.okpd2_match for h in hits)
    blocks: list[str] = []
    for i, h in enumerate(hits, 1):
        is_target = h.okpd2_match if has_match else (i == 1)
        cap = MAX_OPS_TARGET if is_target else MAX_OPS_OTHER
        sect = f"«{h.section_title}»" if h.section_title else f"Раздел {h.section_roman}"
        head = f"[{i}] {h.product_name} (раздел {sect})"
        if h.okpd2_match:
            head += "  СОВПАДЕНИЕ ПО КОДУ ОКПД2 (наиболее вероятная позиция)"
        lines = [head]
        if h.okpd2_codes:
            lines.append(f"    ОКПД2: {', '.join(h.okpd2_codes)}")
        # Порог: из самой позиции, иначе (для ЦЕЛЕВОГО хита) — из примечаний-таблиц по годам
        # (thresholds.py; напр. Чиллеры разд.XVI прим.77). Числа дословны → заземлены для гарда.
        mt = h.min_threshold or (lookup_threshold(h.okpd2_codes, h.product_name, h.section_roman)
                                 if is_target else None)
        ops = _hit_operations(h, NOTE_CAP_TARGET if is_target else NOTE_CAP_OTHER, mt)
        # R6 шаг 3: своих требований нет → показываем требования ГРУППЫ с явной атрибуцией.
        # Подмены не происходит: строка-атрибуция называет позицию-источник, а промпт обязан
        # это воспроизвести. Баллы не суммируем — это решает эксперт по первоисточнику.
        parent = None if ops else inheritance.lookup(h.section_roman, h.product_name)
        attribution_line = None
        if parent:
            attribution_line = "    " + inheritance.attribution(parent)
            ops = list(parent.get("operations") or [])
            if parent.get("min_threshold") and not mt:
                mt = (f"{parent['min_threshold']} — порог ГРУППЫ, указан у позиции "
                      f"«{parent.get('product_name', '')}»")
        # R7: различаем «порог не нашли» и «порога НЕТ в 719». Если требования позиции — перечень
        # обязательных операций без баллов (модель «operations»), то порога не существует, и молчание
        # заставляло модель писать «в контексте не указан» — читается как пробел в данных и было
        # жалобой №1 теста. Утверждаем это только при ДВУХ согласных признаках: ни у одной операции
        # нет баллов И тип требований не балльный. При «points»/«mixed» без баллов молчим — там
        # возможна потеря при разборе, и выдумывать «порога нет» нельзя.
        rtype = (h.payload or {}).get("requirement_type")
        if mt:
            lines.append(f"    Порог: {mt}")
        elif ops and not any(o.get("points") is not None for o in ops) and rtype in (None, "operations"):
            lines.append("    Порог: не предусмотрен — требования этой позиции заданы ПЕРЕЧНЕМ "
                         "обязательных операций, баллы за них не начисляются.")
        if attribution_line:
            lines.append(attribution_line)
        total = len(ops)
        if total > cap:
            ops = _rank_operations(ops, query)
        shown = ops[:cap]
        if shown:
            lines.append("    Ключевые операции группы:" if parent else "    Ключевые операции:")
            cur_parent = None
            for o in shown:
                # K4: вводная фраза блока печатается при смене группы — операции перестают
                # висеть без указания, частью какого требования они являются. При усечении
                # список пересортирован по релевантности, и заголовок может повториться —
                # это лучше, чем оставить операцию без её условия.
                op_parent = o.get("_parent")
                if op_parent and op_parent != cur_parent:
                    lines.append(f"      ▸ {op_parent}")
                cur_parent = op_parent
                pts = o.get("points")
                ptxt = f" — {pts} балл." if pts is not None else " — баллы в контексте не указаны"
                indent = "        " if op_parent else "      "
                lines.append(f"{indent}• {o.get('text', '')}{ptxt}")
            if total > cap:
                rel = " (показаны наиболее релевантные запросу)" if query else ""
                lines.append(
                    f"      СПИСОК ОПЕРАЦИЙ НЕПОЛНЫЙ: показаны {len(shown)} из {total} операций"
                    f"{rel}; полный перечень требований и баллов — в первоисточнике ПП №719 (этот раздел)."
                )
        # R29: у позиции требования заведомо неполны — общая ячейка группы расколота при конвертации
        # таблицы, и здесь лежит лишь её обрывок. Помечаем ВСЕГДА (даже когда операций мало и кап не
        # сработал): иначе фрагмент выглядит как полный перечень. Маркер тот же, что выше, — правило
        # 2а промпта заставит модель предупредить эксперта и не считать, наберётся ли порог.
        if fragments.is_fragmented(h.product_name):
            lines.append("      " + fragments.NOTICE)
        # D9: у позиции в законе несколько порогов (по узлам изделия или видам работ), а в записи
        # поместился один. Показанный порог выглядит порогом всего изделия, и недобор по узлу
        # проходит незамеченным — пометка обязательна, пока схема не научится хранить их все.
        if fragments.has_incomplete_thresholds(h.section_roman, h.product_name):
            lines.append("      " + fragments.THRESHOLD_NOTICE)
        if h.source_anchor:
            lines.append(f"    Источник: {h.source_anchor}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# --- Детерминированная таблица баллов (P4, 16.08.2026) --------------------------------------
# ЗАЧЕМ. Замер детерминизма даёт 8/10 при пороге гайда 0.95: на один и тот же вопрос эксперт
# получает разные наборы баллов. Тай-брейк ретрива (`retriever._order_key`) убрал половину
# причины — контекст стал стабильным, — но остаток лежит в ГЕНЕРАЦИИ: из шести десятков операций
# контекста модель каждый раз выбирает своё подмножество.
#
# Просить её «перечислять все» бесполезно: на длинных списках модели дрейфуют, и это ровно тот
# режим, где сорвётся и текстовая точность (искажение формулировки операции сейчас не ловится
# НИЧЕМ — текстового faithfulness в проекте нет).
#
# Поэтому длинный перечень печатает КОД, дословно из того же контекста. Модель остаётся автором
# разбора: позиция, порог, что уточнить, следующий шаг — всё это по-прежнему её работа.
#
# ТОЧЕЧНО, а не всегда: у коротких позиций модель справляется, а таблица вместо живого списка
# сделала бы типовой ответ суше без всякой пользы. Порог включения — `POINTS_TABLE_MIN`.
POINTS_TABLE_MIN = 12  # балльных операций у целевой позиции, начиная с которых печатает код

_POINTS_TABLE_TITLE = "Операции и баллы — дословно из приложения"


def _target_hit(hits: list[Hit]) -> Hit | None:
    """Позиция, вокруг которой строится ответ: совпадение по коду, иначе top-1 (как в контексте)."""
    if not hits:
        return None
    return next((h for h in hits if h.okpd2_match), hits[0])


def points_table(hit: Hit | None, query: str | None = None) -> str:
    """Markdown-таблица «Операция | Баллы» целевой позиции, либо пусто.

    Числа и формулировки берутся из той же записи, что легла в контекст, поэтому таблица
    заземлена по построению: faithfulness-гарду тут нечего ловить, а детерминизм абсолютный.
    Единица «балл./%» в ячейке сохраняется намеренно — иначе число перестаёт быть проверяемым
    (то же правило, что у 4б промпта)."""
    if hit is None:
        return ""
    mt = hit.min_threshold or lookup_threshold(hit.okpd2_codes, hit.product_name, hit.section_roman)
    ops = _hit_operations(hit, NOTE_CAP_TARGET, mt)
    # Таблица обязана быть ПОДМНОЖЕСТВОМ контекста, а не независимой выборкой из записи. Иначе
    # два ранжирования расходятся: в таблицу попадает операция, которой модель не видела, и
    # faithfulness-гард честно помечает её баллы как незаземлённые — сверка идёт с КОНТЕКСТОМ.
    # Поэтому повторяем ровно тот отбор, что делает `format_context`, и лишь потом фильтруем
    # балльные.
    shown_all = (_rank_operations(ops, query)[:MAX_OPS_TARGET]
                 if len(ops) > MAX_OPS_TARGET else ops)
    scored = [o for o in shown_all if o.get("points") is not None]
    if len(scored) < POINTS_TABLE_MIN:
        return ""
    # Числа в подписи НЕ печатаем. Контекст уже несёт свою пометку «показаны N из M операций», и
    # модель её цитирует; вторая пара чисел рядом (у нас M считалось бы по балльным — 94 против
    # 95) читается как расхождение данных. Один факт — один источник.
    incomplete = " (перечень неполный)" if len(ops) > len(shown_all) else ""
    rows = ["", f"**{_POINTS_TABLE_TITLE}{incomplete}:**", "",
            "| Операция или условие | Баллы |", "|---|---|"]
    for o in scored:
        text = " ".join((o.get("text") or "").split()).replace("|", "/")
        parent = " ".join((o.get("_parent") or "").split()).replace("|", "/")
        if parent:
            text = f"{parent} — {text}"
        rows.append(f"| {text} | {o['points']} балл. |")
    if incomplete:
        # Количество уже в подписи — здесь только напоминание, чтобы усечение было видно и тому,
        # кто копирует одну таблицу без подписи.
        rows.append("| _…перечень неполный, полный список — в первоисточнике ПП №719_ | |")
    return "\n".join(rows)


def format_cases(cases: list[dict]) -> str:
    blocks: list[str] = []
    for i, c in enumerate(cases, 1):
        lines = [f"[Кейс {i}] {c.get('product_name', '')} (ОКПД2 {c.get('okpd2', '—')})"]
        lines.append(f"    Ситуация: {c.get('query', '')}")
        lines.append(f"    Ответ эксперта: {c.get('expert_answer', '')}")
        if c.get("source"):
            lines.append(f"    Источник: {c['source']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


RULES_TEXT_CAP = 1400  # символов на пункт Правил в контексте (длинные усекаем, помечая)
# Пункт с перечнем документов (P2) — исключение из капа: перечень стоит В КОНЦЕ пункта, и общий
# кап оставлял от п. 4.1 (3844 знака) одну вводную фразу. Ответ на самый частый вопрос июля
# («какие документы готовить», 31 упоминание) снова превращался в отсылку «см. раздел 4».
# Расширенный кап действует ТОЛЬКО на пункты, добранные под этот вопрос (не более двух).
RULES_TEXT_CAP_DOC_LIST = 4000


def format_rules_context(rules: list[dict]) -> str:
    """Контекст процедурного ответчика: пронумерованные пункты Правил реестра ([1], [2], …)."""
    blocks: list[str] = []
    for i, r in enumerate(rules, 1):
        # Атрибуция — из source_anchor записи (Правила / тело ПП №719 / Приказ ТПП №52); фолбэк на
        # «Правила ведения реестра» для записей без анкера (обратная совместимость/тесты).
        anchor = (r.get("source_anchor") or "").strip()
        if anchor:
            head = f"[{i}] {anchor}"
        else:
            point = r.get("point") or "?"
            sect = r.get("section_title") or r.get("section_roman") or ""
            head = f"[{i}] Правила ведения реестра, п. {point}" + (f" ({sect})" if sect else "")
        text = (r.get("text") or "").strip()
        cap = RULES_TEXT_CAP_DOC_LIST if r.get("_doc_list") else RULES_TEXT_CAP
        if len(text) > cap:
            text = text[:cap].rstrip() + " …(пункт приведён не полностью; полный текст — в первоисточнике)"
        # Вводная фраза родительского пункта (P2): без неё «4.2.1. Правоустанавливающие и
        # регистрационные документы заявителя…» — список неизвестно к чему. То же правило, что
        # K4 применила к требованиям приложения.
        intro = (r.get("parent_intro") or "").strip()
        if intro and intro not in text:
            text = f"(в контексте пункта: {intro})\n{text}"
        blocks.append(head + "\n" + text)
    return "\n\n".join(blocks)


@lru_cache(maxsize=1)
def _client():
    """Клиент DeepSeek, ОДИН на процесс (R15).

    Раньше функция строила новый OpenAI() на каждый вызов — а вызовов на один вопрос до трёх
    (контекстуализация follow-up → реранкер → генерация). Каждый новый клиент = новый httpx-пул,
    то есть заново TCP+TLS к api.deepseek.com. На рваной сети с DPI (заявленное ограничение
    проекта) это ровно то, что рвётся первым. Кэш даёт переиспользование keep-alive соединений.

    Кэшируем сам клиент, а не результат запроса — он потокобезопасен (FastAPI гонит sync-эндпоинты
    в threadpool). Исключение при пустом ключе lru_cache НЕ кэширует → после правки .env и
    перезапуска всё поднимется; в тестах сбрасывается через `_client.cache_clear()`.
    """
    from openai import OpenAI

    if not settings.DEEPSEEK_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY пуст — заполни .env")
    # timeout+ретраи: без них openai-дефолт 600с×2 → на рваной РФ-сети зависший запрос держит поток
    # ~10 мин, эксперт смотрит в спиннер. Дефолт 30с покрывает генерацию и контекстуализацию;
    # реранкер берёт ЭТОТ ЖЕ клиент, но передаёт свой per-request timeout=20с.
    return OpenAI(
        api_key=settings.DEEPSEEK_API_KEY, base_url=settings.DEEPSEEK_BASE_URL,
        timeout=30.0, max_retries=1,
    )


# --- Faithfulness-постпроверка (P0 анти-галлюцинаций) ------------------------------
# Число-претензия в ОТВЕТЕ — величина рядом с единицей «балл» или «процент/%». Именно такие
# числа модель ОБЯЗАНА брать из контекста (правило 2 промпта); даты/сроки («5 лет», «2018 г.»)
# другой единицы и сюда не попадают. Те же функции использует scripts/eval_answers.py —
# рантайм и замер меряют ОДНО И ТО ЖЕ.
_NUM = r"\d+(?:[.,]\d+)?"
_BALL_CLAIM_RE = re.compile(rf"({_NUM})\s*балл", re.IGNORECASE)
_PCT_CLAIM_RE = re.compile(rf"({_NUM})\s*(?:процент|%)", re.IGNORECASE)


def claim_numbers(text: str) -> list[str]:
    """Числа баллов/процентов, заявленные в ответе (нормализованы: запятая→точка)."""
    return [n.replace(",", ".") for n in _BALL_CLAIM_RE.findall(text) + _PCT_CLAIM_RE.findall(text)]


def number_in_context(num: str, context: str) -> bool:
    """True, если числовой токен есть в контексте (граница — не-цифра; «,»≡«.»)."""
    return any(
        re.search(rf"(?<!\d){re.escape(v)}(?!\d)", context)
        for v in {num, num.replace(".", ",")}
    )


def unverified_numbers(text: str, context: str, question: str = "") -> list[str]:
    """Числа баллов/% из ОТВЕТА, которых НЕТ ни в контексте, ни в ВОПРОСЕ (кандидаты в галлюцинации).

    Консервативно: число незаземлено, только если его НЕТ вовсе (это занижает, а не завышает).
    Уникальные значения в порядке появления.

    ПОЧЕМУ ВОПРОС ТОЖЕ СЧИТАЕТСЯ ИСТОЧНИКОМ (замер 16.08.2026). Гард сверял ответ ТОЛЬКО с
    контекстом, поэтому цифра, которую назвал сам пользователь и которую ответ процитировал,
    помечалась как выдуманная. Поймано на ловушке «выполняем 4 операции, набрали 3200 баллов,
    мы пройдём?»: ответ честно говорит, что подходящей позиции нет («…соответствовала продукции,
    связанной с „4 операциями“ и „3200 баллами“»), а гард ставит флаг на 3200.

    Это не мелочь: «посчитайте мне, пройду ли я» — один из самых частых классов вопросов, и
    каждый такой ответ копил ложный флаг в админке, то есть портил ровно ту метрику, ради
    которой гард заведён. Эхо вопроса выделено в `echoed_numbers` — сигнал не теряется, а
    отделяется от выдумки."""
    seen: set[str] = set()
    out: list[str] = []
    for n in claim_numbers(text):
        if n not in seen and not number_in_context(n, context) and not number_in_context(n, question):
            seen.add(n)
            out.append(n)
    return out


def echoed_numbers(text: str, context: str, question: str = "") -> list[str]:
    """Числа баллов/% из ответа, которых нет в контексте, но которые ЕСТЬ в вопросе.

    Отдельный класс, а не «всё в порядке»: модель повторяет цифру заявителя, и по самому числу
    видно только то, что она пришла не из первоисточника. Выдумкой это не является, поэтому в
    приёмочный флаг не идёт; но в журнал пишется, чтобы класс оставался наблюдаемым."""
    seen: set[str] = set()
    out: list[str] = []
    for n in claim_numbers(text):
        if n not in seen and not number_in_context(n, context) and number_in_context(n, question):
            seen.add(n)
            out.append(n)
    return out


# Сроки в процедурном ответе («10 рабочих дней», «15 календарных дней») — отдельный класс числовых
# претензий, которые unverified_numbers НЕ ловит (там только «балл»/«процент/%»), а риск выдумки на
# процедурной ветке — именно сроки. Проверяем так же: срок незаземлён, если числа нет в контексте
# Правил. НЕ трогаем claim_numbers/unverified_numbers — от их семантики зависят eval_answers.py и тесты.
_DEADLINE_CLAIM_RE = re.compile(rf"({_NUM})\s*(?:рабоч|календарн)\w*\s+дн", re.IGNORECASE)


def unverified_deadlines(text: str, context: str, question: str = "") -> list[str]:
    """Сроки в днях из ОТВЕТА, которых НЕТ в контексте Правил (кандидаты в выдумки процедуры).

    Вопрос — такой же законный источник числа, как контекст (см. `unverified_numbers`):
    «нам обещали 10 рабочих дней, правда?» не должно превращаться во флаг выдуманного срока."""
    seen: set[str] = set()
    out: list[str] = []
    for n in (v.replace(",", ".") for v in _DEADLINE_CLAIM_RE.findall(text)):
        if n not in seen and not number_in_context(n, context) and not number_in_context(n, question):
            seen.add(n)
            out.append(f"{n} дн.")
    return out


# Рантайм-очистка эмодзи/символов-значков из ответа модели (внутренний продукт — без эмодзи).
# Промпт просит их не использовать, но модель изредка добавляет (напр. ⚠); подчищаем гарантированно.
# Диапазоны: emoji, misc symbols/dingbats (⚠ ✓ ✅), доп. символы/стрелки, variation selector.
# Типографику (• « » — … № ™ ®) НЕ трогаем — она вне этих диапазонов.
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️]")


def _strip_emoji(text: str) -> str:
    return _EMOJI_RE.sub("", text)


# --- Мультитёрн: контекстуализация follow-up для ПОИСКА (P1a) -----------------------
# Уточняющий вопрос («а какой порог?», «а для гидравлического?») без своего предмета даёт
# контекст-фри поиск. Переписываем его в самостоятельный запрос по истории — ТОЛЬКО для
# ретрива и гейтов; генерация видит историю диалога отдельно (messages). Ошибка → исходный запрос.
_REWRITE_SYSTEM = (
    "Ты переписываешь уточняющий вопрос пользователя в САМОСТОЯТЕЛЬНЫЙ поисковый запрос по "
    "контексту диалога о продукции и требованиях ПП №719. Если новый вопрос ссылается на "
    "предыдущее (местоимения, «а …», опущенный предмет — «а какой порог?», «а для "
    "микропроцессорного?»), подставь продукт/тему из диалога и верни ПОЛНЫЙ запрос. Если вопрос "
    "уже самодостаточен — верни его без изменений. Ответь ТОЛЬКО текстом запроса, без пояснений."
)
_HAS_CODE_RE = re.compile(r"\d{2}\.\d{2}")


def _needs_context(query: str) -> bool:
    """Дёшево отсеиваем заведомо самодостаточные запросы (свой код ОКПД2 или длинный текст),
    чтобы не звать LLM-переписыватель на каждый ход зря."""
    return not _HAS_CODE_RE.search(query) and len(query) <= 80


def _contextualize(query: str, history: list[dict]) -> str:
    """Follow-up → самостоятельный поисковый запрос по истории. Ошибка → исходный запрос."""
    if not history:
        return query
    try:
        dialog = "\n".join(
            f"{'Пользователь' if m.get('role') == 'user' else 'Ассистент'}: {(m.get('content') or '')[:400]}"
            for m in history[-4:]
        )
        resp = _client().chat.completions.create(
            model=settings.DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": _REWRITE_SYSTEM},
                {"role": "user", "content": f"Диалог:\n{dialog}\n\nНовый вопрос: {query}\n\nСамостоятельный запрос:"},
            ],
            temperature=0,
            max_tokens=120,
        )
        out = (resp.choices[0].message.content or "").strip().strip('"«»')
        return out or query
    except Exception:  # noqa: BLE001 — переписывание не должно ронять ответ
        return query


# --- Мультитёрн: якорь-код диалога (T6) --------------------------------------------
# Если код уже подтверждён в диалоге, а текущий ход — продолжение (ссылается на установленную
# позицию, а не вводит новый продукт), переносим последний код пользователя из истории в поиск.
# Иначе ретрив «уходит» в другие коды — частая жалоба экспертов («код определили, а ИИ ищет другие»).
_USER_CODE_RE = re.compile(r"\b\d{2}\.\d{2}(?:\.\d+)*\b")
# Сильная обратная ссылка на установленную позицию (требования/порог/баллы/операции/документы/это…).
_STRONG_BACKREF_RE = re.compile(
    r"поро[гв]|балл|требовани|операци|неполн|перечень|целиком|состав|дальше|"
    r"это[йгм]?\b|эту\s+позици|по\s+(?:этому|нашему|нему)\s+код|как\s+подтверд|документ",
    re.IGNORECASE,
)
# Голая команда/согласие («да», «покажи», «поясни») — продолжение.
_BARE_CMD_RE = re.compile(
    r"^\W*(?:да|нет|ага|ок|окей|покажи|поясни|распиши|подробнее|продолжай?|дальше|давай)\W*$",
    re.IGNORECASE,
)
# Новый субъект: предлог + НЕ-местоимение (≥3 букв) → вопрос вводит ДРУГОЙ продукт («к насосам»,
# «для станков») — это НЕ продолжение установленной позиции, код-якорь не переносим. Местоимения
# (этой/него/данной/такой/что…) из-под guard'а исключены — «требования для этой позиции» = продолжение.
_NEW_SUBJECT_RE = re.compile(
    r"\b(?:к|для|по|на|про)\s+(?!эт|нег|не[её]|них|наш|данн|указанн|тако|котор|что\b|так\b)\w{3,}",
    re.IGNORECASE,
)


def _is_continuation(query: str) -> bool:
    """Ход продолжает установленную позицию (переносить код-якорь), а не вводит новый продукт."""
    q = (query or "").strip()
    if not q or _HAS_CODE_RE.search(q):
        return False
    if _NEW_SUBJECT_RE.search(q):  # «к насосам»/«для станков» → новый продукт, код не якорим
        return False
    if _BARE_CMD_RE.match(q):
        return True
    return len(q) <= 60 and bool(_STRONG_BACKREF_RE.search(q))


def _anchor_code(history: list[dict] | None) -> str | None:
    """Последний код ОКПД2, НАЗВАННЫЙ ПОЛЬЗОВАТЕЛЕМ в истории (якорь темы диалога)."""
    if not history:
        return None
    for m in reversed(history):
        if m.get("role") != "user":
            continue
        codes = _USER_CODE_RE.findall(m.get("content") or "")
        if codes:
            return codes[-1]
    return None


def _answer_procedural(query: str, search_query: str,
                       history: list[dict] | None = None) -> Answer:
    """Процедурный вопрос → ответ по корпусу «Правила ведения реестра» (коллекция pp719_rules).

    Фолбэк: порядок действий и сроки НЕ выдумываем ни при каких условиях. Две причины —
    два РАЗНЫХ честных сообщения (R4): выключено настройкой → `DEFLECTION_DISABLED` (повтор не
    поможет), корпус недоступен/пуст → `DEFLECTION` (предложить повторить). Иначе генерируем
    grounded-ответ по найденным пунктам + пост-проверка незаземлённых чисел баллов/% И СРОКОВ
    (unverified_deadlines)."""
    from app.rag import followup, procedural, topics

    # Оба дефера уходят БЕЗ подсказки (`input_hint` пуст): процедурная ветка сейчас не отвечает,
    # и предлагать следующий вопрос по ней — обещать то, чего сервис в этот момент не может.
    if not settings.PROCEDURAL_ANSWER_FROM_RULES:
        return Answer(text=procedural.DEFLECTION_DISABLED, hits=[])
    # K12: намерение вопроса задаёт и приоритет документов в окне, и оговорки промпта. Тема
    # определяется детерминированно (регулярки), поэтому маршрутизация бесплатна и воспроизводима.
    topic = topics.classify(search_query)
    rules = search_rules(search_query, limit=RULES_TOP_K, primary_docs=topics.doc_types(topic))
    if not rules:  # Qdrant недоступен / коллекции нет / пусто → честный дефер, а не выдумка процедуры
        return Answer(text=procedural.DEFLECTION, hits=[])

    ctx = format_rules_context(rules)
    user = build_procedural_user_prompt(query, ctx, topic_fragment=topics.fragment(topic))
    messages = [{"role": "system", "content": PROCEDURAL_SYSTEM_PROMPT}]
    if history:  # мультитёрн: процедурный follow-up видит историю диалога
        messages.extend(history)
    messages.append({"role": "user", "content": user})
    resp = _client().chat.completions.create(
        model=settings.DEEPSEEK_MODEL,
        messages=messages,
        temperature=0,  # детерминизм (как товарный путь): стабильный набор шагов/сроков
    )
    usage = resp.usage
    raw = _strip_emoji(resp.choices[0].message.content or "")
    # Незаземлённые числа: баллы/% (общий guard) + СРОКИ в днях (спец. для процедуры).
    # Числа из вопроса источником считаются законным (см. `unverified_numbers`).
    asked = _user_text(query, history)
    ungrounded = unverified_numbers(raw, ctx, asked) + unverified_deadlines(raw, ctx, asked)
    echoed = echoed_numbers(raw, ctx, asked)
    if echoed:
        logger.info("процедурный ответ повторяет числа из вопроса (не выдумка): {}", echoed)
    return Answer(
        text=raw,
        hits=[],
        low_relevance=False,
        unverified_numbers=ungrounded,
        echoed_numbers=echoed,
        prompt_tokens=usage.prompt_tokens if usage else 0,
        completion_tokens=usage.completion_tokens if usage else 0,
        rule_sources=rules,  # те же пункты и в том же порядке, что в контексте [1]…[n] → кликабельные источники
        # U5: тему вопроса уже определил роутер окна (`_topic` в payload) — берём её, а не считаем
        # заново, иначе подсказка и отбор пунктов могли бы разъехаться на одном и том же вопросе.
        input_hint=followup.after_procedural(rules[0].get("_topic")),
    )


def _user_text(query: str, history: list[dict] | None = None) -> str:
    """Всё, что в этом диалоге написал ПОЛЬЗОВАТЕЛЬ — источник чисел наравне с контекстом.

    Берём и историю, а не только текущий вопрос: числа обычно называют один раз («у нас 3200
    баллов»), а спрашивают следующим ходом («так мы пройдём?»). Проверяй мы только текущую
    реплику — эхо снова считалось бы выдумкой, просто на ход позже."""
    parts = [query or ""]
    for m in history or []:
        if m.get("role") == "user":
            parts.append(m.get("content") or "")
    return "\n".join(parts)


@dataclass
class _Plan:
    """План генерации (вся пред-работа сделана) — общий для answer()/answer_stream().
    messages = [system, ...история, текущий вопрос]; grounding — контекст для faithfulness-гарда."""
    messages: list[dict]
    grounding: str
    hits: list[Hit]
    cases: list[dict]
    low_relevance: bool
    # Готовая таблица баллов, которую печатает КОД (P4). Пусто — печатает модель, как раньше.
    points_table: str = ""
    input_hint: str = ""  # U5: подсказка следующего шага, см. Answer.input_hint


def _resolve_tnved(query: str) -> tuple[str, list[str]] | None:
    """(код ТН ВЭД из запроса, его ОКПД2 по переходным ключам) либо None, если кода нет.

    ПУСТОЙ список — тоже результат, а не «ничего не нашли»: код дан, но соответствия в ключе нет.
    Раньше в этом случае механизм молча выключался — позиции подбирались по наименованию, а
    пользователь считал, что ответ дан по его коду. Прямой путь перевода (`translate`) о таком
    говорит честно, товарный молчал."""
    tn = okpd2_ref.extract_tnved(query)
    return (tn, okpd2_ref.tnved_to_okpd2(tn)) if tn else None


def _plan_answer(query: str, okpd2: str | None = None, limit: int = 8,
                 history: list[dict] | None = None) -> "Answer | _Plan":
    """Пред-работа (без финальной генерации): meta → контекстуализация → процедурный гейт →
    поиск/реранк/кейсы → out-of-scope guard → сборка messages. Возвращает либо ранний Answer
    (meta / процедурный / нет-позиции — модель уже не нужна или отработала), либо _Plan для
    LLM-вызова. Общий для answer() (non-stream) и answer_stream() → их поведение ДО вызова
    модели идентично (один и тот же путь).

    limit=8 (не 5): пограничные, но валидные позиции с обобщённым наименованием
    («Прицепы и полуприцепы прочие» 29.20.23) садятся на ранг 5–7 чистого ретрива и при
    limit=5 выпадали из окна на мелкой смене формулировки (ед./мн. число) — модель их не
    видела и ложно отказывала. Окно 8 стабильно вводит их в контекст; лишние кандидаты
    ограничены MAX_OPS_OTHER и служат материалом для уточнения по коду (правило 1г)."""
    # Базовые (meta) реплики (приветствие / что умеешь / как работать) — заготовки без LLM.
    from app.rag import followup, meta
    if meta.is_meta(query):
        return Answer(text=meta.response(query), hits=[], input_hint=followup.ask_for_product())

    # T9: прямой запрос на ПЕРЕВОД кода ТН ВЭД↔ОКПД2 — отвечаем детерминированно из справочника
    # переходных ключей (без LLM: навигатор строго по 719 и на такой вопрос раньше отказывал).
    from app.rag import translate
    if translate.is_translate(query):
        return Answer(text=translate.answer(query), hits=[], input_hint=followup.ask_for_product())

    # Мультитёрн: уточняющий вопрос переписываем в самостоятельный — ТОЛЬКО для поиска/гейтов
    # (генерация ниже видит историю диалога через messages).
    search_query = query
    if history and _needs_context(query):
        search_query = _contextualize(query, history)

    # Процедурный дефер-предохранитель: чистый процедурный вопрос (внесение в реестр, ГИСП,
    # подача заявления, сроки, обжалование) корпусом НЕ покрыт. Деферим детерминированно ДО
    # поиска — без вызова LLM (ноль галлюцинаций/стоимости). Товарные/смешанные вопросы (есть
    # код или товарно-балльный сигнал) сюда не попадают — их обрабатывает обычный пайплайн.
    if settings.PROCEDURAL_DEFLECT_ENABLED:
        from app.rag import procedural
        if procedural.is_procedural(search_query, has_code=bool(okpd2)):
            # Раньше здесь был немедленный дефер. Теперь маршрутизируем на корпус Правил реестра;
            # дефер остаётся ФОЛБЭКОМ внутри _answer_procedural (корпус пуст / ничего не нашлось).
            return _answer_procedural(query, search_query, history)

    # T6: якорь-код диалога — код текущего хода приоритетен; если его нет, а ход продолжает
    # установленную позицию, берём последний код пользователя из истории (не «уходим» в другие коды).
    effective_okpd2 = okpd2
    if effective_okpd2 is None and _is_continuation(query):
        effective_okpd2 = _anchor_code(history)

    # T9: код ТН ВЭД в запросе (из сертификата/декларации) → перевод в ОКПД2 по переходным ключам,
    # затем обычный поиск/проверка в приложении 719. Только если своего кода ОКПД2 нет.
    tnved = _resolve_tnved(query) if effective_okpd2 is None else None
    if tnved and tnved[1]:
        effective_okpd2 = tnved[1][0]  # первый — для иерархического буста ретрива

    # R16: dense-вектор запроса считаем ОДИН раз и переиспользуем во всех обращениях к Qdrant
    # (позиции → подстраховка по коду → кейсы → out-of-scope guard). Раньше e5-large прогонялся
    # на один вопрос 3–4 раза подряд по одному и тому же тексту: на 2 vCPU это сотни мс впустую.
    qvec = embed_query(search_query)
    hits = search(search_query, okpd2=effective_okpd2, limit=limit, qvec=qvec)
    # Реранкер (стадия 2): переупорядочивает top-k через DeepSeek, но ТОЛЬКО при отсутствии
    # совпадения по коду ОКПД2 (код авторитетнее). Поднял recall@1 0.95→0.98 без регресса.
    if settings.RERANK_ENABLED and hits and not any(h.okpd2_match for h in hits):
        from app.rag.reranker import rerank
        hits = rerank(search_query, hits)
    cases = search_cases(search_query, limit=MAX_CASES, qvec=qvec)  # подтверждённые экспертом — высший приоритет
    if not hits and not cases:
        return Answer(
            text="Подходящая позиция в приложении к ПП №719 не найдена. Уточните "
            "наименование продукции или укажите код ОКПД2.",
            hits=[],
            input_hint=followup.ask_for_product(),  # ответ просит назвать продукцию — туда же ведёт подсказка
        )

    # Out-of-scope guard: совпадение по коду ОКПД2 или подтверждённый кейс = высокая
    # уверенность, флаг не поднимаем. Иначе смотрим dense top-1 (один лёгкий запрос).
    #
    # R30: `not cases` снова означает то, что здесь подразумевалось. Раньше `search_cases`
    # возвращала top-3 БЕЗ порога — кейс находился на любой запрос, включая заведомо
    # посторонние, и гасил этот гард. Два предохранителя выключали друг друга: нерелевантный
    # кейс и подмешивался в контекст с высшим приоритетом, и снимал флаг «похоже, вне сферы».
    # Теперь кейс проходит отсечку по dense-косинусу (`CASE_RELEVANCE_MIN`), поэтому сам факт
    # его наличия — уже сигнал уверенности, и подавление флага здесь корректно.
    confident = bool(cases) or any(h.okpd2_match for h in hits)
    low_rel = not confident and dense_top1(search_query, qvec) < RELEVANCE_SOFT  # R16: тот же вектор

    # Второй сигнал (16.08.2026): косинус исчерпан — полоса перекрытия расширилась с 14 тысячных
    # до 44, и пять негативов из 36 проходят порог. Класс ОКПД2 из классификатора — сигнал другой
    # природы: приложение 719 покрывает 20 классов обрабатывающей промышленности, и сельское
    # хозяйство, отходы или перевозки в нём отсутствуют конструктивно. Только ПОДНИМАЕТ флаг
    # (никогда не снимает) и только когда пользователь НЕ назвал код — названный код авторитетнее
    # любого подбора по наименованию.
    if not low_rel and not confident and effective_okpd2 is None:
        from app.rag import scope
        if scope.out_of_scope_by_classifier(search_query):
            logger.info("guard: класс ОКПД2 вне покрытия 719 ({}) — поднимаю флаг релевантности",
                        scope.classifier_divisions(search_query))
            low_rel = True

    # T9: поиск по наименованию без уверенного совпадения (вероятно вне приложения 719) → подсказка
    # кодов ОКПД2 по ВСЕМУ классификатору (для пути СТ-1 / уточнения), помимо позиций 719.
    okpd2_suggestions = None
    if low_rel and effective_okpd2 is None:
        sugg = okpd2_ref.suggest_okpd2_by_name(search_query, k=4)
        if sugg:
            okpd2_suggestions = [(c, n) for c, n, _s in sugg]

    ctx = format_context(hits, search_query)
    cases_ctx = format_cases(cases) if cases else None
    resolved = search_query if search_query != query else None
    # K12: смешанный вопрос — про продукцию И про состав документов. Бинарный роутер считает такие
    # товарными (719-якоря в них нет), и до сих пор ответ советовал «спросите отдельно»: 23 реальных
    # вопроса июльской волны уходили без перечня, хотя это кластер жалоб №1. Тема известна
    # детерминированно, поэтому просто доносим пункты раздела 4 Приказа №52 до контекста.
    docs_ctx = None
    from app.rag import topics
    if topics.classify(search_query) == "documents":
        doc_points = search_rules(search_query, limit=RULES_DOC_POINTS,
                                  primary_docs=topics.doc_types("documents"))
        if doc_points:
            docs_ctx = format_rules_context(doc_points)
    # P4: длинный перечень баллов печатает код, а не модель (см. `points_table`). Промпт об этом
    # обязан знать — иначе перечень задвоится: один раз от модели, второй от нас.
    table = points_table(_target_hit(hits), search_query)
    user = build_navigator_user_prompt(
        query, ctx, effective_okpd2, cases=cases_ctx, low_relevance=low_rel, resolved=resolved,
        suggest_okpd2=(effective_okpd2 is None),  # искал по наименованию → предложить код (запрос эксперта)
        tnved=tnved, okpd2_suggestions=okpd2_suggestions, points_table_appended=bool(table),
        documents=docs_ctx,
    )
    # Генерация видит историю диалога (мультитёрн): messages = [system, ...история, текущий вопрос].
    messages = [{"role": "system", "content": NAVIGATOR_SYSTEM_PROMPT}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user})
    # Блок документов — часть контекста, значит и часть заземления: иначе числа пунктов Приказа
    # («не старше 30 дней») гард объявил бы выдумкой.
    grounding = ctx + ("\n" + cases_ctx if cases_ctx else "") + ("\n" + docs_ctx if docs_ctx else "")
    return _Plan(messages=messages, grounding=grounding, hits=hits, cases=cases,
                 low_relevance=low_rel, points_table=table,
                 input_hint=followup.after_product(low_rel))


def answer(query: str, okpd2: str | None = None, limit: int = 8,
           history: list[dict] | None = None) -> Answer:
    """Синхронный ответ навигатора (non-stream). Пред-работа — в _plan_answer; здесь только
    финальная генерация DeepSeek + faithfulness-постпроверка. Поведение НЕ изменилось при
    выделении _plan_answer — тот же путь, что и раньше (прогнать eval для подтверждения)."""
    planned = _plan_answer(query, okpd2=okpd2, limit=limit, history=history)
    if isinstance(planned, Answer):  # ранний путь (meta / процедурный / нет-позиции)
        return planned
    resp = _client().chat.completions.create(
        model=settings.DEEPSEEK_MODEL,
        messages=planned.messages,
        temperature=0,  # детерминизм (INTERIM-фикс P2): при 0.1 модель на мега-продуктах
        # перечисляла РАЗНЫЕ подмножества операций → числа баллов «плавали» (eval_determinism
        # 0.60). 0 стабилизирует набор чисел; реранкер и так temp=0.
    )
    usage = resp.usage
    # Faithfulness-постпроверка: числа баллов/% из ответа сверяем с контекстом. Незаземлённые
    # НЕ удаляем и НЕ пишем дисклеймер в ответ (внутренний продукт) — но фиксируем в
    # Answer.unverified_numbers: эксперт-admin видит флаг в логах диалогов.
    raw = _strip_emoji(resp.choices[0].message.content or "")
    if planned.points_table:  # P4: длинный перечень баллов печатает код — дословно и одинаково
        raw = raw.rstrip() + "\n" + planned.points_table
    asked = _user_text(query, history)
    ungrounded = unverified_numbers(raw, planned.grounding, asked)
    echoed = echoed_numbers(raw, planned.grounding, asked)
    if echoed:
        logger.info("ответ повторяет числа из вопроса пользователя (не выдумка): {}", echoed)
    return Answer(
        text=raw,
        hits=planned.hits,
        cases=planned.cases,
        low_relevance=planned.low_relevance,
        unverified_numbers=ungrounded,
        echoed_numbers=echoed,
        prompt_tokens=usage.prompt_tokens if usage else 0,
        completion_tokens=usage.completion_tokens if usage else 0,
        input_hint=planned.input_hint,
    )


def answer_stream(query: str, okpd2: str | None = None, limit: int = 8,
                  history: list[dict] | None = None):
    """Стриминг ответа (T18): генератор — yield ('delta', str) по мере генерации, затем
    ('done', Answer) с финальными метаданными (hits→sources / флаги / токены).

    Ранние пути (meta / процедурный / нет-позиции) НЕ стримятся токен-за-токеном — отдаём готовый
    текст одним 'delta' + 'done' (стрим для _answer_procedural — фолоу-ап). LLM-путь: stream=True,
    копим текст; эмодзи чистим по кускам (дисплей = финал), в КОНЦЕ — faithfulness-постпроверка.
    Исключения наружу НЕ глушим — эндпоинт ловит их и сигналит фронту фолбэк на /api/chat."""
    planned = _plan_answer(query, okpd2=okpd2, limit=limit, history=history)
    if isinstance(planned, Answer):  # ранний путь — генерация не нужна / уже сделана
        yield "delta", planned.text
        yield "done", planned
        return
    resp = _client().chat.completions.create(
        model=settings.DEEPSEEK_MODEL,
        messages=planned.messages,
        temperature=0,  # тот же детерминизм, что и non-stream answer()
        stream=True,
        stream_options={"include_usage": True},  # usage приходит финальным чанком (choices пуст)
    )
    parts: list[str] = []
    prompt_tokens = completion_tokens = 0
    for chunk in resp:
        usage = getattr(chunk, "usage", None)
        if usage:
            prompt_tokens = usage.prompt_tokens
            completion_tokens = usage.completion_tokens
        if not chunk.choices:
            continue
        piece = _strip_emoji(chunk.choices[0].delta.content or "")
        if piece:
            parts.append(piece)
            yield "delta", piece
    raw = "".join(parts)
    if planned.points_table:
        # Таблицу отдаём последним куском потока: фронт дорисует её тем же рендером markdown,
        # что и остальной ответ, а копирование/экспорт заберут её вместе с текстом.
        tail = "\n" + planned.points_table
        raw = raw.rstrip() + tail
        yield "delta", tail
    asked = _user_text(query, history)
    ungrounded = unverified_numbers(raw, planned.grounding, asked)
    echoed = echoed_numbers(raw, planned.grounding, asked)
    if echoed:
        logger.info("ответ повторяет числа из вопроса пользователя (не выдумка): {}", echoed)
    yield "done", Answer(
        text=raw,
        hits=planned.hits,
        cases=planned.cases,
        low_relevance=planned.low_relevance,
        unverified_numbers=ungrounded,
        echoed_numbers=echoed,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        input_hint=planned.input_hint,
    )


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit('Использование: python -m app.rag.pipeline "вопрос" [код ОКПД2]')
    query = sys.argv[1]
    okpd2 = sys.argv[2] if len(sys.argv) > 2 else None
    ans = answer(query, okpd2)
    print("=" * 70)
    print(f"ВОПРОС: {query}" + (f"  [ОКПД2 {okpd2}]" if okpd2 else ""))
    print("=" * 70)
    print("\nНАЙДЕНО:")
    for i, h in enumerate(ans.hits, 1):
        mark = "✓код" if h.okpd2_match else "    "
        print(f"  {i}. [{mark}][{h.section_roman}] {h.product_name[:60]} (score={h.score:.3f})")
    print("\nОТВЕТ:\n")
    print(ans.text)


if __name__ == "__main__":
    main()
