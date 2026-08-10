"""RAG-пайплайн навигатора: запрос → гибрид-поиск → контекст → DeepSeek → ответ.

Ответ всегда снабжён обязательной пометкой (AI — черновик, вердикт за экспертом ТПП).
Запуск как смоук (нужен поднятый Qdrant с коллекцией и DEEPSEEK_API_KEY в .env):
  .venv/Scripts/python.exe -m app.rag.pipeline "производим прицепы для легковых авто" 29.20.23
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from functools import lru_cache

from app.core.config import settings
from app.core.prompts import (
    NAVIGATOR_SYSTEM_PROMPT,
    PROCEDURAL_SYSTEM_PROMPT,
    build_navigator_user_prompt,
    build_procedural_user_prompt,
)
from app.rag import okpd2_ref, sparse
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
MAX_OPS_PER_HIT = MAX_OPS_TARGET  # обратная совместимость (test_rag / eval_truncation берут как дефолт)
MAX_CASES = 3  # сколько подтверждённых кейсов подмешивать в контекст
RULES_TOP_K = 6  # сколько пунктов Правил реестра тянуть для процедурного ответа (проза → синтез из нескольких)
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
    prompt_tokens: int = 0  # токены DeepSeek за ответ (учёт затрат в админ-логах)
    completion_tokens: int = 0
    # Пункты первоисточников процедурного ответа (Правила/тело ПП №719/Приказ №52) в порядке [n] —
    # для кликабельных источников. Товарный путь их не заполняет (там источники строятся из hits).
    rule_sources: list[dict] = field(default_factory=list)


def _hit_operations(h: Hit) -> list[dict]:
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
    (без балльной оценки)», как и положено требованию без балльной оценки."""
    ops: list[dict] = []
    for b in h.requirement_blocks:
        block_ops = b.get("operations") or []
        if block_ops:
            ops.extend(block_ops)
            continue
        comp = (b.get("component") or "").strip()
        if comp:
            ops.append({"text": comp, "points": None})
    return ops


def _rank_operations(ops: list[dict], query: str | None) -> list[dict]:
    """Переставляет операции так, чтобы релевантные запросу шли первыми.

    Нужно для мега-продуктов (сотни операций): усечение до MAX_OPS_PER_HIT иначе режет
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
        if mt:
            lines.append(f"    Порог: {mt}")
        ops = _hit_operations(h)
        total = len(ops)
        if total > cap:
            ops = _rank_operations(ops, query)
        shown = ops[:cap]
        if shown:
            lines.append("    Ключевые операции:")
            for o in shown:
                pts = o.get("points")
                ptxt = f" — {pts} балл." if pts is not None else " — баллы в контексте не указаны"
                lines.append(f"      • {o.get('text', '')}{ptxt}")
            if total > cap:
                rel = " (показаны наиболее релевантные запросу)" if query else ""
                lines.append(
                    f"      СПИСОК ОПЕРАЦИЙ НЕПОЛНЫЙ: показаны {len(shown)} из {total} операций"
                    f"{rel}; полный перечень требований и баллов — в первоисточнике ПП №719 (этот раздел)."
                )
        if h.source_anchor:
            lines.append(f"    Источник: {h.source_anchor}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


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
        if len(text) > RULES_TEXT_CAP:
            text = text[:RULES_TEXT_CAP].rstrip() + " …(пункт приведён не полностью; полный текст — в первоисточнике)"
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


def unverified_numbers(text: str, context: str) -> list[str]:
    """Числа баллов/% из ОТВЕТА, которых НЕТ в контексте (кандидаты в галлюцинации).

    Консервативно: число незаземлено, только если в контексте его НЕТ вовсе (это занижает,
    а не завышает — ложноположительных нет). Уникальные значения в порядке появления."""
    seen: set[str] = set()
    out: list[str] = []
    for n in claim_numbers(text):
        if n not in seen and not number_in_context(n, context):
            seen.add(n)
            out.append(n)
    return out


# Сроки в процедурном ответе («10 рабочих дней», «15 календарных дней») — отдельный класс числовых
# претензий, которые unverified_numbers НЕ ловит (там только «балл»/«процент/%»), а риск выдумки на
# процедурной ветке — именно сроки. Проверяем так же: срок незаземлён, если числа нет в контексте
# Правил. НЕ трогаем claim_numbers/unverified_numbers — от их семантики зависят eval_answers.py и тесты.
_DEADLINE_CLAIM_RE = re.compile(rf"({_NUM})\s*(?:рабоч|календарн)\w*\s+дн", re.IGNORECASE)


def unverified_deadlines(text: str, context: str) -> list[str]:
    """Сроки в днях из ОТВЕТА, которых НЕТ в контексте Правил (кандидаты в выдумки процедуры)."""
    seen: set[str] = set()
    out: list[str] = []
    for n in (v.replace(",", ".") for v in _DEADLINE_CLAIM_RE.findall(text)):
        if n not in seen and not number_in_context(n, context):
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
    from app.rag import procedural

    if not settings.PROCEDURAL_ANSWER_FROM_RULES:
        return Answer(text=procedural.DEFLECTION_DISABLED, hits=[])
    rules = search_rules(search_query, limit=RULES_TOP_K)
    if not rules:  # Qdrant недоступен / коллекции нет / пусто → честный дефер, а не выдумка процедуры
        return Answer(text=procedural.DEFLECTION, hits=[])

    ctx = format_rules_context(rules)
    user = build_procedural_user_prompt(query, ctx)
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
    ungrounded = unverified_numbers(raw, ctx) + unverified_deadlines(raw, ctx)
    return Answer(
        text=raw,
        hits=[],
        low_relevance=False,
        unverified_numbers=ungrounded,
        prompt_tokens=usage.prompt_tokens if usage else 0,
        completion_tokens=usage.completion_tokens if usage else 0,
        rule_sources=rules,  # те же пункты и в том же порядке, что в контексте [1]…[n] → кликабельные источники
    )


@dataclass
class _Plan:
    """План генерации (вся пред-работа сделана) — общий для answer()/answer_stream().
    messages = [system, ...история, текущий вопрос]; grounding — контекст для faithfulness-гарда."""
    messages: list[dict]
    grounding: str
    hits: list[Hit]
    cases: list[dict]
    low_relevance: bool


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
    from app.rag import meta
    if meta.is_meta(query):
        return Answer(text=meta.response(query), hits=[])

    # T9: прямой запрос на ПЕРЕВОД кода ТН ВЭД↔ОКПД2 — отвечаем детерминированно из справочника
    # переходных ключей (без LLM: навигатор строго по 719 и на такой вопрос раньше отказывал).
    from app.rag import translate
    if translate.is_translate(query):
        return Answer(text=translate.answer(query), hits=[])

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
    tnved = None
    if effective_okpd2 is None:
        tn = okpd2_ref.extract_tnved(query)
        if tn:
            tn_okpd2 = okpd2_ref.tnved_to_okpd2(tn)
            if tn_okpd2:
                effective_okpd2 = tn_okpd2[0]  # первый — для иерархического буста ретрива
                tnved = (tn, tn_okpd2)

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
        )

    # Out-of-scope guard: совпадение по коду ОКПД2 или подтверждённый кейс = высокая
    # уверенность, флаг не поднимаем. Иначе смотрим dense top-1 (один лёгкий запрос).
    low_rel = (
        not cases
        and not any(h.okpd2_match for h in hits)
        and dense_top1(search_query, qvec) < RELEVANCE_SOFT  # R16: тот же вектор, без пере-эмбеддинга
    )

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
    user = build_navigator_user_prompt(
        query, ctx, effective_okpd2, cases=cases_ctx, low_relevance=low_rel, resolved=resolved,
        suggest_okpd2=(effective_okpd2 is None),  # искал по наименованию → предложить код (запрос эксперта)
        tnved=tnved, okpd2_suggestions=okpd2_suggestions,
    )
    # Генерация видит историю диалога (мультитёрн): messages = [system, ...история, текущий вопрос].
    messages = [{"role": "system", "content": NAVIGATOR_SYSTEM_PROMPT}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user})
    grounding = ctx + ("\n" + cases_ctx if cases_ctx else "")
    return _Plan(messages=messages, grounding=grounding, hits=hits, cases=cases, low_relevance=low_rel)


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
    ungrounded = unverified_numbers(raw, planned.grounding)
    return Answer(
        text=raw,
        hits=planned.hits,
        cases=planned.cases,
        low_relevance=planned.low_relevance,
        unverified_numbers=ungrounded,
        prompt_tokens=usage.prompt_tokens if usage else 0,
        completion_tokens=usage.completion_tokens if usage else 0,
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
    ungrounded = unverified_numbers(raw, planned.grounding)
    yield "done", Answer(
        text=raw,
        hits=planned.hits,
        cases=planned.cases,
        low_relevance=planned.low_relevance,
        unverified_numbers=ungrounded,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
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
