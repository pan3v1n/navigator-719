"""Дозаполнить поле okpd2_prefixes в существующей коллекции pp719 БЕЗ ре-эмбеддинга (T5, Волна 2).

Поле okpd2_prefixes (все префиксы кодов записи) нужно ретриверу для поиска по ЧАСТИЧНОМУ коду
(класс-код «26.51.52» находит дочерние «26.51.52.120»). `load_kb.py` пишет его при полной
переиндексации; этот скрипт добавляет его к УЖЕ проиндексированной коллекции через set_payload
(быстро, векторы не трогаются) — чтобы не гонять 20-минутный ре-эмбеддинг только ради payload-поля.

Запуск (из корня, нужен поднятый Qdrant с коллекцией pp719):
  .venv/Scripts/python.exe scripts/backfill_okpd2_prefixes.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings  # noqa: E402


def okpd2_prefixes(codes: list[str]) -> list[str]:
    """26.51.52.120 → [26, 26.51, 26.51.52, 26.51.52.120]; объединение по всем кодам записи."""
    out: set[str] = set()
    for c in codes or []:
        segs = [s for s in str(c).replace(" ", "").split(".") if s]
        for i in range(len(segs)):
            out.add(".".join(segs[: i + 1]))
    return sorted(out)


def main() -> None:
    from qdrant_client import QdrantClient, models

    client = QdrantClient(url=settings.QDRANT_URL, timeout=60)
    name = settings.QDRANT_COLLECTION
    if not client.collection_exists(name):
        sys.exit(f"Нет коллекции {name} — сначала load_kb.py")

    try:
        client.create_payload_index(name, "okpd2_prefixes", models.PayloadSchemaType.KEYWORD)
        print("payload-индекс okpd2_prefixes создан")
    except Exception as e:  # noqa: BLE001 — индекс мог уже существовать
        print(f"payload-индекс: {str(e)[:70]}")

    offset, seen, updated = None, 0, 0
    while True:
        pts, offset = client.scroll(name, limit=256, offset=offset,
                                    with_payload=["okpd2_codes"], with_vectors=False)
        if not pts:
            break
        for p in pts:
            seen += 1
            pref = okpd2_prefixes((p.payload or {}).get("okpd2_codes") or [])
            if pref:
                client.set_payload(name, payload={"okpd2_prefixes": pref}, points=[p.id])
                updated += 1
        if offset is None:
            break
    print(f"Обработано точек: {seen} | проставлено okpd2_prefixes: {updated}")
    print(f"✅ Коллекция '{name}': точек = {client.get_collection(name).points_count}")


if __name__ == "__main__":
    main()
