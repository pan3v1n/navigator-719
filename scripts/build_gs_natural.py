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

Запуск:
  .venv/Scripts/python.exe scripts/build_gs_natural.py --db D:/navigator-backups/navigator_app.db \\
      --wave 3 --out scripts/eval_gs_natural.json
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

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


def collect(db_path: Path, min_rating: int, wave: str) -> tuple[list[dict], dict]:
    with sqlite3.connect(db_path) as db:   # закрываем: на Windows открытый файл не даёт его убрать
        db.row_factory = sqlite3.Row
        msgs = list(db.execute("select id, session_id, ts, role, content, sources_json, "
                               "low_relevance from messages order by session_id, ts, id"))
        rated = {r["message_id"]: r["rating"] for r in db.execute(
            "select message_id, rating from feedback "
            "where message_id is not null and rating is not null")}
    db.close()

    stats = {"оценённых ответов": 0, "есть вопрос": 0, "товарных": 0,
             "с якорем позиции": 0, "отброшено: guard поднимал флаг": 0,
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
            sources = json.loads(m["sources_json"] or "[]")
        except Exception:  # noqa: BLE001
            sources = []
        # Якорь и код берём из ОДНОГО источника: раньше anchor искался по первому с якорем, а
        # okpd2 читался из sources[0] — две половины эталона могли описывать разные позиции.
        src = next((s for s in sources if s.get("source_anchor")), None)
        anchor = src.get("source_anchor") if src else None
        if not anchor:
            continue
        stats["с якорем позиции"] += 1
        if detect(query):          # ПДн → кейс не берём вовсе (частичное обезличивание — иллюзия)
            stats["отброшено по ПДн"] += 1
            continue
        seen.add(key)
        cases.append({
            "id": f"nat_{len(cases) + 1:03d}",
            "query": query,
            "expect": {"anchor": anchor, "okpd2": src.get("okpd2") or []},
            "source": f"волна {wave}, оценка {rated[m['id']]}★",
        })
    stats["итого кейсов"] = len(cases)
    return cases, stats


def main() -> None:
    ap = argparse.ArgumentParser(description="EV3: собрать gs_natural из оценённых ответов волны")
    ap.add_argument("--db", required=True, help="боевая БД (вне репозитория!)")
    ap.add_argument("--wave", default="3")
    ap.add_argument("--min-rating", type=int, default=4)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        sys.exit(f"Нет файла базы: {db_path}")
    if ROOT in db_path.resolve().parents:
        sys.exit("База с ПДн лежит ВНУТРИ репозитория — вынеси её наружу (CLAUDE.md, 152-ФЗ).")

    cases, stats = collect(db_path, args.min_rating, args.wave)
    print("=" * 70)
    print(f"gs_natural из {db_path.name} (оценка ≥{args.min_rating}★)")
    print("=" * 70)
    for k, v in stats.items():
        print(f"  {k:<22} {v}")
    print(f"\n  Цель гайда — 120+ кейсов. Сейчас: {len(cases)}."
          f" {'Готово.' if len(cases) >= 120 else 'Набор пополняется после каждой волны тем же прогоном.'}")
    if not args.out:
        for c in cases[:10]:
            print(f"    {c['id']}: {c['query'][:56]:56} → {c['expect']['anchor'][:34]}")
        return
    out = Path(args.out) if Path(args.out).is_absolute() else ROOT / args.out
    payload = {"_meta": {
        "purpose": "gs_natural (EV3): вопросы так, как их задают люди, с верной позицией. "
                   "Эталон — не ручная разметка, а оценка эксперта ≥4★ в волне тестирования.",
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
    main()
