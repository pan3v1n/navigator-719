"""test43 — этапы 0–3 одним рантаймовым релизом: режимы источника, точность товарной ветки, сноски.

ЗАЧЕМ. Корпус и индекс не меняются (1584 / 767 / 12, как у test42), меняется код и рантайм-данные
(`knowledge_base/pp719/code_cells.json`). Счётчики точек тут ничего не скажут: доехало ли — видно только
по ответу кода на тех позициях, ради которых правки делались, и по поиску в живом индексе.

Что проверяется (провал любого — провал выкатки):
  1. `L1`/`L2`: четыре режима источника, `ChatRequest` принимает режим; колонки `corpus` в боевой базе
     появились (миграция на старте — только `ADD COLUMN`; база здесь только ЧИТАЕТСЯ);
  2. `D20` #225 / `D21` #230: пороги, которые на test42 были ЧУЖИМИ, — свои или их нет;
  3. `K2-3` #174: закупочный порог прим. 5 у раздела II — по видам;
  4. `D16` #173: пометка «из» у кодов позиции (файл `code_cells.json` в образе, не отстал);
  5. `D15` #155: маркер сноски «<38.12>» — не код ОКПД2, вопрос идёт процедурной веткой;
  6. ЖИВОЙ индекс: на 26.20.11.120 исключающая позиция не целевая (`D20`); на «что значит <38(3)>» первой
     идёт своя сноска <38.3> (`D15`); поиск с кодом и пересчётом score окна (`R31`) не падает.

⚠ Блоки 1–5 офлайновые, 6 требует живого Qdrant. Имена коллекций — из настроек: локально та же проверка
гоняется на любых коллекциях (`QDRANT_COLLECTION=… QDRANT_RULES_COLLECTION=…`). Положительный контроль —
прогон на коде test42 (результат в профиле релиза).
"""
from app.core.console import enable_utf8

enable_utf8()  # #107: проверка печатает значки вне cp1251

import json  # noqa: E402
from pathlib import Path  # noqa: E402

from app.rag import thresholds as _t  # noqa: E402

# Корень данных — от модуля, а не от `__file__`: проверка идёт через `python - < файл`. Модуль — тот,
# что есть и в прежнем коде: положительный контроль на test42 обязан отчитаться блоками, а не упасть на импорте.
STRUCTURED = Path(_t._CHUNK).parent.parent / "structured"


def _records() -> list[dict]:
    out = []
    for f in sorted(STRUCTURED.glob("*.json")):
        out += [r for r in json.loads(f.read_text(encoding="utf-8")) if r.get("record_type") != "section_methodology"]
    return out


def main() -> int:
    bad = 0

    def check(ok: bool, good: str, fail: str) -> None:
        nonlocal bad
        if ok:
            print(f"   OK: {good}")
        else:
            bad += 1
            print(f"   ❌ {fail}")

    recs = _records()

    def rec(start: str, code: str | None = None) -> dict | None:
        return next((r for r in recs if r["product_name"].startswith(start)
                     and (code is None or code in (r.get("okpd2_codes") or []))), None)

    def thr(r, proc=False):
        from app.rag import thresholds as t
        fn = t.lookup_procurement_threshold if proc else t.lookup_threshold
        return (fn(r["okpd2_codes"], r["product_name"], r["section_roman"]) if r else None) or ""

    # 1. L1 / L2 — режимы источника и колонки базы
    try:
        from app.api.chat import ChatRequest
        from app.rag import corpora
        modes_ok = set(corpora.MODE_TITLES) == {"auto", "appendix", "rules", "metodrek"}
        req_ok = ChatRequest(message="проверка", corpus="rules").corpus == "rules"
        check(modes_ok and req_ok, "режимы источника: auto/appendix/rules/metodrek, ChatRequest их принимает",
              f"режимы {sorted(getattr(corpora, 'MODE_TITLES', {}))}, ChatRequest — {req_ok}")
    except Exception as e:  # noqa: BLE001
        check(False, "", f"режимы источника недоступны: {type(e).__name__}: {e}")
    try:
        from sqlalchemy import inspect

        from app.db.engine import engine
        cols = {t: {c["name"] for c in inspect(engine).get_columns(t)} for t in ("messages", "guest_messages")}
        check(all("corpus" in c for c in cols.values()), "колонки messages.corpus и guest_messages.corpus есть",
              f"колонок corpus нет: {[t for t, c in cols.items() if 'corpus' not in c]}")
    except Exception as e:  # noqa: BLE001
        check(False, "", f"схема базы не прочиталась: {type(e).__name__}: {e}")

    # 2. D20 / D21 — пороги, которые на test42 были чужими
    for start, code, want, foreign in (
            ("Устройства ввода или вывода", "26.20.16", "не менее 55 баллов", "не менее 70 баллов"),
            ("Устройства запоминающие и прочие", "26.20.2", "не менее 110 баллов", "не менее 23 баллов"),
            ("Оборудование коммутации и маршрутизации", "26.30.11.122", "не менее 35 баллов", "не менее 40 баллов"),
            ("Средства связи радиоэлектронные", "26.30.11.150", "не менее 22 баллов", "не менее 40 баллов")):
        got = thr(rec(start, code))
        check(want in got and foreign not in got, f"{start[:40]}: свой порог ({want})",
              f"{start[:40]}: {got[:70] or '—'}")
    for start, code in (("Обувь валяная", "15.20.14.130"), ("Обувь детская валяная", "15.20.14.143")):
        got = thr(rec(start, code), proc=True)
        check(got.startswith("не менее 60 баллов"), f"{start}: закупочный — свой (60)", f"{start}: {got[:70] or '—'}")
    for start in ("Принтеры (кроме принтеров для печати этикеток", "Сканеры биометрические",
                  "Аппараты для ингаляционного наркоза (за исключением", "Система мониторинга окружающей среды",
                  "Терминал экстренной связи", "Турникеты биометрические", "Маммографы, рентгеновские аппараты"):
        got = thr(rec(start))
        check(not got, f"{start[:40]}: чужого порога нет", f"{start[:40]}: чужой порог {got[:60]}")

    # 3. K2-3 — закупочный порог прим. 5 по видам
    got = thr(rec("Средства транспортные, их шасси", "29.10.2"), proc=True)
    check(got.startswith("по видам продукции [прим. 5 к разд. II]") and "не менее 3200 баллов" in got,
          "легковые: закупочный порог прим. 5 по видам (3200…)", f"легковые, закупочный: {got[:80] or '—'}")

    # 4. D16 — пометка «из» и файл ячеек кода
    try:
        from app.rag import code_cells
        r = rec("Эндопротезы суставов конечностей", "32.50.22.110")
        m = code_cells.marks(r["section_roman"], r["product_name"], r["okpd2_codes"]) if r else None
        check(bool(m) and all(x == code_cells.IZ for x in m) and "из 32.50.22.110" in code_cells.render(r["okpd2_codes"], m),
              "«Эндопротезы суставов»: коды с пометкой «из»", f"пометки «из» нет: {m}")
        known = sum(code_cells.marks(x["section_roman"], x["product_name"], x.get("okpd2_codes")) is not None for x in recs)
        check(known >= len(recs) - 3, f"ячейки кода известны у {known} из {len(recs)} позиций",
              f"ячейки кода известны только у {known} из {len(recs)} — файл отстал от корпуса")
    except Exception as e:  # noqa: BLE001
        check(False, "", f"code_cells недоступен: {type(e).__name__}: {e}")

    # 5. D15 — маркер сноски не код
    from app.rag import okpd2_ref, procedural
    check(okpd2_ref.extract_codes("что означает <38.12>") == [] and procedural.is_procedural("что означает <38.12>"),
          "«<38.12>» — сноска, не код ОКПД2: процедурная ветка", "«<38.12>» читается кодом ОКПД2")
    # 2а. K2-3 и D21 — функции на месте (в старом коде их нет: провал здесь, а не исключение ниже)
    check(hasattr(_t, "_kind_schedules") and hasattr(_t, "_scope_conflict"),
          "разбор графиков по видам и правило противоположных оговорок на месте",
          "в коде нет _kind_schedules / _scope_conflict")

    # 6. Живой индекс
    from app.rag import pipeline, retriever
    try:
        hits = retriever.search("ноутбук 26.20.11.120", okpd2="26.20.11.120", limit=8)
    except Exception as e:  # noqa: BLE001
        hits = []
        check(False, "", f"живой поиск по коду: {type(e).__name__}: {e}")
    try:
        targets = [h.product_name for h in pipeline.target_hits(hits, "26.20.11.120")] if hits else []
        check(any(t.startswith("Портативные персональные компьютеры") for t in targets)
              and not any(t.startswith("Компьютеры портативные") for t in targets),
              "26.20.11.120: целевая — своя позиция, исключающая — нет",
              f"26.20.11.120: целевые {[t[:30] for t in targets]}")
        w = retriever.search_rules("что значит <38(3)>", limit=6)
        check(bool(w) and w[0].get("point") == "<38.3>", "«что значит <38(3)>» → первой своя сноска <38.3>",
              f"«что значит <38(3)>» → первой {w[0].get('point') if w else None}")
        ctl = retriever.search("бульдозеры гусеничные 28.92.21.110", okpd2="28.92.21.110", limit=8)
        check(any(h.okpd2_match for h in ctl), "контроль: поиск с кодом и пересчётом окна отвечает",
              "контроль: поиск с кодом не нашёл совпадений")
    except Exception as e:  # noqa: BLE001
        check(False, "", f"живой индекс: {type(e).__name__}: {e}")

    print(f"   итог test43: провалов {bad}")
    return bad


if __name__ == "__main__":
    raise SystemExit(main())
