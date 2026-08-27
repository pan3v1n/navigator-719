"""Релиз `v0.5.0-test20`: пять находок ревью захода 5 (PR #112) — на боевом рантайме.

⚠ ЧЕСТНО О ГРАНИЦАХ ЭТОЙ ПРОВЕРКИ. Главная находка (слот перечня доставался куску
Методрекомендаций, а не п. 4.1 Приказа №52) НА ТЕКУЩЕМ КОРПУСЕ НЕ СРАБАТЫВАЛА — Приказ выигрывал
ПОРЯДКОМ в пуле. Поэтому поведенческая проверка «в окне п. 4.1 Приказа» зеленела бы и ДО правки:
это был бы фиктивный положительный контроль — ровно то, что ревью захода 3 нашло у `dec_pool`.
Здесь проверяется ПРЕДПОСЫЛКА, которая делает ключ-пару необходимой: коллизия номеров между двумя
документами РЕАЛЬНА и жива. Форму самого ключа сторожат маркеры профиля релиза.
"""
import sys

from app.core import manifest as kb_manifest
from app.core.console import enable_utf8
from app.core.prompts import PROCEDURAL_SYSTEM_PROMPT
from app.rag.documents_ref import documents_context_block
from app.rag.retriever import RULES_DOC_LIST_GROUPS

enable_utf8()  # #107: проверка печатает значки вне cp1251

sys.path.insert(0, "scripts")
import load_rules_kb as loader  # noqa: E402 — путь к загрузчикам добавляется выше

# --- 1. Коллизия номеров между документами жива → ключ обязан быть парой -----------------------
recs, _ = loader.load_records()
owners: dict[str, set] = {}
for r in recs:
    owners.setdefault(str(r.get("point") or ""), set()).add(r.get("doc_type"))

collided = {g: sorted(owners.get(g, ())) for g in RULES_DOC_LIST_GROUPS if len(owners.get(g, ())) > 1}
assert collided, (
    "коллизия номеров пунктов исчезла — предпосылка ключа (doc_type, point) больше не выполняется. "
    "Это не обязательно плохо, но ключ и его обоснование надо перечитать")
for g, docs in collided.items():
    assert "tpp_order_52" in docs, f"группа перечня {g} больше не принадлежит Приказу №52: {docs}"
print(f"   OK: коллизия номеров жива ({', '.join(f'{g}: {len(d)} док.' for g, d in collided.items())}) "
      f"— ключ (doc_type, point) необходим")

# --- 2. Правило, которое некому исполнить, останавливает загрузку ------------------------------
declared = {d["doc_type"] for d in kb_manifest.load_manifest()["documents"]
            if d.get("exclude_fragments")}
orphan = declared - loader.SUPPORTS_EXCLUDE_FRAGMENTS
assert not orphan, (
    f"в манифесте есть exclude_fragments у документов, чей парсер их НЕ режет: {sorted(orphan)}. "
    f"Отменённая норма уехала бы в индекс молча")
print(f"   OK: exclude_fragments объявлен только там, где исполняется ({sorted(declared)})")

# --- 3. Закрытость справочника ограничена ПОДТВЕРЖДАЮЩИМИ документами -------------------------
# Дефект латентный: живая проверка (4 прогона) показала, что модель и до правки не отрицала копию
# устава. Закрыт формулировкой — значит и проверять надо формулировку.
assert "Закрытость касается ТОЛЬКО документов, ПОДТВЕРЖДАЮЩИХ производство" in PROCEDURAL_SYSTEM_PROMPT, \
    "правило 3б снова закрывает ВСЕ документы, включая приложения к заявке"
assert "НИКОГДА не говори, что их не существует" in PROCEDURAL_SYSTEM_PROMPT, \
    "из правила 3б пропала оговорка про приложения к заявке"
# ⚠ Разрыв фразы переносом строки уже случался в этой же правке («Не␣␣␣␣соглашайся»): проверяем
# СОБРАННУЮ строку, а не исходник промпта.
assert "Не соглашайся с названием только потому" in PROCEDURAL_SYSTEM_PROMPT, \
    "фраза правила 3б снова разорвана переносом — промпт собирается не тем, чем читается"
block = documents_context_block("нужно ли заключение ТПП для внесения продукции в реестр")
assert "ПОДТВЕРЖДАЮЩИХ ПРОИЗВОДСТВО, нет" in block and "под закрытость не подпадают" in block, \
    "шапка справочника снова объявляет закрытым ВЕСЬ состав документов"
print("   OK: закрытость ограничена подтверждающими; приложения к заявке не отрицаются")

# --- 4. Деградация манифеста больше НЕ кэшируется навсегда -------------------------------------
# Положительный контроль мутацией: ломаем чтение, убеждаемся что деградировало, чиним —
# и следующий вызов обязан ВОССТАНОВИТЬСЯ. Прежний код вернул бы {} и после починки.
alive = kb_manifest.legal_force_by_doc()
assert alive, "K13: манифест не читается на боевой машине — пометки силы источника молча потеряны"

real = kb_manifest.load_manifest
try:
    def _boom(*a, **k):
        raise OSError("проверка выкатки: имитация сбоя чтения манифеста")
    kb_manifest.load_manifest = _boom
    kb_manifest.legal_force_by_doc.cache_clear()
    assert kb_manifest.legal_force_by_doc() == {}, "сбой чтения обязан деградировать в пустой словарь"
finally:
    kb_manifest.load_manifest = real

recovered = kb_manifest.legal_force_by_doc()
assert recovered == alive, (
    f"деградация ЗАПОМНИЛАСЬ: после починки чтения сила источников не вернулась "
    f"({len(recovered)} против {len(alive)}) — lru_cache снова висит на внешней функции")
print(f"   OK: K13 жив ({len(alive)} документов) и восстанавливается после сбоя чтения")
