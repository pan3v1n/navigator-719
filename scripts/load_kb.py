"""Загрузка структурированной базы знаний ПП №719 в Qdrant (гибрид dense + sparse).

Схема (см. docs/V1_SPEC.md, Шаг 0–3):
  • один вид продукции = одна точка (чанк);
  • dense-вектор — multilingual-e5-large (1024d, косинус);
  • sparse-вектор — локальный BM25 (app/rag/sparse, Modifier.IDF на коллекции);
  • payload — ВСЯ структура записи из structured/*.json (чтобы не перепарсивать) +
    служебные поля для фильтра (okpd2_codes, section_roman, text).
  • тексты для векторов РАЗНЫЕ (R9, асимметрия):
      – DENSE — ИДЕНТИЧНОСТЬ записи (build_embedding_text: имя/раздел/код/порог), БЕЗ операций.
        Это F1 (2026-06-30): полный текст топил дискриминативный сигнал имени бойлерплейтом →
        product-level recall@1 0.886→0.972, перефраз raw 0.533→0.663 (docs/eval_coverage_report.md,
        docs/eval_paraphrase_report.md).
      – SPARSE — ПОЛНЫЙ текст записи (build_text: + компоненты, операции, методичка, примечания).
        До R9 sparse строился из того же identity-текста, и BM25 терял свой единственный смысл:
        лексический поиск по формулировкам требований («закалка зубьев», «пайка волной») не
        работал НИ ОДНИМ каналом — этих слов не было в индексе. Гибрид вырождался в
        «dense + BM25 по четырём полям идентичности».
    Откат обоих каналов на полный текст (состояние до F1) — флаг --full-text.

Запуск (из корня, через venv; нужен поднятый Qdrant на :6333):
  .venv/Scripts/python.exe scripts/load_kb.py            # пересоздать коллекцию и загрузить всё
  .venv/Scripts/python.exe scripts/load_kb.py --smoke    # + прогнать smoke-запросы
  .venv/Scripts/python.exe scripts/load_kb.py --smoke-only  # только запросы (без перезагрузки)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings  # noqa: E402
from app.rag.embeddings import embed_passages, embed_query  # noqa: E402
from app.rag.sparse import doc_length, document_vector, query_vector  # noqa: E402

STRUCT_DIR = ROOT / "knowledge_base" / "pp719" / "structured"
CACHE_DIR = ROOT / ".emb_cache"
DENSE = "dense"
SPARSE = "bm25"

# Примеры для smoke-теста: (запрос, ожидаемый раздел/тема для глазами-проверки)
SMOKE_QUERIES = [
    ("производим прицепы и полуприцепы для легковых автомобилей", "29.20.23"),
    ("сварка и окраска кузова легкового автомобиля", None),
    ("станки металлорежущие с числовым программным управлением", None),
    ("таблетки и капсулы, фармацевтическое производство", None),
    ("чиллеры и компрессорно-конденсаторные блоки", None),
]


# --------------------------------------------------------------------------- #
# Текст записи для эмбеддинга (общий для dense и sparse)
# --------------------------------------------------------------------------- #
def build_text(rec: dict) -> str:
    parts: list[str] = [rec.get("product_name") or ""]
    title = rec.get("section_title")
    if title:
        parts.append(f"Раздел {rec.get('section_roman', '')}: {title}")
    codes = rec.get("okpd2_codes") or []
    if codes:
        parts.append("ОКПД2: " + ", ".join(codes))
    mt = rec.get("min_threshold")
    if mt:
        parts.append(str(mt))
    for b in rec.get("requirement_blocks") or []:
        comp = b.get("component")
        if comp:
            parts.append(comp)
        for o in b.get("operations") or []:
            t = o.get("text")
            if t:
                parts.append(t)
    for ln in rec.get("methodology_thresholds") or []:  # запись-методичка
        parts.append(ln)
    notes = rec.get("notes")
    if notes:
        parts.append(str(notes))
    return "\n".join(p for p in parts if p)


def build_embedding_text(rec: dict) -> str:
    """F1: текст ДЛЯ ЭМБЕДДИНГА — только ИДЕНТИЧНОСТЬ продукта (имя/раздел/код), БЕЗ операций
    (рецепта локализации) и БЕЗ порога. Операции и порог остаются в payload (для показа,
    генерации и расчёта), но не топят дискриминативный сигнал имени в векторе.

    Мотивация: `build_text` эмбеддит весь рецепт локализации (многословные generic операции
    сборки/сварки + повсеместный бойлерплейт ЕАЭС) → dense-вектор схлопывается к centroid'у
    «обобщённой машины», короткоотличительные записи становятся недостижимы даже отличительным
    запросом (аномалия «Машины самоходные для добычи» → top-1 «Присадки к топливу»). Валидировано
    e5-косинусами: identity-only поднял cos с отличительными запросами 0.78→0.84 и перевернул
    результат. Попутно сокращает документ (≈×7) → снимает штраф BM25 за длину. См. memory
    navigator-719-test-battery.

    ⚠ ПОРОГ УБРАН ИЗ ВЕКТОРА 17.08.2026 (EV5, issue #82). Он попадал сюда как часть «идентичности»,
    но идентичностью не является: формулировка «до 31 декабря 2023 г. - не менее 90 баллов;
    с 1 января 2024 г. - не менее 120 баллов…» — общий бойлерплейт сотен позиций, и у короткого
    наименования она занимает БОЛЬШУЮ ЧАСТЬ документа. Следствие измерено: запрос-пустышка
    «сколько баллов нужно для производства» — без единого товара — давал «Конвейеры скребковые»
    с косинусом 0.858, то есть выше, чем целевая позиция получает на своём же продукте. Любой
    вопрос про баллы (а это самый частый класс) притягивался к коротким записям с порогом.

    Замер на e5 до переиндексации, запрос «сколько баллов нужно для производства городских
    автобусов»: «Комбайны проходческие» 0.8433 → **0.7799**, «Конвейеры скребковые» 0.8420 →
    **0.7784**, целевые «Автобусы…» 0.8107 (порога в записи нет, вектор не менялся) — порядок
    переворачивается. Компромисс «оставить только действующее значение» проверен и отвергнут:
    0.827, всё ещё выше целевой."""
    parts: list[str] = [rec.get("product_name") or ""]
    title = rec.get("section_title")
    if title:
        parts.append(f"Раздел {rec.get('section_roman', '')}: {title}")
    codes = rec.get("okpd2_codes") or []
    if codes:
        parts.append("ОКПД2: " + ", ".join(codes))
    return "\n".join(p for p in parts if p)


def load_records() -> list[dict]:
    recs: list[dict] = []
    for f in sorted(STRUCT_DIR.glob("*.json")):
        recs.extend(json.loads(f.read_text(encoding="utf-8")))
    if not recs:
        sys.exit(f"Нет записей в {STRUCT_DIR} — сначала структуризация (structure_kb.py)")
    return recs


def point_id(rec: dict, i: int) -> str:
    anchor = rec.get("source_anchor") or f"{rec.get('section_roman')}|{i}"
    return str(uuid5(NAMESPACE_URL, f"pp719|{anchor}|{rec.get('product_name', '')}"))


# --------------------------------------------------------------------------- #
# Qdrant
# --------------------------------------------------------------------------- #
def make_client():
    from qdrant_client import QdrantClient

    return QdrantClient(url=settings.QDRANT_URL, timeout=60)


def okpd2_prefixes(codes: list[str]) -> list[str]:
    """Все префиксы кодов записи: 26.51.52.120 → [26, 26.51, 26.51.52, 26.51.52.120]. Поле для
    фильтра «поиск по частичному коду» (T5): класс-код запроса (26.51.52) матчит дочерние позиции
    ветки (26.51.52.120), которые точный MatchAny по okpd2_codes не находит."""
    out: set[str] = set()
    for c in codes or []:
        segs = [s for s in str(c).replace(" ", "").split(".") if s]
        for i in range(len(segs)):
            out.add(".".join(segs[: i + 1]))
    return sorted(out)


def recreate_collection(client) -> None:
    from qdrant_client import models

    name = settings.QDRANT_COLLECTION
    if client.collection_exists(name):
        client.delete_collection(name)
    client.create_collection(
        collection_name=name,
        vectors_config={
            DENSE: models.VectorParams(
                size=settings.EMBEDDING_DIM, distance=models.Distance.COSINE
            )
        },
        sparse_vectors_config={
            SPARSE: models.SparseVectorParams(modifier=models.Modifier.IDF)
        },
    )
    # индекс по кодам ОКПД2 — для жёсткого фильтра/буста в retriever
    client.create_payload_index(name, "okpd2_codes", models.PayloadSchemaType.KEYWORD)
    client.create_payload_index(name, "okpd2_prefixes", models.PayloadSchemaType.KEYWORD)  # T5: частичный код
    client.create_payload_index(name, "section_roman", models.PayloadSchemaType.KEYWORD)


def _text_key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _model_slug(model: str) -> str:
    """Имя модели, пригодное для имени файла на любой ФС (D7).

    EMBEDDING_MODEL — это либо репозиторий HF (`intfloat/multilingual-e5-large`), либо
    АБСОЛЮТНЫЙ путь к скачанной папке (`D:/navigator-719/models/...`, см. SETUP.md шаг 5).
    Прежняя схема подставляла значение как есть, и двоеточие после буквы диска Windows
    понимал как разделитель альтернативного потока NTFS: на диске оставался файл нулевого
    размера, а 17 МБ векторов уезжали в поток — невидимый для `du`, бэкапа и обычного
    копирования папки. Берём последний сегмент пути (читаемо) + хэш полного значения
    (две разные модели с одинаковым именем папки не делят кэш).
    """
    tail = re.split(r"[\\/]+", model.strip().rstrip("\\/"))[-1]
    safe = re.sub(r"[^0-9A-Za-z._-]+", "_", tail) or "model"
    return f"{safe}_{hashlib.sha1(model.encode('utf-8')).hexdigest()[:8]}"


def _cache_file() -> Path:
    return CACHE_DIR / f"dense_{_model_slug(settings.EMBEDDING_MODEL)}.npz"


def _legacy_cache_file() -> Path:
    """Имя кэша до D7 — нужно ровно для однократного переноса."""
    return CACHE_DIR / f"dense_{settings.EMBEDDING_MODEL.replace('/', '_')}.npz"


def _migrate_legacy_cache() -> None:
    """Перенести кэш со старого имени на новое — без пересчёта эмбеддингов.

    Без переноса первый прогон после D7 не нашёл бы кэш и посчитал бы e5 по всей базе
    заново (на CPU это десятки минут), а старые 17 МБ остались бы висеть в NTFS-потоке.
    """
    import numpy as np

    new, old = _cache_file(), _legacy_cache_file()
    if new == old or new.exists() or not old.exists():
        return

    CACHE_DIR.mkdir(exist_ok=True)
    with np.load(old, allow_pickle=True) as data:
        np.savez(new, keys=data["keys"], vecs=data["vecs"])
    old.unlink()
    # На Windows старое имя было ПОТОКОМ: удаление потока оставляет пустой файл-носитель
    # (`.emb_cache/dense_D`) — убираем и его, иначе мусор переживёт миграцию.
    carrier = CACHE_DIR / old.name.split(":", 1)[0]
    if carrier != old and carrier.exists() and carrier.stat().st_size == 0:
        carrier.unlink()
    print(f"Кэш эмбеддингов перенесён: {old.name} -> {new.name}")


def dense_vectors_cached(texts: list[str], batch: int = 128) -> list[list[float]]:
    """Dense-векторы с диск-кэшем: e5 считаем только для новых/изменённых текстов.

    Ключ — sha1(текст). Кэш привязан к модели (имя в имени файла). Повторные загрузки
    после правок схемы переиндексируют лишь изменённые чанки — экономия минут на CPU.
    """
    import numpy as np

    try:
        from tqdm import tqdm
    except ImportError:
        tqdm = None

    _migrate_legacy_cache()

    cache: dict[str, "np.ndarray"] = {}
    f = _cache_file()
    if f.exists():
        data = np.load(f, allow_pickle=True)
        cache = {k: v for k, v in zip(data["keys"], data["vecs"])}

    keys = [_text_key(t) for t in texts]
    missing = [i for i, k in enumerate(keys) if k not in cache]
    print(f"Кэш эмбеддингов: {len(texts) - len(missing)}/{len(texts)} готовы, считаем {len(missing)} новых")

    if missing:
        bar = tqdm(total=len(missing), unit="чанк", desc="Эмбеддинг e5") if tqdm else None
        for s in range(0, len(missing), batch):
            idxs = missing[s : s + batch]
            vecs = embed_passages([texts[i] for i in idxs])
            for i, v in zip(idxs, vecs):
                cache[keys[i]] = np.asarray(v, dtype=np.float32)
            if bar:
                bar.update(len(idxs))
        if bar:
            bar.close()
        CACHE_DIR.mkdir(exist_ok=True)
        all_keys = list(cache.keys())
        np.savez(
            f,
            keys=np.array(all_keys, dtype=object),
            vecs=np.stack([cache[k] for k in all_keys]),
        )

    return [cache[k].tolist() for k in keys]


def index_all(client, recs: list[dict], batch: int = 128,
              text_fn=build_embedding_text, sparse_text_fn=build_text) -> None:
    """Индексация с АСИММЕТРИЧНЫМ текстом: dense — identity, sparse — полный текст (R9).

    Почему асимметрия. F1 (2026-06-30) убрал операции из текста эмбеддинга и поднял
    product-level recall@1 0.886→0.972 — для dense это верно: многословные generic-операции
    схлопывают вектор к centroid'у «обобщённой машины». Но тот же текст использовался и для
    sparse, и BM25 из-за этого потерял СВОЙ ЕДИНСТВЕННЫЙ СМЫСЛ — лексический поиск по
    формулировкам требований. Запрос «закалка зубьев» или «пайка волной» не находил ничего
    ни одним каналом: этих слов не было в индексе, гибрид выродился в «dense + BM25 по четырём
    полям идентичности».

    Теперь: dense — `build_embedding_text` (имя/раздел/код/порог), sparse — `build_text`
    (то же плюс компоненты, операции, методичка, примечания). `avgdl` считается по SPARSE-текстам,
    иначе нормировка BM25 по длине была бы к чужому корпусу."""
    from qdrant_client import models

    try:
        from tqdm import tqdm
    except ImportError:
        tqdm = None

    name = settings.QDRANT_COLLECTION
    texts = [text_fn(r) for r in recs]                     # dense: идентичность записи
    sparse_texts = [sparse_text_fn(r) for r in recs]       # sparse: полный текст записи
    # средняя длина документа в токенах — для нормировки BM25 по длине (по SPARSE-текстам!)
    lengths = [doc_length(t) for t in sparse_texts]
    avgdl = (sum(lengths) / len(lengths)) if lengths else 1.0
    d_len = [doc_length(t) for t in texts]
    print(f"avgdl sparse (токенов на чанк): {avgdl:.1f}  | макс={max(lengths)}  мин={min(lengths)}")
    print(f"для сравнения, длина dense-текста: сред={sum(d_len)/len(d_len):.1f}  макс={max(d_len)}")

    dense_all = dense_vectors_cached(texts, batch=batch)

    bar = tqdm(total=len(recs), unit="чанк", desc="Апсерт в Qdrant") if tqdm else None
    for start in range(0, len(recs), batch):
        points = []
        for k in range(start, min(start + batch, len(recs))):
            rec, text = recs[k], texts[k]
            idx, val = document_vector(sparse_texts[k], avgdl)  # R9: BM25 — по ПОЛНОМУ тексту
            payload = dict(rec)
            payload["text"] = text  # идентичность (то, что легло в dense) — для отладки выдачи
            payload["okpd2_prefixes"] = okpd2_prefixes(rec.get("okpd2_codes") or [])  # T5: частичный код
            points.append(
                models.PointStruct(
                    id=point_id(rec, k),
                    vector={
                        DENSE: dense_all[k],
                        SPARSE: models.SparseVector(indices=idx, values=val),
                    },
                    payload=payload,
                )
            )
        client.upsert(collection_name=name, points=points)
        if bar:
            bar.update(len(points))
    if bar:
        bar.close()


# --------------------------------------------------------------------------- #
# Smoke-поиск (мини-гибрид: dense + sparse, слияние RRF)
# --------------------------------------------------------------------------- #
def hybrid_search(client, query: str, limit: int = 5):
    from qdrant_client import models

    dvec = embed_query(query)
    idx, val = query_vector(query)
    res = client.query_points(
        collection_name=settings.QDRANT_COLLECTION,
        prefetch=[
            models.Prefetch(query=dvec, using=DENSE, limit=20),
            models.Prefetch(
                query=models.SparseVector(indices=idx, values=val),
                using=SPARSE,
                limit=20,
            ),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=limit,
        with_payload=True,
    )
    return res.points


def run_smoke(client) -> None:
    print("\n" + "=" * 70)
    print("SMOKE-ТЕСТ ГИБРИДНОГО ПОИСКА (dense e5 + sparse BM25, RRF)")
    print("=" * 70)
    for query, okpd2 in SMOKE_QUERIES:
        print(f"\n🔎 «{query}»" + (f"  [ОКПД2 {okpd2}]" if okpd2 else ""))
        for i, p in enumerate(hybrid_search(client, query), 1):
            pl = p.payload or {}
            codes = ", ".join(pl.get("okpd2_codes") or []) or "—"
            print(
                f"  {i}. [{pl.get('section_roman', '?')}] "
                f"{(pl.get('product_name') or '')[:70]}  "
                f"(score={p.score:.4f}, ОКПД2 {codes})"
            )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description="Загрузка базы знаний ПП №719 в Qdrant")
    ap.add_argument("--smoke", action="store_true", help="после загрузки прогнать smoke-запросы")
    ap.add_argument("--smoke-only", action="store_true", help="только smoke-запросы (без перезагрузки)")
    ap.add_argument("--batch", type=int, default=128, help="размер батча апсерта")
    ap.add_argument("--full-text", action="store_true",
                    help="откат: DENSE тоже на полном тексте с операциями (до F1). По умолчанию "
                         "асимметрия — dense на идентичности (F1), sparse на полном тексте (R9)")
    args = ap.parse_args()

    client = make_client()

    if not args.smoke_only:
        recs = load_records()
        n_prod = sum(1 for r in recs if r.get("record_type") != "section_methodology")
        print(f"Записей к загрузке: {len(recs)} (продуктов {n_prod} + методичек {len(recs) - n_prod})")
        text_fn = build_text if args.full_text else build_embedding_text
        mode = ("FULL-TEXT: и dense, и sparse на полном тексте (откат до F1)" if args.full_text
                else "АСИММЕТРИЯ (R9): dense — идентичность (F1), sparse — полный текст")
        print(f"Эмбеддинг: {mode}. Коллекция: {settings.QDRANT_COLLECTION}")
        recreate_collection(client)
        index_all(client, recs, batch=args.batch, text_fn=text_fn)
        info = client.get_collection(settings.QDRANT_COLLECTION)
        print(f"\n✅ Коллекция '{settings.QDRANT_COLLECTION}': точек = {info.points_count}")

    if args.smoke or args.smoke_only:
        run_smoke(client)


if __name__ == "__main__":
    main()
