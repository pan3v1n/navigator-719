"""Справочник ОКПД2 + переходные ключи ТН ВЭД↔ОКПД2 (T9) — детерминированные лукапы, без сети/модели.

Данные (сборка `scripts/build_classifiers.py` из xlsx заказчика, коммитятся в репо):
  - knowledge_base/classifiers/okpd2.tsv       — «код<TAB>наименование» (весь классификатор ~19k)
  - knowledge_base/classifiers/tnved_okpd2.tsv — «тнвэд_цифры<TAB>окпд2» (переходные ключи)

Закрывает частые запросы теста (боль «определение кода — вход в реестр»):
  - `okpd2_name(code)`          — официальное наименование по коду (валидация/подпись; иерарх. фолбэк);
  - `tnved_to_okpd2(tnved)`     — ОКПД2 по коду ТН ВЭД из сертификата (→ дальше проверка в приложении 719);
  - `okpd2_to_tnved(okpd2)`     — обратное;
  - `suggest_okpd2_by_name(...)`— подсказка кода по наименованию по ВСЕМУ классификатору (глубже, чем 719).

Всё лениво кэшируется на процесс. Числа/коды — дословно из первоисточника (не выдумываются)."""
from __future__ import annotations

import math
import re
from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).resolve().parents[2] / "knowledge_base" / "classifiers"
_OKPD2_TSV = _DIR / "okpd2.tsv"
_KEYS_TSV = _DIR / "tnved_okpd2.tsv"


@lru_cache(maxsize=1)
def _okpd2_names() -> dict[str, str]:
    if not _OKPD2_TSV.exists():
        return {}
    out: dict[str, str] = {}
    for ln in _OKPD2_TSV.read_text(encoding="utf-8").splitlines():
        if "\t" in ln:
            code, name = ln.split("\t", 1)
            out[code] = name
    return out


@lru_cache(maxsize=1)
def _keys() -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """(тнвэд→[окпд2], окпд2→[тнвэд]) из переходных ключей."""
    t2o: dict[str, list[str]] = {}
    o2t: dict[str, list[str]] = {}
    if _KEYS_TSV.exists():
        for ln in _KEYS_TSV.read_text(encoding="utf-8").splitlines():
            if "\t" not in ln:
                continue
            tn, ok = ln.split("\t", 1)
            t2o.setdefault(tn, []).append(ok)
            o2t.setdefault(ok, []).append(tn)
    return t2o, o2t


# --- Код ОКПД2 в свободном тексте — ЕДИНСТВЕННОЕ место разбора ------------------------
# ⚠ ДАТА ВЫГЛЯДИТ КАК КОД, и это не теория. Ревью PR #94 воспроизвело пять форм на живом коде:
# «27.12.2023г» давало РЕАЛЬНЫЙ код `27.12` («Аппаратура распределительная») — регулярка с ``
# откатывалась на префикс, потому что кириллическая «г» границей слова не является; «01.07.26»
# и «31.12.26» проходили как коды (двузначный год короче трёх цифр); «09.00»/«18.00» — часы работы.
# С `EV8` каждый найденный код становится ЦЕЛЕВЫМ и приносит в контекст полные требования своей
# позиции: дата в вопросе снова открывала утечку чужих чисел, которую закрывала `EV7`, и утечку
# НЕВИДИМУЮ — числа лежат в контексте, поэтому ни `unverified_numbers`, ни гейт их не помечают.
#
# ⚠ Прежний фильтр стоял В ОДНОЙ копии регулярки из семи (`navigator.OKPD2_IN_TEXT`), а `_anchor_code`,
# `_HAS_CODE_RE`, `retriever._CODE_IN_TEXT` разбирали текст сами. Поэтому разбор переехал СЮДА:
# одно решение — одно место (иначе правка чинит метрику, а не продукт).
#
# Два признака, оба из ДАННЫХ, а не из смысла фразы:
#   1) ФОРМА. Токен берётся ЦЕЛИКОМ (`\d+(?:\.\d+)+` без отката на префикс), у ОКПД2 сегменты
#      2, 2 и далее 1–3 цифры, не более четырёх. `27.12.2023` отбрасывается как целое.
#   2) КЛАССИФИКАТОР. Форму «ДД.ММ», «ЧЧ.ММ» и «ДД.ММ.ГГ» отличить от кода нельзя — обе
#      правдоподобны. Спрашиваем справочник (19 200 кодов + их точечные префиксы): `27.12` и
#      `29.10` в нём есть и остаются кодами, `09.00`, `01.07`, `01.07.26`, `31.12.26` — нет и
#      отбрасываются. Именно ПРЕФИКСЫ, а не точное вхождение: см. `_code_prefixes`.
# Если файла классификатора нет (усечённая выкатка), сомнительный токен ПРИНИМАЕТСЯ — прежнее
# поведение: молча ослепнуть на реальных кодах хуже, чем пропустить дату.
_CODE_IN_TEXT = re.compile(r"(?<![\d.])\d+(?:\.\d+)+(?!\d|\.\d)")


@lru_cache(maxsize=1)
def _code_prefixes() -> frozenset[str]:
    """Все коды классификатора И их точечные префиксы: «26.11.22.200» → 26 · 26.11 · 26.11.22.

    ⚠ Почему префиксы, а не точное вхождение. Файл классификатора неполон на уровне КЛАССОВ:
    `29.10` (автомобилестроение), `32.50` (медизделия), `21.20`, `26.30`, `21.10` в нём
    отсутствуют, хотя это ходовые коды корпуса. Точная проверка отбрасывала бы их как «дату»
    — правка под один запрос сломала бы соседние (радиус измерен: 1000 кодов корпуса)."""
    out: set[str] = set()
    for code in _okpd2_names():
        segs = code.split(".")
        for i in range(1, len(segs) + 1):
            out.add(".".join(segs[:i]))
    return frozenset(out)


def _shape_is_okpd2(segs: list[str]) -> bool:
    # ОКПД2: раздел XX · группа XX.X · класс XX.XX · категория XX.XX.X · подкатегория XX.XX.XX ·
    # вид XX.XX.XX.XXX. Второй сегмент бывает и однозначным («27.2», «13.2» — в корпусе есть).
    return (2 <= len(segs) <= 4 and len(segs[0]) == 2 and 1 <= len(segs[1]) <= 2
            and all(1 <= len(x) <= 3 for x in segs[2:]))


def _date_or_clock_like(segs: list[str]) -> bool:
    """Токен неотличим по форме от даты или времени — решать должен справочник, не форма."""
    try:
        a, b = int(segs[0]), int(segs[1])
    except ValueError:
        return False
    if len(segs) == 2:
        return (1 <= a <= 31 and 1 <= b <= 12) or (a <= 23 and b <= 59)  # ДД.ММ либо ЧЧ.ММ
    if len(segs) == 3 and len(segs[2]) == 2:
        return 1 <= a <= 31 and 1 <= b <= 12                              # ДД.ММ.ГГ
    return False


def is_okpd2_code(token: str | None) -> bool:
    """Токен — код ОКПД2, а не дата, время или номер пункта."""
    if not token:
        return False
    segs = token.split(".")
    if not _shape_is_okpd2(segs):
        return False
    if _date_or_clock_like(segs):
        known = _code_prefixes()
        return token in known if known else True
    return True


def extract_codes(text: str | None) -> list[str]:
    """ВСЕ коды ОКПД2 из текста, в порядке появления, без повторов."""
    out: list[str] = []
    for m in _CODE_IN_TEXT.finditer(text or ""):
        tok = m.group(0)
        if is_okpd2_code(tok) and tok not in out:
            out.append(tok)
    return out


def has_okpd2_code(text: str | None) -> bool:
    r"""В тексте есть код ОКПД2. ⚠ Не `\d{2}\.\d{2}`: дата в вопросе выключала мультитёрн."""
    for m in _CODE_IN_TEXT.finditer(text or ""):
        if is_okpd2_code(m.group(0)):
            return True
    return False


def normalize_tnved(s: str | None) -> str:
    """ТН ВЭД → только цифры («0101 21» / «8407 34 000 0» → «010121» / «8407340000»)."""
    return "".join(ch for ch in (s or "") if ch.isdigit())


# Детект кода ТН ВЭД в свободном тексте. КОНСЕРВАТИВНО (иначе ловим номера договоров/ИНН/годы):
# либо явный маркер «ТН ВЭД <код>», либо код с ПРОБЕЛЬНОЙ группировкой («8471 30», «8407 34 100 0») —
# так его пишут в сертификатах/декларациях. Голый слитный 10-значный без маркера НЕ трогаем.
_TNVED_CUE_RE = re.compile(r"(?:тн\s*вэд|тнвэд)\D{0,6}(\d{4}\s?\d{2}(?:\s?\d{2,4}){0,3})", re.IGNORECASE)
_TNVED_GROUPED_RE = re.compile(r"(?<![\d.,])(\d{4}\s\d{2}(?:\s?\d{2,4}){0,3})(?![\d.,])")


def extract_tnved(text: str | None) -> str | None:
    """Код ТН ВЭД из текста (маркер «ТН ВЭД …» ИЛИ пробел-группированный код), иначе None."""
    t = text or ""
    m = _TNVED_CUE_RE.search(t) or _TNVED_GROUPED_RE.search(t)
    return m.group(1).strip() if m else None


# --- ТОВАРНАЯ ПОЗИЦИЯ для оси СТ-1 (`K14` #35) -------------------------------------------------
#
# ⚠⚠ ОТДЕЛЬНАЯ ФУНКЦИЯ, А НЕ ПРАВКА `extract_tnved`, И ПРИЧИНА В ГРАНУЛЯРНОСТИ КЛЮЧА.
# `extract_tnved` требует не меньше ШЕСТИ цифр (`\d{4}\s?\d{2}`) — он писался под переходные ключи
# ТН ВЭД↔ОКПД2, которые живут на уровне HS6. А Перечень условий Соглашения СНГ ключуется на
# ЧЕТЫРЁХЗНАЧНОЙ товарной позиции (8403, 8501, 8704): собственная гранулярность второго ключа
# прежнему извлекателю НЕ ВИДНА ВОВСЕ — ни один вопрос приёмочного набора он не разбирал.
# Править его на месте нельзя: радиус ляжет на `translate.py`, у которого своя приёмка.
#
# ⚠ Маркер ОБЯЗАТЕЛЕН. Четыре цифры подряд — слишком частый узор («в 2026 году», «приказ 1392»),
# и без явного «ТН ВЭД» / «товарная позиция» блок условий уезжал бы в чужие вопросы. Номер
# документа исключён отдельно: «постановление № 1392» маркер бы не спас, если он рядом.
_TNVED_POSITION_CUE_RE = re.compile(r"тн\s*вэд|тнвэд|товарн\w*\s+позици", re.IGNORECASE)
# ⚠ Хвост запрещает ПРОДОЛЖЕНИЕ ЧИСЛА, а не пунктуацию. Первая редакция стояла `(?![\d.,])` и
# роняла «мой код ТН ВЭД 8501, какие условия» — код отвергался обычной запятой предложения.
# Найдено прогоном приёмочного набора, а не чтением регулярки.
_TNVED_POSITION_RE = re.compile(r"(?<![\d.,№])(\d{4}(?:\s?\d{2}){0,3})(?!\d|[.,]\d)")
_TNVED_POSITION_SKIP_RE = re.compile(
    r"(?:№|N)\s*$|(?:постановлени|приказ|распоряжени|закон)\w*\s*(?:№|N)?\s*$", re.IGNORECASE)


def extract_tnved_position(text: str | None) -> str | None:
    """Товарная позиция ТН ВЭД (4+ знака) для условий Перечня СТ-1, иначе None.

    Маркер «ТН ВЭД» / «товарная позиция» обязателен и может стоять ДО или ПОСЛЕ кода: реальный
    вопрос пишет и «код ТН ВЭД 8403», и «для товарной позиции 8402 по ТН ВЭД».
    """
    t = text or ""
    if not _TNVED_POSITION_CUE_RE.search(t):
        return None
    for m in _TNVED_POSITION_RE.finditer(t):
        if _TNVED_POSITION_SKIP_RE.search(t[:m.start()]):
            continue  # номер нормативного акта, а не товарная позиция
        return m.group(1).strip()
    return None


def okpd2_name(code: str | None) -> str | None:
    """Наименование по коду ОКПД2. Точного нет → иерархический фолбэк на родителя (28.13.14.190 → 28.13.14)."""
    names = _okpd2_names()
    code = (code or "").strip()
    if not code:
        return None
    if code in names:
        return names[code]
    parts = code.split(".")
    while len(parts) > 1:
        parts = parts[:-1]
        parent = ".".join(parts)
        if parent in names:
            return names[parent]
    return None


def tnved_to_okpd2(tnved: str | None) -> list[str]:
    """Коды ОКПД2 по коду ТН ВЭД (переходные ключи, уровень HS6). Для 8/10-значного — фолбэк на 6-знач. префикс."""
    t2o, _ = _keys()
    d = normalize_tnved(tnved)
    if not d:
        return []
    if d in t2o:
        return t2o[d]
    if len(d) > 6 and d[:6] in t2o:  # ключи на 6-значном уровне ГС
        return t2o[d[:6]]
    return []


def okpd2_to_tnved(okpd2: str | None) -> list[str]:
    """Коды ТН ВЭД по коду ОКПД2 (обратное направление ключей)."""
    _, o2t = _keys()
    return o2t.get((okpd2 or "").strip(), [])


@lru_cache(maxsize=1)
def _name_index() -> tuple[list[tuple[str, str]], dict[str, list[int]], dict[str, float]]:
    """Инвертированный IDF-индекс по наименованиям классификатора (для подсказки кода по имени).
    Строится один раз (~тысячи коротких строк, стем-токенизация BM25). Кэш на процесс."""
    from app.rag import sparse

    entries = list(_okpd2_names().items())  # [(код, наименование)]
    postings: dict[str, list[int]] = {}
    for i, (_code, name) in enumerate(entries):
        for tok in set(sparse.tokenize(name)):
            postings.setdefault(tok, []).append(i)
    n = len(entries) or 1
    idf = {t: math.log(1 + n / len(p)) for t, p in postings.items()}
    return entries, postings, idf


# Служебные/общие термины: их совпадение не должно перевешивать (иначе «прочие»/«для» тянут мусор наверх).
_STOP = {"прочий", "прочее", "проч", "друг", "включ", "групп", "издел", "продукц", "услуг"}


def suggest_okpd2_by_name(text: str, k: int = 5) -> list[tuple[str, str, float]]:
    """Подсказка кодов ОКПД2 по наименованию продукции — по ВСЕМУ классификатору (не только 719).
    top-k [(код, наименование, score)] по IDF-взвешенному пересечению стем-токенов запроса и наименования.
    Пусто, если значимого пересечения нет."""
    from app.rag import sparse

    entries, postings, idf = _name_index()
    qterms = {t for t in sparse.tokenize(text) if t not in _STOP}
    if not qterms:
        return []
    scores: dict[int, float] = {}
    matched: dict[int, int] = {}
    for t in qterms:
        w = idf.get(t)
        if w is None:
            continue
        for i in postings[t]:
            scores[i] = scores.get(i, 0.0) + w
            matched[i] = matched.get(i, 0) + 1
    if not scores:
        return []
    # Приоритет: больше совпавших уникальных терминов, затем IDF-сумма (специфичные термины важнее).
    top = sorted(scores, key=lambda i: (matched[i], scores[i]), reverse=True)[:k]
    return [(entries[i][0], entries[i][1], round(scores[i], 3)) for i in top]
