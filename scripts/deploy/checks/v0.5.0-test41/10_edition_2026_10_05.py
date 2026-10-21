"""Актуализация под ред. ПП №719 от 05.10.2026 N 1297 — на БОЕВОМ индексе, а не в файлах.

ЗАЧЕМ ИМЕННО ЭТА ПРОВЕРКА. Релиз — это данные: раздел VII изложен заново на баллах (N 1297,
126 → 175 записей, таблица прим. 80.1 «группа + виды»), правки XI, XVII, XXI, XXII и сноски
<61>–<66>. Счётчик `EXPECT` видит ЧИСЛО точек и не видит их СОДЕРЖАНИЯ — класс `D12` («код
новый, данные прежние, снаружи неотличимо от успеха»). Поэтому блоки 1–4 утверждают то, что на
корпусе `test40` (ред. N 1254) ЛОЖНО. Положительный контроль 21.10.2026 на локальных коллекциях
обеих редакций: прежнее состояние — 9 провалов из 11, новое — 0. Блоки 5 и 6 проходят на обоих:
5 стережёт регрессию, найденную по ходу актуализации (мутацией она краснеет, см.
`tests/test_edition_2026_10_05.py`), 6 — контроль.

  1. текст корпуса и ссылка «открыть редакцию» — на N 1297; акта N 1257 (вступает 31.10) нет;
  2. индекс `pp719`: у КАЖДОЙ точки штамп редакции N 1297, в VII ровно 175 точек; позиции,
     исключённой N 1297, в индексе нет;
  3. индекс `pp719_rules`: сноски <61>…<66> — свои записи (в прошлой редакции их не было);
  4. позиция, которой в прошлой редакции не было, находится поиском и получает порог прим. 80.1;
  5. строка 80.1 не раздаётся позиции, которой в таблице нет (правило «≥2 общих слова» раздавало);
  6. КОНТРОЛЬ: нетронутый раздел отвечает как прежде.

⚠ Блоки 1 и 5 офлайновые; 2–4 и 6 требуют живого Qdrant — ради них проверка и стоит в выкатке.
Имена коллекций берутся из настроек, поэтому ту же проверку можно прогнать локально на любых
коллекциях (`QDRANT_COLLECTION=… QDRANT_RULES_COLLECTION=…`).
"""
from app.core.console import enable_utf8

enable_utf8()  # #107: проверка печатает значки вне cp1251

from qdrant_client import models  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.rag import edition, retriever  # noqa: E402
from app.rag.thresholds import lookup_threshold  # noqa: E402

EDITION = "ред. от 05.10.2026 N 1297"
KONTUR_ID = "508767"
VII_POINTS = 175              # structured/VII_medizdeliya.json этой редакции (было 126)
# Исключена N 1297 (акт, стр. 2–4): «из 32.50.13.190 "Устройства для переливания крови…"».
EXCLUDED = "Устройства для переливания крови, кровезаменителей и инфузионных растворов, в том числе и с микрофильтром"
# Позиция, которой в ред. 29.09.2026 не было (код 32.50.22.170 появился с N 1297).
NEW_POS = (["26.60.14.120", "32.50.22.170", "32.50.22.190"], "Система кохлеарной имплантации")
NEW_THR = "2026 год — не менее 65 баллов; 2027 год — не менее 155 баллов"
# Позиция VII, строки которой в 80.1 нет: до правки ей уходили виды «Устройств для переливания».
FOREIGN = (["32.50.13.190"], "Аппараты и одноразовые системы для переливания крови; ванны ультразвуковые "
           "для дезинфекции и очистки медицинских инструментов")


def _count(collection: str, flt) -> int:
    return retriever._client().count(collection_name=collection, count_filter=flt, exact=True).count


def _match(key: str, value: str):
    return models.FieldCondition(key=key, match=models.MatchValue(value=value))


def _rules_point(point: str) -> dict | None:
    pts, _ = retriever._client().scroll(
        collection_name=settings.QDRANT_RULES_COLLECTION, limit=2, with_payload=True,
        scroll_filter=models.Filter(must=[_match("doc_type", "appendix_footnotes"),
                                          _match("point", point)]))
    return pts[0].payload if pts else None


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
    got = edition.corpus_edition()
    check(got == EDITION, f"текст корпуса — {got}", f"текст корпуса на «{got}», ждали «{EDITION}»")
    url = edition.kontur_719_url() or ""
    check(url.endswith(f"documentId={KONTUR_ID}"), f"ссылка на редакцию у Контура — {KONTUR_ID}",
          f"ссылка ведёт не на действующую редакцию: {url}")
    # Проверка идёт через `python - < файл`: `__file__` нет, корень — у модуля редакции.
    full = (edition._ROOT / "knowledge_base" / "pp719" / "pp719_full.txt").read_text(encoding="utf-8")
    check("N 1297" in full and "N 1257" not in full, "в тексте N 1297, акта N 1257 нет",
          "текст корпуса не той редакции: " + ("есть N 1257 (вступает 31.10)" if "N 1257" in full
                                                 else "нет N 1297"))

    # --- 2. товарный индекс -----------------------------------------------------------------
    coll = settings.QDRANT_COLLECTION
    total = _count(coll, None)
    stale = _count(coll, models.Filter(must_not=[_match("edition", EDITION)]))
    # ⚠ Положительный контроль: «устаревших 0» при пустой коллекции — не результат.
    check(total > 1000 and stale == 0, f"{coll}: {total} точек, все со штампом {EDITION}",
          f"{coll}: точек {total}, без штампа новой редакции {stale} — индекс не пересобран")
    pts, _ = retriever._client().scroll(
        collection_name=coll, limit=1000, with_payload=["product_name"],
        scroll_filter=models.Filter(must=[_match("section_roman", "VII")]))
    check(len(pts) == VII_POINTS, f"{coll}: раздел VII — {len(pts)} точек",
          f"{coll}: раздел VII — {len(pts)} точек, ждали {VII_POINTS} (прошлая редакция — 126)")
    left = [p for p in pts if (p.payload.get("product_name") or "").startswith(EXCLUDED)]
    check(len(pts) > 0 and not left, "исключённой N 1297 позиции в индексе нет",
          f"в индексе исключённая позиция «{EXCLUDED[:50]}…» ({len(left)})")

    # --- 3. процедурный индекс --------------------------------------------------------------
    missing = [f"<{n}>" for n in range(61, 67) if _rules_point(f"<{n}>") is None]
    check(not missing, "сноски <61>–<66> — свои записи",
          f"сносок нет в {settings.QDRANT_RULES_COLLECTION}: {', '.join(missing)}")

    # --- 4. новая позиция находится и несёт порог 80.1 --------------------------------------
    hits = retriever.search(NEW_POS[1].lower(), okpd2="32.50.22.170", limit=3)
    top = hits[0] if hits else None
    check(bool(top) and top.product_name == NEW_POS[1] and top.section_roman == "VII",
          f"«{NEW_POS[1]}» (32.50.22.170) — top-1 в VII",
          f"новая позиция не находится: top-1 = {top.product_name if top else None}")
    thr = lookup_threshold(NEW_POS[0], NEW_POS[1], "VII") or ""
    check(NEW_THR in thr and "[прим. 80.1]" in thr, f"порог «{NEW_POS[1]}»: {thr[:70]}…",
          f"порог новой позиции не из прим. 80.1: {thr[:120]!r}")

    # --- 5. строка 80.1 не уходит позиции, которой в таблице нет -----------------------------
    v = lookup_threshold(*FOREIGN, "VII")
    check(v is None, "«Аппараты и одноразовые системы для переливания крови…» — чужой строки 80.1 нет",
          f"позиции отдан порог чужой строки: {(v or '')[:120]!r}")

    # --- 6. контроль: нетронутый раздел -----------------------------------------------------
    ctl = retriever.search("бульдозеры на гусеничных тракторах", okpd2="28.92.21.110", limit=3)
    c0 = ctl[0] if ctl else None
    check(bool(c0) and "28.92.21.110" in (c0.okpd2_codes or []),
          f"контроль: бульдозеры → {c0.product_name if c0 else None} [{c0.section_roman if c0 else '?'}]",
          f"нетронутый раздел отвечает иначе: top-1 = {c0.product_name if c0 else None}")

    print(f"\nИТОГ: провалов {bad}")
    return 1 if bad else 0


raise SystemExit(main())
