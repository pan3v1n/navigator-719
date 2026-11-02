"""test45 — пакет промпта, организация по ИНН, SEO этапа 1: рантаймовый релиз без смены корпуса.

ЗАЧЕМ. Корпус и индекс те же, что у test44 (1584 / 767 / 12): счётчики точек ничего не скажут. Доехало ли
— видно только по поведению кода на тех случаях, ради которых шли правки. Проверка идёт в контейнере
приложения, лендинга и Caddy отсюда не видно: их сверяют маркеры профиля, шаг публичного входа в `deploy.sh`
и внешняя проверка после выкатки (§5 профиля).

Что проверяется (провал любого пункта — провал выкатки):
  1. `R40` #239: порог мягкой релевантности 0.82;
  2. `EV20` #117: имя раздела без сносок и «в ред.», маркеры сносок отдельно;
  3. `K2-2` #128: у выключателей второй график той же позиции, у контакторов прежний «ИНОЙ порог»;
  4. `UI-1` #165: пункты Приказа №52 продолжают нумерацию, в источниках стоят после позиций;
  5. `EV11-1` #262: таблица баллов не дописывается к отказу и дописывается к разбору;
  6. `LANG-1` #264: правило 7 в обоих промптах, иероглифы вычищаются, полноширинные цифры сохраняются;
  7. организация по ИНН: 12 цифр и опечатка наружу не уходят, маршрут подключён, ключ виден приложению.
     Живой запрос к DaData: 401/403 (ключ отвергнут или исчерпан суточный лимит) — провал, поиск не работает
     ни у кого. ТРЕТИЙ исход «не проверено» — сеть, 429, 5xx и ответ без разобранной записи: это про канал, а не про релиз; он
     провалом не считается и печатается, чтобы его прочитал человек (канал сервер → DaData не мерился).

⚠ Значение ключа не печатается. Блоки 1–6 офлайновые. Положительный контроль — прогон на коде test44
(результат в профиле релиза): старый код обязан отчитаться провалами, а не упасть на импорте.
"""
from app.core.console import enable_utf8

enable_utf8()  # #107: проверка печатает значки вне cp1251

from types import SimpleNamespace  # noqa: E402


def main() -> int:
    bad = 0

    def check(ok: bool, good: str, fail: str) -> None:
        nonlocal bad
        if ok:
            print(f"   OK: {good}")
        else:
            bad += 1
            print(f"   ❌ {fail}")

    from app.rag import pipeline, retriever
    from app.rag import thresholds as T

    print("--- 1. R40: порог мягкой релевантности")
    check(pipeline.RELEVANCE_SOFT == 0.82, "RELEVANCE_SOFT = 0.82",
          f"RELEVANCE_SOFT = {pipeline.RELEVANCE_SOFT}, ожидалось 0.82")

    print("--- 2. EV20: имя раздела")
    name_of = getattr(retriever, "section_name", None)
    notes_of = getattr(retriever, "section_footnotes", None)
    if name_of and notes_of:
        title = "Продукция судостроения <9>, <9(1)> (в ред. Постановления Правительства РФ от 25.12.2025 N 2146)"
        check(name_of(title) == "Продукция судостроения", "имя чистое", f"имя раздела: {name_of(title)!r}")
        check(notes_of(title) == ["<9>", "<9(1)>"], "маркеры сносок отдельно", f"маркеры: {notes_of(title)!r}")
    else:
        check(False, "", "в retriever нет section_name / section_footnotes")

    print("--- 3. K2-2: второй график той же позиции")
    branch, other = getattr(T, "BRANCH_MARK", None), getattr(T, "OTHER_MARK", None)
    if branch and other:
        breaker = T.lookup_threshold(["27.12"], "Выключатель автоматический (воздушный) низковольтный в литом "
                                                "корпусе (до 1000 В) на токи до 6300 А", "V") or ""
        contactor = T.lookup_threshold(["27.33"], "Контакторы и пускатели электромагнитные низковольтные", "V") or ""
        check(branch in breaker and other not in breaker, "выключатели: другой график этой же позиции",
              "у выключателей нет ярлыка второго графика")
        check(other in contactor and branch not in contactor, "контроль: контакторы — прежний «ИНОЙ порог»",
              "у контакторов сменился ярлык")
    else:
        check(False, "", "в thresholds нет BRANCH_MARK / OTHER_MARK")

    print("--- 4. UI-1: нумерация пунктов и порядок источников")
    from app.api import chat
    rule = {"source_anchor": "Приказ ТПП РФ № 52, п. 4.2.1", "doc_type": "tpp_order_52", "point": "4.2.1",
            "text": "Текст пункта."}
    try:
        block = pipeline.format_rules_context([rule], start=9)
        check("[9]" in block and "[1]" not in block, "пункт Приказа получил номер [9]", "нумерация не продолжена")
    except TypeError:
        check(False, "", "format_rules_context не принимает start")
    make = getattr(chat, "_answer_sources", None)
    if make:
        hit = retriever.Hit(score=0.9, section_roman="XIX", section_title="Насосы", product_name="Насосы прочие",
                            okpd2_codes=["28.13.19"], min_threshold=None, requirement_blocks=[],
                            source_anchor="Раздел XIX, позиция 2")
        kinds = [s.kind for s in make(SimpleNamespace(hits=[hit], rule_sources=[rule]))]
        check(kinds == ["product", "rule"], "пункт стоит в источниках после позиции", f"порядок источников: {kinds}")
    else:
        check(False, "", "в chat нет _answer_sources")

    print("--- 5. EV11-1: таблица баллов только к разбору")
    keep = getattr(pipeline, "_keep_points_table", None)
    if keep:
        check(not keep("Вопрос вне сферы постановления.", "таблица", []), "к отказу таблица не дописывается",
              "таблица дописана к отказу")
        check(bool(keep("**Позиция:** Станки", "таблица", [])), "к разбору дописывается", "разбор потерял таблицу")
        check(not keep("**Позиция:** Станки", "", []), "пустая таблица не дописывается", "дописана пустая таблица")
    else:
        check(False, "", "в pipeline нет _keep_points_table")

    print("--- 6. LANG-1: только по-русски")
    from app.core import prompts
    for name in ("NAVIGATOR_SYSTEM_PROMPT", "PROCEDURAL_SYSTEM_PROMPT"):
        check("ТОЛЬКО ПО-РУССКИ" in getattr(prompts, name), f"правило 7 в {name}", f"нет правила 7 в {name}")
    clean = getattr(pipeline, "_clean_model_text", None)
    if clean:
        text, removed = clean("юридические 咨询 услуги, не менее １００ баллов")
        check(removed == 2 and "咨" not in text, "иероглифы вычищены и посчитаны", f"чистка: {removed}, {text!r}")
        check("100 баллов" in text, "полноширинное число сохранено", f"число потеряно: {text!r}")
    else:
        check(False, "", "в pipeline нет _clean_model_text")

    print("--- 7. Организация по ИНН")
    try:
        from app.core import org_registry
        from main import app
    except ImportError as e:
        check(False, "", f"модуль не импортируется: {e}")
        return report(bad)
    check(not org_registry.can_lookup("500100732259"), "12 цифр наружу не уходят", "ИНН физлица разрешён к отправке")
    check(not org_registry.can_lookup("7721581041"), "опечатка наружу не уходит", "опечатка разрешена к отправке")
    check(org_registry.can_lookup("7721581040"), "ИНН организации разрешён", "верный ИНН отвергнут")
    paths = {getattr(r, "path", "") for r in app.routes}
    check("/api/org-lookup" in paths, "маршрут /api/org-lookup подключён", "маршрута нет")
    check(org_registry.enabled(), "ключ DaData виден приложению", "DADATA_API_KEY не дошёл до контейнера")
    if org_registry.enabled():
        # `_ask`, а не `lookup`: только он отдаёт паузу, а по ней отказ в ключе отличается от сбоя канала
        res, pause = org_registry._ask("7721581040")
        if res.outcome == org_registry.FOUND:
            check(res.org.active, f"живой запрос: найдена, {res.org.status_text}", "живой запрос: не действующая")
        elif res.outcome == org_registry.NOT_FOUND:
            check(False, "", "живой запрос: DaData не нашла организацию с известным ИНН")
        elif pause == org_registry.PAUSE_LONG_S:
            check(False, "", "живой запрос: DaData отвергла запрос (401/403) — неверный ключ или исчерпан лимит")
        else:
            print("   ⚠ НЕ ПРОВЕРЕНО: DaData не дала разобранного ответа с сервера, форма работает без поиска. Разобрать канал.")
    return report(bad)


def report(bad: int) -> int:
    print(f"=== релизная проверка test45: провалов {bad} ===")
    return 1 if bad else 0


raise SystemExit(main())
