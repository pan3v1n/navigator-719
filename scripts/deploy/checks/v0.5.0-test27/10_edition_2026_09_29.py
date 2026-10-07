"""Актуализация под ред. ПП №719 от 29.09.2026 N 1254 — на БОЕВОМ индексе, а не в файлах.

ЗАЧЕМ ИМЕННО ЭТА ПРОВЕРКА. Релиз — это данные: раздел XVIII изложен заново (N 1002, 187 → 344
записи), кабели раздела IV (N 1026), сноска <12> (N 1254). Счётчик `EXPECT` видит ЧИСЛО точек и
не видит их СОДЕРЖАНИЯ: коллекция, пересозданная из прежних файлов, или старый индекс под новым
кодом дали бы те же предупреждения «всё сошлось» — класс `D12` («код новый, данные прежние,
снаружи неотличимо от успеха»). Поэтому каждый блок ниже утверждает то, что на корпусе `test26`
ЛОЖНО (положительный контроль на прошлую редакцию проверен 07.10.2026 на локальных коллекциях
обеих редакций):

  1. текст корпуса и ссылка «открыть редакцию» — на N 1254;
  2. индекс `pp719`: у КАЖДОЙ точки штамп редакции N 1254, в XVIII ровно 344 точки;
  3. индекс `pp719_rules`: сноска <12> — «не более 1500 баллов с 2026 года» (было 1000);
     сноска «<12(1)>» — отдельная запись (до 07.10 разборщик вклеивал её в «<12>»);
  4. позиция, которой в прошлой редакции не было, находится поиском и получает порог из
     таблицы прим. 17(3);
  5. таблица 17(3) не раздаёт пороги судостроения чужим разделам (это уже случалось в ходе
     актуализации — поймано радиусом по всем разделам);
  6. КОНТРОЛЬ: нетронутый раздел отвечает как прежде.

⚠ Блок 1 и 5 офлайновые; 2–4 и 6 требуют живого Qdrant — ради них проверка и стоит в выкатке.
Имена коллекций берутся из настроек, поэтому ту же проверку можно прогнать локально на любых
коллекциях (`QDRANT_COLLECTION=… QDRANT_RULES_COLLECTION=…`).
"""
from app.core.console import enable_utf8

enable_utf8()  # #107: проверка печатает значки вне cp1251

from qdrant_client import models  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.rag import edition, retriever  # noqa: E402
from app.rag.thresholds import lookup_threshold  # noqa: E402

EDITION = "ред. от 29.09.2026 N 1254"
KONTUR_ID = "508572"
XVIII_POINTS = 344            # structured/XVIII_sudostroenie.json этой редакции (было 187)
# Позиция, которой в ред. 22.07.2026 не было вовсе (код 22.19.60.191 появился с N 1002).
NEW_POS = ("22.19.60.191", "Гидротермокостюмы")
NOTE_17_3 = "[прим. 17(3) к разд. XVIII]"


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
    check(url.endswith(f"documentId={KONTUR_ID}"), "ссылка на редакцию у Контура — 508572",
          f"ссылка ведёт не на действующую редакцию: {url}")

    # --- 2. товарный индекс -----------------------------------------------------------------
    coll = settings.QDRANT_COLLECTION
    total = _count(coll, None)
    stale = _count(coll, models.Filter(must_not=[_match("edition", EDITION)]))
    # ⚠ Положительный контроль: «устаревших 0» при пустой коллекции — не результат.
    check(total > 1000 and stale == 0, f"{coll}: {total} точек, все со штампом {EDITION}",
          f"{coll}: точек {total}, без штампа новой редакции {stale} — индекс не пересобран")
    n18 = _count(coll, models.Filter(must=[_match("section_roman", "XVIII")]))
    check(n18 == XVIII_POINTS, f"{coll}: раздел XVIII — {n18} точек",
          f"{coll}: раздел XVIII — {n18} точек, ждали {XVIII_POINTS} (прошлая редакция — 187)")

    # --- 3. процедурный индекс --------------------------------------------------------------
    fn12 = _rules_point("<12>") or {}
    t12 = fn12.get("text") or ""
    check("не более 1500 баллов с 2026 года" in t12 and "не более 1000 баллов с 2026 года" not in t12,
          "сноска <12>: НИОКР легковых — 1500 баллов с 2026 года (N 1254)",
          f"сноска <12> не в ред. N 1254 (запись {'есть' if t12 else 'НЕ НАЙДЕНА'})")
    check(_rules_point("<12(1)>") is not None and "<12(1)>" not in t12,
          "сноска <12(1)> — своя запись, в <12> не вклеена",
          "сноски <12(1)> нет отдельной записью — уехал прежний разборщик")

    # --- 4. новая позиция находится и несёт порог 17(3) -------------------------------------
    hits = retriever.search(NEW_POS[1].lower(), okpd2=NEW_POS[0], limit=3)
    top = hits[0] if hits else None
    check(bool(top) and top.product_name == NEW_POS[1] and top.section_roman == "XVIII",
          f"«{NEW_POS[1]}» ({NEW_POS[0]}) — top-1 в XVIII",
          f"новая позиция не находится: top-1 = {top.product_name if top else None}")
    thr = lookup_threshold([NEW_POS[0]], NEW_POS[1], "XVIII") or ""
    check(NOTE_17_3 in thr, f"порог «{NEW_POS[1]}»: {thr[:70]}…",
          f"порог новой позиции не из прим. 17(3): {thr[:120]!r}")

    # --- 5. таблица 17(3) остаётся в своём разделе ------------------------------------------
    v = lookup_threshold(["27.12.10.110"],
                         "Выключатели силовые высоковольтные напряжением 6 кВ и выше", "V") or ""
    check("прим. 27" in v and "17(3)" not in v, "раздел V держит своё прим. 27",
          f"разделу V отдан порог судостроения: {v[:120]!r}")

    # --- 6. контроль: нетронутый раздел -----------------------------------------------------
    ctl = retriever.search("бульдозеры на гусеничных тракторах", okpd2="28.92.21.110", limit=3)
    c0 = ctl[0] if ctl else None
    check(bool(c0) and "28.92.21.110" in (c0.okpd2_codes or []),
          f"контроль: бульдозеры → {c0.product_name if c0 else None} [{c0.section_roman if c0 else '?'}]",
          f"нетронутый раздел отвечает иначе: top-1 = {c0.product_name if c0 else None}")

    print(f"\nИТОГ: провалов {bad}")
    return 1 if bad else 0


raise SystemExit(main())
