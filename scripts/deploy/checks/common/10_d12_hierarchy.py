"""D12 (#90): исключённые позиции ушли из корпуса, добор по иерархии жив.

Проверялась на выкатке test11 и ИСЧЕЗЛА в test12 — ровно то, ради чего заведён каталог common.

⚠ Звать надо тем же путём, что рантайм: буст по коду включается только при переданном `okpd2`,
который пайплайн получает из `extract_okpd2`. Без него top-1 — мусор по совпадению хвоста «131».
"""
from app.rag.retriever import search
from app.tools.navigator import extract_okpd2

q = "14.12.30.131"  # кода в корпусе НЕТ, добираем группу 14.12
hits = search(q, okpd2=extract_okpd2(q), limit=5)
top = hits[0]
print("   top-1:", (top.product_name or "")[:60], "|", top.okpd2_codes)
assert "Спецодежда" in (top.product_name or ""), "добор по иерархии сломан"
for h in hits:
    assert not (h.product_name or "").startswith("до 1 января"), "заглушка-обрывок в выдаче"
print("   OK: 14.12.30.131 → «Спецодежда», заглушек нет")
