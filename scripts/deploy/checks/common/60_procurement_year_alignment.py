"""EV13 (#97): закупочный порог стоит против СВОЕГО года.

Пустые ячейки сдвигали колонки, и значение уезжало на год назад.
"""
from app.core.console import enable_utf8
from app.rag.thresholds import lookup_procurement_threshold

enable_utf8()  # #107: проверка печатает значки вне cp1251

v = lookup_procurement_threshold(["26.51.66.190"], "Меры твердости Роквелла", "XXII")
print("   ", v)
assert v and "2024" in v, f"год съехал: {v}"
print("   OK: значение стоит против своего года")
