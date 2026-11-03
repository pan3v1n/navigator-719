"""test43 — этапы 0–3 одним рантаймовым релизом: режимы источника, точность товарной ветки, сноски.

ЗАЧЕМ. Корпус и индекс не меняются (1583 / 767 / 12, как у test41 — пересборка 28.10: код `dev` БЕЗ корпуса
N 1257, решение владельца), меняется код и рантайм-данные
(`knowledge_base/pp719/code_cells.json`). Счётчики точек тут ничего не скажут: доехало ли — видно только
по ответу кода на тех позициях, ради которых правки делались, и по поиску в живом индексе.

Что проверяется (провал любого — провал выкатки):
  1. `L1`/`L2`: четыре режима источника, `ChatRequest` принимает режим; колонки `corpus` в боевой базе
     появились (миграция на старте — только `ADD COLUMN`; база здесь только ЧИТАЕТСЯ);
  2. `D20` #225 / `D21` #230 / `D22` #235: пороги, которые на test42 были ЧУЖИМИ, — свои или их нет;
  3. `K2-3` #174: закупочный порог прим. 5 у раздела II — по видам;
  4. `D16` #173: пометка «из» у кодов позиции (файл `code_cells.json` в образе, не отстал);
  5. `D15` #155: маркер сноски «<38.12>» — не код ОКПД2, вопрос идёт процедурной веткой;
  6. ЖИВОЙ индекс: на 26.20.11.120 исключающая позиция не целевая (`D20`); на «что значит <38(3)>» первой
     идёт своя сноска <38.3> (`D15`); поиск с кодом и пересчётом score окна (`R31`) не падает и не
     плавает между прогонами (`R31-2` #245: ничья sparse-score — окно одно в восьми прогонах, пересчёт
     отработал в каждом без сбоя — сервер принял пакет dense + sparse);
  7. КОРПУС test41: редакция N 1297 и ссылка на 508767; позиции N 1257 «Иглы швейные» 25.93.18.110 нет ни в
     данных образа, ни в живом индексе (N 1257 вступает 31.10 и едет следующим релизом, test44).

⚠ Блоки 1–5 офлайновые, 6 требует живого Qdrant. Имена коллекций — из настроек: локально та же проверка
гоняется на любых коллекциях (`QDRANT_COLLECTION=… QDRANT_RULES_COLLECTION=…`). Положительный контроль —
прогон на коде test41 и на корпусе test42 (результат в профиле релиза).
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
        r = rec(start)
        got = thr(r)
        # Ревью D22 (LOW): нет записи — `thr` даёт "", и отрицательная проверка прошла бы, ничего не проверив.
        check(r is not None and not got, f"{start[:40]}: чужого порога нет",
              f"{start[:40]}: {'позиция не найдена' if r is None else 'чужой порог ' + got[:60]}")
    # D22 — примечание, ограниченное разделом, чужому разделу порога не даёт, своему — даёт
    for start, proc in (("Медицинские маски (за исключением", True), ("Противопролежневые матрацы воздушные", True),
                        ("Мебель металлическая судовая", True), ("Плиты, листы теплоизоляционные из вспененного", False)):
        r = rec(start)
        got = thr(r, proc=proc)
        check(r is not None and not got, f"{start[:40]}: порога чужого раздела нет",
              f"{start[:40]}: {'позиция не найдена' if r is None else 'порог чужого раздела ' + got[:60]}")
    got = thr(rec("Мебель металлическая для офисов"), proc=True)
    check("[прим. 52]" in got, "мебель для офисов (XI): закупочный прим. 52 на месте",
          f"мебель для офисов (XI): {got[:60] or '—'}")

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
        # R31-2 #245: ничья sparse-score XXI/27 и XXI/30 — до правки первой была то одна, то другая
        # (7 из 12 / 5 из 12); восемь прогонов подряд без правки совпадают с вероятностью ~1 %.
        # ⚠ Ревью PR #248 (MED-1): одной устойчивости мало — окно ДО `R31` тоже устойчиво, и пункт
        # проходил, когда пересчёт падал (сервер отверг пакет, вернул не всех) и молча откатывался.
        # Поэтому ещё: слияние вызвано в каждом прогоне и ни одного предупреждения пересчёта.
        from loguru import logger as _log
        q = "Краски полиграфические 20.30.24"
        qv = retriever.embed_query(q)
        warns, calls = [], [0]
        orig = getattr(retriever, "_rrf_tied", None)
        sink = _log.add(lambda m: warns.append(m.record["message"]), level="WARNING",
                        filter=lambda r: "пересчёт score окна" in r["message"])
        if orig is not None:
            def _spy(lists):
                calls[0] += 1
                return orig(lists)
            retriever._rrf_tied = _spy
        try:
            firsts = {tuple(h.source_anchor for h in retriever.search(q, okpd2="20.30.24", limit=8, qvec=qv)[:3])
                      for _ in range(8)}
        finally:
            _log.remove(sink)
            if orig is not None:
                retriever._rrf_tied = orig
        check(orig is not None and len(firsts) == 1 and calls[0] == 8 and not warns,
              "«Краски полиграфические 20.30.24»: пересчёт окна отработал 8 раз из 8, окно одно (R31-2)",
              f"пересчёт окна: _rrf_tied {'есть' if orig else 'нет'}, вызван {calls[0]} из 8, сбоев "
              f"{len(warns)}, порядков top-3 {len(firsts)}" + (f" — {warns[0][:160]}" if warns else ""))
    except Exception as e:  # noqa: BLE001
        check(False, "", f"живой индекс: {type(e).__name__}: {e}")

    # 7. Корпус — test41 (решение владельца 28.10: код `dev`, корпус БЕЗ N 1257). N 1257 едет test44 в ночь на
    # 01.11: приехать раньше вступления в силу (31.10) ей нельзя — ни в данных образа, ни в живом индексе.
    from app.rag import edition
    for f in ("corpus_edition", "edition_key"):
        getattr(getattr(edition, f, None), "cache_clear", lambda: None)()
    key = getattr(edition, "edition_key", edition.corpus_edition)()  # в коде test41 ключа ещё нет
    url = edition.kontur_719_url()
    check(key == "ред. от 05.10.2026 N 1297" and url.endswith("documentId=508767"),
          "редакция корпуса — N 1297 (действует с 16.10), ссылка → 508767",
          f"редакция корпуса {key!r}, ссылка {url}")
    check(rec("Иглы швейные") is None, "в данных образа позиции N 1257 «Иглы швейные» нет",
          "в данных образа есть позиция N 1257 «Иглы швейные» — корпус не test41")
    try:
        nh = retriever.search("иглы швейные из черных металлов 25.93.18.110", okpd2="25.93.18.110", limit=8)
        check(not any(h.product_name.startswith("Иглы швейные") for h in nh),
              "в живом индексе позиции «Иглы швейные» 25.93.18.110 нет",
              "в живом индексе есть «Иглы швейные» 25.93.18.110 — индекс уже на N 1257")
    except Exception as e:  # noqa: BLE001
        check(False, "", f"живой поиск 25.93.18.110: {type(e).__name__}: {e}")

    print(f"   итог test43: провалов {bad}")
    return bad


if __name__ == "__main__":
    raise SystemExit(main())
