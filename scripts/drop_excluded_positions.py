"""Чистка structured/*.json от пустых записей-заглушек (исключённые позиции приложения).

Раньше парсер (scripts/structure_kb.py) порождал «продукт» из строки-кода, у которой нет
ни наименования, ни требований — это исключённые из приложения позиции
(«из 28.30.31 - Позиции исключены.», «28.99.39.190 - Позиция исключена.») и осиротевшие
подкоды ОКПД2. В индексе они бесполезны и вредны: на запрос по коду всплывает хит с
ПУСТЫМ именем. Парсер исправлен (фильтр пустых строк-кодов после привязки компонентов);
этот скрипт применяет ТО ЖЕ правило к уже сгенерированным JSON — БЕЗ повторного вызова
DeepSeek (записи детерминированные, LLM для них не вызывался).

Правило выброса: record_type отсутствует И нет requirement_blocks И нет min_threshold И
product_name либо пуст, либо РАВЕН заголовку раздела. Вторая форма (D4, 12.08.2026): у строки
«из 26.51.20.121. - Код исключен.» наименования нет, и LLM подставила в product_name заголовок
раздела («Продукция радиоэлектроники»). Непустое имя проходило прежний фильтр, и запись жила
в индексе как позиция без единого требования — конкурируя за тот же код ОКПД2 с реальной
позицией. Во всём корпусе такая запись одна.

Реальные продукты с пустой ячейкой имени, но требованиями ниже (напр. 29.10.52.190)
НЕ затрагиваются — у них непустые requirement_blocks.

Идемпотентно: повторный запуск удаляет 0. Запуск:
  .venv/Scripts/python.exe scripts/drop_excluded_positions.py            # применить
  .venv/Scripts/python.exe scripts/drop_excluded_positions.py --dry-run  # только показать
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STRUCT = ROOT / "knowledge_base" / "pp719" / "structured"


def _strip_footnotes(s: str) -> str:
    """Убирает хвостовые маркеры сносок: 'Продукция радиоэлектроники <5>' → без '<5>'."""
    return re.sub(r"\s*<[\d.,\s]+>\s*$", "", s or "").strip()


def is_excluded_stub(rec: dict) -> bool:
    """True для пустой записи-заглушки исключённой позиции (см. модуль-докстринг)."""
    if rec.get("record_type"):  # методички (section_methodology) не трогаем
        return False
    if rec.get("requirement_blocks"):
        return False
    if rec.get("min_threshold"):
        return False

    name = (rec.get("product_name") or "").strip()
    if not name:
        return True
    # Вторая форма той же заглушки: у строки «из 26.51.20.121. - Код исключен.» наименования
    # нет, и LLM подставила в product_name ЗАГОЛОВОК РАЗДЕЛА. Пустое имя фильтр ловил, эту —
    # нет, поэтому запись пережила чистку и попала в индекс (D4, раздел IX).
    return _strip_footnotes(name) == _strip_footnotes(rec.get("section_title") or "") != ""


# --- Третья форма (D12, #90): наименованием стала ЯЧЕЙКА ТРЕБОВАНИЙ исключённой строки ---
#
# В первоисточнике строка читается так:
#     14.12.11,
#     14.12.21
#     14.12.30.131
#     14.12.30.132
#     14.12.30.160 - Позиции исключены.|до 1 января 2019 г.: наличие у юридического лица …
# Маркер стоит в ячейке КОДА, поэтому наименованием строки парсер взял текст ячейки ТРЕБОВАНИЙ.
# В корпус попали три записи раздела VII с именем-формулой требования и ЖИВЫМИ кодами спецодежды:
# по коду 14.12.30.131 рантайм делал целевой такой обрывок, и ответ выходил БЕЗ требований вовсе,
# хотя настоящая «Спецодежда» (код 14.12) лежит в разделе XVII и покрывает запрошенный код по
# иерархии. Код 20.59.52.120 такая заглушка делит с живым «Воском зуботехническим» (5 блоков) —
# точь-в-точь дефект D4 с кодом 26.51.20.121.
#
# ⚠ ПРИЗНАК БЕРЁТСЯ ИЗ ПЕРВОИСТОЧНИКА, А НЕ ИЗ ФОРМЫ ИМЕНИ. Регулярка по прозе здесь запрещена
# уроком EV9 (#89): под похожую регулярку подходили 16 записей корпуса, из них 13 — настоящая
# продукция. Здесь совпадение ТОЧНОЕ: имя записи обязано быть началом текста требований той самой
# строки, которую первоисточник помечает исключённой, а коды записи — подмножеством её кодов.
#
# ⚠ РАДИУС ИЗМЕРЕН. Две более слабые редакции правила проверены и ОТВЕРГНУТЫ:
#   * «пустая запись + код помечен исключённым» задевала 20 записей: коды исключённых строк
#     переиспользуются живыми позициями (27.12 «Реле защиты», 28.99.39.190 судовые системы,
#     20.30.2 лакокрасочные) — 17 из них настоящая продукция, получающая требования по НАСЛЕДОВАНИЮ;
#   * то же правило с проверкой наследования давало верные 3, но зависело от `inherited_requirements.json`
#     — генерируемого файла. Устарей он (а он уже отставал 13.08), и правило снесло бы 17 живых
#     позиций молча. Признак из первоисточника от состояния генерируемых файлов не зависит.
_MARK_RE = re.compile(r"(?:[Пп]озици[яи]|[Кк]од[ыа]?)\s+исключен[аыо]?\.?")
_CODE_RE = re.compile(r"(?:из\s+)?(\d{2}(?:\.\d+)*)")
_FULL_TXT = ROOT / "knowledge_base" / "pp719" / "pp719_full.txt"


def _norm(s: str | None) -> str:
    return " ".join((s or "").split()).lower()


def excluded_rows() -> list[tuple[set[str], str]]:
    """(коды строки, нормализованный текст её ячейки требований) для строк с маркером исключения."""
    if not _FULL_TXT.exists():
        return []
    text = _FULL_TXT.read_text(encoding="utf-8", errors="ignore")
    out: list[tuple[set[str], str]] = []
    for m in _MARK_RE.finditer(text):
        cell = text[max(0, m.start() - 300):m.start()].rsplit("|", 1)[-1]
        codes = {c.rstrip(".") for c in _CODE_RE.findall(cell) if "." in c.rstrip(".")}
        tail = text[m.end():m.end() + 1200].lstrip()
        if not codes or not tail.startswith("|"):
            continue
        req = _norm(tail[1:].split("|", 1)[0])
        # ⚠ НАЙДЕНО РЕВЬЮ 20.08.2026: здесь стояло `if req:`, и строка с ПУСТОЙ ячейкой требований
        # выбрасывалась целиком. Текст требований нужен только `is_requirement_text_stub`; второму
        # предикату, `is_section_title_stub`, нужны одни КОДЫ — а он берёт их из `excluded_codes()`,
        # построенного на этом же отфильтрованном списке. Замер на `pp719_full.txt`: 53 маркера →
        # 52 строки, потерянная — `из 32.50.13.110 Позиция исключена.||`, то есть ТРЕТЬЕ из пяти
        # канонических написаний, которые закрепляет тест. «Маркер + пустая ячейка требований» —
        # обычная форма исключённой строки, а не экзотика: без её кода предикат-фантом молчит,
        # и заглушка остаётся в индексе конкурировать за код с живыми позициями.
        # Пустой `req` для `is_requirement_text_stub` безвреден: он сверяет `req.startswith(probe)`
        # с НЕПУСТЫМ именем записи, поэтому пустая строка не совпадёт ни с чем.
        out.append((codes, req))
    return out


def excluded_codes(rows: list[tuple[set[str], str]]) -> set[str]:
    """Плоское множество кодов, помеченных первоисточником как исключённые."""
    out: set[str] = set()
    for codes, _ in rows:
        out |= codes
    return out


def is_section_title_stub(rec: dict, codes_excluded: set[str]) -> bool:
    """Исключённая позиция, которой LLM дала имя ЗАГОЛОВКА РАЗДЕЛА и ЧУЖИЕ требования.

    ⚠ Отличается от второй формы выше тем, что запись НЕ пуста: у `из 32.50.23.000 - Позиция
    исключена.` наименования нет, LLM подставила «Медицинские изделия» (заголовок раздела) и
    прицепила требования из следующей ячейки — получилась позиция-фантом с двумя блоками, которая
    отвечает требованиями по ИСКЛЮЧЁННОМУ коду. Прежний фильтр до имени не доходил: он выходил
    раньше по `if rec.get("requirement_blocks")`.

    Дефект всплыл ровно тогда, когда парсер научился отбрасывать такие строки: `reconcile` сел
    29/29 → 28/29 («в JSON но не в чанке: 32.50.23.000»), то есть расхождение сторон и показало
    запись, которую обе стороны прежде держали молча.

    ⚠ Радиус измерен по всему корпусу: таких записей РОВНО ОДНА. Условие «код подтверждён
    исключённым В ПЕРВОИСТОЧНИКЕ» здесь несущее — без него правило било бы по любой позиции,
    которую LLM назвала заголовком раздела."""
    codes = {c.strip() for c in (rec.get("okpd2_codes") or []) if c and c.strip()}
    if not codes or not codes <= codes_excluded:
        return False
    name = _strip_footnotes(rec.get("product_name") or "")
    return bool(name) and name == _strip_footnotes(rec.get("section_title") or "")


def is_requirement_text_stub(rec: dict, rows: list[tuple[set[str], str]]) -> bool:
    """True — наименование записи есть текст требований строки, ИСКЛЮЧЁННОЙ первоисточником."""
    if rec.get("record_type") or rec.get("requirement_blocks") or rec.get("min_threshold"):
        return False
    name = _norm(rec.get("product_name"))
    codes = {c.strip() for c in (rec.get("okpd2_codes") or []) if c and c.strip()}
    if not name or not codes:
        return False
    probe = name[:80]
    return any(codes <= row_codes and req.startswith(probe) for row_codes, req in rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="Удаление пустых записей исключённых позиций из structured/*.json")
    ap.add_argument("--dry-run", action="store_true", help="не записывать, только показать что удалится")
    args = ap.parse_args()

    rows = excluded_rows()
    if not rows:
        print("⚠ первоисточник не прочитан или маркеров исключения в нём нет — третья форма "
              "(D12) НЕ проверяется. Это отказ проверки, а не «чисто».")
    else:
        print(f"  строк с маркером исключения в первоисточнике: {len(rows)}")
    codes_excluded = excluded_codes(rows)

    grand_total = 0
    for jf in sorted(STRUCT.glob("*.json")):
        data = json.loads(jf.read_text(encoding="utf-8"))
        kept = [r for r in data
                if not is_excluded_stub(r)
                and not is_requirement_text_stub(r, rows)
                and not is_section_title_stub(r, codes_excluded)]
        removed = len(data) - len(kept)
        if removed:
            grand_total += removed
            print(f"  {jf.name}: {len(data)} → {len(kept)}  (−{removed})")
            if not args.dry_run:
                jf.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")

    verb = "удалится" if args.dry_run else "удалено"
    print(f"\nИТОГО записей {verb}: {grand_total}")
    if args.dry_run and grand_total:
        print("(--dry-run: файлы не изменены)")


if __name__ == "__main__":
    main()
