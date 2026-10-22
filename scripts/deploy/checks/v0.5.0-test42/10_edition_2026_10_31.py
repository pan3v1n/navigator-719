"""Актуализация под редакцию ПП №719, действующую с 31.10.2026 (N 1297 + N 1257), — на БОЕВОМ индексе.

ЗАЧЕМ ИМЕННО ЭТА ПРОВЕРКА. Релиз — это данные: N 1257 добавила в раздел XXIII позицию «Иглы швейные
из черных металлов» (25.93.18.110, не менее 115 баллов) и строку её порога под примечанием 10.
Счётчик точек (1583 → 1584) говорит лишь, что загрузка прошла; доехала ли позиция до поиска и не
втянул ли её порог чужой график — видно только запросом к индексу и разбору порогов.

Что проверяется (провал любого — провал выкатки):
  1. текст корпуса: подпись редакции «ред. от 05.10.2026 N 1297», ключ «… + N 1257», ссылка
     «открыть редакцию» — Контур 508574; акт N 1257 в тексте есть;
  2. индекс `pp719`: у каждой точки штамп редакции, в XXIII ровно 12 точек (11 позиций + методичка);
  3. новая позиция находится поиском (top-1 по коду) с 15 операциями и обязательным требованием;
  4. порог: у игл — свой 115, у магнитов (прим. 10) — прежний график без фразы про иглы;
  5. КОНТРОЛЬ: нетронутый раздел отвечает как прежде.

⚠ Блоки 1 и 4 офлайновые; 2, 3 и 5 требуют живого Qdrant — ради них проверка и стоит в выкатке.
Имена коллекций берутся из настроек, поэтому ту же проверку можно прогнать локально на любых
коллекциях (`QDRANT_COLLECTION=… QDRANT_RULES_COLLECTION=…`).
"""
from app.core.console import enable_utf8

enable_utf8()  # #107: проверка печатает значки вне cp1251

import re  # noqa: E402

from qdrant_client import models  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.rag import edition, retriever  # noqa: E402
from app.rag.thresholds import lookup_threshold  # noqa: E402

EDITION = "ред. от 05.10.2026 N 1297"
KEY = "ред. от 05.10.2026 N 1297 + N 1257"
KONTUR_ID = "508574"
XXIII_POINTS = 12             # structured/XXIII_metallurgiya.json: 11 позиций + методичка (было 11)
NEEDLES = (["25.93.18.110"], "Иглы швейные из черных металлов")
MAGNETS = (["25.99.29.110"], "Магниты редкоземельные постоянные")
MAGNETS_THR = ("до 1 января 2022 г. - 70 баллов; с 1 января 2022 г. - 270 баллов; "
               "с 1 июля 2023 г. - 900 баллов [прим. 10]")


def _count(collection: str, flt) -> int:
    return retriever._client().count(collection_name=collection, count_filter=flt, exact=True).count


def _match(key: str, value: str):
    return models.FieldCondition(key=key, match=models.MatchValue(value=value))


def main() -> int:
    bad = 0

    def check(ok: bool, good: str, fail: str) -> None:
        nonlocal bad
        if ok:
            print(f"   OK: {good}")
        else:
            bad += 1
            print(f"   ❌ {fail}")

    # --- 1. текст корпуса -------------------------------------------------------------------
    edition.corpus_edition.cache_clear()
    # Код прошлого релиза ключа ссылки не знает — это провал пункта, а не падение проверки.
    key_fn = getattr(edition, "edition_key", None)
    if key_fn is not None:
        key_fn.cache_clear()
    got, key = edition.corpus_edition(), (key_fn() if key_fn else "нет edition_key")
    check(got == EDITION and key == KEY, f"текст корпуса — {got}; ключ ссылки — {key}",
          f"текст корпуса на «{got}» (ключ «{key}»), ждали «{EDITION}» / «{KEY}»")
    url = edition.kontur_719_url() or ""
    check(url.endswith(f"documentId={KONTUR_ID}"), f"ссылка на редакцию у Контура — {KONTUR_ID}",
          f"ссылка ведёт не на действующую редакцию: {url}")
    # Проверка идёт через `python - < файл`: `__file__` нет, корень — у модуля редакции.
    full = (edition._ROOT / "knowledge_base" / "pp719" / "pp719_full.txt").read_text(encoding="utf-8")
    has_1257 = re.search(r"[NН№]\s*1257(?!\d)", full) is not None
    check(has_1257 and f'25.93.18.110|{NEEDLES[1]}|' in full, "в тексте акт N 1257 и позиция «Иглы швейные»",
          "текст корпуса не той редакции: нет N 1257 или позиции 25.93.18.110")

    # --- 2. товарный индекс -----------------------------------------------------------------
    coll = settings.QDRANT_COLLECTION
    total = _count(coll, None)
    stale = _count(coll, models.Filter(must_not=[_match("edition", EDITION)]))
    # ⚠ Штамп у этой и прошлой редакции ОДИН («ред. от 05.10.2026 N 1297» — подпись по дате последнего
    # акта), поэтому этот пункт непересобранный индекс НЕ различает (ревью `A7`): его ловят XXIII = 12
    # и поиск ниже. Здесь — только что индекс не пуст и не смешан с чужой редакцией.
    check(total > 1000 and stale == 0, f"{coll}: {total} точек, все со штампом {EDITION}",
          f"{coll}: точек {total}, со штампом не этой редакции {stale}")
    n23 = _count(coll, models.Filter(must=[_match("section_roman", "XXIII")]))
    check(n23 == XXIII_POINTS, f"{coll}: раздел XXIII — {n23} точек",
          f"{coll}: раздел XXIII — {n23} точек, ждали {XXIII_POINTS} (прошлая редакция — 11)")

    # --- 3. новая позиция находится поиском -------------------------------------------------
    hits = retriever.search("иглы швейные из черных металлов", okpd2=NEEDLES[0][0], limit=3)
    top = hits[0] if hits else None
    check(bool(top) and top.product_name == NEEDLES[1] and top.section_roman == "XXIII",
          f"«{NEEDLES[1]}» (25.93.18.110) — top-1 в XXIII",
          f"новая позиция не находится: top-1 = {top.product_name if top else None}")
    ops = [o for b in (top.requirement_blocks if top else []) for o in b.get("operations") or []]
    pts = [o["points"] for o in ops if o.get("points") is not None]
    duty = any("техническую документацию" in (o.get("text") or "") for o in ops if o.get("points") is None)
    check(len(pts) == 15 and sum(pts) == 145 and duty,
          "у позиции 15 операций (145 баллов) и обязательное требование о правах на документацию",
          f"требования позиции не те: операций {len(pts)}, сумма {sum(pts)}, обязательное {duty}")

    # --- 4. пороги ---------------------------------------------------------------------------
    # Правило рантайма (`pipeline.source_text`): порог записи, иначе — добранный из примечаний.
    thr = (top.min_threshold if top else None) or lookup_threshold(*NEEDLES, "XXIII") or ""
    check(thr == "не менее 115 баллов", f"порог игл: {thr}", f"порог игл не 115: {thr!r}")
    mg = lookup_threshold(*MAGNETS, "XXIII")
    check(mg == MAGNETS_THR, "магниты (прим. 10) — прежний график, без фразы про иглы",
          f"порог магнитов изменился: {(mg or '')[:160]!r}")

    # --- 5. контроль: нетронутый раздел -----------------------------------------------------
    ctl = retriever.search("бульдозеры на гусеничных тракторах", okpd2="28.92.21.110", limit=3)
    c0 = ctl[0] if ctl else None
    check(bool(c0) and "28.92.21.110" in (c0.okpd2_codes or []),
          f"контроль: бульдозеры → {c0.product_name if c0 else None} [{c0.section_roman if c0 else '?'}]",
          f"нетронутый раздел отвечает иначе: top-1 = {c0.product_name if c0 else None}")

    print(f"\nИТОГ: провалов {bad}")
    return 1 if bad else 0


raise SystemExit(main())
