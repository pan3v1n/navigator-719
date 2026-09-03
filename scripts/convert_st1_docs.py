"""Исходники СТ-1 → текст корпуса (`K14` #35): Соглашение СНГ и Приказ ТПП РФ №14.

ЗАЧЕМ СКРИПТ, А НЕ РАЗОВАЯ КОНВЕРТАЦИЯ. Исходники в репозиторий не едут (RTF 1 МБ и MHTML 1.6 МБ
репозиторию ничего не дают, а история необратима), поэтому единственное, что связывает файлы
`knowledge_base/pp719/*.txt` с первоисточниками, — этот файл. Тот же довод, что у
`convert_metodrek.py`: разбор первоисточника обязан воспроизводиться.

============================================================================================
СОГЛАШЕНИЕ СНГ — из корпуса ВЫРЕЗАЕТСЯ ПРИЛОЖЕНИЕ 1 (ПЕРЕЧЕНЬ УСЛОВИЙ), И ЭТО ГЛАВНОЕ РЕШЕНИЕ
============================================================================================
Перечень — 91 700 знаков, 65 % документа — уже разобран в ТАБЛИЦУ ФАКТОВ
(`scripts/convert_st1_perechen.py` → `classifiers/tnved_st1_conditions.json`) и обслуживается
детерминированным лукапом `app/rag/st1_ref.py`.

⚠⚠ ПОЛОЖИТЬ ЕГО ЕЩЁ И В ВЕКТОР ЗНАЧИЛО БЫ ВЕРНУТЬ РОВНО ТОТ ДЕФЕКТ, КОТОРЫЙ ТАБЛИЦА УСТРАНЯЕТ.
Перечень — список ИСКЛЮЧЕНИЙ из общего правила, поэтому вероятностный поиск по нему не даёт
пустого ответа: он даёт ДРУГОЕ, правдоподобное и неверное условие. Хуже того, два источника
одного факта расходятся молча — в контекст попадал бы то фрагмент таблицы (без соседних строк,
без пометки «из», без диапазона), то результат лукапа, и различить их в ответе было бы нечем.
Один факт — одно место. Это тот же принцип, по которому пороги баллов живут в данных, а не в
промпте.

Из корпуса также вырезаются БЛАНКИ (приложения 2, 3, 5): это подписи граф формы, а не норма.
Их присутствие в векторе — ровно класс `D12` («записи-обрывки: имя позиции есть, смысла нет»).
Приложение 4 (Положение об электронной системе сертификации) — проза нормы, остаётся.

============================================================================================
ПРИКАЗ ТПП РФ №14 — MHTML, НЕ WORD, И С ОБВЯЗКОЙ САЙТА
============================================================================================
Файл имеет расширение `.doc`, но это выгрузка alta.ru в MHTML. Первые строки текста — контакты
и адрес агрегатора («+7 (495) 995-95-55», «alta@alta.ru», «www.alta.ru»). ⚠ В корпусе это стало
бы ТЕЛЕФОНОМ РЯДОМ С НОРМОЙ: faithfulness-гард считает заземлённым всё, что лежит в контексте, и
выдуманным такой номер уже не назовёт — ровно класс, из-за которого заводился `EV5`. Агрегатор
к тому же не является первоисточником, а сервис печатает источники пользователю.

⚠ Записанные в задачнике «210 642 символа» ВОСПРОИЗВЕСТИ НЕ УДАЛОСЬ: эта выгрузка даёт 223 958.
Число зависит от способа извлечения (какие части MHTML берутся, как схлопываются пробелы) и
потому КРИТЕРИЕМ БЫТЬ НЕ МОЖЕТ — оставлено как справка. Соглашение СНГ сошлось точно: 155 853.

У КАЖДОГО ВЫРЕЗАНИЯ ЕСТЬ ПОЛОЖИТЕЛЬНЫЙ КОНТРОЛЬ: скрипт падает, если не нашёл того, что режет.
«Ноль срабатываний» и «формат исходника изменился, чистка ослепла» снаружи неразличимы, а второй
случай тихо занёс бы в корпус и Перечень, и телефон агрегатора.

Запуск:
    .venv\\Scripts\\python scripts/convert_st1_docs.py
    .venv\\Scripts\\python scripts/convert_st1_docs.py --check
"""

from __future__ import annotations

import argparse
import email
import html as htmllib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.console import enable_utf8  # noqa: E402

enable_utf8()

DL = Path.home() / "Downloads"
DEFAULT_SNG = DL / ("Соглашение о правилах определения страны происхождения товаров "
                    "в Содружестве Независимых Государств.rtf")
DEFAULT_PRIKAZ = DL / "24a00014.doc"
OUT_SNG = ROOT / "knowledge_base" / "pp719" / "sng_origin_rules.txt"
OUT_PRIKAZ = ROOT / "knowledge_base" / "pp719" / "prikaz14_tpp_full.txt"

# ⚠ ЗАГОЛОВОК ПРИЛОЖЕНИЯ РАЗОРВАН ПЕРЕВОДАМИ СТРОК И НЕОДИНАКОВ: «Приложение 2 \nк Правилам…»
# (с хвостовым пробелом) против «Приложение 4\nк Правилам…» (без). Поэтому граница задаётся
# ШАБЛОНОМ, а не литералом: первая редакция искала строку целиком и не находила ничего —
# положительный контроль это и поймал.
def _appx(n: int) -> re.Pattern:
    return re.compile(rf"Приложение\s+{n}\s*\nк Правилам")


# Приложения Соглашения, которые в корпус НЕ идут, и почему.
_SNG_CUTS = [
    (_appx(1), _appx(2),
     "Приложение 1 (Перечень условий) — обслуживается таблицей фактов classifiers/"
     "tnved_st1_conditions.json и лукапом st1_ref; в вектор не идёт, чтобы факт жил в одном месте"),
    (_appx(2), _appx(4),
     "Приложения 2 и 3 — БЛАНКИ сертификата и дополнительного листа: подписи граф, а не норма"),
    (_appx(5), None,
     "Приложение 5 — бланк декларации о происхождении"),
]
# Обвязка выгрузки alta.ru: контакты агрегатора в начале текста.
_ALTA_CHROME = re.compile(r"^\s*(?:\+7\s*\(\d{3}\)[\d\s-]+|[\w.@-]+@alta\.ru|www\.alta\.ru"
                          r"|Таможенные документы\s*::.*|Альта-Софт.*)\s*$", re.I)


def _cut_span(text: str, start_re: re.Pattern, end_re: re.Pattern | None) -> tuple[str, int]:
    """Вырезать участок [начало start_re, начало end_re). Возвращает текст и сколько знаков убрано.
    ⚠ Возвращает 0, если начальный маркер не найден — решение о провале принимает вызывающий."""
    ms = start_re.search(text)
    if not ms:
        return text, 0
    i = ms.start()
    me = end_re.search(text, ms.end()) if end_re else None
    j = me.start() if me else len(text)
    return text[:i] + text[j:], j - i


def convert_sng(path: Path) -> tuple[str, list[str]]:
    from striprtf.striprtf import rtf_to_text

    raw = rtf_to_text(path.read_bytes().decode("cp1251", "replace"), errors="ignore")
    notes = [f"исходник: {len(raw)} знаков"]
    text = raw
    for start, end, reason in _SNG_CUTS:
        text, removed = _cut_span(text, start, end)
        notes.append(f"  [cut] {removed} знаков: {reason}")
        if not removed:
            sys.exit(f"ОСТАНОВ: не найден фрагмент «{start.pattern}» — формат исходника изменился, "
                     "чистка ослепла. Проверить документ, а не снимать контроль.")
    notes.append(f"в корпус: {len(text)} знаков")
    return text, notes


def convert_prikaz(path: Path) -> tuple[str, list[str]]:
    msg = email.message_from_bytes(path.read_bytes())
    parts = [p for p in msg.walk() if p.get_content_type() == "text/html"]
    if not parts:
        sys.exit("ОСТАНОВ: в MHTML нет части text/html — не тот файл")
    part = parts[0]
    raw = (part.get_payload(decode=True) or b"").decode(
        part.get_content_charset() or "cp1251", "replace")

    body = re.sub(r"(?is)<(script|style|head).*?</\1>", " ", raw)
    body = re.sub(r"(?s)<[^>]+>", "\n", body)
    body = htmllib.unescape(body)
    lines = [" ".join(ln.split()) for ln in body.splitlines()]

    kept, chrome = [], 0
    for ln in lines:
        if not ln:
            continue
        if _ALTA_CHROME.match(ln):
            chrome += 1
            continue
        kept.append(ln)
    if not chrome:
        sys.exit("ОСТАНОВ: обвязка alta.ru не найдена ни в одной строке. Либо формат выгрузки "
                 "изменился и чистка ослепла, либо это другой файл. Контроль не снимать: "
                 "иначе телефон агрегатора уедет в корпус рядом с нормой.")
    notes = [f"HTML-часть: {len(raw)} знаков, строк {len(lines)}",
             f"  [cut] строк обвязки alta.ru: {chrome}",
             f"в корпус: {sum(len(x) for x in kept)} знаков, строк {len(kept)}"]
    return "\n".join(kept), notes


def main() -> None:
    ap = argparse.ArgumentParser(description="Исходники СТ-1 → текст корпуса (K14)")
    ap.add_argument("--sng", type=Path, default=DEFAULT_SNG)
    ap.add_argument("--prikaz", type=Path, default=DEFAULT_PRIKAZ)
    ap.add_argument("--check", action="store_true", help="разобрать и проверить, не записывать")
    args = ap.parse_args()

    for label, src, conv, out in (
            ("СОГЛАШЕНИЕ СНГ", args.sng, convert_sng, OUT_SNG),
            ("ПРИКАЗ ТПП РФ №14", args.prikaz, convert_prikaz, OUT_PRIKAZ)):
        print(f"\n=== {label} ===")
        if not src.exists():
            sys.exit(f"ОСТАНОВ: нет исходника {src}")
        text, notes = conv(src)
        for n in notes:
            print(n)
        if args.check:
            print("(--check: файл не записан)")
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"записано: {out.relative_to(ROOT)} ({out.stat().st_size} байт)")


if __name__ == "__main__":
    main()
