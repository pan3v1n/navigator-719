"""Релиз `v0.5.0-test14`: паспорт доехал до точек и раздел IV Правил подписан верно.

ЗАЧЕМ ЭТА ПРОВЕРКА ИМЕННО ЗДЕСЬ. Правки `K8` (#39) и `#105` меняют payload КАЖДОГО пункта
процедурного корпуса, а не код ответа. Код может доехать целиком и правильно, а данные остаться
прежними — и снаружи это неотличимо от успешной выкатки: сервис отвечает, ошибок нет, просто
паспорта нет и раздел прежний. Ровно так `D12` однажды «выкатилась», не доехав.

⚠ Проверяется РАНТАЙМОМ, а не файлами пакета: вопрос не «лежит ли новый код», а «что лежит в
коллекции». Маркеры пакета отвечают на первый вопрос, эта проверка — на второй.
"""
from app.core.console import enable_utf8
from app.core.config import settings
from app.rag.retriever import _client

enable_utf8()  # #107: проверка печатает «→» — на консоли Windows падала бы

name = settings.QDRANT_RULES_COLLECTION
client = _client()

points, _ = client.scroll(collection_name=name, limit=512, with_payload=True)
assert points, f"коллекция {name} пуста"

# 1. Паспорт (K8) — у КАЖДОЙ точки, а не у первой попавшейся.
missing = [p for p in points if not (p.payload or {}).get("status")]
print(f"   точек в выборке: {len(points)}, без паспорта: {len(missing)}")
assert not missing, f"{len(missing)} точек без паспорта — переиндексация не доехала"

forces = {(p.payload or {}).get("doc_title"): (p.payload or {}).get("legal_force") for p in points}
print("   юридическая сила по документам:", forces)
assert all(isinstance(v, int) for v in forces.values()), "legal_force не число — K13 сломается"

# 2. Утративший силу документ в коллекцию не попал (второй контур — фильтр — проверяет тест).
retired = [p for p in points if (p.payload or {}).get("status") == "утратил силу"]
assert not retired, f"в индексе {len(retired)} точек утратившего силу документа"

# 3. #105: пункты 31–43 Правил принадлежат разделу IV, а не III.
rules = {(p.payload or {}).get("point"): (p.payload or {}) for p in points
         if (p.payload or {}).get("doc_type") == "rules_registry"}
for point in ("31", "43"):
    pl = rules.get(point)
    assert pl, f"пункт {point} Правил не найден в индексе"
    assert pl.get("section_roman") == "IV", \
        f"п. {point} снова подписан разделом {pl.get('section_roman')} вместо IV"
    assert "Формирование реестровой записи" in (pl.get("source_anchor") or ""), \
        f"п. {point}: якорь ведёт не в тот раздел — {pl.get('source_anchor')}"
assert rules.get("30", {}).get("section_roman") == "III", "граница разделов уехала вверх"
print("   OK: паспорт у всех точек, п. 31–43 в разделе IV, п. 30 остался в III")
