"""K2 (#47): ни одна позиция не получает ЗАКУПОЧНЫЙ порог как общий.

Самый дорогой класс дефекта — правильное число из первоисточника, привязанное не к тому вопросу.
Проверяется по всему корпусу, а не выборочно.
"""
import glob
import json
import re

from app.core.console import enable_utf8
from app.rag.thresholds import lookup_threshold, note_scope

enable_utf8()  # #107: проверка печатает значки вне cp1251

bad = []
for f in glob.glob("knowledge_base/pp719/structured/*.json"):
    for rec in json.load(open(f, encoding="utf-8")):
        if (rec.get("min_threshold") or "").strip():
            continue
        got = lookup_threshold(rec.get("okpd2_codes") or [],
                               rec.get("product_name") or "", rec.get("section_roman"))
        if not got:
            continue
        # ⚠⚠ НОМЕР ПРИМЕЧАНИЯ БЕРЁТСЯ ЦЕЛИКОМ, ВКЛЮЧАЯ ПОДПУНКТ В СКОБКАХ (ревью захода 3,
        # 26.08.2026). Прежняя форма `(\d+(?:\.\d+)?)` обрезала «прим. 8(1)» до «8», а это РАЗНЫЕ
        # области: note_scope('8') = procurement, note_scope('8(1)') = general. Последствия
        # двусторонние и оба плохие: верный релиз, сославшийся на 8(1), был бы ОСТАНОВЛЕН
        # предохранителем («дороже отсутствующего» — так говорит шапка самого deploy.sh), а
        # закупочный подпункт N(M) при общем N проскочил бы непроверенным. Корпус сейчас отдаёт
        # здесь 26(2)/26(3), и спасало только то, что прим. 26 общее.
        for n in re.findall(r"прим\.\s*(\d+(?:\.\d+)?(?:\(\d+\))?)", got):
            if note_scope(n) != "general":
                bad.append((n, (rec.get("product_name") or "")[:40]))
assert not bad, f"закупочное примечание выдано как ОБЩИЙ порог: {bad[:3]}"
print("   OK: ни одна позиция не получает закупочный порог как общий")
