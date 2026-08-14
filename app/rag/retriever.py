"""Гибридный поиск по базе знаний ПП №719 в Qdrant (dense e5 + sparse BM25, RRF).

Возвращает наиболее релевантные позиции приложения для запроса. Если передан код
ОКПД2 — позиции с совпадающим (по иерархии) кодом поднимаются наверх (буст), а при
наличии точных совпадений гарантированно добавляются в выдачу (жёсткая подстраховка).

См. docs/V1_SPEC.md, Шаг 2–3. Используется из app/rag/pipeline.py и API.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from loguru import logger

from app.core.config import settings
from app.rag.embeddings import embed_query
from app.rag.sparse import query_vector

DENSE = "dense"
SPARSE = "bm25"


@dataclass
class Hit:
    score: float
    section_roman: str
    section_title: str
    product_name: str
    okpd2_codes: list[str]
    min_threshold: str | None
    requirement_blocks: list[dict]
    source_anchor: str | None
    okpd2_match: bool = False
    payload: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# ОКПД2: иерархическое сопоставление кодов
# --------------------------------------------------------------------------- #
def _segments(code: str) -> list[str]:
    return [s for s in code.strip().replace(" ", "").split(".") if s]


def okpd2_match(rec_codes: list[str], query_code: str) -> bool:
    """True, если код записи и код запроса лежат на одной ветке ОКПД2 (один — префикс
    другого посегментно). Так «29.20.23.110» матчит группу «29.20.23», а «29.20» —
    более частную «29.20.23»."""
    q = _segments(query_code)
    if not q:
        return False
    for rc in rec_codes:
        r = _segments(rc)
        n = min(len(q), len(r))
        if n and q[:n] == r[:n]:
            return True
    return False


def _prefixes(code: str) -> list[str]:
    segs = _segments(code)
    return [".".join(segs[: i + 1]) for i in range(len(segs))]


# --------------------------------------------------------------------------- #
# Поиск
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def _client():
    from qdrant_client import QdrantClient

    return QdrantClient(url=settings.QDRANT_URL, timeout=30)


def _clean_section_title(title: str | None) -> str:
    """Название раздела без задвоения (R28).

    В части записей `section_title` содержит название дважды через перенос строки
    («Продукция судостроения\\n\\nXVIII. Продукция судостроения») — дефект нарезки заголовка,
    исправленный в `structure_kb.section_title`. Здесь подчищаем ЕЩЁ РАЗ, в рантайме: индекс
    Qdrant переиндексируется не сразу, а искажённое название уходит эксперту в ответ."""
    first = (title or "").strip().split("\n")[0].lstrip("#").strip()
    left, dot, right = first.partition(".")
    if dot and left.strip().isupper() and left.strip("IVXLC ") == "":
        first = right.strip()
    return first


def _to_hit(p) -> Hit:
    pl = p.payload or {}
    return Hit(
        score=p.score,
        section_roman=pl.get("section_roman", "?"),
        section_title=_clean_section_title(pl.get("section_title")),
        product_name=pl.get("product_name", ""),
        okpd2_codes=pl.get("okpd2_codes") or [],
        min_threshold=pl.get("min_threshold"),
        requirement_blocks=pl.get("requirement_blocks") or [],
        source_anchor=pl.get("source_anchor"),
        payload=pl,
    )


# M1: точный (переборный) поиск вместо приближённого HNSW. Только для ЗАМЕРОВ.
#
# Зачем. `eval_coverage` на неизменном индексе и одной команде давал разные числа: четыре прогона
# подряд 12.08.2026 дали recall@1 = 0.933 / 0.937 / 0.941 / 0.941, коллизии гуляли 80–92. Причина —
# приближённый HNSW плюс слияние RRF: на 1380 точках с множеством сиблингов-вариантов top-1 среди
# почти равных кандидатов перескакивает между прогонами. Разброс ±0.005 оказался БОЛЬШЕ разниц,
# по которым принимались решения, — то есть метрика мерила себя, а не изменения.
#
# Почему не включаем в проде: точный поиск перебирает всю коллекцию. В горячем пути это лишняя
# латентность на 2 vCPU без всякой пользы — пользователю нужен хороший ответ, а не воспроизводимый
# до третьего знака. Замер же не в горячем пути, и ему нужна именно повторяемость.
EXACT_SEARCH = False


def _search_params():
    """Параметры поиска Qdrant: точный перебор при `EXACT_SEARCH`, иначе дефолтный HNSW."""
    if not EXACT_SEARCH:
        return None
    from qdrant_client import models
    return models.SearchParams(exact=True)


def _hybrid(query: str, limit: int, qfilter=None, collection: str | None = None,
            qvec: list[float] | None = None):
    """Гибрид dense+sparse с RRF. `qvec` — заранее посчитанный dense-вектор запроса: на один
    вопрос к Qdrant уходит несколько обращений (позиции + подстраховка по коду + кейсы + guard),
    и e5-large незачем прогонять на каждое. Не передан — считаем сами (обратная совместимость:
    eval-скрипты зовут search/search_cases напрямую).

    При `EXACT_SEARCH` (только замеры, см. M1) обе ветви префетча идут точным перебором —
    иначе число «плавает» между прогонами сильнее, чем измеряемая разница."""
    from qdrant_client import models

    dvec = qvec if qvec is not None else embed_query(query)
    idx, val = query_vector(query)  # sparse дёшев (mmh3+Snowball локально) — переиспользовать нечего
    sp = _search_params()
    res = _client().query_points(
        collection_name=collection or settings.QDRANT_COLLECTION,
        prefetch=[
            models.Prefetch(query=dvec, using=DENSE, limit=max(limit, 20), filter=qfilter,
                            params=sp),
            models.Prefetch(
                query=models.SparseVector(indices=idx, values=val),
                using=SPARSE,
                limit=max(limit, 20),
                filter=qfilter,
                params=sp,
            ),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=limit,
        with_payload=True,
    )
    return res.points


def search(query: str, okpd2: str | None = None, limit: int = 5, pool: int = 40,
           qvec: list[float] | None = None) -> list[Hit]:
    """Гибридный поиск. При наличии okpd2 — буст совпадающих по коду позиций и
    жёсткая подстраховка (отдельный фильтрованный запрос по префиксам кода).

    `qvec` — заранее посчитанный вектор запроса (см. `_hybrid`). Если не передан, считаем ОДИН
    раз здесь и переиспользуем в подстраховке по коду — раньше второй запрос эмбеддил тот же
    текст заново."""
    from qdrant_client import models

    if qvec is None:
        qvec = embed_query(query)
    hits = [_to_hit(p) for p in _hybrid(query, pool, qvec=qvec)]

    if okpd2:
        for h in hits:
            h.okpd2_match = okpd2_match(h.okpd2_codes, okpd2)

        # Ветку кода (родители + дети) подтягиваем ВСЕГДА, а не только при отсутствии совпадений:
        # пул мог включить родителя (26.51), но пропустить дочерние позиции (26.51.52.120). Две ветви
        # OR: РОДИТЕЛИ — код записи равен префиксу запроса (okpd2_codes ∋ один из префиксов); ДЕТИ/сам —
        # префикс-набор записи содержит код запроса (okpd2_prefixes ∋ okpd2), т.е. класс-код «26.51.52»
        # находит «26.51.52.120» (T5, поиск по частичному коду).
        prefixes = _prefixes(okpd2)
        if prefixes:
            qfilter = models.Filter(should=[
                models.FieldCondition(key="okpd2_codes", match=models.MatchAny(any=prefixes)),
                models.FieldCondition(key="okpd2_prefixes", match=models.MatchAny(any=[okpd2.strip()])),
            ])
            seen = {h.source_anchor for h in hits}
            for p in _hybrid(query, max(limit, 12), qfilter=qfilter, qvec=qvec):
                h = _to_hit(p)
                if h.source_anchor not in seen:
                    h.okpd2_match = True
                    seen.add(h.source_anchor)
                    hits.append(h)

        # Стабильная пересортировка: совпавшие по коду — выше, далее по score.
        hits.sort(key=lambda h: (not h.okpd2_match, -h.score))

    return hits[:limit]


def dense_top1(query: str, qvec: list[float] | None = None) -> float:
    """Косинусное сходство top-1 по ЧИСТО dense-поиску (e5). Сигнал релевантности для
    out-of-scope guard: на продукции вне 719 оно стабильно ниже, чем на профильной
    (калибровка — `docs/eval_report.md`). `qvec` — заранее посчитанный вектор запроса
    (чтобы не эмбедить дважды), иначе считаем сами."""
    dvec = qvec if qvec is not None else embed_query(query)
    res = _client().query_points(
        collection_name=settings.QDRANT_COLLECTION,
        query=dvec,
        using=DENSE,
        limit=1,
        with_payload=False,
    )
    return float(res.points[0].score) if res.points else 0.0


# Код ОКПД2 в тексте запроса. Дубль регулярки из app/tools/navigator, чтобы retriever
# (нижний слой) не тянул зависимость от tools (верхний) — иначе импорт закольцуется.
_CODE_IN_TEXT = re.compile(r"\b\d{2}\.\d{2}(?:\.\d+)*\b")


def _case_code_hit(query: str, payload: dict) -> bool:
    """Назван ли в запросе код ОКПД2, попадающий в ветку кода этого кейса.

    Зачем отдельный путь мимо dense-порога: код — детерминированный сигнал, и он
    АВТОРИТЕТНЕЕ семантики (тот же принцип, что у товарного ОКПД2-буста). Замер показал,
    что без этого пути порог срезает ровно те кейсы, ради которых заводился лексический
    канал: «что означает код 27.40.33.130» давал dense 0.847 и отсекался, хотя код в
    запросе совпадает с кодом кейса буквально."""
    code = payload.get("okpd2")
    if not code:
        return False
    return any(okpd2_match([str(code)], m) or okpd2_match([m], str(code))
               for m in _CODE_IN_TEXT.findall(query or ""))


def _case_dense_scores(qvec: list[float], probe: int) -> dict:
    """id точки → ЧИСТЫЙ dense-косинус по коллекции кейсов (для порога релевантности, R30).

    Отдельный лёгкий запрос: фьюжн-score из `_hybrid` мерит РАНГ, а не близость, и для отсечки
    непригоден. Коллекция кейсов мала (десятки записей), поэтому проба с запасом дешева."""
    res = _client().query_points(
        collection_name=settings.QDRANT_CASES_COLLECTION,
        query=qvec, using=DENSE, limit=probe, with_payload=False,
    )
    return {p.id: float(p.score) for p in res.points}


def search_cases(query: str, limit: int = 3, qvec: list[float] | None = None) -> list[dict]:
    """Поиск по подтверждённым экспертом кейсам (коллекция verified_cases).

    Возвращает payload'ы кейсов со score в `_score` и dense-косинусом в `_dense`. Если коллекции
    нет, она пуста ИЛИ ни один кейс не прошёл порог релевантности — пустой список (петля кейсов
    опциональна, база работает и без неё). `qvec` — тот же вектор запроса, что и у товарного
    поиска (см. `_hybrid`).

    ПОРОГ РЕЛЕВАНТНОСТИ (R30) — почему он здесь обязателен. Кейс уходит в контекст с ВЫСШИМ
    приоритетом (правило 1а промпта навигатора), выше первоисточника. Поэтому нерелевантный
    кейс — не безобидный шум, а правдоподобная дезинформация: на «оказываем юридические услуги»
    подтягивался кейс про НИОКР грузового автотранспорта, а на веб-студию — кейс «нет в
    приложении → идите путём СТ-1». Плюс наличие кейса гасит out-of-scope-гард в пайплайне,
    так что два предохранителя выключали друг друга.

    Отсекаем по ЧИСТОМУ dense-косинусу, а НЕ по фьюжн-score: последний — результат RRF-слияния
    рангов, на маленькой коллекции что-то всегда оказывается первым с нормированным 1.0, и
    абсолютной релевантности в нём нет. На dense-косинусе полоса чистая (замер 12.08.2026,
    12 кейсов): уместные — от 0.839, посторонние — до 0.803. Порог `CASE_RELEVANCE_MIN`.
    Ранжирование при этом остаётся гибридным — лексический канал нужен, чтобы кейс находился
    по коду ОКПД2 и точным терминам.

    Сам запрос под try: кейсы — НЕОБЯЗАТЕЛЬНОЕ обогащение контекста, их сбой не должен
    ронять уже найденный товарный ответ (раньше исключение из `_hybrid` улетало наверх)."""
    name = settings.QDRANT_CASES_COLLECTION
    try:
        client = _client()
        if not client.collection_exists(name):
            return []
        dvec = qvec if qvec is not None else embed_query(query)
        points = _hybrid(query, limit, collection=name, qvec=dvec)
        if not points:
            return []
        # проба с запасом: гибрид мог поднять запись, которой нет в dense-топе того же размера
        dense = _case_dense_scores(dvec, probe=max(limit * 8, 64))
    except Exception as e:  # noqa: BLE001 — Qdrant недоступен: не валим основной поиск
        # но НЕ молча: беззвучный отказ здесь выключает всю петлю обучения, и снаружи это
        # неотличимо от «подходящих кейсов не нашлось». На таком молчании проект уже горел
        # (classifiers/*.tsv в T9, POSIX-путь graphify).
        logger.warning("петля кейсов недоступна, отвечаем без неё: {}: {}", type(e).__name__, e)
        return []
    out: list[dict] = []
    for p in points:
        pl = dict(p.payload or {})
        cos = dense.get(p.id)
        by_code = _case_code_hit(query, pl)
        if not by_code and (cos is None or cos < settings.CASE_RELEVANCE_MIN):
            continue  # не показать лучше, чем показать чужое
        pl["_score"] = p.score
        pl["_dense"] = cos
        pl["_by_code"] = by_code
        out.append(pl)
    return out


# --------------------------------------------------------------------------- #
# K10: тема процедурного вопроса → приоритетный документ корпуса Правил
# --------------------------------------------------------------------------- #
# Три документа корпуса `pp719_rules` имеют разную зону ответственности, и запрос почти всегда
# адресован одному из них. Детерминированно (регулярками) — как и остальные роутеры проекта:
# бесплатно, воспроизводимо, покрыто тестами, не способно выдумать.
#
# Подсчёт совпадений, а НЕ первое сработавшее правило. Порядок правил хрупок: запрос «что делать
# при отказе ТПП в выдаче акта экспертизы» законно задевает и Приказ, и Правила, и при
# первом-совпавшем результат зависел бы от того, в каком порядке я перечислил документы.
# Побеждает документ с бо́льшим числом попаданий; при ничьей темы нет — и это честно, потому что
# квота тогда просто раздаёт места поровну.
_RULES_TOPIC: tuple[tuple[str, tuple[re.Pattern, ...]], ...] = (
    # Приказ ТПП №52: состав документов, акт экспертизы, формы заявок и приложений
    ("tpp_order_52", (
        re.compile(r"как(ие|ой)?\s+документ|состав\s+документ|перечень\s+документ|пакет\s+документ", re.I),
        re.compile(r"акт\w*\s+экспертиз|экспертиз|оценк\w*\s+тпп", re.I),
        re.compile(r"ст-?1|сертификат\w*\s+о\s+происхожд", re.I),
        re.compile(r"что\s+(нужно|надо)\s+(собрать|подготовить|предостав|указ)", re.I),
        re.compile(r"формирова\w+\s+заявк|заполн|форм\w*\s+заявк|приложени\w*\s+к\s+положени", re.I),
        re.compile(r"сведени\w*\s+о\s+(производител|производственн)|производственн\w*\s+площадк", re.I),
        re.compile(r"объ[её]м\w*\s+производств|отгрузк|компонент", re.I),
    )),
    # Правила ведения реестра: подача, сроки, реестровая запись, изменения, отказы, каталог
    ("rules_registry", (
        re.compile(r"срок|рассмотрен|доработк", re.I),
        re.compile(r"подач|подать|заявлени\w*\s+о\s+включ", re.I),
        re.compile(r"реестров(ая|ой|ую)\s+запис|внесени\w*\s+изменени|как\s+внести", re.I),
        re.compile(r"гисп|каталог\w*\s+продукц|выписк|единый\s+реестр", re.I),
        re.compile(r"отказ|порядок\s+(включени|внесени|формирован|ведени)", re.I),
        re.compile(r"предоставлени\w*\s+сведени|радиоэлектронн\w*\s+продукц", re.I),
    )),
    # Тело постановления: критерии подтверждения производства (пункт 1, подпункты а–д)
    ("decree_body", (
        re.compile(r"критери|подпункт|пункт\w*\s*1\b|постановлени\w*\s+(предусм|устанавл|говорит)", re.I),
        re.compile(r"подтвержд\w*\s+(производств|соответстви|наличи)", re.I),
        re.compile(r"специальн\w*\s+инвестиционн\w*\s+контракт|спик", re.I),
        re.compile(r"уполномочен\w*\s+(орган|федеральн)|минпромторг", re.I),
        re.compile(r"стади\w*\s+(технологическ\w*\s+)?процесс\w*\s+производств|лекарственн", re.I),
        re.compile(r"требовани\w*,?\s+предусмотренн\w*\s+приложени|отнесени\w*\s+к\s+российск", re.I),
    )),
    # Сноски приложения: «что означает <44>», «сноска 6 к требованию» (D3)
    ("appendix_footnotes", (
        re.compile(r"сноск|<\s*\d{1,2}(?:\.\d)?\s*>", re.I),
        re.compile(r"что\s+(означа|значит)\w*\s+(значок|обозначени|отсылк)", re.I),
    )),
)

RULES_QUOTA_MIN = 1  # мест, гарантированных КАЖДОМУ документу, у которого есть кандидаты
RULES_QUOTA_PRIMARY = 3  # мест, гарантированных документу по теме вопроса
# Документы «по запросу»: место в окне НЕ резервируется, пока документ не стал темой вопроса.
# Сноски — не процедурная норма: они уточняют требование и нужны ровно тогда, когда спросили про
# сноску. Дай им гарантированное место наравне с Правилами — и на КАЖДОМ процедурном вопросе одно
# из шести мест уходило бы определению сноски, вытесняя норму. По общему рангу они конкурируют
# на общих основаниях: если сноска действительно релевантна, она войдёт в окно и без квоты.
RULES_QUOTA_ON_DEMAND = frozenset({"appendix_footnotes"})

# P2: вопрос «какие документы готовить» — кластер жалоб №1 июльского теста (31 упоминание).
# Квота K10 доводит до окна нужный ДОКУМЕНТ (Приказ №52), но внутри него отбор идёт общим рангом,
# и раздел 4 («Документы, необходимые для получения акта экспертизы…») проигрывает: его пункты
# длинные (4.1 — 3842 знака, BM25 штрафует длину), а короткие пункты про сроки, печати и
# электронную подпись содержат те же слова «документы» и «акт экспертизы». Итог — ответ честно
# сообщал, что «перечень содержится в разделе 4 (в контексте не представлен)».
_ASKS_DOC_LIST = (
    re.compile(r"как\w*\s+документ|перечен\w*\s+документ|список\s+документ|состав\w*\s+документ", re.I),
    re.compile(r"пакет\s+документ|как\w*\s+бумаг|что\s+(нужно\s+)?(подготовит|приложит|представит|предостав)", re.I),
    re.compile(r"документ\w*\s+(для|необходим\w*\s+для)\s+(получени|подтвержден|подач)", re.I),
)
# Раздел Приказа №52 с самим перечнем и места, гарантированные ему в окне.
RULES_DOC_LIST_SECTION = "4"
RULES_QUOTA_DOC_LIST = 3
# Ответ «какие документы» собирается из ТРЁХ разных частей раздела 4, и брать просто топ-3 по
# релевантности нельзя: выигрывают 4.1 и 4.4 (в них есть слова «акт экспертизы»), а сам перечень
# приложений лежит в 4.2.x, где таких слов нет — там «копия устава», «выписка из ЕГРЮЛ».
# Поэтому берём ЛУЧШИЙ пункт из каждой подгруппы: заявка и сведения в ней · приложения к заявке ·
# документы под конкретные критерии 719.
RULES_DOC_LIST_GROUPS = ("4.1", "4.2", "4.3")


def asks_document_list(query: str) -> bool:
    """Вопрос о СОСТАВЕ документов — тот класс, ради которого держим раздел 4 Приказа №52."""
    q = query or ""
    return any(p.search(q) for p in _ASKS_DOC_LIST)


def rules_topic(query: str) -> str | None:
    """`doc_type` документа, которому адресован процедурный вопрос, либо None при ничьей.

    Побеждает документ с наибольшим числом сработавших правил. Ничья → None: навязывать тему
    при равных признаках хуже, чем не навязывать, — квота тогда раздаёт места поровну, и решает
    релевантность, а не мой порядок в списке."""
    q = query or ""
    scores = {dt: sum(1 for p in pats if p.search(q)) for dt, pats in _RULES_TOPIC}
    best = max(scores.values(), default=0)
    if not best:
        return None
    winners = [dt for dt, s in scores.items() if s == best]
    return winners[0] if len(winners) == 1 else None


def search_rules(query: str, limit: int = 6, qvec: list[float] | None = None) -> list[dict]:
    """Гибрид-поиск по корпусу «Правила ведения реестра» (отдельная коллекция pp719_rules).

    Возвращает payload'ы пунктов со score в `_score` и темой запроса в `_topic`. Если коллекции
    нет или Qdrant недоступен — пустой список (процедурный путь тогда честно деферится).
    БЕЗ ОКПД2-буста и реранкера (то и другое заточено под товарные записи, не под прозу норм).

    K10 — КВОТА НА ИСТОЧНИК, и вот зачем. В коллекции три документа разной юридической силы и
    разной зоны ответственности, а выдача была плоским top-k без единого фильтра — при том что
    индекс по `doc_type` создан в `recreate_collection` и не использовался ни разу. Приказ ТПП
    №52 занимает 172 пункта из 244 (71 % корпуса) и потому вытеснял остальных: замер 12.08.2026
    на пяти контрольных запросах дал ему **24 места из 30**, а на вопросе «сроки рассмотрения
    заявления» — **все 6**, хотя сроки устанавливают Правила ведения реестра. Это прямо бьёт по
    самому частому кластеру претензий июльского теста (состав документов и процедура, 31 упоминание).

    Механика: берём пул кандидатов шире окна, затем гарантируем каждому документу с кандидатами
    `RULES_QUOTA_MIN` мест, а документу по теме вопроса — `RULES_QUOTA_PRIMARY`. Остаток окна
    добираем по общему рангу. Итог сортируется обратно по рангу, чтобы сильнейший пункт остался
    первым — квота меняет СОСТАВ окна, а не порядок внутри него.

    Отказоустойчивость (R4): САМ ЗАПРОС тоже под try — иначе падение Qdrant в момент поиска
    улетало исключением наверх и превращалось в 503, а честный процедурный дефер (ради которого
    эта ветка и возвращает пустой список) был недостижим."""
    name = settings.QDRANT_RULES_COLLECTION
    try:
        client = _client()
        if not client.collection_exists(name):
            return []
        # пул шире окна: из него квота набирает представителей каждого документа
        points = _hybrid(query, max(limit * 4, 24), collection=name, qvec=qvec)
    except Exception as e:  # noqa: BLE001 — Qdrant недоступен: не валим ответ, деферим
        logger.warning("корпус Правил недоступен, процедурный ответ деферится: {}: {}",
                       type(e).__name__, e)
        return []
    if not points:
        return []

    primary = rules_topic(query)
    by_doc: dict[str, list[int]] = {}
    for i, p in enumerate(points):
        by_doc.setdefault((p.payload or {}).get("doc_type") or "—", []).append(i)

    # K9 показал дыру в квоте: она раздаёт места только тем документам, что попали в ПУЛ.
    # Тело постановления — 14 пунктов из 244, оно в широкий пул часто не доходит, и вопрос
    # «что говорит подпункт г пункта 1» не получал его вовсе (0 мест из 6) — при том что это
    # вторая по частоте претензия июля. Добираем тематический документ отдельным запросом
    # с фильтром, как товарный поиск добирает позиции по коду ОКПД2.
    if primary and primary not in by_doc:
        try:
            from qdrant_client import models
            extra = _hybrid(query, RULES_QUOTA_PRIMARY, collection=name, qvec=qvec,
                            qfilter=models.Filter(must=[models.FieldCondition(
                                key="doc_type", match=models.MatchValue(value=primary))]))
            for p in extra:
                by_doc.setdefault(primary, []).append(len(points))
                points.append(p)
        except Exception as e:  # noqa: BLE001 — подстраховка необязательна, не валим ответ
            logger.warning("добор темы «{}» не удался: {}: {}", primary, type(e).__name__, e)

    # P2: на вопрос о составе документов раздел 4 Приказа №52 добираем отдельным запросом с
    # фильтром — ровно так же, как выше добирается тематический документ. Без этого перечень в
    # окно не попадал: его пункты длинные и проигрывают по рангу коротким пунктам про сроки и печати.
    doc_list_idxs: list[int] = []
    if asks_document_list(query):
        try:
            from qdrant_client import models
            extra = _hybrid(query, 12, collection=name, qvec=qvec,
                            qfilter=models.Filter(must=[
                                models.FieldCondition(key="doc_type",
                                                      match=models.MatchValue(value="tpp_order_52")),
                                models.FieldCondition(key="section_roman",
                                                      match=models.MatchValue(value=RULES_DOC_LIST_SECTION)),
                            ]))
            # Пункт может уже лежать в широком пуле — тогда берём ЕГО индекс, а не пропускаем:
            # «в пуле» не значит «в окне», квота отбирает только первые по рангу, и п. 4.1
            # (3844 знака, второй в разделе) в окно так и не попадал.
            known = {(p.payload or {}).get("point"): i for i, p in enumerate(points)}
            taken_groups: set[str] = set()
            for p in extra:
                point = str((p.payload or {}).get("point") or "")
                group = next((g for g in RULES_DOC_LIST_GROUPS if point.startswith(g)), None)
                if group is None or group in taken_groups:
                    continue  # либо не часть перечня (4.4/4.5), либо эта часть уже представлена
                taken_groups.add(group)
                if point in known:
                    doc_list_idxs.append(known[point])
                else:
                    doc_list_idxs.append(len(points))
                    points.append(p)
        except Exception as e:  # noqa: BLE001 — добор необязателен, ответ не валим
            logger.warning("добор состава документов не удался: {}: {}", type(e).__name__, e)

    chosen: set[int] = set(doc_list_idxs[:RULES_QUOTA_DOC_LIST])
    for doc_type, idxs in by_doc.items():  # квота: сначала представительство
        if doc_type == primary:
            quota = RULES_QUOTA_PRIMARY
        elif doc_type in RULES_QUOTA_ON_DEMAND:
            quota = 0  # документ «по запросу» — только по общему рангу
        else:
            quota = RULES_QUOTA_MIN
        chosen.update(idxs[:quota])
    for i in range(len(points)):  # остаток окна — по общему рангу
        if len(chosen) >= limit:
            break
        chosen.add(i)

    # Порядок: пункты ТЕМАТИЧЕСКОГО документа идут первыми, внутри групп — исходный ранг гибрида.
    # K9 показал, почему одного состава мало: тема угадывалась в 84 % случаев, а атрибуция@1
    # стояла на 72 % — ответ строится вокруг ПЕРВОГО источника, и если сверху оказывался более
    # многословный Приказ №52, вопрос о сроках уходил к нему, а не к Правилам. Внутри документа
    # релевантность не трогаем — переставляем только группы.
    # P2: пункт с перечнем документов идёт ПЕРВЫМ, когда спросили именно о составе документов.
    # K9 показал, что ответ строится вокруг первого источника: 4.1 в хвосте окна модель
    # использовала как ссылку («предусмотрено разделом 4»), а не как перечень.
    doc_list_set = set(doc_list_idxs)
    order = sorted(chosen, key=lambda i: (
        0 if i in doc_list_set else 1,
        0 if (points[i].payload or {}).get("doc_type") == primary else 1, i))

    out: list[dict] = []
    doc_list_set = set(doc_list_idxs)
    for i in order[:limit]:
        pl = dict(points[i].payload or {})
        pl["_score"] = points[i].score
        pl["_topic"] = primary
        # Пометка для форматтера контекста: это пункт с самим перечнем документов, его нельзя
        # резать общим капом — перечень стоит в конце пункта, и обрезка оставляет одну вводную
        # фразу («заявитель представляет…»), из-за чего ответ снова уходил в отсылку к разделу 4.
        if i in doc_list_set:
            pl["_doc_list"] = True
        out.append(pl)
    return out
