"""Загрузка корпуса «Правила формирования и ведения реестра российской промышленной продукции»
в ОТДЕЛЬНУЮ коллекцию Qdrant (гибрид dense e5 + sparse BM25, RRF) — для ответов на ПРОЦЕДУРНЫЕ
вопросы (внесение в реестр, ГИСП, подача заявления, сроки, состав документов).

Почему отдельная коллекция (не в pp719):
  • pp719 — товарные позиции приложения к ПП №719 (требования/баллы); out-of-scope guard
    (retriever.dense_top1) и измеренный recall@1 калиброваны ИМЕННО на ней. Правила — проза норм,
    чужеродная товарной схеме и identity-эмбеддингу (F1). Отдельная коллекция pp719_rules
    оставляет товарный путь нетронутым (нулевой регресс) и не требует его переиндексации.

ВАЖНО про источник: «Правила ведения реестра» — ОТДЕЛЬНЫЙ акт (ст. 17.1 ФЗ «О промышленной
политике»), который ссылается на ПП №719, но это НЕ само 719. Атрибуция ссылок в ответах —
«Правила ведения реестра», см. app/core/prompts.PROCEDURAL_SYSTEM_PROMPT.

Источник — 5 сырых чанков knowledge_base/pp719/chunks/{11..15}_*.txt (нумерованные пункты норм).
Редакция берётся из самих текстов (последняя пометка «(в ред. Постановления … от ДД.ММ.ГГГГ N …)»)
и печатается — сверить с актуальной консолидированной редакцией на дату теста (директива заказчика).

Запуск (из корня, нужен поднятый Qdrant):
  .venv/Scripts/python.exe scripts/load_rules_kb.py           # пересоздать коллекцию и загрузить
  .venv/Scripts/python.exe scripts/load_rules_kb.py --smoke   # + смоук-запросы
  .venv/Scripts/python.exe scripts/load_rules_kb.py --smoke-only
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings  # noqa: E402

# embeddings/sparse импортируем ЛЕНИВО внутри index_all/hybrid_search (там, где нужны): так модуль
# импортируется дёшево (без загрузки sentence_transformers) — парсер тестируется без тяжёлых зависимостей.

CHUNKS_DIR = ROOT / "knowledge_base" / "pp719" / "chunks"
# ЯВНЫЙ список из 5 файлов (НЕ glob `1[1-5]*.txt` — он зацепит товарные 110_…129_ и 130_).
RULES_FILES = [
    "11_I_obshchie_polozheniya.txt",
    "12_II_vklyuchenie_svedeniy.txt",
    "13_III_vnesenie_izmeneniy.txt",
    "14_V_predostavlenie_svedeniy.txt",
    "15_VI_katalog_produkcii.txt",
]
DENSE = "dense"
SPARSE = "bm25"

SMOKE_QUERIES = [
    "какой порядок внесения продукции в реестр",
    "какие документы нужны для заявки в реестр",
    "сколько времени рассматривают заявку",
    "как обжаловать отказ во включении в реестр",
    "что такое каталог продукции",
]

# Пункт нормы начинается с начала строки: «1. », «3.1. », «12. » (номер, точка, пробел).
_POINT_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3})*)\.\s")
# Пометка редакции: «(в ред. Постановления Правительства РФ от 13.04.2026 N 400)».
_AMEND_RE = re.compile(r"от\s+(\d{1,2}\.\d{1,2}\.(\d{4}))\s+N\s*(\d+)")


def _normalize(text: str) -> str:
    """Чистка форматных артефактов: неразрывные пробелы → обычные (иначе маркер `N 400` и т.п.
    не ловится regex), схлопывание висячих пробелов, унификация переносов."""
    text = text.replace(" ", " ").replace(" ", " ").replace(" ", " ").replace("⁠", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # схлопнуть длинные прогоны пробелов/табов (но не переносы строк)
    return re.sub(r"[ \t]{2,}", " ", text)


def parse_rules_file(path: Path) -> list[dict]:
    """Разбирает один файл Правил в записи-пункты. Первая строка `# <ROMAN>. <title>` даёт
    section_roman/section_title; тело режется на пункты по _POINT_RE (подпункты «а)/б)», строки
    определений и пометки «(в ред. …)» остаются внутри текущего пункта)."""
    raw = _normalize(path.read_text(encoding="utf-8"))
    lines = raw.split("\n")
    roman, title = "", ""
    body_start = 0
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s:
            continue
        m = re.match(r"^#\s*([IVXLC]+)\.\s*(.+)$", s)
        if m:
            roman, title = m.group(1), m.group(2).strip()
            body_start = i + 1
        break  # заголовок — первая непустая строка; дальше тело

    records: list[dict] = []
    cur_point: str | None = None
    cur_lines: list[str] = []

    def _flush():
        if cur_point is None:
            return
        text = "\n".join(cur_lines).strip()
        if not text:
            return
        records.append({
            "doc_type": "rules_registry",
            "section_roman": roman,
            "section_title": title,
            "point": cur_point,
            "text": text,
            "source_anchor": f"Правила ведения реестра, п. {cur_point}"
                             + (f" ({roman}. {title})" if roman else ""),
        })

    for ln in lines[body_start:]:
        m = _POINT_RE.match(ln)
        if m:
            _flush()
            cur_point = m.group(1)
            cur_lines = [ln.rstrip()]
        elif cur_point is not None:
            cur_lines.append(ln.rstrip())
        # строки до первого пункта (пустые/остаток заголовка) игнорируем
    _flush()
    return records


def detect_edition(all_text: str) -> str:
    """Последняя (по дате) пометка «(в ред. … от ДД.ММ.ГГГГ N …)» во всём корпусе — честный штамп
    редакции индексируемого текста."""
    best_key, best_label = (0, 0, 0, 0), None
    for m in _AMEND_RE.finditer(all_text):
        d, mth, y = (int(x) for x in m.group(1).split("."))
        num = int(m.group(3))
        key = (y, mth, d, num)
        if key > best_key:
            best_key, best_label = key, f"ред. от {m.group(1)} N {m.group(3)}"
    return best_label or "редакция не определена в тексте"


def load_records() -> tuple[list[dict], str]:
    recs: list[dict] = []
    corpus_text: list[str] = []
    for name in RULES_FILES:
        path = CHUNKS_DIR / name
        if not path.exists():
            sys.exit(f"Нет файла Правил: {path}")
        txt = _normalize(path.read_text(encoding="utf-8"))
        corpus_text.append(txt)
        part = parse_rules_file(path)
        if not part:
            print(f"⚠️  {name}: не распознано ни одного пункта — проверь формат")
        recs.extend(part)
    edition = detect_edition("\n".join(corpus_text))
    for r in recs:
        r["edition"] = edition
    if not recs:
        sys.exit("Правила не распознаны — проверь knowledge_base/pp719/chunks/11..15")
    return recs, edition


def point_id(rec: dict) -> str:
    return str(uuid5(NAMESPACE_URL, f"rules|{rec.get('section_roman')}|{rec.get('point')}"))


def make_client():
    from qdrant_client import QdrantClient

    return QdrantClient(url=settings.QDRANT_URL, timeout=60)


def recreate_collection(client) -> None:
    from qdrant_client import models

    name = settings.QDRANT_RULES_COLLECTION
    if client.collection_exists(name):
        client.delete_collection(name)
    client.create_collection(
        collection_name=name,
        vectors_config={
            DENSE: models.VectorParams(size=settings.EMBEDDING_DIM, distance=models.Distance.COSINE)
        },
        sparse_vectors_config={SPARSE: models.SparseVectorParams(modifier=models.Modifier.IDF)},
    )
    client.create_payload_index(name, "doc_type", models.PayloadSchemaType.KEYWORD)
    client.create_payload_index(name, "section_roman", models.PayloadSchemaType.KEYWORD)


def index_all(client, recs: list[dict], batch: int = 64) -> None:
    from qdrant_client import models

    from app.rag.embeddings import embed_passages
    from app.rag.sparse import doc_length, document_vector

    try:
        from tqdm import tqdm
    except ImportError:
        tqdm = None

    name = settings.QDRANT_RULES_COLLECTION
    texts = [r["text"] for r in recs]  # эмбеддим содержание пункта (Правила ищутся по смыслу нормы)
    lengths = [doc_length(t) for t in texts]
    avgdl = (sum(lengths) / len(lengths)) if lengths else 1.0
    print(f"Пунктов Правил: {len(recs)} | avgdl (токенов): {avgdl:.1f} | макс={max(lengths)} мин={min(lengths)}")

    bar = tqdm(total=len(recs), unit="пункт", desc="Эмбеддинг+апсерт") if tqdm else None
    for start in range(0, len(recs), batch):
        chunk_texts = texts[start:start + batch]
        dvecs = embed_passages(chunk_texts)
        points = []
        for k, dvec in enumerate(dvecs):
            j = start + k
            rec, text = recs[j], texts[j]
            idx, val = document_vector(text, avgdl)
            payload = dict(rec)
            points.append(models.PointStruct(
                id=point_id(rec),
                vector={DENSE: dvec, SPARSE: models.SparseVector(indices=idx, values=val)},
                payload=payload,
            ))
        client.upsert(collection_name=name, points=points)
        if bar:
            bar.update(len(points))
    if bar:
        bar.close()


def hybrid_search(client, query: str, limit: int = 5):
    from qdrant_client import models

    from app.rag.embeddings import embed_query
    from app.rag.sparse import query_vector

    dvec = embed_query(query)
    idx, val = query_vector(query)
    res = client.query_points(
        collection_name=settings.QDRANT_RULES_COLLECTION,
        prefetch=[
            models.Prefetch(query=dvec, using=DENSE, limit=20),
            models.Prefetch(query=models.SparseVector(indices=idx, values=val), using=SPARSE, limit=20),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=limit,
        with_payload=True,
    )
    return res.points


def run_smoke(client) -> None:
    print("\n" + "=" * 70)
    print("SMOKE-ТЕСТ ПОИСКА ПО ПРАВИЛАМ РЕЕСТРА (dense e5 + sparse BM25, RRF)")
    print("=" * 70)
    for query in SMOKE_QUERIES:
        print(f"\n🔎 «{query}»")
        for i, p in enumerate(hybrid_search(client, query), 1):
            pl = p.payload or {}
            snippet = (pl.get("text") or "").replace("\n", " ")[:90]
            print(f"  {i}. п.{pl.get('point', '?')} [{pl.get('section_roman', '?')}] "
                  f"(score={p.score:.4f}) {snippet}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Загрузка корпуса Правил ведения реестра в Qdrant")
    ap.add_argument("--smoke", action="store_true", help="после загрузки прогнать смоук-запросы")
    ap.add_argument("--smoke-only", action="store_true", help="только смоук (без перезагрузки)")
    ap.add_argument("--batch", type=int, default=64)
    args = ap.parse_args()

    client = make_client()

    if not args.smoke_only:
        recs, edition = load_records()
        print(f"Редакция индексируемого текста Правил: {edition}")
        print(f"Коллекция: {settings.QDRANT_RULES_COLLECTION}")
        recreate_collection(client)
        index_all(client, recs, batch=args.batch)
        info = client.get_collection(settings.QDRANT_RULES_COLLECTION)
        print(f"\n✅ Коллекция '{settings.QDRANT_RULES_COLLECTION}': точек = {info.points_count}")

    if args.smoke or args.smoke_only:
        run_smoke(client)


if __name__ == "__main__":
    main()
