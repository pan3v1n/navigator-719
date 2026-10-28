"""Явный выбор корпуса (`L1` #146) и подпись «по чему собран ответ» (`L2` #147).

ЗАЧЕМ. Ветку ответа выбирает скрытый гейт `procedural.is_procedural`, и 03.09.2026 замером
закреплено, что порога, разделяющего классы, у него нет: полосы сходства перекрываются по существу.
Значит калибровать гейт дальше — тупик; выход — показать маршрут и дать его переопределить
(`docs/DUAL_CORPUS_TZ.md` §4). Режим по умолчанию — `auto`: прежний автомат, побайтно.

ОДНО МЕСТО на режимы и подписи: их читают пайплайн (маршрут), API (проверка запроса, подпись
ответа), шаблон чата (переключатель) и выгрузки. Вторая копия названий во фронте уже стоила проекту
ссылки на недействующую редакцию — поэтому шаблон берёт их отсюда, а не держит свои.
"""

from __future__ import annotations

from typing import Literal

# --- Режим, который ВЫБРАЛ пользователь (запрос) -------------------------------------------------
AUTO, APPENDIX, RULES, METODREK = "auto", "appendix", "rules", "metodrek"
Mode = Literal["auto", "appendix", "rules", "metodrek"]
MODES: tuple[str, ...] = (AUTO, APPENDIX, RULES, METODREK)
MODE_TITLES = {
    AUTO: "Авто",
    APPENDIX: "Приложение к ПП №719",
    RULES: "Правила реестра и приказы ТПП",
    METODREK: "Методрекомендации ТПП",
}
# Жёсткий фильтр процедурного поиска (`search_rules(only_docs=…)`, `EV18` #110) для режимов,
# которые ведут в корпус норм. Пустой кортеж — весь процедурный корпус, как у автомата.
ONLY_DOCS: dict[str, tuple[str, ...]] = {RULES: (), METODREK: ("metodrek_tpp",)}

# --- Корпус, по которому ответ ФАКТИЧЕСКИ собран (`Answer.corpus`) -------------------------------
# Пустая строка — корпуса нет: приветствие, честный отказ процедурной ветки, сбой.
USED_APPENDIX = "appendix"
USED_APPENDIX_ORDER52 = "appendix+order52"   # смешанный вопрос: позиции + пункты Приказа №52
USED_RULES = "rules"
USED_METODREK = "metodrek"
USED_CLASSIFIER = "classifier"               # перевод ТН ВЭД ↔ ОКПД2 по справочнику ключей
USED_LABELS = {
    USED_APPENDIX: "приложению к ПП №719",
    USED_APPENDIX_ORDER52: "приложению к ПП №719 и Приказу ТПП РФ №52",
    USED_RULES: "Правилам реестра и приказам ТПП",
    USED_METODREK: "Методрекомендациям ТПП",
    USED_CLASSIFIER: "справочнику переходных ключей ТН ВЭД ↔ ОКПД2",
}


def used_for_mode(mode: str) -> str:
    """Корпус процедурного ответа по режиму: «Методрекомендации» — свой, остальное — весь корпус норм."""
    return USED_METODREK if mode == METODREK else USED_RULES


def answered_by(used: str | None, mode: str | None) -> str:
    """Строка `L2` под ответом и в выгрузке; «» — подписывать нечего (корпуса у ответа нет).

    ⚠ Указывает на МЕХАНИЗМ, который мог ошибиться (как дисклеймер Нейроюриста про «выбор сфер
    права»): при автомате корпус выбрал сервис — эксперту стоит проверить, тот ли."""
    label = USED_LABELS.get(used or "")
    if not label:
        return ""
    how = "выбран вручную" if mode in MODE_TITLES and mode != AUTO else "выбран автоматически"
    return f"Ответ собран по {label} · источник {how}"


def stored(used: str | None, mode: str | None) -> str | None:
    """Значение колонки `messages.corpus`: «корпус|режим». None — подписывать нечего."""
    return f"{used}|{mode or AUTO}" if used else None


def mode_of(value: str | None) -> str:
    """Режим, в котором получен сохранённый ответ. Нет отметки — «Авто»: так отвечали все до `L1`,
    так же записаны приветствие и отказ (корпуса у них нет, а маршрут шёл автоматом)."""
    return (value or "").partition("|")[2] or AUTO


def label_from_stored(value: str | None) -> str:
    """Подпись по сохранённому значению; у реплик, записанных до `L2`, колонки нет — «»."""
    if not value:
        return ""
    used, _, mode = value.partition("|")
    return answered_by(used, mode or AUTO)
