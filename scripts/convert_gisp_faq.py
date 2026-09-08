"""PDF страницы «Часто задаваемые вопросы» ГИСП → текст корпуса (`K16` #48).

ЗАЧЕМ СКРИПТ. Та же причина, что у `convert_metodrek.py` и `convert_polozhenie49.py`: исходник в
репозиторий не едет (`.gitignore`, `/*.pdf`), и единственное, что связывает
`knowledge_base/pp719/gisp_faq.txt` со страницей ГИСП, — этот файл.

ЧТО ЭТО ЗА ДОКУМЕНТ И ПОЧЕМУ ОН НЕ НОРМА. Раздел FAQ портала ГИСП «Реестры российской промышленной
и радиоэлектронной продукции». Это РАЗЪЯСНЕНИЕ ОПЕРАТОРА, а не нормативный акт: в манифесте он
идёт `legal_force: 5` («практика») и `topic: ГИСП`, и промпт обязан помечать такой источник как
«не норма» — это дословный критерий приёмки issue #48.

⚠⚠ ПОЧЕМУ ИСТОЧНИК ТОЛЬКО ПОРТАЛ, А НЕ ПЕРЕСКАЗ. `gisp.gov.ru` закрыт JS-проверкой ServicePipe:
любой путь отдаёт 1789 байт заглушки, автоматически страницу не забрать (проверено 07.09.2026).
Соблазн взять пересказ со стороннего сайта — **ловушка**: сторонние изложения этого же FAQ
называют шагом «получить ЗАКЛЮЧЕНИЕ ТПП, подтверждающее производство», а такого документа НЕ
СУЩЕСТВУЕТ — под него `K15` завела закрытый перечень и рантайм-гард. Попади такой текст в корпус,
он бы ЗАЗЕМЛИЛ выдумку, и гард пропустил бы её: гард ловит НЕзаземлённое, а не неверное.
⚠ Настоящая страница портала этой ошибки НЕ содержит (проверено: «заключени» — 0 вхождений) и
прямо отсылает к ПП №719 и приказу ТПП РФ №52, которые в корпусе уже есть.
Файл сохраняется владельцем из браузера и передаётся скрипту.

⚠⚠ ЧИСТКА С ПОЛОЖИТЕЛЬНЫМ КОНТРОЛЕМ. Со страницы срезается обвязка сайта (меню, хлебные крошки,
повтор шапки на второй странице, подвал). «Ноль срабатываний» и «вёрстка изменилась, чистка
ослепла» снаружи неразличимы — скрипт падает, если не нашёл того, что обязан найти.
⚠ Отдельно опасен подвал «Остались вопросы? Напишите нам»: он кончается знаком вопроса и без
чистки стал бы ДЕСЯТЫМ «вопросом» корпуса с пустым ответом.

Запуск:
    .venv\\Scripts\\python scripts/convert_gisp_faq.py --pdf "путь.pdf"
    .venv\\Scripts\\python scripts/convert_gisp_faq.py --pdf "путь.pdf" --check
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.console import enable_utf8  # noqa: E402

OUT = ROOT / "knowledge_base" / "pp719" / "gisp_faq.txt"

# Обвязка сайта: меню, крошки, подвал. Каждая строка ЦЕЛИКОМ, а не подстрокой, — иначе чистка
# съела бы содержательный текст, где встретилось слово «Новости» или «Каталог».
_CHROME_EXACT = (
    "Все Каталог Помощь и Меры",
    "Новости Войти",
    "сервисы продукции техподдержка поддержки",
    "Каталог Меры",
    "продукции поддержки",
)
_CHROME_PREFIX = ("Главная >", "Остались вопросы?")
_MIN_CHROME = 6          # положительный контроль на чистку
_MIN_QUESTIONS = 8       # на странице их девять

_UPPER_START_RE = re.compile(r"^[А-ЯЁA-Z«\"]")
_FORBIDDEN = ("Войти", "Напишите нам", "Главная >")

# ⚠⚠⚠ ДЕТЕКТОР ФАНТОМА БЕРЁТСЯ У `documents_ref`, А НЕ ПИШЕТСЯ ЗАНОВО (находка раунда 12).
# Первая редакция несла свою регулярку `заключени\w*\s+(?:ТПП|торгово)`, и она была СЛАБЕЕ
# рантайм-гарда в обе стороны:
#   * пропускала «ТПП выдаёт заключение, подтверждающее производство» и «нужно получить
#     заключение УПОЛНОМОЧЕННОЙ ТПП» — то есть ровно те формулировки, которыми пересказы и
#     описывают несуществующий документ. Такой текст прошёл бы конвертер, прошёл бы релизную
#     проверку и ЗАЗЕМЛИЛ бы выдумку в корпусе;
#   * не знала оговорки `_EXPERT_CONCLUSION`: «экспертное заключение» — ДЕЙСТВУЮЩИЙ документ
#     (итог выездной проверки ТПП РФ), и на странице, законно его упоминающей, конвертер упал бы.
# `unverified_documents` калиброван на обоих классах и уже пережил ложное срабатывание на бою
# 25.08. Копия детектора — это «две таблицы про одно» применительно к предохранителю.
from app.rag.documents_ref import unverified_documents  # noqa: E402


def extract(pdf_path: Path) -> list[str]:
    try:
        import pdfplumber
    except ImportError:
        sys.exit("нужен pdfplumber: .venv\\Scripts\\pip install pdfplumber")
    with pdfplumber.open(pdf_path) as pdf:
        pages = [p.extract_text() or "" for p in pdf.pages]
    return "\n".join(pages).split("\n")


def clean(lines: list[str]) -> list[str]:
    kept, dropped = [], 0
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if s in _CHROME_EXACT or s.startswith(_CHROME_PREFIX):
            dropped += 1
            continue
        kept.append(s)
    if dropped < _MIN_CHROME:
        sys.exit(f"обвязки вырезано {dropped} строк, ожидалось ≥{_MIN_CHROME} — "
                 "вёрстка страницы изменилась, чистка ослепла")
    # Заголовок раздела продублирован вёрсткой — оставляем одно вхождение.
    if len(kept) > 1 and kept[0] == kept[1]:
        kept = kept[1:]
    flow = " ".join(kept)
    for bad in _FORBIDDEN:
        if bad in flow:
            sys.exit(f"после чистки в тексте осталась обвязка «{bad}» — проверьте правила")
    phantom = unverified_documents(flow)
    if phantom:
        sys.exit(f"в тексте найдено {phantom[:2]} — признак ПЕРЕСКАЗА со стороннего сайта, а не "
                 "страницы ГИСП. Такого документа НЕ СУЩЕСТВУЕТ (`K15`), в корпус нельзя")
    return kept


def to_points(lines: list[str]) -> list[tuple[str, str]]:
    """Пары «вопрос → ответ» — по СТРОКАМ страницы, а не по предложениям.

    ⚠⚠ РАЗБОР ПО ПРЕДЛОЖЕНИЯМ ЗДЕСЬ НЕ РАБОТАЕТ, и это поймал положительный контроль, а не
    рассуждение. Ответ на вопрос про ОКПД2 вне 719 заканчивается БЕЗ точки («…реестровая запись
    в Реестре российской продукции»), поэтому следующий вопрос приклеивался к нему и вся склейка
    становилась одним «вопросом» с пустым ответом. Пунктуация на этой странице необязательна —
    опираться на неё нельзя.

    Признак взят из вёрстки: вопрос ЗАКАНЧИВАЕТСЯ строкой со знаком вопроса, а НАЧИНАЕТСЯ строкой
    с заглавной буквы; перенос внутри вопроса всегда даёт строку со строчной. Ответ — всё между
    концом одного вопроса и началом следующего."""
    q_end = [i for i, s in enumerate(lines) if s.endswith("?")]
    if len(q_end) < _MIN_QUESTIONS:
        sys.exit(f"строк со знаком вопроса {len(q_end)}, ожидалось ≥{_MIN_QUESTIONS}")

    spans: list[tuple[int, int]] = []
    for e in q_end:
        s = e
        while s > 0 and not _UPPER_START_RE.match(lines[s]):
            s -= 1
        spans.append((s, e))

    out: list[tuple[str, str]] = []
    for k, (s, e) in enumerate(spans):
        a_from = e + 1
        a_to = spans[k + 1][0] if k + 1 < len(spans) else len(lines)
        question = " ".join(lines[s:e + 1]).strip()
        answer = " ".join(lines[a_from:a_to]).strip()
        out.append((question, answer))

    empty = [q for q, a in out if not a]
    if empty:
        sys.exit(f"вопрос без ответа: {empty[:2]} — разбор строк сломался")
    if len(out) < _MIN_QUESTIONS:
        sys.exit(f"вопросов найдено {len(out)}, ожидалось ≥{_MIN_QUESTIONS}")
    return out


def build(pdf_path: Path) -> str:
    pairs = to_points(clean(extract(pdf_path)))
    head = (
        "# Часто задаваемые вопросы ГИСП: реестры российской промышленной "
        "и радиоэлектронной продукции\n"
        "# Источник: gisp.gov.ru, раздел «Часто задаваемые вопросы».\n"
        "# ⚠ НЕ НОРМАТИВНЫЙ АКТ: разъяснение оператора ГИСП (Минпромторг). "
        "Юридическая сила — практика (legal_force 5).\n"
        "# Собран скриптом scripts/convert_gisp_faq.py из PDF страницы.\n"
    )
    body = "\n\n".join(f"{i}. {q}\n{a}" for i, (q, a) in enumerate(pairs, 1))
    return head + "\n" + body + "\n"


def main() -> None:
    enable_utf8()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pdf", required=True, help="путь к PDF страницы FAQ ГИСП")
    ap.add_argument("--check", action="store_true", help="только проверить, не писать файл")
    a = ap.parse_args()

    pdf = Path(a.pdf)
    if not pdf.exists():
        sys.exit(f"нет файла {pdf}")
    text = build(pdf)
    pairs = to_points(clean(extract(pdf)))
    print(f"вопросов: {len(pairs)} · знаков: {len(text)}")
    for i, (q, _) in enumerate(pairs, 1):
        print(f"  {i}. {q[:78]}")
    if a.check:
        print("--check: файл не записан")
        return
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(f"записано: {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
