"""K2 (#47): ни одна позиция не получает ЗАКУПОЧНЫЙ порог как общий.

Самый дорогой класс дефекта — правильное число из первоисточника, привязанное не к тому вопросу.
Проверяется по всему корпусу, а не выборочно.
"""
import glob
import json
import re

from app.rag.thresholds import lookup_threshold, note_scope

bad = []
for f in glob.glob("knowledge_base/pp719/structured/*.json"):
    for rec in json.load(open(f, encoding="utf-8")):
        if (rec.get("min_threshold") or "").strip():
            continue
        got = lookup_threshold(rec.get("okpd2_codes") or [],
                               rec.get("product_name") or "", rec.get("section_roman"))
        if not got:
            continue
        for n in re.findall(r"прим\.\s*(\d+(?:\.\d+)?)", got):
            if note_scope(n) != "general":
                bad.append((n, (rec.get("product_name") or "")[:40]))
assert not bad, f"закупочное примечание выдано как ОБЩИЙ порог: {bad[:3]}"
print("   OK: ни одна позиция не получает закупочный порог как общий")
