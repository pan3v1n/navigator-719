"""Свип МАРШРУТА вопроса: товарная ветка или процедурная (`K15-1` #120). Офлайн, без Qdrant и LLM.

ЗАЧЕМ. `procedural.is_procedural` — развилка, после которой вопрос уже не встретит ни требований
приложения, ни корпуса Правил, смотря куда ушёл. Правка её маркеров бьёт по КАЖДОМУ вопросу, а не
по тем, ради которых писалась, и до сих пор мерить это было нечем: маршрут проверялся глазами на
двух-трёх примерах.

⚠⚠ ПОВОД. `K15-1` #120: «нужно ли заключение ТПП для внесения ПРОДУКЦИИ в реестр» не проходило
маркер — одно слово между «внесение» и «в реестр». У глагола «внести» разрыв до 4 слов был
разрешён, у отглагольного существительного — ноль; в `topics.py` тот же случай уже был починен
разрывом до трёх слов, и урок не переехал через модуль. Правка узкая, а радиус у неё — весь поток
вопросов, поэтому нужен свип, а не рассуждение.

ЧТО СЧИТАЕМ. Для каждого вопроса всех приёмочных наборов — маршрут и, если он процедурный, чем
именно вызван (STRONG-маркер, GENERIC+якорь, сноска). Плюс сводка по классам набора: у товарных
кейсов процедурный маршрут — это ЛОЖНОЕ срабатывание, у процедурных — норма.

⚠ Свип НЕ говорит, какой маршрут верен: он говорит, что изменилось. Верность решает разметка
наборов и глаз на списке расхождений — поэтому скрипт печатает поимённо, а не только числа.

ЗАПУСК:
    .venv/Scripts/python.exe scripts/eval_routing.py
    ... --report docs/eval_runs/<дата>_routing.md
    ... --json  → машинно-читаемо, для diff между «до» и «после» правки
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.console import enable_utf8  # noqa: E402

enable_utf8()  # #107: скрипт печатает значки вне cp1251

from app.rag import procedural, topics  # noqa: E402

# Наборы вопросов и КЛАСС каждого: «товарный» (процедурный маршрут = ложное срабатывание),
# «процедурный» (норма), «вне сферы» (маршрут не важен, важно что не падаем).
# ⚠ Берём ВСЕ наборы репозитория: правка маршрута бьёт по всему потоку, а не по своей теме.
SETS: tuple[tuple[str, str, str], ...] = (
    ("eval_golden.json", "cases", "товарный"),          # 50: товарные + 4 документных
    ("eval_golden_rules.json", "cases", "процедурный"),  # 25: процедурная ось K9
    ("eval_topics.json", "cases", "по разметке"),        # 57: размечены руками (expect=None → товарный)
    ("eval_golden_negative.json", "cases", "вне сферы"),  # 36
    ("eval_golden_borderline.json", "cases", "товарный"),  # 20
    # ⚠⚠ ВТОРОЙ КЛЮЧ (`K14` #35, 31.08.2026). До этого набора свип по ТН ВЭД не мерил НИЧЕГО:
    # во всех пяти наборах выше — ноль вопросов по второму ключу, и «чисто» означало «не считаю».
    # Ровно тот дефект инструмента, который прятал находку: пять вопросов с темой `st1_origin`
    # проходили по случайным якорям (ГИСП, Минпромторг, ТПП), а голое «как получить СТ-1» — нет.
    ("eval_st1_route.json", "cases", "процедурный"),      # 21: 19 СТ-1/ТН ВЭД + 2 контроля
)


# ⚠⚠ ОЖИДАЕМЫЙ МАРШРУТ ДОКУМЕНТНЫХ КЕЙСОВ — ПОИМЁННО, А НЕ «КЛАССОМ».
# Первая редакция свипа считала их отдельным классом «маршрут не важен» — и СПРЯТАЛА регрессию:
# кандидатная правка уводила смешанный вопрос («какие документы нужны для этикетировщиков») на
# процедурную ветку, где он потерял бы требования позиции. Ради него и заводилась `K12`: он обязан
# остаться ТОВАРНЫМ и получить блок документов ДОПОЛНИТЕЛЬНО. Чистый документный вопрос — наоборот.
EXPECTED_DOCUMENTS_ROUTE: dict[int, bool] = {
    47: True,    # чистый документный — процедурная ветка
    48: False,   # СМЕШАННЫЙ товарный+документный — обязан остаться товарным (K12)
    49: False,   # поднимает несуществующий документ, но спрашивает и про продукцию
    50: True,    # чистый процедурный (K15-1 #120)
}


def _class_of(case: dict, default: str) -> str:
    if default != "по разметке":
        # Документные кейсы `EV21` — со своим ожидаемым маршрутом, см. EXPECTED_DOCUMENTS_ROUTE.
        if case.get("kind") == "documents":
            return "документный"
        # ⚠ Товарные КОНТРОЛИ внутри процедурного набора (`K14`). Без своего класса они считались
        # бы пропусками — то есть правка, перехватившая товарный вопрос, выглядела бы улучшением.
        # Контроль обязан лежать в классе, где его срабатывание считается ЛОЖНЫМ.
        if case.get("kind") == "control_product":
            return "товарный"
        return default
    return "товарный" if case.get("expect") is None else "процедурный"


def collect() -> list[dict]:
    rows: list[dict] = []
    for name, key, default in SETS:
        data = json.loads((ROOT / "scripts" / name).read_text(encoding="utf-8"))
        for c in data[key]:
            q = c["query"]
            proc = procedural.is_procedural(q, has_code=False)
            rows.append({
                "set": name.replace("eval_", "").replace(".json", ""),
                "id": c["id"],
                "class": _class_of(c, default),
                "query": q,
                "procedural": proc,
                "why": _why(q) if proc else "",
                "topic": topics.classify(q),
            })
    return rows


def _why(q: str) -> str:
    """Чем вызван процедурный маршрут — чтобы расхождение можно было объяснить, а не только увидеть.

    ⚠⚠ ПОРЯДОК ПОВТОРЯЕТ `is_procedural`, И ЭТО НЕ ПЕДАНТИЗМ. Первая редакция (а) не знала про
    маршрут по ТЕМЕ, добавленный той же правкой, и печатала «?» для 5 строк из 61; (б) сверяла
    сноску без условия «и кода в вопросе нет», которое стоит в рантайме; (в) при GENERIC-глаголе
    БЕЗ якоря — когда маршрут дала тема — писала «GENERIC:…», то есть неверную причину. Врущее
    объяснение хуже отсутствующего: инструмент затем и нужен, чтобы объяснять, ПОЧЕМУ маршрут
    изменился (ревью пакета 26.08.2026)."""
    from app.rag.okpd2_ref import has_okpd2_code

    if procedural._FOOTNOTE_REF_RE.search(q) and not has_okpd2_code(q):
        return "сноска"
    m = procedural._STRONG_RE.search(q)
    if m:
        return f"STRONG:{' '.join(m.group(0).split())[:40]}"
    m = procedural._GENERIC_RE.search(q)
    if m and procedural._ANCHOR_RE.search(q):
        return f"GENERIC:{' '.join(m.group(0).split())[:40]}"
    if procedural._has_routing_topic(q):
        return f"ТЕМА:{topics.classify(q)}"
    return "?"


def summarize(rows: list[dict]) -> list[str]:
    classes = ("товарный", "документный", "процедурный", "вне сферы")
    L = ["=" * 78,
         f"МАРШРУТ ВОПРОСА — {len(rows)} вопросов из {len(SETS)} наборов",
         "=" * 78, ""]
    for cls in classes:
        sub = [r for r in rows if r["class"] == cls]
        if not sub:
            continue
        proc = sum(1 for r in sub if r["procedural"])
        note = ""
        if cls == "товарный":
            note = "   ← процедурный маршрут здесь = ЛОЖНОЕ срабатывание"
        elif cls == "процедурный":
            note = "   ← товарный маршрут здесь = ПРОПУСК"
        L.append(f"  {cls:<12} {len(sub):>3} вопросов · процедурных {proc:>3}{note}")
    L.append("")

    false_pos = [r for r in rows if r["class"] == "товарный" and r["procedural"]]
    L.append(f"ЛОЖНЫЕ СРАБАТЫВАНИЯ (товарный вопрос уходит процедурной веткой): {len(false_pos)}")
    for r in false_pos:
        L.append(f"  {r['set']}#{r['id']:<4} [{r['why']}] {r['query'][:52]}")
    L.append("")

    # ⚠⚠ ВНЕ-719 НА ПРОЦЕДУРНОЙ ВЕТКЕ — ТОЖЕ ПРОВАЛ, и первая редакция свипа этого не считала.
    # Шапка `procedural.py` прямо называет «порядок получения загранпаспорта» примером того, чего
    # гейт делать не должен: такой вопрос обязан получить обычный отказ «вне сферы 719», а не
    # процедурный ответ. Кандидатная правка `P3` уводила его на процедурную ветку, и свип молчал.
    oos = [r for r in rows if r["class"] == "вне сферы" and r["procedural"]]
    L.append(f"ВНЕ СФЕРЫ 719 НА ПРОЦЕДУРНОЙ ВЕТКЕ (обязано быть 0): {len(oos)}")
    for r in oos:
        L.append(f"  {r['set']}#{r['id']:<4} [{r['why']}] {r['query'][:52]}")
    L.append("")

    wrong_docs = [r for r in rows if r["class"] == "документный"
                  and r["procedural"] != EXPECTED_DOCUMENTS_ROUTE.get(r["id"])]
    L.append(f"ДОКУМЕНТНЫЕ НЕ ПО СВОЕМУ МАРШРУТУ (обязано быть 0): {len(wrong_docs)}")
    for r in wrong_docs:
        want = "процедурной" if EXPECTED_DOCUMENTS_ROUTE.get(r["id"]) else "товарной"
        L.append(f"  {r['set']}#{r['id']:<4} ожидалась {want} ветка: {r['query'][:46]}")
    L.append("")

    missed = [r for r in rows if r["class"] == "процедурный" and not r["procedural"]]
    L.append(f"ПРОПУСКИ (процедурный вопрос уходит товарной веткой): {len(missed)}")
    for r in missed:
        L.append(f"  {r['set']}#{r['id']:<4} тема={r['topic']}  {r['query'][:52]}")
    L.append("")

    # ⚠ Вопрос получил ТЕМУ, но не получил ВЕТКУ, на которой тема применяется — это и есть #120.
    # Тема без своей ветки не делает ничего: фрагмент промпта и квота окна живут на процедурном
    # пути, а `docs_ctx` товарного пути строится только для темы `documents`.
    orphan = [r for r in rows if r["topic"] and not r["procedural"] and r["topic"] != topics.DOCUMENTS]
    L.append(f"ТЕМА ЕСТЬ, ВЕТКИ НЕТ (тема не documents, маршрут товарный): {len(orphan)}")
    for r in orphan:
        L.append(f"  {r['set']}#{r['id']:<4} тема={r['topic']:<15} {r['query'][:48]}")
    return L


def main() -> int:
    ap = argparse.ArgumentParser(description="Свип маршрута вопроса (K15-1 #120), офлайн")
    ap.add_argument("--report", type=Path)
    ap.add_argument("--json", action="store_true", help="печатать строки JSON — для diff до/после")
    args = ap.parse_args()

    rows = collect()
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1, sort_keys=True))
        return 0
    out = "\n".join(summarize(rows))
    print(out)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text("# Свип маршрута вопроса (K15-1 #120)\n\n```\n" + out + "\n```\n",
                               encoding="utf-8")
        print(f"\nОтчёт сохранён: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
