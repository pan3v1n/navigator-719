"""test46: перечни и строки ячеек по первоисточнику (D23 #251, D24 #267, D25 #285, D19 #210, D18 #208) и
слияние RRF пула на нашей стороне (R31-3 #247) — на БОЕВОМ индексе.

ЗАЧЕМ ИМЕННО ЭТА ПРОВЕРКА. Релиз — это прежде всего данные: обе коллекции пересоздаются (`REINDEX_FULL`), а
счётчики точек у них те же, что у test45 (1584 / 767). Доехала ли новая сборка до поиска — видно только по
содержимому индекса и по тексту, который получает эксперт (панель «Источник», `pipeline.source_text`, — тот же
отбор, что у контекста модели). Каждый пункт ниже на данных test45 даёт провал (положительный контроль снят).

Что проверяется (провал любого — провал выкатки):
  1. D23 — общие перечни целиком: «Комбайны зерноуборочные» III-57 — 178 операций (на test45 — 1), суда XVIII-333…335
     — по 412 (одна ячейка из семи кусков);
  2. D24 — строки ячейки, выпавшие при разборе: у «Бульдозеров на гусеничных тракторах» III-2 в панели права на
     документацию, сервисный центр и условие <18>;
  3. D25 — доля иностранных товаров со своими датами: «Корнеты» XXV-29 — «до 31 декабря 2020 г. …», «с 1 января
     2021 г. …» (на test45 те же проценты без дат);
  4. пороги спецмашиностроения — график прим. 38.1 у IX-108, IX-111, IX-133 (на test45 — «140 баллов», чужая
     строка); СТОРОЖ: IX-92 «Продукция радиоэлектроники» — прим. 38, как на бою (раунд 6 ревью нашёл, что сборка
     его теряла);
  5. инвариант корпуса (`scripts/kb_invariants.py`) по payload ИНДЕКСА — ровно 6 записей: 5 законных повторов
     источника + известный дефект XXIII-3 (тесты в образ не входят, список — здесь);
  6. D18 — служебная строка Контура «нумерация сносок соответствует…» не вклеена ни в одну запись процедурного
     индекса, сноски <25>, <53>, <56> на месте;
  7. R31-3 — путь ответа сливает dense и sparse сам: ни один запрос к Qdrant при товарном, процедурном и
     поиске по наименованию не несёт `FusionQuery`, списки идут пакетом `query_batch_points`;
  8. КОНТРОЛЬ: нетронутое — «Иглы швейные» (N 1257) top-1 по коду с порогом 115.

⚠ Пункт 5 импортирует `scripts/kb_invariants.py` из образа. Пункты 1–7 требуют живого Qdrant. Имена коллекций —
из настроек: локально та же проверка идёт на любых коллекциях (`QDRANT_COLLECTION=… QDRANT_RULES_COLLECTION=…`).
"""
from app.core.console import enable_utf8

enable_utf8()  # #107: проверка печатает значки вне cp1251

import sys  # noqa: E402
from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from qdrant_client import QdrantClient, models  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.rag import pipeline, retriever  # noqa: E402
from app.rag.thresholds import lookup_threshold  # noqa: E402

ANCHOR = "Приложение к ПП №719, Раздел {}, позиция {}"
INVARIANT_EXPECTED = {"III-46", "VII-16", "VII-17", "VII-23", "XXII-12", "XXIII-3"}
KONTUR_LINE = "нумерация сносок соответствует"
NEEDLES = ("25.93.18.110", "Иглы швейные из черных металлов")


def _scroll(collection: str, flt=None) -> list:
    out, off = [], None
    while True:
        pts, off = retriever._client().scroll(collection_name=collection, scroll_filter=flt, limit=512,
                                              with_payload=True, offset=off)
        out += pts
        if off is None:
            return out


def _match(key: str, value: str):
    return models.FieldCondition(key=key, match=models.MatchValue(value=value))


def _hit(sec: str, pos: int):
    pts = _scroll(settings.QDRANT_COLLECTION, models.Filter(must=[_match("source_anchor", ANCHOR.format(sec, pos))]))
    # scroll отдаёт Record без score, `_to_hit` ждёт ScoredPoint
    return retriever._to_hit(SimpleNamespace(id=pts[0].id, payload=pts[0].payload, score=0.0)) if pts else None


def _ops(h) -> int:
    return sum(len(b.get("operations") or []) for b in (h.requirement_blocks or [])) if h else 0


def _panel(h) -> str:
    return pipeline.source_text(h, cap=100000) if h else ""


def _threshold(h) -> str:
    return next((ln for ln in _panel(h).splitlines() if ln.startswith("Порог")), "")


def main() -> int:
    bad = 0

    def check(ok: bool, good: str, fail: str) -> None:
        nonlocal bad
        if ok:
            print(f"   OK: {good}")
        else:
            bad += 1
            print(f"   ❌ {fail}")

    # --- 1. D23: общие перечни целиком ------------------------------------------------------
    n57 = _ops(_hit("III", 57))
    check(n57 == 178, "III-57 «Комбайны зерноуборочные» — 178 операций", f"III-57: операций {n57}, ждали 178 (test45 — 1)")
    ships = {p: _ops(_hit("XVIII", p)) for p in (333, 334, 335)}
    check(set(ships.values()) == {412}, "XVIII-333…335 — по 412 операций (суда, одна ячейка)",
          f"XVIII-333…335: операций {ships}, ждали по 412")

    # --- 2. D24: строки ячейки ------------------------------------------------------------
    p2 = _panel(_hit("III", 2))
    got = {"права на документацию": "налогового резидента" in p2, "сервисный центр": "сервисного центра" in p2,
           "условие <18>": "<18>" in p2}
    check(all(got.values()), "III-2 «Бульдозеры»: в панели права на документацию, сервисный центр, условие <18>",
          f"III-2 «Бульдозеры»: строки ячейки не доехали — {got}")

    # --- 3. D25: доля со своими датами ----------------------------------------------------
    p29 = _panel(_hit("XXV", 29))
    dated = ("до 31 декабря 2020 г. соблюдение процентной доли" in p29
             and "с 1 января 2021 г. соблюдение процентной доли" in p29)
    check(dated, "XXV-29 «Корнеты»: доля иностранных товаров с датами ступеней",
          "XXV-29 «Корнеты»: доля без дат — эксперт не отличит ступени 55 % и 50 %")

    # --- 4. пороги спецмашиностроения + сторож IX-92 ---------------------------------------
    for pos in (108, 111, 133):
        thr = _threshold(_hit("IX", pos))
        check("[прим. 38.1]" in thr and "не менее 245 баллов" in thr and "140 баллов" not in thr,
              f"IX-{pos}: порог прим. 38.1", f"IX-{pos}: порог не тот — {thr[:160]!r}")
    thr92 = _threshold(_hit("IX", 92))
    check("[прим. 38]" in thr92 and "38.1" not in thr92 and "2028 год и далее — не менее 245 баллов" in thr92,
          "IX-92 «Продукция радиоэлектроники»: порог прим. 38 (сторож)",
          f"IX-92: порог потерян или чужой — {thr92[:160]!r}")

    # --- 5. инвариант по индексу ------------------------------------------------------------
    scripts = Path(retriever.__file__).resolve().parents[2] / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        import kb_invariants as inv
    except ImportError as e:
        check(False, "", f"инвариант не импортируется из образа: {e}")
    else:
        found = {inv.key(p.payload) for p in _scroll(settings.QDRANT_COLLECTION) if inv.record_violations(p.payload)}
        check(found == INVARIANT_EXPECTED, f"инвариант по индексу: {len(found)} записей — ровно ожидаемые",
              f"инвариант по индексу: лишние {sorted(found - INVARIANT_EXPECTED)}, "
              f"пропали {sorted(INVARIANT_EXPECTED - found)}")

    # --- 6. D18: служебная строка Контура ---------------------------------------------------
    rules = _scroll(settings.QDRANT_RULES_COLLECTION)
    glued = [(p.payload.get("doc_type"), p.payload.get("point")) for p in rules
             if KONTUR_LINE in (p.payload.get("text") or "").lower()]
    marks = {(p.payload.get("point") or "") for p in rules if p.payload.get("doc_type") == "appendix_footnotes"}
    check(not glued and {"<25>", "<53>", "<56>"} <= marks,
          f"{settings.QDRANT_RULES_COLLECTION}: строки Контура нет ни в одной из {len(rules)} записей, сноски на месте",
          f"служебная строка Контура в записях {glued[:5]} (всего {len(glued)}); сноски <25>/<53>/<56>: "
          f"{sorted({'<25>', '<53>', '<56>'} & marks)}")

    # --- 7. R31-3: слияние на нашей стороне -------------------------------------------------
    seen = {"fusion": 0, "batch": 0, "single": 0}

    def has_fusion(q) -> bool:
        if q is None:
            return False
        if isinstance(q, models.FusionQuery):
            return True
        nested = getattr(q, "prefetch", None) or []
        return any(has_fusion(getattr(x, "query", None)) or has_fusion(x)
                   for x in (nested if isinstance(nested, list) else [nested]))

    orig_qp, orig_qbp = QdrantClient.query_points, QdrantClient.query_batch_points

    def spy_qp(self, *a, **kw):
        seen["single"] += 1
        pre = kw.get("prefetch") or []
        if has_fusion(kw.get("query")) or any(has_fusion(getattr(x, "query", None))
                                               for x in (pre if isinstance(pre, list) else [pre])):
            seen["fusion"] += 1
        return orig_qp(self, *a, **kw)

    def spy_qbp(self, *a, **kw):
        seen["batch"] += 1
        reqs = kw.get("requests") or (a[1] if len(a) > 1 else [])
        seen["fusion"] += sum(1 for r in reqs if has_fusion(getattr(r, "query", None))
                              or any(has_fusion(getattr(x, "query", None)) for x in (getattr(r, "prefetch", None) or [])))
        return orig_qbp(self, *a, **kw)

    QdrantClient.query_points, QdrantClient.query_batch_points = spy_qp, spy_qbp
    try:
        retriever.search("бульдозеры на гусеничных тракторах", okpd2="28.92.21.110", limit=3)
        retriever.search("Устройства наведения промышленные", okpd2=None, limit=5)
        retriever.search_rules("как получить сертификат происхождения СТ-1")
    finally:
        QdrantClient.query_points, QdrantClient.query_batch_points = orig_qp, orig_qbp
    check(seen["fusion"] == 0 and seen["batch"] >= 3,
          f"R31-3: путь ответа без FusionQuery, пакетных запросов {seen['batch']}",
          f"R31-3: FusionQuery на пути ответа — {seen['fusion']} раз, пакетных запросов {seen['batch']} "
          f"(одиночных {seen['single']})")

    # --- 8. контроль: нетронутое -------------------------------------------------------------
    hits = retriever.search("иглы швейные из черных металлов", okpd2=NEEDLES[0], limit=3)
    top = hits[0] if hits else None
    thr = ((top.min_threshold if top else None)
           or lookup_threshold([NEEDLES[0]], NEEDLES[1], "XXIII") or "")
    check(bool(top) and top.product_name == NEEDLES[1] and thr == "не менее 115 баллов",
          f"контроль: «{NEEDLES[1]}» — top-1, порог {thr}",
          f"контроль не прошёл: top-1 = {top.product_name if top else None}, порог {thr!r}")

    print(f"\nИТОГ: провалов {bad}")
    return 1 if bad else 0


raise SystemExit(main())
