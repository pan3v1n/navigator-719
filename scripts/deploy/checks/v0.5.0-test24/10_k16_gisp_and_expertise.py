"""`K16` #48: процедура экспертизы происхождения и разъяснения ГИСП — на боевом рантайме.

ЗАЧЕМ ИМЕННО ЭТА ПРОВЕРКА. Релиз добавляет в корпус два документа, и у каждого свой способ
сломаться тихо:

* **Положение №49** может доехать в индекс и при этом проигрывать СВОЙ ЖЕ предмет Приказу №52 —
  так и было до правки квоты: «какие документы нужны для экспертизы происхождения» возглавлял
  документ, у которого экспертиза подтверждает ПРОИЗВОДСТВО, а не определяет СТРАНУ. Счётчик
  точек этого не видит: там 752 в обоих случаях;
* **FAQ ГИСП** — не норма, и весь смысл его паспорта в том, чтобы это было видно в ответе.
  Критерий приёмки issue #48 сформулирован дословно: «ответы по процедуре ГИСП снабжены
  источником и пометкой "не норма"»;
* **фантом `K15`.** Главный риск релиза: в корпус приехал документ ПРО ГИСП, а несуществующее
  «заключение ТПП, подтверждающее производство» живёт ровно в этой теме. Настоящая страница
  портала его не содержит, но проверить это НА БОЮ обязательно — если уехал не тот файл,
  выдумка окажется ЗАЗЕМЛЁННОЙ, и рантайм-гард её пропустит: он ловит незаземлённое, а не
  неверное.

⚠ Блоки 1-2 офлайновые (таблица тем, паспорт манифеста) и стоят ноль. Блоки 3-5 зовут поиск,
то есть требуют живого Qdrant — они и проверяют, что корпус на сервере ДОЕХАЛ, а не только
посчитан счётчиком `EXPECT`.
"""
from app.core.console import enable_utf8

enable_utf8()  # #107: проверка печатает значки вне cp1251

# Вопросы Положения №49 — его собственный предмет.
P49_SUBJECT = (
    "какие документы нужны для экспертизы происхождения товара",
    "что входит в акт экспертизы происхождения",
    "как проводится экспертиза по определению страны происхождения",
    "сроки проведения экспертизы происхождения",
)
# ⚠ ОБЯЗАНЫ ОСТАТЬСЯ ЗА ПРИКАЗОМ №52. Сужение ствола «экспертиз» опасно ровно этим.
O52_SUBJECT = (
    "какие документы нужны для получения акта экспертизы",
    "перечень документов для подтверждения производства",
    "что такое акт экспертизы и когда он нужен",
)
# Вопросы, на которые отвечает только FAQ портала.
GISP_QUESTIONS = (
    "как попасть в реестр российской промышленной продукции",
    "до какого числа подавать отчёт о произведённой продукции",
    "включает ли стоимость реализации НДС",
)
# ⚠ Контроль `K15`: документа не существует, и корпус про ГИСП не должен это изменить.
PHANTOM = "нужно ли заключение ТПП для внесения продукции в реестр"
# ⚠ Товарный контроль: релиз не должен трогать ветку продукции.
PRODUCT = ("какие требования к бульдозерам 28.92.21.110",
           "сколько баллов нужно для лифтов 28.22.16.111")


def main() -> int:
    bad = 0

    print("1. ТАБЛИЦА ТЕМ РАЗВОДИТ ДВА ПРЕДМЕТА (офлайн)")
    from app.rag import retriever as R

    def scores(q):
        return {dt: sum(1 for p in pats if p.search(q)) for dt, pats in R._RULES_TOPIC}

    for q in P49_SUBJECT:
        s = scores(q)
        ok = s.get("polozhenie49_tpp", 0) > s.get("tpp_order_52", 0)
        bad += not ok
        print(f"   {'OK ' if ok else '!! '}№49 > №52 ({s.get('polozhenie49_tpp')}:"
              f"{s.get('tpp_order_52')}): {q[:46]}")
    for q in O52_SUBJECT:
        s = scores(q)
        ok = s.get("tpp_order_52", 0) > s.get("polozhenie49_tpp", 0)
        bad += not ok
        print(f"   {'OK ' if ok else '!! '}№52 удержал свой предмет: {q[:46]}")

    print("2. ПАСПОРТ: ГИСП — ПРАКТИКА, ПОЛОЖЕНИЕ — ВЕДОМСТВЕННЫЙ АКТ (офлайн)")
    from app.core import manifest as M

    by = {d["doc_type"]: d for d in M.load_manifest()["documents"]}
    for dt, force, topic in (("gisp_faq", 5, "ГИСП"), ("polozhenie49_tpp", 3, "СТ-1")):
        d = by.get(dt)
        ok = d is not None and d["legal_force"] == force and d["topic"] == topic
        bad += not ok
        print(f"   {'OK ' if ok else '!! '}{dt}: legal_force={d and d['legal_force']} "
              f"topic={d and d['topic']}")

    print("3. НОВЫЕ ДОКУМЕНТЫ НАХОДЯТСЯ ПОИСКОМ (корпус доехал, а не только посчитан)")
    from app.rag import topics
    from app.rag.pipeline import RULES_TOP_K
    from app.rag.retriever import search_rules

    def window(q):
        hits = search_rules(q, limit=RULES_TOP_K,
                            primary_docs=topics.doc_types(topics.classify(q)))
        return [(h.get("doc_type"), h.get("source_anchor") or "") for h in hits]

    for q in P49_SUBJECT[:2]:
        docs = [d for d, _ in window(q)]
        ok = "polozhenie49_tpp" in docs
        bad += not ok
        print(f"   {'OK ' if ok else '!! '}Положение №49 в окне: {q[:44]}  окно={docs[:4]}")
    for q in GISP_QUESTIONS:
        docs = [d for d, _ in window(q)]
        ok = "gisp_faq" in docs
        bad += not ok
        print(f"   {'OK ' if ok else '!! '}FAQ ГИСП в окне: {q[:44]}  окно={docs[:4]}")

    print("4. ⚠⚠⚠ КРИТЕРИЙ ПРИЁМКИ #48: ИСТОЧНИК НЕСЁТ ПОМЕТКУ «НЕ НОРМА»")
    for q in GISP_QUESTIONS:
        anchors = [a for d, a in window(q) if d == "gisp_faq"]
        ok = bool(anchors) and all("не норма" in a for a in anchors)
        bad += not ok
        print(f"   {'OK ' if ok else '!! '}пометка «не норма» у якоря: {q[:44]}")

    print("5. ⚠⚠⚠ КОНТРОЛЬ `K15`: ФАНТОМ НЕ ПРИЕХАЛ ВМЕСТЕ С КОРПУСОМ ПРО ГИСП")
    import re

    phantom_re = re.compile(r"заключени\w*\s+(?:ТПП|торгово)", re.I)
    leaked = []
    for q in GISP_QUESTIONS + (PHANTOM,):
        hits = search_rules(q, limit=RULES_TOP_K,
                            primary_docs=topics.doc_types(topics.classify(q)))
        for h in hits:
            if h.get("doc_type") == "gisp_faq" and phantom_re.search(h.get("text") or ""):
                leaked.append((q, (h.get("source_anchor") or "")[:40]))
    ok = not leaked
    bad += not ok
    print(f"   {'OK ' if ok else '!! '}в записях FAQ фантома нет; найдено: {leaked[:2]}")

    print("6. ТОВАРНАЯ ВЕТКА НЕ ТРОНУТА")
    from app.rag import procedural
    from app.tools.navigator import extract_okpd2

    for q in PRODUCT:
        proc = procedural.is_procedural(q, has_code=bool(extract_okpd2(q)))
        ok = not proc
        bad += not ok
        print(f"   {'OK ' if ok else '!! '}остался товарным: {q[:46]}")

    print(f"\nИТОГ: провалов {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
