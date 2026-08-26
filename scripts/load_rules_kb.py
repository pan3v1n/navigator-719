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
from app.core.console import enable_utf8  # noqa: E402  (только после sys.path)
from app.core import manifest as kb_manifest  # noqa: E402

# Windows-консоль по умолчанию cp1251 и не знает «⚠», «✅», «→»: без этого печать
# предупреждения роняет скрипт UnicodeEncodeError'ом. Подробности — в app/core/console.py.
enable_utf8()

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


def parse_rules_file(path: Path, doc: dict | None = None) -> list[dict]:
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


def parse_decree_body(path: Path, doc: dict | None = None) -> list[dict]:
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


def parse_order52(path: Path, doc: dict | None = None) -> list[dict]:
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


def parse_footnotes(path: Path, doc: dict | None = None) -> list[dict]:
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


# Версия вместо редакции: «ВЕРСИЯ 1.18.3» у методрекомендаций ТПП (см. detect_edition).
_VERSION_RE = re.compile(r"(?i)\bверси[яи]\s+(\d+(?:\.\d+)+)")

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
    if best_label:
        return best_label
    # ⚠ K15: не всякий документ ВЕРСИОНИРУЕТСЯ редакциями. Методрекомендации ТПП пометок
    # «(в ред. …)» не несут вовсе — у них ВЕРСИЯ. Без этой ветки штамп у них был бы
    # «редакция не определена в тексте», то есть A6 (отставание корпуса от действующей
    # редакции) на этом документе не работал бы НИКОГДА.
    # ⚠ Ветка вторая, а не первая: у документа с пометками «в ред.» они и остаются штампом,
    # даже если слово «версия» встретится в его тексте. Радиус проверен — штампы четырёх
    # прежних документов не изменились (тест).
    mv = _VERSION_RE.search(all_text)
    if mv:
        return f"Версия {mv.group(1)}"
    return "редакция не определена в тексте"


def _stamp_edition(recs: list[dict], text: str) -> None:
    """Проставить каждой записи штамп редакции, вычисленный по её собственному тексту."""
    ed = detect_edition(text)
    for r in recs:
        r["edition"] = ed


# --------------------------------------------------------------------------- #
# K15 #36: Методические рекомендации ТПП РФ — разъяснение, а не норма
# --------------------------------------------------------------------------- #
# Заголовок раздела: «4. ДОКУМЕНТЫ, ПРИЛАГАЕМЫЕ К ЗАЯВЛЕНИЮ…». Часть заголовков переносится на
# вторую строку, и она тоже сплошь прописная — поэтому продолжение подбирается отдельно.
_METODREK_HEAD_RE = re.compile(r"^(\d+)\.\s+([А-ЯЁ][А-ЯЁ0-9 ,\-()«»]+)$")
_METODREK_CAP = 1200  # символов на запись: RULES_TEXT_CAP в контексте = 1400, режем с запасом


def cut_fragments(text: str, doc: dict | None) -> tuple[str, int]:
    """Вырезать из текста фрагменты, перечисленные в манифесте (`exclude_fragments`).

    ⚠⚠ ЗАЧЕМ ЭТО ЕСТЬ ВООБЩЕ. Документ бывает действующим и при этом описывающим ОТМЕНЁННЫЙ шаг.
    Методрекомендации вер. 1.18.3 помечены действующими, но их вводная и схема процесса ведут
    заявителя за «заключением Минпромторга», а Правила выдачи заключения утратили силу 29.06.2024
    (ПП РФ N 894) и заменены Правилами ведения реестра. Статуса «действует, но частично устарел» в
    паспорте нет, а `legal_force` решает КОНФЛИКТ НОРМ, а не устаревание, — поэтому устаревшие
    фрагменты не индексируются вовсе.

    ⚠ РЕШЕНИЕ ЖИВЁТ В МАНИФЕСТЕ, а не здесь. Ровно по той причине, по которой туда переехало
    исключение утративших силу Правил: «эти строки не индексируем» — решение, и знать его должен
    тот, кто читает состав корпуса, а не тот, кто читает парсер.

    ⚠ ПОЛОЖИТЕЛЬНЫЙ КОНТРОЛЬ ОБЯЗАТЕЛЕН. Правило, переставшее совпадать (текст переиздали, пробел
    сменился), молча вернуло бы устаревшую норму в корпус — а снаружи «нечего вырезать» и «чистка
    ослепла» неразличимы. Поэтому несовпавшее правило останавливает загрузку.
    """
    rules = (doc or {}).get("exclude_fragments") or []
    cut = 0
    for rule in rules:
        frm, to = rule.get("from", ""), rule.get("to", "")
        i = text.find(frm) if frm else -1
        j = text.find(to, i) if (i != -1 and to) else -1
        if i == -1 or j == -1:
            sys.exit(f"{(doc or {}).get('doc_type')}: правило exclude_fragments не совпало "
                     f"({rule.get('reason') or 'без причины'}). Текст изменился — сверьте границы, "
                     f"молчаливый пропуск вернул бы устаревшую норму в индекс.")
        removed = j + len(to) - i
        print(f"   [cut] {removed} символов: {rule.get('reason') or 'причина не указана'} "
              f"(с {rule.get('since') or '—'})")
        text = text[:i] + text[j + len(to):]
        cut += removed
    return text, cut


def parse_metodrek(path: Path, doc: dict | None = None) -> list[dict]:
    """Методрекомендации ТПП РФ: одна запись на смысловой кусок раздела.

    Пунктов вида N.N в документе нет — это проза с шестью разделами, поэтому режем по разделам, а
    длинные разделы добираем по границам строк до `_METODREK_CAP`. Резать мельче нечем: подзаголовков
    внутри разделов документ не имеет.
    """
    raw, _ = cut_fragments(_normalize(path.read_text(encoding="utf-8")), doc)
    lines = raw.split("\n")

    sections: list[tuple[str, str, list[str]]] = []
    num, title, buf = "", "", []
    for ln in lines:
        m = _METODREK_HEAD_RE.match(ln.strip())
        if m:
            if num:
                sections.append((num, title, buf))
            num, title, buf = m.group(1), m.group(2).strip(), []
            continue
        # Перенос заголовка на вторую строку: сплошь прописная строка сразу после заголовка.
        if num and not buf and ln.strip() and ln.strip() == ln.strip().upper() and len(ln.strip()) > 3:
            title = (title + " " + ln.strip()).strip()
            continue
        if num:
            buf.append(ln)
    if num:
        sections.append((num, title, buf))

    records: list[dict] = []
    for num, title, buf in sections:
        chunks: list[list[str]] = [[]]
        size = 0
        for ln in buf:
            if size + len(ln) > _METODREK_CAP and chunks[-1]:
                chunks.append([])
                size = 0
            chunks[-1].append(ln)
            size += len(ln) + 1
        chunks = [c for c in chunks if "".join(c).strip()]
        nice = title.capitalize() if title.isupper() else title
        for k, c in enumerate(chunks, 1):
            text = "\n".join(c).strip()
            if len(re.sub(r"[_\s|.\-–—]", "", text)) < 25:  # обрывок/разделитель
                continue
            part = f" ч. {k}" if len(chunks) > 1 else ""
            records.append({
                "doc_type": "metodrek_tpp",
                "section_roman": num,
                "section_title": nice,
                "point": f"{num}.{k}",
                "text": text,
                "source_anchor": f"Методрекомендации ТПП РФ, раздел {num} ({nice}){part}",
            })
    return records


# --------------------------------------------------------------------------- #
# K8: манифест корпуса — единственный источник правды о его составе
# --------------------------------------------------------------------------- #
# Манифест читает общий модуль `app.core.manifest`: он нужен ТРЁМ загрузчикам сразу (процедурный
# корпус, товарный, кейсы), и три копии правил разбора означали бы три расходящихся понимания
# того, что такое «состав корпуса». Псевдонимы ниже — чтобы вызывающий код и тесты не знали,
# в каком слое живёт читалка.
MANIFEST_PATH = kb_manifest.MANIFEST_PATH
PASSPORT_FIELDS = kb_manifest.PASSPORT_FIELDS
RETIRED = kb_manifest.RETIRED
COLLECTION = "pp719_rules"

# Как читать источники документа. Ключ — `doc_type` из манифеста; в манифесте этого сопоставления
# НЕТ намеренно: он описывает ДОКУМЕНТ (сила, статус, срок), а не устройство разбора. Формат
# файла — забота кода, и тест следит, что у каждого документа с источниками парсер есть.
PARSERS = {
    "rules_registry": parse_rules_file,
    "decree_body": parse_decree_body,
    "tpp_order_52": parse_order52,
    "appendix_footnotes": parse_footnotes,
    "metodrek_tpp": parse_metodrek,
}

# ⚠⚠ КАКИЕ ПАРСЕРЫ ДЕЙСТВИТЕЛЬНО РЕЖУТ `exclude_fragments` (ревью захода 5, 26.08.2026).
# `cut_fragments` зовётся ровно из одного парсера — `parse_metodrek`. Остальные принимают
# аргумент `doc` и правило молча игнорируют, а `load_manifest` посторонних ключей не проверяет.
# Манифест при этом подаёт `exclude_fragments` как ОБЩИЙ механизм, с обоснованием «несовпавшее
# правило останавливает загрузку». Допиши кто-нибудь такое правило, скажем, к `rules_registry` —
# фрагмент проиндексировался бы без изменений, без ошибки и без предупреждения, а
# задокументированная гарантия молча не применилась бы. Отменённая норма вернулась бы в индекс
# ровно тем способом, от которого `exclude_fragments` и заводилась.
SUPPORTS_EXCLUDE_FRAGMENTS = frozenset({"metodrek_tpp"})


def load_manifest(path=None) -> dict:
    """Манифест с проверкой словарей. Ошибку превращаем в выход: индексировать корпус с
    неизвестным паспортом хуже, чем не индексировать вовсе."""
    try:
        return kb_manifest.load_manifest(path)
    except kb_manifest.ManifestError as e:
        sys.exit(str(e))


def load_records(manifest: dict | None = None) -> tuple[list[dict], str]:
    """Собирает записи процедурного корпуса ПО МАНИФЕСТУ.

    Порядок документов и набор файлов больше не зашиты в код: раньше они жили тремя списками
    (`RULES_FILES`, `BODY_PATH`, …), а решение «утратившие силу Правила выдачи заключения не
    индексируем» не было записано нигде — его знал только тот, кто его принял."""
    man = manifest or load_manifest()
    recs: list[dict] = []
    rules_edition = ""

    # ⚠ ТОЛЬКО СВОЯ КОЛЛЕКЦИЯ. Манифест описывает все три корпуса сервиса; без фильтра этот
    # загрузчик попытался бы разобрать товарное приложение парсером норм и упал бы на первом же
    # разделе — или, хуже, тихо добавил бы мусор в процедурную коллекцию.
    for doc in [d for d in man["documents"] if d.get("collection") == COLLECTION]:
        dt = doc["doc_type"]
        title = doc.get("short") or doc.get("title") or dt

        # Предохранитель на путь действия: правило, которое некому исполнить, останавливает
        # загрузку здесь, а не превращается в тихо проиндексированный фрагмент.
        if doc.get("exclude_fragments") and dt not in SUPPORTS_EXCLUDE_FRAGMENTS:
            sys.exit(f"{dt}: в манифесте есть exclude_fragments, но парсер этого документа их НЕ "
                     f"режет (режут только: {', '.join(sorted(SUPPORTS_EXCLUDE_FRAGMENTS))}). "
                     f"Фрагмент уехал бы в индекс молча — добавьте вызов cut_fragments в парсер "
                     f"и допишите doc_type в SUPPORTS_EXCLUDE_FRAGMENTS.")

        if doc.get("status") != kb_manifest.ACTIVE:
            # Не молча: исключение документа — решение, и оно обязано быть видно в логе загрузки.
            print(f"[skip] {title}: статус «{doc.get('status')}» — в индекс НЕ идёт "
                  f"(до {doc.get('valid_to') or '—'})")
            continue

        try:
            sources = kb_manifest.resolve_sources(doc)
        except kb_manifest.ManifestError as e:
            sys.exit(str(e))
        if not sources:
            print(f"⚠️  {title}: в манифесте нет источников — документ пропущен")
            continue
        parser = PARSERS.get(dt)
        if parser is None:
            sys.exit(f"{dt}: документ в манифесте есть, а парсер для него не заведён (PARSERS)")

        part: list[dict] = []
        texts: list[str] = []
        for path in sources:
            texts.append(_normalize(path.read_text(encoding="utf-8")))
            chunk = parser(path, doc)
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
        stamp = kb_manifest.passport(doc)
        for r in part:
            r["edition"] = edition
            r.update(stamp)
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


def corpus_avgdl(recs: list[dict]) -> float:
    """Средняя длина документа ПО ВСЕМУ КОРПУСУ — в токенах sparse-канала.

    ⚠⚠ ВЫНЕСЕНО ОТДЕЛЬНО РАДИ `K11`. `document_vector` ЗАПЕКАЕТ `avgdl` в значения sparse-вектора
    (нормировка BM25 по длине). Если переиндексировать один документ, посчитав среднюю длину
    только по нему, его вектора окажутся в ДРУГОМ масштабе, чем у соседей по коллекции: у Приказа
    №52 пункты длинные, у сносок — короткие, средние отличаются кратно. Ранжирование поедет у
    ВСЕХ, и ни один тест выдачи этого не покажет — числа останутся правдоподобными.

    Поэтому инкрементальная переиндексация всё равно РАЗБИРАЕТ весь корпус (это дёшево, без
    модели) и считает `avgdl` по нему, а эмбеддит и грузит только целевой документ. Экономится
    ровно то, что дорого, — прогон e5."""
    from app.rag.sparse import doc_length

    lengths = [doc_length(r.get("index_text") or r["text"]) for r in recs]
    return (sum(lengths) / len(lengths)) if lengths else 1.0


def index_all(client, recs: list[dict], batch: int = 64, avgdl: float | None = None) -> None:
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
    # `avgdl` передаётся снаружи при частичной загрузке (`K11`): он обязан быть корпусным, иначе
    # вектора документа окажутся в другом масштабе, чем у соседей. См. `corpus_avgdl`.
    if avgdl is None:
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


def _document_has_points(client, collection: str, doc_type: str) -> bool:
    """Есть ли в коллекции хоть одна точка этого документа. Ошибку связи трактуем как «есть».

    ⚠ Направление умолчания выбрано намеренно. Ошибка счёта не должна ПРЕВРАЩАТЬ переиндексацию в
    отказ: оператор в этот момент чинит бой, и предохранитель, срабатывающий от обрыва сети,
    научит его добавлять --allow-new-doc всегда — то есть выключит себя сам."""
    from qdrant_client import models

    try:
        got = client.count(collection_name=collection, exact=True, count_filter=models.Filter(
            must=[models.FieldCondition(key="doc_type", match=models.MatchValue(value=doc_type))]))
        return got.count > 0
    except Exception as e:  # noqa: BLE001
        print(f"⚠ не удалось сосчитать точки {doc_type}: {type(e).__name__}: {e} — "
              f"считаю, что документ в коллекции уже есть")
        return True


def reindex_document(client, doc_type: str, recs_all: list[dict], batch: int = 64,
                     allow_new: bool = False) -> int:
    """`K11` (#41): переиндексировать ОДИН документ, не трогая точки остальных.

    ЗАЧЕМ. `recreate_collection` сносит коллекцию целиком: добавление одного приказа
    переиндексирует все 311 пунктов, то есть прогоняет e5 по всему корпусу. Дальше в очереди
    четыре задачи, которые ДОБАВЛЯЮТ документы (`K15`, `K14`, `K16`, `K17`) — без этого каждая
    платит полной переиндексацией.

    ⚠⚠ `avgdl` СЧИТАЕТСЯ ПО ВСЕМУ КОРПУСУ (`recs_all`), а не по целевому документу. Причина — в
    `corpus_avgdl`: иначе вектора документа окажутся в другом масштабе BM25, чем у соседей.
    Поэтому весь корпус здесь разбирается (дёшево), а эмбеддится только целевой документ.

    ⚠ СНАЧАЛА UPSERT, ПОТОМ УБОРКА. Обратный порядок («удалить документ, затем загрузить») даёт
    окно, в котором пункты этого документа отсутствуют, — а сервис в это время отвечает. Уборка
    точечная: точки ЭТОГО документа, которых нет в новой выдаче (пункт исчез из редакции).

    Возвращает число загруженных пунктов."""
    from qdrant_client import models

    name = settings.QDRANT_RULES_COLLECTION
    if not client.collection_exists(name):
        sys.exit(f"коллекции {name} нет — сначала полная загрузка (без --doc)")

    target = [r for r in recs_all if r.get("doc_type") == doc_type]
    if not target:
        sys.exit(f"в корпусе нет пунктов документа {doc_type!r} — проверьте манифест")

    # ⚠⚠ ДОБАВЛЕНИЕ ДОКУМЕНТА — НЕ ТО ЖЕ, ЧТО ЕГО ПЕРЕИНДЕКСАЦИЯ. Найдено при заводе K15.
    # `avgdl` считается по ВСЕМУ корпусу и ЗАПЕКАЕТСЯ в sparse-вектор каждой точки. Пока документ
    # уже был в коллекции, частичная загрузка безопасна: состав корпуса не менялся, avgdl тот же.
    # Но НОВЫЙ документ меняет среднюю длину для ВСЕХ — и тогда в другом масштабе оказываются не
    # его точки, а точки СОСЕДЕЙ, загруженные раньше. Замер на K15: 95.84 -> 96.72 (+0.92 %).
    # Сдвиг мал, но он неизмерен по влиянию, а «мало» без замера — это догадка. Полная загрузка
    # стоит минуты; молчаливое расхождение масштабов BM25 не стоит ничего заметить.
    # ⚠ Предохранитель стоит НА ПУТИ действия, а не рядом с ним: оператор идёт сюда именно тогда,
    # когда добавляет документ (K15, K14, K16, K17 — все четыре про это).
    if not _document_has_points(client, name, doc_type) and not allow_new:
        sys.exit(
            f"{doc_type}: документа НЕТ в коллекции {name} — это ДОБАВЛЕНИЕ, а не переиндексация.\n"
            f"Новый документ меняет avgdl корпуса, а он запечён в векторах уже загруженных точек:\n"
            f"  соседи останутся в прежнем масштабе BM25, новый документ — в новом.\n"
            f"Нужна ПОЛНАЯ загрузка коллекции (без --doc).\n"
            f"Если расхождение осознанно принято — повторите с --allow-new-doc.")

    index_all(client, target, batch=batch, avgdl=corpus_avgdl(recs_all))

    fresh_ids = [point_id(r) for r in target]
    client.delete(
        collection_name=name,
        points_selector=models.FilterSelector(filter=models.Filter(
            must=[models.FieldCondition(key="doc_type", match=models.MatchValue(value=doc_type))],
            must_not=[models.HasIdCondition(has_id=fresh_ids)],
        )),
    )
    print(f"{doc_type}: загружено {len(target)} пунктов, устаревшие точки этого документа убраны")
    return len(target)


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
    ap.add_argument("--doc", metavar="DOC_TYPE",
                    help="K11: переиндексировать ОДИН документ, не трогая остальные "
                         "(коллекция не пересоздаётся)")
    ap.add_argument("--allow-new-doc", action="store_true",
                    help="разрешить частичную загрузку документа, которого в коллекции ещё"
                         " нет (он сдвигает avgdl корпуса — обычно нужна полная загрузка)")
    ap.add_argument("--list-docs", action="store_true", help="показать документы корпуса и выйти")
    ap.add_argument("--batch", type=int, default=64)
    args = ap.parse_args()

    if args.list_docs:
        for doc in kb_manifest.documents(COLLECTION):
            mark = " " if doc.get("status") == kb_manifest.ACTIVE else "×"
            print(f" {mark} {doc['doc_type']:32} {doc.get('short') or doc.get('title')}")
        return

    client = make_client()

    if args.doc:
        # ⚠ Разбираем ВЕСЬ корпус, грузим ОДИН документ: `avgdl` обязан быть корпусным
        # (см. `corpus_avgdl`), а дорог здесь только прогон e5 — он и экономится.
        recs, _ = load_records()
        print(f"Коллекция: {settings.QDRANT_RULES_COLLECTION} (частичная переиндексация)")
        reindex_document(client, args.doc, recs, batch=args.batch,
                         allow_new=args.allow_new_doc)
        info = client.get_collection(settings.QDRANT_RULES_COLLECTION)
        print(f"\n✅ Коллекция '{settings.QDRANT_RULES_COLLECTION}': точек = {info.points_count}")
    elif not args.smoke_only:
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
