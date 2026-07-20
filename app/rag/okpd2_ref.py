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
