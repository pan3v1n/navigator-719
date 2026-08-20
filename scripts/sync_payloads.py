"""Обновляет payload точек `pp719` из `structured/*.json` БЕЗ переиндексации.

ЗАЧЕМ. Часть правок меняет ПОЛЯ записи, а не текст, по которому строится вектор: так устроена
`D9` (пороги блоков легли в `requirement_blocks[].min_threshold`). Полная переиндексация ради
такого — час на 2 vCPU и пересоздание коллекции, то есть просадка поиска на всё это время;
обновление payload — секунды и без остановки.

Второе применение — деплой по рваному каналу. Снапшот коллекции весит десятки мегабайт, и когда
до VM не доходит даже мегабайт, дешевле доставить код (в нём лежат `structured/*.json`) и
пересобрать payload на месте: сеть нужна только под сам архив.

КАК СОПОСТАВЛЯЮТСЯ ТОЧКИ. По `source_anchor` («Приложение к ПП №719, Раздел XXIV, позиция 6») —
он есть у каждой записи корпуса и уникален. Привычный ключ (раздел + имя) здесь НЕ годится: в корпусе
7 пар записей делят название внутри раздела («Шасси с установленными двигателями…», «Устройства
наведения промышленные», «Стерилизаторы хирургические или лабораторные» и др.), и по такому ключу
14 точек получили бы payload ЧУЖОЙ записи — то есть скрипт, задуманный чинить данные, тихо портил
бы их. Поймано пробным прогоном на локальной коллекции: сверка показала «расхождения» там, где
их не было. Отсюда правило: ключ проверяется на уникальность ДО записи, коллизия — отказ.

Точки читаются постранично из самой коллекции, поэтому схема идентификаторов `load_kb` не важна.

ВЕКТОР. Если у записи изменился текст идентичности (`payload["text"]` — из него строится dense),
такая точка переэмбеддится, но только она. Модель уже загружена рантаймом, одна запись считается
секунды. Если изменились операции, а идентичность нет — вектор не трогаем: `build_embedding_text`
их и не включает (R9/F1).

⚠ Это НЕ замена `load_kb.py`. Скрипт не создаёт коллекцию, не удаляет лишние точки и не меняет
sparse-вектора: он приводит payload существующих точек к корпусу. Если менялся `build_text`
(sparse) или состав записей — нужна полная переиндексация.

Запуск:
  .venv/Scripts/python.exe scripts/sync_payloads.py                     # показать расхождения
  .venv/Scripts/python.exe scripts/sync_payloads.py --write             # применить
  docker compose exec -T app python scripts/sync_payloads.py --write    # на VM
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings                          # noqa: E402
from scripts.load_kb import build_embedding_text               # noqa: E402

STRUCT = ROOT / "knowledge_base" / "pp719" / "structured"

# Поля, которые считает сам загрузчик, а не корпус, — сверять их бессмысленно.
DERIVED = ("okpd2_prefixes",)


def _key(rec: dict) -> str:
    """Ключ записи — `source_anchor`. Пусто → запись сопоставить нельзя (см. модульный докстринг)."""
    return " ".join((rec.get("source_anchor") or "").split()).strip().lower()


def _same(a, b) -> bool:
    return json.dumps(a, ensure_ascii=False, sort_keys=True) == \
           json.dumps(b, ensure_ascii=False, sort_keys=True)


def main() -> None:
    write = "--write" in sys.argv
    from qdrant_client import QdrantClient, models

    client = QdrantClient(url=settings.QDRANT_URL, timeout=120)
    collection = settings.QDRANT_COLLECTION

    records: list[dict] = []
    for f in sorted(STRUCT.glob("*.json")):
        records.extend(json.loads(f.read_text(encoding="utf-8")))

    by_key: dict[str, dict] = {}
    nameless = [r for r in records if not _key(r)]
    collisions = []
    for r in records:
        k = _key(r)
        if not k:
            continue
        if k in by_key:
            collisions.append(k)
        by_key[k] = r
    if nameless or collisions:
        # Молча пропустить такие записи нельзя: точка получит payload чужой записи.
        print(f"⚠ ОТКАЗ: записей без source_anchor — {len(nameless)}, "
              f"повторяющихся ключей — {len(collisions)} {collisions[:3]}")
        print("  Сопоставление ненадёжно, ничего не трогаю. Чинить корпус или ключ.")
        raise SystemExit(2)
    print(f"записей в корпусе: {len(records)}, ключей: {len(by_key)}; коллекция: {collection}")

    changed: list[tuple] = []
    orphans: list[str] = []
    seen = 0
    offset = None
    while True:
        points, offset = client.scroll(collection_name=collection, limit=256, offset=offset,
                                       with_payload=True, with_vectors=False)
        for p in points:
            seen += 1
            pl = p.payload or {}
            rec = by_key.get(_key(pl))
            if rec is None:
                orphans.append(str(pl.get("product_name"))[:48])
                continue
            want = dict(rec)
            want["text"] = build_embedding_text(rec)
            diff = [k for k in want if k not in DERIVED and not _same(want[k], pl.get(k))]
            if diff:
                changed.append((p.id, pl.get("product_name"), diff, rec))
        if offset is None:
            break

    revec = [c for c in changed if "text" in c[2]]
    print(f"точек просмотрено: {seen}; payload разошёлся у {len(changed)}; "
          f"из них требуют нового вектора: {len(revec)}")
    for _, name, diff, _rec in changed[:20]:
        print(f"  · {str(name)[:52]!r} → {diff}")
    if orphans:
        print(f"⚠ точек, которым нет записи в корпусе: {len(orphans)} {orphans[:3]}")
        # ⚠ НАЙДЕНО РЕВЬЮ 20.08.2026. Раньше при пустом `changed` последней строкой печаталось
        # «расхождений нет — коллекция уже соответствует корпусу», и скрипт выходил нулём — даже
        # когда выше стояло предупреждение о точках-сиротах. А удаление записей (D12: −4 из
        # `VII_medizdeliya.json`) даёт ровно такую форму: полей никто не менял, `changed` пуст.
        # Оператор дешёвого пути выкатки (код + `sync_payloads --write` вместо часовой
        # переиндексации) читал ПОСЛЕДНЮЮ строку, считал шаг закрытым — и четыре исключённые
        # позиции оставались живыми в индексе, то есть правка до ответа не доезжала, а инструмент
        # рапортовал «чисто». Предохранитель, успокаивающий на реальной проблеме, хуже
        # отсутствующего: он закрывает шаг, который не выполнен.
        print(f"\n❌ {len(orphans)} точек не имеют записи в корпусе — payload их не чинит. "
              f"Нужна ПЕРЕИНДЕКСАЦИЯ (`load_kb.py`) или снапшот: удаление записей "
              f"`sync_payloads` не выполняет.")
        raise SystemExit(3)

    if not changed:
        print("расхождений нет — коллекция уже соответствует корпусу")
        return
    if not write:
        print("\nПРОБНЫЙ ПРОГОН — ничего не записано (нужен --write)")
        return

    for pid, _name, _diff, rec in changed:
        payload = dict(rec)
        payload["text"] = build_embedding_text(rec)
        client.set_payload(collection_name=collection, payload=payload, points=[pid])
    print(f"payload обновлён: {len(changed)} точек")

    if revec:
        from app.rag.embeddings import embed_passages
        vectors = {pid: embed_passages([build_embedding_text(rec)])[0]
                   for pid, _n, _d, rec in revec}
        client.update_vectors(collection_name=collection, points=[
            models.PointVectors(id=pid, vector={"dense": vec}) for pid, vec in vectors.items()])
        print(f"вектор пересчитан: {len(vectors)} точек")
    print("готово")


if __name__ == "__main__":
    main()
