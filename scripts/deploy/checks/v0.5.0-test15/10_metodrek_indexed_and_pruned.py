"""Релиз `v0.5.0-test15`: Методрекомендации доехали до индекса — и БЕЗ отменённых фрагментов.

ЗАЧЕМ ИМЕННО ЭТО. Релиз добавляет документ в корпус, и у такой правки два разных способа
«выкатиться, не выкатившись»:

* документ не доехал вовсе (полная переиндексация прошла на прежнем тексте, счётчик 311 вместо
  328) — снаружи неотличимо от успеха, сервис отвечает как раньше;
* документ доехал ЦЕЛИКОМ, вместе с отменённым 29.06.2024 шагом «получите заключение
  Минпромторга». Это хуже первого случая: сервис начнёт уверенно вести заявителя по
  недействующему порядку, со ссылкой на действующий документ ТПП.

⚠ Второе нельзя проверить по файлам пакета: `exclude_fragments` исполняется ЗАГРУЗЧИКОМ. Файл
`metodrek_tpp_full.txt` содержит отменённый текст и обязан его содержать — вырезание происходит
на индексации. Значит, вопрос только рантаймовый: что лежит В КОЛЛЕКЦИИ.
"""
from app.core.console import enable_utf8
from app.core.config import settings
from app.rag.retriever import _client

enable_utf8()  # #107: проверка печатает «→» — на консоли Windows падала бы

name = settings.QDRANT_RULES_COLLECTION
client = _client()

points, _ = client.scroll(collection_name=name, limit=512, with_payload=True)
assert points, f"коллекция {name} пуста"

met = [p for p in points if (p.payload or {}).get("doc_type") == "metodrek_tpp"]
print(f"   точек всего: {len(points)}, из них Методрекомендаций: {len(met)}")
assert met, "Методрекомендаций нет в индексе — полная переиндексация шла на прежнем тексте"

# 1. Паспорт нового документа — разъяснение, а не норма.
forces = {(p.payload or {}).get("legal_force") for p in met}
assert forces == {4}, f"legal_force Методрекомендаций = {forces}, ожидалось {{4}}"
editions = {(p.payload or {}).get("edition") for p in met}
assert editions == {"Версия 1.18.3"}, f"редакция Методрекомендаций: {editions}"

# 2. ОТМЕНЁННЫЙ ШАГ НЕ ДОЕХАЛ. Главная проверка этого релиза.
text = "\n".join((p.payload or {}).get("text") or "" for p in met)
for phrase in ("Получите заключение Минпромторга России",
               "Заключение выдается на основании одного из двух",
               "наличие заключения Министерства промышленности и торговли"):
    assert phrase not in text, (
        f"в индекс доехал отменённый 29.06.2024 порядок: {phrase!r}. "
        f"exclude_fragments не сработал — сервис поведёт заявителя по недействующему пути")

# 3. ОБРАТНАЯ ПОЛОВИНА: полезное не потеряно. Правило, вырезавшее лишнее, прошло бы п. 2.
assert "12.34.56.789" in text, "алгоритм поиска по коду ОКПД потерян — вырезано слишком много"
assert "СТ-1" in text, "выбор документа по наличию в приложении потерян"

# 4. Разделы на месте: их шесть, и каждый несёт свой якорь.
sections = sorted({(p.payload or {}).get("section_roman") for p in met})
assert sections == ["1", "2", "3", "4", "5", "6"], f"разделы Методрекомендаций: {sections}"
bad_anchor = [p for p in met
              if not ((p.payload or {}).get("source_anchor") or "").startswith("Методрекомендации")]
assert not bad_anchor, f"{len(bad_anchor)} точек с чужим якорем — источник напечатается неверно"

# 5. Соседи не пострадали: полная переиндексация обязана вернуть ВСЕ документы, а не только новый.
docs = {(p.payload or {}).get("doc_type") for p in points}
for must in ("decree_body", "rules_registry", "tpp_order_52", "appendix_footnotes"):
    assert must in docs, f"после полной переиндексации в коллекции нет {must}"
assert "pravila_vydachi_zaklyucheniya" not in docs, "утративший силу документ снова в индексе"

print(f"   OK: Методрекомендации {len(met)} точек, сила 4, отменённый шаг вырезан, "
      f"алгоритм ОКПД на месте, соседи целы ({len(docs)} документов)")
