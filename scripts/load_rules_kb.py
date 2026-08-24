"""Загрузка корпуса «Правила формирования и ведения реестра российской промышленной продукции»
в ОТДЕЛЬНУЮ коллекцию Qdrant (гибрид dense e5 + sparse BM25, RRF) — для ответов на ПРОЦЕДУРНЫЕ
вопросы (внесение в реестр, ГИСП, подача заявления, сроки, состав документов).

Почему отдельная коллекция (не в pp719):
  • pp719 — товарные позиции приложения к ПП №719 (требования/баллы); out-of-scope guard
    (retriever.dense_top1) и измеренный recall@1 калиброваны ИМЕННО на ней. Правила — проза норм,
    чужеродная товарной схеме и identity-эмбеддингу (F1). Отдельная коллекция pp719_rules
    оставляет товарный путь нетронутым (нулевой регресс) и не требует его переиндексации.

ВАЖНО про источник: «Правила ведения реестра» — ОТДЕЛЬНЫЙ акт (ст. 17.1 ФЗ «О промышленной
политике»), который ссылается на ПП №719, но это НЕ само 719. Атрибуция ссылок в ответах —
«Правила ведения реестра», см. app/core/prompts.PROCEDURAL_SYSTEM_PROMPT.

Источник — 5 сырых чанков knowledge_base/pp719/chunks/{11..15}_*.txt (нумерованные пункты норм).
Редакция берётся из самих текстов (последняя пометка «(в ред. Постановления … от ДД.ММ.ГГГГ N …)»)
и печатается — сверить с актуальной консолидированной редакцией на дату теста (директива заказчика).

Запуск (из корня, нужен поднятый Qdrant):
  .venv/Scripts/python.exe scripts/load_rules_kb.py           # пересоздать коллекцию и загрузить
  .venv/Scripts/python.exe scripts/load_rules_kb.py --smoke   # + смоук-запросы
  .venv/Scripts/python.exe scripts/load_rules_kb.py --smoke-only
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings  # noqa: E402

# embeddings/sparse импортируем ЛЕНИВО внутри index_all/hybrid_search (там, где нужны): так модуль
# импортируется дёшево (без загрузки sentence_transformers) — парсер тестируется без тяжёлых зависимостей.

CHUNKS_DIR = ROOT / "knowledge_base" / "pp719" / "chunks"

# ⚠ СОСТАВ КОРПУСА ЗАДАЁТ МАНИФЕСТ (`knowledge_base/manifest.yaml`, K8), а не эти константы.
# Здесь остались УДОБНЫЕ РУЧКИ для тестов парсеров: им нужен путь к файлу, а не паспорт документа.
# Расхождение с манифестом ловит тест `TestManifestMatchesLoaderPaths` — тот же приём, которым
# `.env.example` держится в согласии с `Settings`. Менять состав правкой этих списков бесполезно:
# `load_records` их не читает.
# ЯВНЫЙ список из 5 файлов (НЕ glob `1[1-5]*.txt` — он зацепит товарные 110_…129_ и 130_).
RULES_FILES = [
    "11_I_obshchie_polozheniya.txt",
    "12_II_vklyuchenie_svedeniy.txt",
    "13_III_vnesenie_izmeneniy.txt",
    "14_V_predostavlenie_svedeniy.txt",
    "15_VI_katalog_produkcii.txt",
]
# Доп. процедурные источники (Волна 1, T1/T4) в ТУ ЖЕ коллекцию pp719_rules, но с иным doc_type и
# атрибуцией (source_anchor): тело ПП №719 (критерии подтверждения — п.1 а/б/в/г, СТ-1) и
# Приказ ТПП РФ №52 (порядок выдачи документов: состав документов, сроки, акт на компоненты).
BODY_PATH = CHUNKS_DIR / "01_postanovlenie.txt"
ORDER52_PATH = CHUNKS_DIR.parent / "prikaz52_tpp_full.txt"  # knowledge_base/pp719/
# Определения сносок приложения (<1>…<56>). Ссылок на них в требованиях сотни, а определения
# до D3 доставались чанку XXIX и не были доступны поиску вообще: чанки приложения в индекс не
# идут (в pp719 попадает structured/*.json), а процедурный корпус их не знал.
FOOTNOTES_PATH = CHUNKS_DIR / "131_SNOSKI_prilozheniya.txt"
DENSE = "dense"
SPARSE = "bm25"

SMOKE_QUERIES = [
    "какой порядок внесения продукции в реестр",
    "какие документы нужны для заявки в реестр",
    "сколько времени рассматривают заявку",
    "как обжаловать отказ во включении в реестр",
    "что такое каталог продукции",
    "как внести в реестр продукцию, которой нет в приложении 719",  # → критерий г (СТ-1), тело 719
    "какие документы нужны для получения акта экспертизы",           # → Приказ 52, Раздел 4
    "срок действия акта экспертизы на компоненты",                   # → Приказ 52, п. 3.8
]

# Пункт нормы начинается с начала строки: «1. », «3.1. », «12. » (номер, точка, пробел).
_POINT_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3})*)\.\s")
# Пометка редакции: «(в ред. Постановления Правительства РФ от 13.04.2026 N 400)».
# ⚠ «N» / «№» / кириллическая «Н» / неразрывный пробел — как в `app/rag/edition.py`.
_AMEND_RE = re.compile(r"от\s*(\d{1,2}\.\d{1,2}\.(\d{4}))\s*[NН№]\s*(\d+)")
# Определение сноски приложения начинается с маркера в начале строки: «<44> В случае …».
# Ссылки на сноски внутри требований идут в середине строки и сюда не попадают.
_FOOTNOTE_RE = re.compile(r"^(<\d+(?:\.\d+)?>)\s")
# Заголовок раздела ВНУТРИ тела файла: «IV. Формирование реестровой записи, …».
# Пункт начинается с цифры и под этот шаблон не подходит; требование заглавной буквы после
# номера отсекает случайные строки прозы. Замер по корпусу Правил: пять файлов, ОДНО совпадение —
# ровно тот заголовок IV, из-за которого 13 пунктов подписывались чужим разделом.
_SECTION_HEAD_RE = re.compile(r"^([IVXLC]+)\.\s+([А-ЯЁ].*)$")


def _normalize(text: str) -> str:
    """Чистка форматных артефактов: неразрывные пробелы → обычные (иначе маркер `N 400` и т.п.
    не ловится regex), схлопывание висячих пробелов, унификация переносов."""
    text = text.replace(" ", " ").replace(" ", " ").replace(" ", " ").replace("⁠", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # схлопнуть длинные прогоны пробелов/табов (но не переносы строк)
    return re.sub(r"[ \t]{2,}", " ", text)


def parse_rules_file(path: Path) -> list[dict]:
    """Разбирает один файл Правил в записи-пункты. Первая строка `# <ROMAN>. <title>` даёт
    section_roman/section_title; тело режется на пункты по _POINT_RE (подпункты «а)/б)», строки
    определений и пометки «(в ред. …)» остаются внутри текущего пункта).

    ⚠ РАЗДЕЛ БЕРЁТСЯ НЕ ТОЛЬКО ИЗ ИМЕНИ ФАЙЛА. Нарезка сложила в `13_III_vnesenie_izmeneniy.txt`
    ДВА раздела: III и IV («Формирование реестровой записи, состав сведений, период действия
    реестровой записи»). Раздел читался из первой строки и применялся ко всему файлу, поэтому
    пункты 31–43 подписывались разделом III — а якорь печатается пользователю как источник, и
    заголовок раздела уезжает в вектор (`add_index_text`). Правильный текст, привязанный не к
    тому месту, — самый дорогой класс дефекта в этом проекте; здесь он жил в 13 пунктах.
    Заголовок раздела внутри тела переключает раздел."""
    raw = _normalize(path.read_text(encoding="utf-8"))
    lines = raw.split("\n")
    roman, title = "", ""
    body_start = 0
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s:
            continue
        m = re.match(r"^#\s*([IVXLC]+)\.\s*(.+)$", s)
        if m:
            roman, title = m.group(1), m.group(2).strip()
            body_start = i + 1
        break  # заголовок — первая непустая строка; дальше тело

    records: list[dict] = []
    cur_point: str | None = None
    cur_lines: list[str] = []

    def _flush():
        if cur_point is None:
            return
        text = "\n".join(cur_lines).strip()
        if not text:
            return
        records.append({
            "doc_type": "rules_registry",
            "section_roman": roman,
            "section_title": title,
            "point": cur_point,
            "text": text,
            "source_anchor": f"Правила ведения реестра, п. {cur_point}"
                             + (f" ({roman}. {title})" if roman else ""),
        })

    for ln in lines[body_start:]:
        head = _SECTION_HEAD_RE.match(ln.strip())
        if head:
            _flush()
            roman, title = head.group(1), head.group(2).strip()
            cur_point, cur_lines = None, []
            continue
        m = _POINT_RE.match(ln)
        if m:
            _flush()
            cur_point = m.group(1)
            cur_lines = [ln.rstrip()]
        elif cur_point is not None:
            cur_lines.append(ln.rstrip())
        # строки до первого пункта (пустые/остаток заголовка) игнорируем
    _flush()
    return records


# Аннотации «(в ред. Постановления … N 553 (ред. от 22.06.2022), … N 894)» — шум для поиска и
# контекста; вырезаем (один уровень вложенных скобок). У Правил их оставляем внутри пункта (см.
# parse_rules_file), у тела/приказа — режем, т.к. они непомерно длинные и топят суть.
_AMEND_STRIP_RE = re.compile(r"\s*\((?:в ред\.|введен)(?:[^()]|\([^()]*\))*\)")
_SUBPOINT_RE = re.compile(r"^([абвгдеёжзиклмн])\)\s")
_DECREE_POINT_RE = re.compile(r"^(\d+(?:\.\d+)*|\d+\(\d+\))\.\s")
_SECTION52_RE = re.compile(r"^Раздел\s+(\d+)\.\s*(.+)$")
_APPX52_RE = re.compile(r"^Приложение\s+(\d+)\b")
_ORDER_POINT_RE = re.compile(r"^(\d+(?:\.\d+)*)\.\s")


def _strip_amend(text: str) -> str:
    return _AMEND_STRIP_RE.sub("", text).strip()


def parse_decree_body(path: Path) -> list[dict]:
    """Тело ПП №719 (критерии подтверждения производства, п. 1 а/б/в/г — где «г» = СТ-1 для
    продукции, отсутствующей в приложении). Приложение (таблица продукции) НЕ индексируется —
    оно живёт в structured/*.json. Пункт 1 режем по подпунктам а/б/в/г, иначе критерий «г» тонет
    в длинном п. 1 и обрезается по кэпу контекста."""
    raw = _normalize(path.read_text(encoding="utf-8"))
    for marker in ("\nПриложение\n", "\nТРЕБОВАНИЯ К ПРОМЫШЛЕННОЙ"):
        i = raw.find(marker)
        if i > 0:
            raw = raw[:i]
            break
    lines = raw.split("\n")
    records: list[dict] = []
    cur, buf = None, []

    def _flush():
        if not cur or not buf or cur == "1":  # bare «1» — лишь лид-ин критериев (несут 1а–1г)
            return
        text = _strip_amend("\n".join(buf))
        if len(re.sub(r"\s", "", text)) < 40 or "утратил силу" in text.lower():
            return
        is_sub = len(cur) == 2 and cur[0] == "1" and cur[1] in "абвгдеёж"
        anchor = (f"ПП №719, п. 1, подпункт «{cur[1]}»" if is_sub else f"ПП №719, п. {cur}")
        records.append({
            "doc_type": "decree_body", "section_roman": "",
            "section_title": "Тело постановления №719 (критерии подтверждения производства)",
            "point": cur, "text": text, "source_anchor": anchor})

    for ln in lines:
        mp = _DECREE_POINT_RE.match(ln)
        ms = _SUBPOINT_RE.match(ln)
        # «в критериях» = сейчас парсим п. 1 целиком ИЛИ уже его подпункт 1а/1б/… (не 1.1)
        in_criteria = cur is not None and cur[0] == "1" and (cur == "1" or cur[1:] in "абвгдеёж")
        if mp:
            _flush(); cur, buf = mp.group(1), [ln]
        elif ms and in_criteria:  # подпункт КРИТЕРИЕВ п. 1 → отдельная запись 1а/1б/1в/1г
            _flush(); cur = "1" + ms.group(1)
            buf = ["Критерии подтверждения производства российской промышленной продукции "
                   "(п. 1 ПП №719): " + ln]
        elif cur:
            buf.append(ln)
    _flush()
    return records


def parse_order52(path: Path) -> list[dict]:
    """Приказ ТПП РФ №52 (Положение о порядке выдачи документов): состав документов (Раздел 4),
    сроки (Раздел 6), акт экспертизы на компоненты (Раздел 13), изменение реестровой записи
    (Раздел 9). Режем по «Раздел N. …» и пунктам N.N.N; приказ-часть до первого раздела
    (утвердить/департаменты) и формы-пустышки приложений отбрасываем фильтром."""
    raw = _normalize(path.read_text(encoding="utf-8"))
    lines = raw.split("\n")
    records: list[dict] = []
    sec_num, sec_title, started = "", "", False
    cur, buf = None, []

    def _flush():
        if not cur or not buf:
            return
        text = _strip_amend("\n".join(buf))
        if len(re.sub(r"[_\s|.\-–—]", "", text)) < 25:  # форма/пустышка
            return
        loc = f"Раздел {sec_num}. {sec_title}" if sec_num.isdigit() else sec_title
        records.append({
            "doc_type": "tpp_order_52", "section_roman": sec_num, "section_title": sec_title,
            "point": cur, "text": text,
            "source_anchor": f"Приказ ТПП РФ №52, п. {cur}" + (f" ({loc})" if loc else "")})

    for ln in lines:
        msec = _SECTION52_RE.match(ln)
        mappx = _APPX52_RE.match(ln)
        mp = _ORDER_POINT_RE.match(ln)
        if msec:
            _flush(); cur, started = None, True
            sec_num, sec_title = msec.group(1), msec.group(2).strip()
        elif mappx:
            _flush(); cur, started = None, True
            sec_num, sec_title = f"прил.{mappx.group(1)}", f"Приложение {mappx.group(1)} к Положению"
        elif started and mp:
            _flush(); cur, buf = mp.group(1), [ln]
        elif cur:
            buf.append(ln)
    _flush()
    return records


def parse_footnotes(path: Path) -> list[dict]:
    """Определения сносок приложения: одна запись на сноску, ключ — её номер («<44>»).

    Сноска — не процедурная норма, но живёт в той же коллекции: она отвечает на вопрос
    «что значит <44> в требовании», а вопрос этот не товарный (позиция тут ни при чём).
    Исключённые сноски («<7> Сноска исключена») отбрасываем — отвечать ими не на что."""
    raw = _normalize(path.read_text(encoding="utf-8"))
    records: list[dict] = []
    cur, buf = None, []

    def _flush() -> None:
        if not cur or not buf:
            return
        text = "\n".join(buf).strip()
        if re.match(r"^<\d+(?:\.\d+)?>\s*[Сс]носка исключена", text):
            return
        records.append({
            "doc_type": "appendix_footnotes", "section_roman": "",
            "section_title": "Сноски к приложению ПП №719",
            "point": cur, "text": text,
            "source_anchor": f"Приложение к ПП №719, сноска {cur}"})

    for ln in raw.split("\n"):
        m = _FOOTNOTE_RE.match(ln)
        if m:
            _flush(); cur, buf = m.group(1), [ln]
        elif cur and ln.strip():
            buf.append(ln)
    _flush()
    return records


# Вводный пункт приклеивается к подпунктам только если он короткий: 4.2 («К заявке прилагаются
# следующие документы») — 122 символа, 4.3 — 350. Длинные пункты вроде 4.1 (3842) — самостоятельная
# норма, а не заголовок перечня; приклеив их, мы бы утопили подпункт в чужом тексте.
PARENT_INTRO_CAP = 600


def _parent_point(point: str | None) -> str | None:
    """«4.2.1» → «4.2», «4.2» → «4», «6» → None."""
    parts = [p for p in (point or "").split(".") if p]
    return ".".join(parts[:-1]) if len(parts) > 1 else None


def add_index_text(recs: list[dict]) -> None:
    """Проставляет `index_text` — текст пункта для векторов, с контекстом его места в документе.

    ЗАЧЕМ (P2, кластер жалоб №1 июльского теста — 31 упоминание). Перечни документов лежат в
    ПОДПУНКТАХ («4.2.1. Правоустанавливающие и регистрационные документы заявителя: копия устава…»),
    а слова, которыми их спрашивают, — в РОДИТЕЛЬСКОМ пункте 4.2 («К заявке на включение сведений в
    реестр прилагаются следующие документы»). Подпункт не содержит ни «заявки», ни «перечня», ни
    «прилагаются», поэтому на вопрос «какие документы нужны» проигрывал пунктам про сроки, печати и
    электронную подпись — и ответ честно сообщал, что перечень «в контексте не представлен».

    Тот же приём, что `K4` применила к требованиям приложения: вводная фраза обязана доезжать до
    индекса вместе с тем, к чему относится. Сам `text` НЕ меняем — он идёт в ответ как цитата, и по
    его началу считается `point_id`."""
    # Раздел в ключе ОБЯЗАТЕЛЕН: в формах приложений Приказа №52 нумерация начинается заново
    # («4. Заключение: при изготовлении компонентов…»), и без раздела пункт 4.2 получил бы
    # родителем кусок чужой формы. Ровно та же коллизия, ради которой `point_id` держит раздел.
    by_key = {(r.get("doc_type"), r.get("section_roman"), r.get("point")): r for r in recs}
    for r in recs:
        intros: list[str] = []
        p = _parent_point(r.get("point"))
        while p:
            parent = by_key.get((r.get("doc_type"), r.get("section_roman"), p))
            if parent and len(parent.get("text") or "") <= PARENT_INTRO_CAP:
                intros.append(parent["text"].strip())
            p = _parent_point(p)
        # ЗАГОЛОВОК РАЗДЕЛА СЮДА НЕ ИДЁТ — проверено замером: приклеенный ко всем 172 пунктам
        # Приказа, он делает их одинаково похожими на «какие документы нужны для акта экспертизы»
        # (ровно эти слова стоят в названии раздела 4) и роняет атрибуцию@1 0.92 → 0.88.
        # Работает только точечная вводная родителя — она различает подпункты, а не уравнивает их.
        head = [t for t in reversed(intros) if t]
        r["index_text"] = "\n".join([*head, r.get("text") or ""]) if head else (r.get("text") or "")
        # Вводная родителя нужна и в ОТВЕТЕ: без неё перечень документов выглядит списком
        # неизвестно к чему. Кладём отдельным полем — цитату пункта не подменяем.
        if intros:
            r["parent_intro"] = intros[0]


def detect_edition(all_text: str) -> str:
    """Последняя (по дате) пометка «(в ред. … от ДД.ММ.ГГГГ N …)» во всём корпусе — честный штамп
    редакции индексируемого текста."""
    best_key, best_label = (0, 0, 0, 0), None
    for m in _AMEND_RE.finditer(all_text):
        d, mth, y = (int(x) for x in m.group(1).split("."))
        num = int(m.group(3))
        key = (y, mth, d, num)
        if key > best_key:
            best_key, best_label = key, f"ред. от {m.group(1)} N {m.group(3)}"
    return best_label or "редакция не определена в тексте"


def _stamp_edition(recs: list[dict], text: str) -> None:
    """Проставить каждой записи штамп редакции, вычисленный по её собственному тексту."""
    ed = detect_edition(text)
    for r in recs:
        r["edition"] = ed


# --------------------------------------------------------------------------- #
# K8: манифест корпуса — единственный источник правды о его составе
# --------------------------------------------------------------------------- #
MANIFEST_PATH = ROOT / "knowledge_base" / "manifest.yaml"

# Как читать источники документа. Ключ — `doc_type` из манифеста; в манифесте этого сопоставления
# НЕТ намеренно: он описывает ДОКУМЕНТ (сила, статус, срок), а не устройство разбора. Формат
# файла — забота кода, и тест следит, что у каждого документа с источниками парсер есть.
PARSERS = {
    "rules_registry": parse_rules_file,
    "decree_body": parse_decree_body,
    "tpp_order_52": parse_order52,
    "appendix_footnotes": parse_footnotes,
}

# Поля паспорта, уезжающие в payload КАЖДОГО пункта документа. `edition` сюда не входит: её
# считает `detect_edition` из текста, а манифест лишь заявляет ожидаемую (см. `load_manifest`).
PASSPORT_FIELDS = ("authority", "legal_force", "doc_kind", "topic", "key_type",
                   "status", "valid_from", "valid_to", "supersedes")
RETIRED = "утратил силу"


def load_manifest(path: Path | None = None) -> dict:
    """Читает манифест и проверяет значения по его же словарям.

    ⚠ Проверка словарём — не бюрократия: опечатка в `status` («утратила силу») тихо превратила бы
    исключённый документ в действующий, а `legal_force: "2"` строкой сломала бы сравнение при
    K13. Ошибка здесь останавливает загрузку: индексировать корпус с неизвестным паспортом хуже,
    чем не индексировать вовсе."""
    import yaml  # локально: парсер Правил тестируется без внешних зависимостей

    path = path or MANIFEST_PATH
    if not path.exists():
        sys.exit(f"Нет манифеста корпуса: {path} (K8 — состав корпуса задаётся им)")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    docs = data.get("documents") or []
    if not docs:
        sys.exit(f"В манифесте {path.name} нет ни одного документа")
    vocab = data.get("vocabularies") or {}
    seen: set[str] = set()
    for d in docs:
        dt = d.get("doc_type")
        if not dt:
            sys.exit(f"В манифесте есть документ без doc_type: {d.get('title')!r}")
        if dt in seen:
            sys.exit(f"doc_type {dt!r} в манифесте дважды — идентификатор обязан быть уникален")
        seen.add(dt)
        for field, allowed in vocab.items():
            if field not in d:
                continue
            value = d[field]
            if value is None and None in allowed:
                continue
            if value not in allowed:
                sys.exit(f"{dt}: недопустимое {field}={value!r}; словарь манифеста: {allowed}")
        missing = [f for f in PASSPORT_FIELDS if f not in d]
        if missing:
            sys.exit(f"{dt}: в паспорте нет полей {missing}")
    return data


def load_records(manifest: dict | None = None) -> tuple[list[dict], str]:
    """Собирает записи процедурного корпуса ПО МАНИФЕСТУ.

    Порядок документов и набор файлов больше не зашиты в код: раньше они жили тремя списками
    (`RULES_FILES`, `BODY_PATH`, …), а решение «утратившие силу Правила выдачи заключения не
    индексируем» не было записано нигде — его знал только тот, кто его принял."""
    man = manifest or load_manifest()
    recs: list[dict] = []
    rules_edition = ""

    for doc in man["documents"]:
        dt = doc["doc_type"]
        title = doc.get("short") or doc.get("title") or dt

        if doc.get("status") != "действует":
            # Не молча: исключение документа — решение, и оно обязано быть видно в логе загрузки.
            print(f"⏭  {title}: статус «{doc.get('status')}» — в индекс НЕ идёт "
                  f"(до {doc.get('valid_to') or '—'})")
            continue

        sources = [ROOT / "knowledge_base" / s for s in (doc.get("sources") or [])]
        if not sources:
            print(f"⚠️  {title}: в манифесте нет источников — документ пропущен")
            continue
        parser = PARSERS.get(dt)
        if parser is None:
            sys.exit(f"{dt}: документ в манифесте есть, а парсер для него не заведён (PARSERS)")

        part: list[dict] = []
        texts: list[str] = []
        for path in sources:
            if not path.exists():
                sys.exit(f"{title}: нет файла {path} (перечислен в манифесте)")
            texts.append(_normalize(path.read_text(encoding="utf-8")))
            chunk = parser(path)
            if not chunk:
                print(f"⚠️  {path.name}: не распознано ни одного пункта — проверь формат")
            part.extend(chunk)

        edition = detect_edition("\n".join(texts))
        expected = doc.get("edition_expected")
        if expected and expected != edition:
            # ⚠ Расхождение НЕ останавливает загрузку: обычно это значит, что текст обновили, и
            # индексировать надо именно его. Гейтом служит тест (`test_kb_manifest`), который
            # ловит расхождение в CI; загрузчик обязан о нём сказать, а не решать за человека.
            print(f"⚠️  {title}: манифест ждёт «{expected}», в тексте «{edition}» — "
                  f"обновите манифест или текст (A6)")
        for r in part:
            r["edition"] = edition
            for f in PASSPORT_FIELDS:
                r[f] = doc.get(f)
            r["doc_title"] = title
        if dt == "rules_registry":
            rules_edition = edition
        recs.extend(part)
        print(f"{title}: {len(part)} пунктов ({edition})")

    if not recs:
        sys.exit("Процедурный корпус пуст — проверь манифест и файлы источников")

    add_index_text(recs)  # P2: подпункт индексируется вместе с вводной родителя и разделом
    with_intro = sum(1 for r in recs if r.get("parent_intro"))
    print(f"Контекст в индексе: {with_intro} подпунктов несут вводную родителя")
    return recs, rules_edition


def point_id(rec: dict) -> str:
    # doc_type + начало текста в ключе — чтобы пункты тела/приказа/правил с одинаковым номером (в т.ч.
    # повторяющиеся «1./2.» в формах приложений Приказа №52) не затирали друг друга при апсерте.
    key = (f"{rec.get('doc_type', 'rules')}|{rec.get('section_roman')}|{rec.get('point')}"
           f"|{(rec.get('text') or '')[:80]}")
    return str(uuid5(NAMESPACE_URL, key))


def make_client():
    from qdrant_client import QdrantClient

    return QdrantClient(url=settings.QDRANT_URL, timeout=60)


def recreate_collection(client) -> None:
    from qdrant_client import models

    name = settings.QDRANT_RULES_COLLECTION
    if client.collection_exists(name):
        client.delete_collection(name)
    client.create_collection(
        collection_name=name,
        vectors_config={
            DENSE: models.VectorParams(size=settings.EMBEDDING_DIM, distance=models.Distance.COSINE)
        },
        sparse_vectors_config={SPARSE: models.SparseVectorParams(modifier=models.Modifier.IDF)},
    )
    client.create_payload_index(name, "doc_type", models.PayloadSchemaType.KEYWORD)
    client.create_payload_index(name, "section_roman", models.PayloadSchemaType.KEYWORD)
    # K8: по `status` фильтруется КАЖДЫЙ процедурный запрос (`retriever.alive_only`) — без индекса
    # это перебор payload'ов на каждом обращении. Урок K10 в силе: индекс по `doc_type` создавался
    # и не использовался ни разу; здесь наоборот — сначала потребитель, потом индекс.
    client.create_payload_index(name, "status", models.PayloadSchemaType.KEYWORD)


def index_all(client, recs: list[dict], batch: int = 64) -> None:
    from qdrant_client import models

    from app.rag.embeddings import embed_passages
    from app.rag.sparse import doc_length, document_vector

    try:
        from tqdm import tqdm
    except ImportError:
        tqdm = None

    name = settings.QDRANT_RULES_COLLECTION
    # Эмбеддим содержание пункта ВМЕСТЕ с его местом в документе (P2): для подпунктов это заголовок
    # раздела и вводная фраза родителя, для остальных — просто текст.
    texts = [r.get("index_text") or r["text"] for r in recs]
    lengths = [doc_length(t) for t in texts]
    avgdl = (sum(lengths) / len(lengths)) if lengths else 1.0
    print(f"Пунктов Правил: {len(recs)} | avgdl (токенов): {avgdl:.1f} | макс={max(lengths)} мин={min(lengths)}")

    bar = tqdm(total=len(recs), unit="пункт", desc="Эмбеддинг+апсерт") if tqdm else None
    for start in range(0, len(recs), batch):
        chunk_texts = texts[start:start + batch]
        dvecs = embed_passages(chunk_texts)
        points = []
        for k, dvec in enumerate(dvecs):
            j = start + k
            rec, text = recs[j], texts[j]
            idx, val = document_vector(text, avgdl)
            # index_text — служебная склейка для векторов; в payload не кладём, иначе он поедет
            # в контекст ответа дублем к самому пункту.
            payload = {k: v for k, v in rec.items() if k != "index_text"}
            points.append(models.PointStruct(
                id=point_id(rec),
                vector={DENSE: dvec, SPARSE: models.SparseVector(indices=idx, values=val)},
                payload=payload,
            ))
        client.upsert(collection_name=name, points=points)
        if bar:
            bar.update(len(points))
    if bar:
        bar.close()


def hybrid_search(client, query: str, limit: int = 5):
    from qdrant_client import models

    from app.rag.embeddings import embed_query
    from app.rag.sparse import query_vector

    dvec = embed_query(query)
    idx, val = query_vector(query)
    res = client.query_points(
        collection_name=settings.QDRANT_RULES_COLLECTION,
        prefetch=[
            models.Prefetch(query=dvec, using=DENSE, limit=20),
            models.Prefetch(query=models.SparseVector(indices=idx, values=val), using=SPARSE, limit=20),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=limit,
        with_payload=True,
    )
    return res.points


def run_smoke(client) -> None:
    print("\n" + "=" * 70)
    print("SMOKE-ТЕСТ ПОИСКА ПО ПРАВИЛАМ РЕЕСТРА (dense e5 + sparse BM25, RRF)")
    print("=" * 70)
    for query in SMOKE_QUERIES:
        print(f"\n🔎 «{query}»")
        for i, p in enumerate(hybrid_search(client, query), 1):
            pl = p.payload or {}
            snippet = (pl.get("text") or "").replace("\n", " ")[:90]
            print(f"  {i}. п.{pl.get('point', '?')} [{pl.get('section_roman', '?')}] "
                  f"(score={p.score:.4f}) {snippet}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Загрузка корпуса Правил ведения реестра в Qdrant")
    ap.add_argument("--smoke", action="store_true", help="после загрузки прогнать смоук-запросы")
    ap.add_argument("--smoke-only", action="store_true", help="только смоук (без перезагрузки)")
    ap.add_argument("--batch", type=int, default=64)
    args = ap.parse_args()

    client = make_client()

    if not args.smoke_only:
        recs, edition = load_records()
        print(f"Редакция индексируемого текста Правил: {edition}")
        print(f"Коллекция: {settings.QDRANT_RULES_COLLECTION}")
        recreate_collection(client)
        index_all(client, recs, batch=args.batch)
        info = client.get_collection(settings.QDRANT_RULES_COLLECTION)
        print(f"\n✅ Коллекция '{settings.QDRANT_RULES_COLLECTION}': точек = {info.points_count}")

    if args.smoke or args.smoke_only:
        run_smoke(client)


if __name__ == "__main__":
    main()
