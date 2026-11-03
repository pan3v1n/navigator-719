"""`gs_natural` — набор «вопрос → верная позиция» из ДАННЫХ ВОЛНЫ, без ручной разметки (EV3, #80).

ЗАЧЕМ. Главная цифра ретрива по гайду не измеряется: набора нет. `eval_coverage` спрашивает
НАИМЕНОВАНИЕМ позиции («Средства транспортные … категории M1, M1G») — это утечка эталона, верхняя
граница 0.940, а не рабочее число. Живой человек пишет «делаем прицепы для легковых» — на таких
формулировках лучшая доступная оценка ретрива exact r@1 = 0.808 (перефраз).

ПОЧЕМУ БЕЗ РАЗМЕТКИ. Эталон уже размечен экспертами волны, просто в другом месте: если эксперт
поставил ответу 4–5★, значит позиция, на которой ответ построен, признана верной. Пара
«вопрос + `source_anchor` этого ответа» — готовый кейс. Размечать руками 120 вопросов не нужно.

⚠ У этой посылки есть ровно одно исключение, и без его отсева набор врал бы: **ответы, на которых
поднимался флаг релевантности**. Там правило 1г запрещает называть баллы и требует список
кандидатов, и высокая оценка означает «спасибо, что честно сказал „точного совпадения не нашёл“»,
а не «позиция верна». Такие ответы отбрасываются по `messages.low_relevance`.

⚠ 152-ФЗ. На вход идёт боевая база с ПДн — она обязана лежать ВНЕ рабочего дерева репозитория.
Наружу выходят только тексты вопросов, и каждый прогоняется детектором `app/core/sensitive.py`;
при срабатывании кейс ОТБРАСЫВАЕТСЯ, а не обезличивается частично. Ни идентификаторов
пользователей, ни регионов, ни времени в набор не попадает.

⚠ Что НЕ берём и почему:
  * продолжения диалога («да», «а порог?») — в бою они переписываются контекстуализатором, и без
    истории такой кейс мерил бы не то, что происходит;
  * процедурные и meta-вопросы — у них нет позиции приложения, значит нет и эталона;
  * ответы без `source_anchor` — не на что ссылаться.

⚠ 03.11.2026 (гейт `1.0`, решение владельца 28.10): волны больше не будет — источник набора
оценки пользователей боя. Отсюда правила, которых не было в июле:
  * оценки — по правилу «Качества» админки: последняя оценка пользователя на ответ (#166,
    `admin_stats._superseded_ratings`), снятая (последняя = 0) не считается;
  * якорь — только у ПОЗИЦИИ приложения (источник с кодом ОКПД2); с `UI-1` в `sources_json`
    товарного ответа лежат и пункты Приказа №52 (`kind = rule`), они эталоном позиции не бывают;
  * якорь сверяется с ТЕКУЩИМ корпусом по наименованию: номера позиций у редакций не вечны
    (XVIII изложен заново N 1254, VII — N 1297), и июльский якорь может указывать на чужую позицию
    (класс `EV24` #244). Наименование разошлось — ищем позицию с тем же наименованием; одна —
    перепривязываем, нет или несколько — кейс отбрасываем со счётчиком;
  * набор пишется ТОЛЬКО ВНЕ репозитория: репозиторий публичный, а тексты вопросов — данные
    пользователей, даже прошедшие детектор (`CLAUDE.md`, 152-ФЗ). На экран тексты не печатаются.

Запуск (и база, и набор — вне репозитория):
  .venv/Scripts/python.exe scripts/build_gs_natural.py --db D:/navigator-backups/navigator_app-<дата>.db \\
      --out D:/navigator-export/gs_natural.json
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.sensitive import detect  # noqa: E402
from app.rag import meta, procedural, translate  # noqa: E402

# ⚠ `\b` в конце обязателен, и без него набор молча терял кейсы. Регулярка идёт через `.match()`,
# то есть проверяет НАЧАЛО строки: без границы слова «да» ловило «датчики давления», «нет» —
# «нетканые материалы», «ок» — «оконные блоки». Это оценённые на 4–5★ товарные вопросы с якорем
# позиции, и отбрасывались они ДО счётчиков статистики — то есть потеря была невидима, а набор
# смещался против всех наименований на да/нет/ок при цели набрать 120 кейсов из 29.
CONTINUATION = re.compile(
    r"^\W*(да|нет|ага|ок|окей|хочу|покажи|поясни|распиши|подробнее|продолжай?|дальше|давай|"
    r"а|и|это|тот|так)\b", re.I)


def _norm_name(name) -> str:
    return " ".join(str(name or "").split()).casefold()


def _feedback(db) -> list:
    """Оценки ответов объектами с полями `admin_stats` (id, user_id, ts, kind, rating, message_id).

    У баз до миграций нет колонок `kind`, `user_id`, `ts` — тогда все строки считаются оценками
    ответа одного пользователя в порядке id (так они и писались)."""
    cols = {r[1] for r in db.execute("pragma table_info(feedback)")}
    pick = [c if c in cols else f"null as {c}" for c in ("id", "user_id", "ts", "kind")]
    rows = db.execute(f"select {', '.join(pick)}, rating, message_id from feedback "
                      "where message_id is not null and rating is not null order by id")
    # ts — строкой ISO (в SQLite так и лежит); пустую заменяем непустой: `_superseded_ratings`
    # подставляет `datetime.min` вместо ложного ts, и сравнение строки с датой упало бы.
    return [SimpleNamespace(id=r["id"], user_id=r["user_id"] or 0, ts=r["ts"] or "0000",
                            kind=r["kind"] or "answer", rating=r["rating"],
                            message_id=r["message_id"]) for r in rows]


def final_ratings(fb: list) -> dict[int, int]:
    """{message_id: оценка} по правилу «Качества»: последняя оценка пользователя на ответ (#166),
    снятая (последняя = 0) не считается. Если ответ оценили несколько пользователей — берётся
    наименьшая: эталоном идёт только то, что никто не оспорил."""
    from app.api.admin_stats import _superseded_ratings

    answer = [f for f in fb if (f.kind or "answer") == "answer"]
    by_user: dict = defaultdict(list)
    for f in answer:
        by_user[f.user_id].append(f)
    superseded = _superseded_ratings(by_user)
    out: dict[int, int] = {}
    for f in answer:
        if f.id in superseded or f.rating == 0:
            continue
        out[f.message_id] = min(out.get(f.message_id, f.rating), f.rating)
    return out


def position_source(sources: list) -> dict | None:
    """Первый источник-ПОЗИЦИЯ ответа: с якорем и кодом ОКПД2, не пункт норм (`kind = rule`)."""
    for s in sources:
        if (isinstance(s, dict) and s.get("source_anchor") and s.get("okpd2")
                and s.get("kind") != "rule"):
            return s
    return None


def resolve_anchor(src: dict, records: list[dict] | None) -> tuple[str | None, str]:
    """(якорь в ТЕКУЩЕМ корпусе, исход): «как был» · «перепривязан» · «устарел» · «неоднозначен».

    Без `records` (разбор старых баз, `--no-corpus-check`) якорь берётся как есть."""
    anchor = src.get("source_anchor")
    if records is None:
        return anchor, "как был"
    name = _norm_name(src.get("product_name"))
    rec = next((r for r in records if r.get("source_anchor") == anchor), None)
    if rec is not None and (not name or _norm_name(rec.get("product_name")) == name):
        return anchor, "как был"
    if not name:
        return None, "устарел"
    same = [r for r in records if _norm_name(r.get("product_name")) == name]
    if len(same) > 1:   # одно наименование у нескольких позиций — уточняем кодом ответа
        codes = set(src.get("okpd2") or [])
        same = [r for r in same if codes & set(r.get("okpd2_codes") or [])] or same
    if len(same) == 1:
        return same[0].get("source_anchor"), "перепривязан"
    return None, ("неоднозначен" if same else "устарел")


def collect(db_path: Path, min_rating: int, wave: str,
            records: list[dict] | None = None) -> tuple[list[dict], dict]:
    # Только чтение: на вход идёт копия боевой базы, менять её нельзя.
    with sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        msgs = list(db.execute("select id, session_id, ts, role, content, sources_json, "
                               "low_relevance from messages order by session_id, ts, id"))
        rated = final_ratings(_feedback(db))
    db.close()                         # закрываем: на Windows открытый файл не даёт его убрать

    stats = {"оценённых ответов": 0, "есть вопрос": 0, "товарных": 0,
             "с якорем позиции": 0, "отброшено: guard поднимал флаг": 0,
             "якорь перепривязан по наименованию": 0, "отброшено: якорь устарел": 0,
             "отброшено по ПДн": 0, "итого кейсов": 0}
    cases, seen = [], set()
    for i, m in enumerate(msgs):
        if m["role"] != "assistant" or rated.get(m["id"], 0) < min_rating:
            continue
        stats["оценённых ответов"] += 1
        # ⚠ Оценка ≥4★ доказывает, что ответ ПОЛЕЗЕН, а не что позиция верна. Если на этом ответе
        # поднимался флаг релевантности, правило 1г запрещало называть баллы и требовало список
        # кандидатов — эксперт ставит 5★ ИМЕННО за честное «точного совпадения не нашёл». Позиция
        # там неподтверждённая, и брать её `source_anchor` эталоном значит мерить recall@1 по
        # догадке ретрива. Тот же признак вручную отсеян в eval_golden_borderline.json.
        if m["low_relevance"]:
            stats["отброшено: guard поднимал флаг"] += 1
            continue
        prev = msgs[i - 1] if i else None
        if not prev or prev["role"] != "user" or prev["session_id"] != m["session_id"]:
            continue
        stats["есть вопрос"] += 1
        query = " ".join((prev["content"] or "").split())
        key = query.lower()
        if not query or key in seen or len(query) < 12 or CONTINUATION.match(query):
            continue
        if meta.is_meta(query) or translate.is_translate(query) or procedural.is_procedural(query):
            continue
        stats["товарных"] += 1
        try:
            sources = json.loads(m["sources_json"] or "[]") or []
        except Exception:  # noqa: BLE001
            sources = []
        # Якорь и код берём из ОДНОГО источника: раньше anchor искался по первому с якорем, а
        # okpd2 читался из sources[0] — две половины эталона могли описывать разные позиции.
        # Источник — только ПОЗИЦИЯ: с UI-1 первым с якорем может оказаться пункт Приказа №52.
        # Старые базы (до кода в источнике) берут первый источник с якорем, как раньше.
        src = position_source(sources) if records is not None else next(
            (s for s in sources if isinstance(s, dict) and s.get("source_anchor")
             and s.get("kind") != "rule"), None)
        if not src:
            continue
        stats["с якорем позиции"] += 1
        anchor, how = resolve_anchor(src, records)
        if anchor is None:
            stats["отброшено: якорь устарел"] += 1
            continue
        if how == "перепривязан":
            stats["якорь перепривязан по наименованию"] += 1
        if detect(query):          # ПДн → кейс не берём вовсе (частичное обезличивание — иллюзия)
            stats["отброшено по ПДн"] += 1
            continue
        seen.add(key)
        cases.append({
            "id": f"nat_{len(cases) + 1:03d}",
            "query": query,
            "expect": {"anchor": anchor, "okpd2": src.get("okpd2") or []},
            "source": f"{wave}, оценка {rated[m['id']]}★",
        })
    stats["итого кейсов"] = len(cases)
    return cases, stats


def main() -> None:
    ap = argparse.ArgumentParser(description="EV3: собрать gs_natural из оценённых ответов боя")
    ap.add_argument("--db", required=True, help="копия боевой БД (вне репозитория!)")
    ap.add_argument("--wave", default="бой", help="подпись источника кейса")
    ap.add_argument("--min-rating", type=int, default=4)
    ap.add_argument("--out", default="", help="файл набора — ТОЛЬКО вне репозитория")
    ap.add_argument("--no-corpus-check", action="store_true",
                    help="не сверять якоря с текущим корпусом (только для разбора старых баз)")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        sys.exit(f"Нет файла базы: {db_path}")
    if ROOT in db_path.resolve().parents:
        sys.exit("База с ПДн лежит ВНУТРИ репозитория — вынеси её наружу (CLAUDE.md, 152-ФЗ).")
    out = Path(args.out).resolve() if args.out else None
    if out is not None and ROOT in out.parents:
        sys.exit("Набор с текстами вопросов пишется ВНЕ репозитория: он публичный (152-ФЗ).")

    records = None
    if not args.no_corpus_check:
        from scripts.load_kb import load_records
        records = load_records()
    cases, stats = collect(db_path, args.min_rating, args.wave, records)
    print("=" * 70)
    print(f"gs_natural из {db_path.name} (оценка ≥{args.min_rating}★)")
    print("=" * 70)
    for k, v in stats.items():
        print(f"  {k:<36} {v}")
    print(f"\n  Цель гайда — 120+ кейсов. Сейчас: {len(cases)}.")
    if out is None:   # тексты вопросов на экран не печатаются: вывод уходит в журналы и беседы
        print("  (--out не задан — набор не сохранён)")
        return
    payload = {"_meta": {
        "purpose": "gs_natural (EV3): вопросы так, как их задают люди, с верной позицией. "
                   "Эталон — не ручная разметка, а последняя оценка ≥4★ пользователя боя "
                   "(правило «Качества», #166); якоря сверены с текущим корпусом.",
        "stats": stats,
        "built_by": "scripts/build_gs_natural.py",
        "pii": "Тексты прогнаны детектором app/core/sensitive.py; кейсы со срабатыванием отброшены "
               "целиком. Идентификаторов пользователей, регионов и времени в наборе нет.",
        "limits": "Отброшены ответы, на которых поднимался флаг релевантности (low_relevance): "
                  "там правило 1г требует список кандидатов, и оценка ≥4★ означает «честно сказал, "
                  "что точного совпадения нет», а не «позиция верна».",
        "size": len(cases), "min_rating": args.min_rating,
    }, "cases": cases}
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nСохранено: {out}")


if __name__ == "__main__":
    from app.core.console import enable_utf8  # #107: значки вне cp1251 роняли бы печать
    enable_utf8()
    main()
