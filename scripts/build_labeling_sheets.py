"""Листы разметки для владельца: `EV8-1` #178 и `K18` #175 — независимый набор из вопросов трафика.

ЗАЧЕМ. Обе задачи упёрлись в одно: проверять правку нечем, кроме наборов, порождённых той же логикой,
что и правка (`EV8-1` отклонена ревью дважды — «набор проб тавтологичен»; у `K18` лексическая прокси
класс не отделяет). Решение владельца 28.10.2026: лист готовит сессия, размечает владелец, лист лежит
ВНЕ репозитория (`D:\\navigator-export`).

  * `EV8-1`: вопросы трафика с кодом ОКПД2, под ТОЧНЫМ ключом которого в приложении ≥ 3 позиций.
    Строка листа — пара «вопрос × позиция под этим кодом» (все позиции ключа, с полными
    наименованиями — оговорки «за исключением» видны). Владелец отмечает, о каких позициях вопрос.
    Это эталон «кто целевой» — независимый от правила сужения.
  * `K18`: вопросы товарной ветки без кода. Стратифицированная выборка: слой А — ни одна значимая
    основа вопроса не встречается в наименованиях окна выдачи (кандидаты в «бытовое слово»), слой Б —
    остальные (контроль: без него размер класса не оценить). Слой в лист НЕ пишется (не подсказывать
    разметчику), он — в файле ключа. Для контекста показаны три первые позиции окна.

«Значимая основа» — основа Snowball (как в sparse-поиске) без цифр, длиной ≥ 3, встречающаяся не
более чем в `--stop-df` доле вопросов пула: частые основы — слова вопроса («требован», «нужн»,
«производ»), а не продукция. Это только стратификация выборки; размер класса даст разметка.

Формат — CSV в UTF-8 с BOM и разделителем «;»: открывается в русском Excel двойным щелчком.
Инструкция — в первых строках листа; строка заголовка начинается с «№».

⚠ 152-ФЗ. На вход — копия боевой базы (вне репозитория, только чтение). Листы и ключ пишутся ТОЛЬКО
вне репозитория; на экран — только агрегаты.

Запуск (Git Bash, нужен Qdrant для окна K18):
  NO_PROXY=localhost,127.0.0.1 QDRANT_COLLECTION=pp719_ed1031 .venv/Scripts/python \\
      scripts/build_labeling_sheets.py --db D:/navigator-backups/navigator_app-<дата>.db \\
      --out-dir D:/navigator-export
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TNVED_RE = re.compile(r"(?<![\d.])\d{4}(?:\s?\d{2}){1,3}(?:\s?\d)?(?![\d.])")
MIN_POSITIONS = 3
SHORT = "Приложение к ПП №719, "

EV81_HELP = [
    "ЛИСТ РАЗМЕТКИ EV8-1 (#178): вопрос с кодом ОКПД2, под которым в приложении несколько позиций.",
    "Что отметить: в колонке «Вопрос про эту позицию?» поставьте «да» у каждой позиции, о которой спрашивает "
    "вопрос (их может быть несколько). «нет» — вопрос явно не про неё. «?» — по вопросу не понять.",
    "Пустая ячейка читается как «нет». Если вопрос вообще не про продукцию под этим кодом — отметьте это в "
    "комментарии к первой строке вопроса.",
    "Не правьте колонки слева от отметки: по номеру и якорю разметка сопоставляется с корпусом.",
]
K18_HELP = [
    "ЛИСТ РАЗМЕТКИ K18 (#175): вопрос о продукции без кода ОКПД2. Справа — три первые позиции, "
    "которые нашёл поиск сервиса.",
    "Колонка «Названа словом не из приложения?»: «да» — продукция названа бытовым, разговорным или "
    "торговым словом, которого нет в наименованиях позиций приложения (пример: «полотенца» при позиции "
    "«Белье туалетное и кухонное»); «нет» — названа так, как в приложении; «не продукция» — вопрос не о продукции.",
    "Колонка «Верная позиция среди показанных?»: номер 1–3, «нет» или «не знаю». Если знаете верную позицию "
    "или код ОКПД2 — впишите в следующую колонку.",
]


def norm_q(text: str) -> str:
    return " ".join((text or "").split())


def load_questions(db_path: Path) -> list[dict]:
    """Уникальные вопросы трафика: чат и пробный режим. Только чтение копии."""
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        tables = {r[0] for r in db.execute("select name from sqlite_master where type='table'")}
        rows = list(db.execute("select 'chat', id, content from messages where role='user'"))
        if "guest_messages" in tables:
            rows += list(db.execute("select 'guest', id, content from guest_messages where role='user'"))
    db.close()
    seen, out = set(), []
    for src, mid, content in rows:
        q = norm_q(content)
        key = q.casefold()
        if not q or key in seen:
            continue
        seen.add(key)
        out.append({"src": src, "mid": mid, "q": q})
    return out


def code_index(records: list[dict]) -> dict[str, list[dict]]:
    idx: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        for c in r.get("okpd2_codes") or []:
            idx[c].append(r)
    return idx


def ev81_rows(questions: list[dict], idx: dict[str, list[dict]]) -> tuple[list[dict], dict]:
    from app.tools.navigator import extract_okpd2_all

    picked, stats = [], Counter()
    for item in questions:
        codes = extract_okpd2_all(item["q"])
        if not codes:
            continue
        stats["вопросов с кодом"] += 1
        for code in codes:
            recs = idx.get(code) or []
            if len(recs) >= MIN_POSITIONS:
                picked.append({**item, "code": code, "recs": recs})
                stats["пар «вопрос × ключ ≥3»"] += 1
                stats["из них ключ ≥10 позиций"] += len(recs) >= 10
    return picked, dict(stats)


def significant_stems(text: str, stop: set[str]) -> set[str]:
    from app.rag.sparse import tokenize

    return {t for t in tokenize(text) if len(t) >= 3 and not any(ch.isdigit() for ch in t)
            and t not in stop}


def k18_pool(questions: list[dict]) -> list[dict]:
    """Вопросы товарной ветки без кода — тем же гейтом, что `build_gs_natural`."""
    from app.rag import meta, procedural, translate
    from app.tools.navigator import extract_okpd2_all
    from build_gs_natural import CONTINUATION

    pool = []
    for item in questions:
        q = item["q"]
        if (len(q) < 12 or CONTINUATION.match(q) or extract_okpd2_all(q) or TNVED_RE.search(q)
                or meta.is_meta(q) or translate.is_translate(q) or procedural.is_procedural(q)):
            continue
        pool.append(item)
    return pool


def window_for(q: str) -> list | None:
    """Окно выдачи (8 позиций) путём ответа до модели; None — путь ушёл бы в модель или не в товарную."""
    from app.rag import pipeline

    try:
        plan = pipeline._plan_answer(q, okpd2=None)
    except RuntimeError:
        return None
    if isinstance(plan, pipeline.Answer):
        return None
    return list(plan.hits)


def k18_rows(pool: list[dict], per_a: int, per_b: int, stop_df: float,
             seed: int) -> tuple[list[dict], dict]:
    from app.rag.sparse import tokenize

    df = Counter()
    for item in pool:
        df.update({t for t in tokenize(item["q"])})
    stop = {t for t, n in df.items() if n > stop_df * max(len(pool), 1)}

    a, b, stats = [], [], Counter(пул=len(pool), **{"частых основ отброшено": len(stop)})
    for item in pool:
        stems = significant_stems(item["q"], stop)
        if not stems:
            stats["без значимых основ"] += 1
            continue
        hits = window_for(item["q"])
        if hits is None:
            stats["путь не товарный"] += 1
            continue
        names = set()
        for h in hits:
            names |= set(tokenize(h.product_name or ""))
        row = {**item, "hits": hits[:3]}
        (a if not (stems & names) else b).append(row)
    stats["слой А (нет общих основ с окном)"] = len(a)
    stats["слой Б (контроль)"] = len(b)
    rnd = random.Random(seed)
    sample = [dict(r, stratum="A") for r in rnd.sample(a, min(per_a, len(a)))]
    sample += [dict(r, stratum="B") for r in rnd.sample(b, min(per_b, len(b)))]
    rnd.shuffle(sample)
    stats["в листе"] = len(sample)
    return sample, dict(stats)


def write_csv(path: Path, help_lines: list[str], header: list[str], rows: list[list]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        for line in help_lines:
            w.writerow([line])
        w.writerow([])
        w.writerow(header)
        w.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", required=True, type=Path, help="копия боевой базы (вне репозитория)")
    ap.add_argument("--out-dir", required=True, type=Path, help="каталог листов (вне репозитория)")
    ap.add_argument("--k18-a", type=int, default=40, help="вопросов слоя А в листе K18")
    ap.add_argument("--k18-b", type=int, default=20, help="вопросов слоя Б (контроль)")
    ap.add_argument("--stop-df", type=float, default=0.05, help="доля вопросов, выше которой основа — служебная")
    ap.add_argument("--seed", type=int, default=719)
    args = ap.parse_args()
    for p in (args.db, args.out_dir):
        if ROOT == p.resolve() or ROOT in p.resolve().parents:
            sys.exit(f"{p}: база и листы с текстами вопросов — только вне репозитория (152-ФЗ).")
    if not args.db.exists():
        sys.exit(f"Нет файла базы: {args.db}")
    args.out_dir.mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(ROOT / "scripts"))
    from app.core.config import settings
    settings.RERANK_ENABLED = False
    from app.rag import pipeline, reranker, retriever
    from scripts.load_kb import load_records

    def _no_llm(*_a, **_k):
        raise RuntimeError("вызов DeepSeek на пути листа")
    pipeline._client = _no_llm            # ни одного платного вызова
    reranker._client = _no_llm
    retriever.EXACT_SEARCH = True

    records = load_records()
    idx = code_index(records)
    questions = load_questions(args.db)
    stamp = date.today().isoformat()

    ev81, s81 = ev81_rows(questions, idx)
    rows81, key81, n = [], [], 0
    for p in ev81:
        n += 1
        recs = sorted(p["recs"], key=lambda r: (r.get("section_roman", ""), r.get("source_anchor", "")))
        for j, r in enumerate(recs):
            rows81.append([n if j == 0 else "", p["q"] if j == 0 else "", p["code"], len(recs),
                           (r.get("source_anchor") or "").replace(SHORT, ""),
                           ", ".join(r.get("okpd2_codes") or []), r.get("product_name") or "", "", ""])
        key81.append({"n": n, "src": p["src"], "message_id": p["mid"], "code": p["code"],
                      "anchors": [r.get("source_anchor") for r in recs]})
    s81["вопросов в листе"] = n
    s81["строк в листе"] = len(rows81)
    f81 = args.out_dir / f"labeling_EV8-1_{stamp}.csv"
    write_csv(f81, EV81_HELP,
              ["№", "Вопрос", "Код в вопросе", "Позиций под кодом", "Позиция (раздел, №)",
               "Коды позиции", "Наименование позиции", "Вопрос про эту позицию? (да / нет / ?)",
               "Комментарий"], rows81)

    k18, s18 = k18_rows(k18_pool(questions), args.k18_a, args.k18_b, args.stop_df, args.seed)
    rows18, key18 = [], []
    for i, p in enumerate(k18, 1):
        cells = []
        for h in p["hits"]:
            cells += [f"{(h.source_anchor or '').replace(SHORT, '')} · {', '.join(h.okpd2_codes or [])}",
                      h.product_name or ""]
        cells += [""] * (6 - len(cells))
        rows18.append([i, p["q"], *cells, "", "", "", ""])
        key18.append({"n": i, "src": p["src"], "message_id": p["mid"], "stratum": p["stratum"],
                      "window_top3": [h.source_anchor for h in p["hits"]]})
    f18 = args.out_dir / f"labeling_K18_{stamp}.csv"
    write_csv(f18, K18_HELP,
              ["№", "Вопрос", "1: позиция · код", "1: наименование", "2: позиция · код", "2: наименование",
               "3: позиция · код", "3: наименование", "Названа словом не из приложения? (да / нет / не продукция)",
               "Верная позиция среди показанных? (1 / 2 / 3 / нет / не знаю)",
               "Верная позиция или код, если знаете", "Комментарий"], rows18)

    fkey = args.out_dir / f"labeling_key_{stamp}.json"
    fkey.write_text(json.dumps({
        "db": args.db.name, "collection": settings.QDRANT_COLLECTION, "seed": args.seed,
        "stop_df": args.stop_df, "EV8-1": {"stats": s81, "rows": key81},
        "K18": {"stats": s18, "rows": key18}}, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"Вопросов трафика (уникальных, чат + пробный режим): {len(questions)}")
    print(f"EV8-1: {s81}")
    print(f"K18:   {s18}")
    print(f"Листы: {f81.name}, {f18.name}; ключ: {fkey.name} — в {args.out_dir}")
    return 0


if __name__ == "__main__":
    from app.core.console import enable_utf8  # #107

    enable_utf8()
    raise SystemExit(main())
