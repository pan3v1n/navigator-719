"""PDF Методических рекомендаций ТПП РФ → текст корпуса (`K15` #36).

ЗАЧЕМ СКРИПТ, А НЕ РАЗОВАЯ КОНВЕРТАЦИЯ. Разбор первоисточника обязан воспроизводиться: исходник
в репозиторий не едет (см. `.gitignore` — 256 КБ PDF репозиторию ничего не дают, а история
необратима), поэтому единственное, что связывает `knowledge_base/pp719/metodrek_tpp_full.txt` с
документом ТПП, — этот файл. Урок проекта: предохранитель вне репозитория живёт один раз.

⚠⚠ ЧТО ИМЕННО ВЫРЕЗАЕТСЯ И ПОЧЕМУ ЭТО НЕ КОСМЕТИКА. PDF получен печатью страницы sudact.ru, и на
КАЖДОЙ из 14 страниц стоят два служебных элемента:
  * шапка «25.08.2026, 10:00 "Методические рекомендации…"» — это ДАТА ПЕЧАТИ. В корпусе она стала
    бы числом рядом с нормативным текстом: faithfulness-гард считает заземлённым всё, что лежит в
    контексте, и выдуманной такую дату уже не назовёт. Ровно тот класс, из-за которого заводился
    `EV5` (число в векторе притягивает вопросы про числа);
  * подвал «https://sudact.ru/law/… 7/14» — адрес агрегатора и номер страницы печати. Агрегатор
    не является первоисточником, а сервис печатает источники пользователю.

⚠ У ВЫРЕЗАНИЯ ЕСТЬ ПОЛОЖИТЕЛЬНЫЙ КОНТРОЛЬ. Скрипт падает, если не нашёл ни шапок, ни подвалов:
«ноль срабатываний» и «формат исходника изменился, чистка ослепла» снаружи неразличимы, а второй
случай тихо занёс бы даты печати в корпус.

Запуск:
    .venv\\Scripts\\python scripts/convert_metodrek.py
    .venv\\Scripts\\python scripts/convert_metodrek.py --pdf "путь.pdf" --check
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.console import enable_utf8  # noqa: E402  (только после sys.path)

enable_utf8()

DEFAULT_PDF = ROOT / "Методические рекомендации по подтверждению производства.pdf"
OUT_PATH = ROOT / "knowledge_base" / "pp719" / "metodrek_tpp_full.txt"

# Шапка печати: «25.08.2026, 10:00 "Методические рекомендации…»; дата любая — печатали не мы.
_PRINT_HEADER = re.compile(r"^\s*\d{2}\.\d{2}\.\d{4},\s*\d{1,2}:\d{2}\s")
# Подвал: адрес агрегатора и «7/14». Ловим по адресу — номер страницы стоит в той же строке.
_PRINT_FOOTER = re.compile(r"https?://\S*sudact\.ru/")


def extract(pdf: Path) -> tuple[str, int, int]:
    """Текст без служебных элементов печати + сколько шапок и подвалов вырезано."""
    import pdfplumber

    lines: list[str] = []
    heads = feet = 0
    with pdfplumber.open(pdf) as doc:
        for page in doc.pages:
            for ln in (page.extract_text() or "").split("\n"):
                if _PRINT_HEADER.match(ln):
                    heads += 1
                    continue
                if _PRINT_FOOTER.search(ln):
                    feet += 1
                    continue
                lines.append(ln.rstrip())
    # Схлопываем подряд идущие пустые строки: разрывы страниц оставляли по две-три.
    out: list[str] = []
    for ln in lines:
        if not ln.strip() and (not out or not out[-1].strip()):
            continue
        out.append(ln)
    return "\n".join(out).strip() + "\n", heads, feet


def main() -> None:
    ap = argparse.ArgumentParser(description="PDF Методрекомендаций ТПП → текст корпуса")
    ap.add_argument("--pdf", type=Path, default=DEFAULT_PDF)
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    ap.add_argument("--check", action="store_true",
                    help="только проверить, не записывая файл")
    args = ap.parse_args()

    if not args.pdf.exists():
        sys.exit(f"нет исходника: {args.pdf}\n"
                 f"Файл в репозиторий не едет (.gitignore) — положите PDF в корень проекта.")

    text, heads, feet = extract(args.pdf)

    # Положительный контроль: чистка обязана была СРАБОТАТЬ.
    if heads == 0 and feet == 0:
        sys.exit("служебных элементов печати не найдено ни одного — формат исходника изменился.\n"
                 "Проверьте вручную: молчаливый пропуск занёс бы дату печати в корпус.")
    # Отрицательный: она не должна была съесть содержимое.
    if len(text) < 15_000:
        sys.exit(f"после чистки осталось {len(text)} символов — подозрительно мало, "
                 f"проверьте регулярки")
    for must in ("1. ВВЕДЕНИЕ", "4. ДОКУМЕНТЫ", "ВЕРСИЯ"):
        if must not in text:
            sys.exit(f"в тексте нет обязательного фрагмента {must!r} — разбор неполон")

    print(f"вырезано: шапок печати {heads}, подвалов {feet}")
    print(f"текста: {len(text)} символов, {len(text.splitlines())} строк")
    if args.check:
        print("--check: файл не записан")
        return
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(f"записано: {args.out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
