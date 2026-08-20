"""Собирает лист сверки для эксперта ТПП: то, что нельзя закрыть кодом (волна 3).

ЗАЧЕМ. Протокол волны отсылал эксперта к машинным дампам (`diag_orphan_requirements.py --json`).
JSON эксперт читать не будет, а без сверки четыре класса дефектов остаются открытыми бессрочно:
восстановить их достоверно кодом невозможно — данные первоисточника при конвертации RTF→текст
повреждены или неоднозначны.

ЧТО СОБИРАЕТ (всё — из корпуса, ничего не зашито списком):
  А. R6-5 — позиции, получившие наследование требований группы после фикса ключа (D6).
     Набор воспроизводится диффом: карта строится текущим ключом и ключом ДО фикса.
     Сгруппировано ПО РОДИТЕЛЮ: решений о наследовании втрое меньше, чем позиций,
     и эксперту проверять надо именно решения.
  Б. R6-4 — позиции, оставшиеся без требований: категории ручного разбора из классификатора
     плюс отказы сборки карты (родитель не найден / у родителя самого нет требований).
  В. R29-4 — группы, где общая ячейка требований расколота между строками таблицы.
  Г. D5 — примечание 72.1 ссылается на продукцию, которой в разделе IV нет (проверяется заново).

⚠ Документ ГЕНЕРИРУЕТСЯ. Правки вносить в корпус и пересобирать, а не редактировать вывод:
скопированные данные устаревают молча (урок D6 про карту наследования).

Запуск:
  .venv/Scripts/python.exe scripts/build_expert_checklist.py
  .venv/Scripts/python.exe scripts/build_expert_checklist.py --out docs/EXPERT_CHECKLIST_WAVE3.md
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import re
import sys
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.rag.thresholds import lookup_threshold  # noqa: E402  (только после sys.path)


def _load_diag():
    """Диагностика лежит скриптом, а не пакетом — подгружаем по пути."""
    spec = importlib.util.spec_from_file_location(
        "diag_orphans", ROOT / "scripts" / "diag_orphan_requirements.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["diag_orphans"] = mod
    spec.loader.exec_module(mod)
    return mod


diag = _load_diag()


def _old_map_key(section: str | None, name: str | None) -> str:
    """Ключ ДО фикса D6: сноски снимались, хвостовая пунктуация — нет.

    Нужен только для диффа «что добавил фикс». Держать его здесь честнее, чем описывать
    результат словами: набор для эксперта обязан быть воспроизводимым.
    """
    first = (name or "").strip().split("\n")[0]
    first = diag._FOOTNOTE.sub(" ", first)
    # Обратный слэш внутри выражения f-строки — SyntaxError до Python 3.12 (PEP 701), а SETUP.md
    # обещает «3.11+ тоже работает». `tests/test_expert_checklist.py` импортирует этот файл на
    # уровне модуля, поэтому на 3.11 падала бы вся батарея на этапе сборки тестов, а CI (3.12)
    # не увидел бы этого никогда.
    key = re.sub(r"\s+", " ", first).strip().lower()
    return f"{section or '?'}|{key}"


def load_corpus():
    chunks: dict[str, list] = {}
    for f in sorted(diag.CHUNKS_DIR.glob("*.txt")):
        m = diag._CHUNK_ROMAN.match(f.name)
        if not m:
            continue
        rows = diag.parse_chunk(f.read_text(encoding="utf-8"))
        diag.assign_groups(rows)
        chunks.setdefault(m.group(1), rows)
    return diag.load_structured(), chunks


def walk_inheritance(recs, chunks, keyfn):
    """Повторяет отбор `build_inheritance_map`, но возвращает и ОТКАЗЫ с самими записями.

    Скрипт-генератор карты печатает отказы только счётчиком — эксперту нужны позиции.
    """
    from app.rag import fragments

    by_key = {keyfn(r.get("section_roman"), r.get("product_name")): r for r in recs}
    ops_by_name: dict[str, int] = {}
    for r in recs:
        k = diag._norm(r.get("product_name"))
        ops_by_name[k] = max(ops_by_name.get(k, 0), diag.n_operations(r))

    children: dict[str, dict] = {}
    refused: list[dict] = []

    for rec in [r for r in recs if diag.n_operations(r) == 0]:
        sec = rec.get("section_roman")
        rows = chunks.get(sec or "")
        if not rows:
            continue
        f = diag.classify(rec, rows, ops_by_name)
        if f.category != "INHERIT" or f.confidence != "HIGH":
            continue
        if fragments.is_fragmented(rec.get("product_name")) or fragments.is_fragmented(f.parent["name"]):
            continue
        p_rec = by_key.get(keyfn(sec, f.parent["name"]))
        if p_rec is None:
            refused.append({"rec": rec, "reason": "родитель не найден в базе",
                            "parent_name": f.parent["name"]})
            continue
        p_ops = diag.record_operations(p_rec)
        if not p_ops:
            refused.append({"rec": rec, "reason": "у родителя самого нет требований",
                            "parent_name": p_rec.get("product_name")})
            continue
        children[keyfn(sec, rec.get("product_name"))] = {
            "rec": rec, "parent": p_rec, "operations": p_ops, "signals": f.signals}
    return children, refused


def _norm_key(k: str) -> str:
    """Ключ старой сборки в виде текущей — иначе две сборки несопоставимы: менялся сам ключ."""
    sec, _, tail = k.partition("|")
    return f"{sec}|{tail.strip(';,.').strip()}"


def _parent_id(entry: dict) -> tuple:
    p = entry["parent"]
    return (p.get("section_roman"), (p.get("product_name") or "").split("\n")[0])


def fresh_after_keyfix(recs, chunks):
    """Позиции, ставшие наследниками ТОЛЬКО после фикса ключа (R6-5).

    Возвращает ещё и `flipped` — позиции, которые были наследниками и ДО фикса, но получили
    ДРУГОГО родителя. Диффа по ключу ребёнка для них недостаточно: такая позиция в список не
    попадёт, а требования у неё сменятся молча. Сегодня их ноль, и проверка нужна именно затем,
    чтобы это перестало быть догадкой при следующей перегенерации корпуса."""
    new, refused_new = walk_inheritance(recs, chunks, diag._map_key)
    old, _ = walk_inheritance(recs, chunks, _old_map_key)
    old_by_norm = {_norm_key(k): v for k, v in old.items()}
    fresh = {k: v for k, v in new.items() if k not in old_by_norm}
    flipped = {k: v for k, v in new.items()
               if k in old_by_norm and _parent_id(v) != _parent_id(old_by_norm[k])}
    return fresh, flipped, refused_new, len(new), len(old)


def _plural(n: int, one: str, few: str, many: str) -> str:
    """«44 позиции», а не «44 позиций» — документ уходит человеку, а не в лог."""
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return f"{n} {one}"
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return f"{n} {few}"
    return f"{n} {many}"


def _codes(rec) -> str:
    return ", ".join(rec.get("okpd2_codes") or []) or "—"


def _name(rec) -> str:
    return " ".join((rec.get("product_name") or "").split("\n")[0].split())


def _name_full(rec, limit: int = 110) -> str:
    """Наименование для таблицы ручного разбора.

    У повреждённых записей первая строка — обрывок ячейки («до 1 января 2019 г.:», «или»),
    и по ней позицию не опознать. В таких случаях добавляем следующую строку: эксперту нужно
    узнать позицию, а не увидеть аккуратное поле.
    """
    lines = [" ".join(x.split()) for x in (rec.get("product_name") or "").split("\n") if x.strip()]
    if not lines:
        return "—"
    head = lines[0]
    if (len(head) < 25 or head.endswith(":")) and len(lines) > 1:
        head = f"{head} {lines[1]}"
    return head[:limit] + ("…" if len(head) > limit else "")


def _points(pts) -> str:
    """«1 балл», «2 балла», «5 баллов», «0,5 балла» — документ читает эксперт, а не парсер.

    Дробные баллы в корпусе есть (все 0,5, раздел VII), и целочисленная форма их бы исказила."""
    if isinstance(pts, float) and not pts.is_integer():
        return f"{pts:g}".replace(".", ",") + " балла"
    return _plural(int(pts), "балл", "балла", "баллов")


def _ops_line(op) -> str:
    txt = " ".join(op["text"].split())
    pts = op.get("points")
    # Именно `is None`: 0 — это значение («менее 25 процентов — 0 баллов»), а не отсутствие,
    # и проверка на истинность молча съедала бы его вместе с None.
    return txt if pts is None else f"{txt} — **{_points(pts)}**"


def render(recs, chunks, fresh, refused, n_new, n_old) -> str:
    from app.rag import fragments  # тот же источник R29, что читает рантайм

    L: list[str] = []
    add = L.append

    add("# Лист сверки для эксперта ТПП — третья волна")
    add("")
    add("> **Документ генерируется** — `scripts/build_expert_checklist.py`. Правки вносятся в корпус")
    add("> и документ пересобирается; редактировать вывод бессмысленно, он перезапишется.")
    add(">")
    add("> Здесь собрано только то, **что нельзя закрыть кодом**: данные первоисточника при")
    add("> конвертации таблицы приложения в текст либо повреждены, либо неоднозначны. Автоматика")
    add("> в таких случаях сознательно отказывается угадывать — лучше не показать, чем показать")
    add("> чужое. Ваша сверка закрывает их окончательно.")
    add("")
    add("**Как отвечать.** По каждому пункту достаточно «верно» / «неверно + как должно быть».")
    add("Формулировка «как должно быть» ценнее указания на ошибку: такие ответы попадают в базу")
    add("проверенных случаев, и система перестаёт повторять ошибку.")
    add("")
    add("---")
    add("")

    # ---------- А ----------
    by_parent: dict[str, list] = {}
    for v in fresh.values():
        by_parent.setdefault(diag._map_key(v["parent"].get("section_roman"),
                                           v["parent"].get("product_name")), []).append(v)
    order = sorted(by_parent.items(), key=lambda kv: -len(kv[1]))

    add(f"## А. Наследование требований группы — {_plural(len(fresh), 'позиция', 'позиции', 'позиций')}, "
        f"{_plural(len(order), 'решение', 'решения', 'решений')}")
    add("")
    add("**Что произошло.** В приложении группа видов продукции делит одну ячейку требований")
    add("(в исходной таблице она объединена по вертикали). При конвертации объединение теряется, и")
    add("у всех позиций группы, кроме первой, ячейка требований оказывается пустой. Система")
    add("восстанавливает такие связи и показывает позиции требования её группы, прямо называя")
    add("позицию-источник.")
    add("")
    add("Недавно исправлена ошибка сопоставления наименований, из-за которой часть источников")
    add(f"«не находилась» ({n_old} → {n_new} позиций с восстановленными требованиями). Ниже — ровно те")
    add("позиции, что появились из-за этого исправления. **Проверить нужно "
        f"{_plural(len(order), 'решение', 'решения', 'решений')}, а не "
        f"{_plural(len(fresh), 'позицию', 'позиции', 'позиций')}:** внутри каждого блока источник один.")
    add("")
    add("**Вопрос по каждому блоку:** действительно ли перечисленные позиции подчиняются требованиям")
    add("позиции-источника — то есть в приложении они входят в одну объединённую ячейку?")
    add("")

    for i, (_, items) in enumerate(order, 1):
        p = items[0]["parent"]
        ops = items[0]["operations"]
        sec = p.get("section_roman")
        add(f"### А{i}. Раздел {sec} · источник «{_name(p)}» ({_codes(p)}) · "
            f"{_plural(len(items), 'позиция', 'позиции', 'позиций')}")
        add("")
        # Порог считаем ТЕМ ЖЕ правилом, что рантайм (`pipeline.format_context`): в записи его
        # может не быть, но `thresholds.lookup_threshold` достаёт порог из примечаний раздела —
        # и именно его видит пользователь. Читать только `min_threshold` значило спросить эксперта
        # про пробел, которого в продукте нет: у всех 26 позиций блока XXI порог есть
        # («не менее 300 баллов [прим. 7]»), а лист требовал разобраться с ним в первую очередь.
        own_thr = p.get("min_threshold")
        from_notes = None if own_thr else lookup_threshold(
            p.get("okpd2_codes") or [], _name(p), p.get("section_roman"))
        thr = own_thr or from_notes
        # ⚠ Порог бывает ДВУСТРОЧНЫМ: с 20.08.2026 `lookup_threshold` выносит оговорку прим. 17
        # («⚠ ИНОЙ порог — за исключением судов…») отдельной строкой. Приписка про источник
        # обязана остаться при ОБЩЕМ графике, а не уехать к оговорке, и markdown-строка «**Порог
        # для источника:**» не должна разорваться посреди значения — иначе документ для эксперта
        # начнёт считать величину иначе, чем рантайм.
        head, sep, tail = (thr or "").partition("\n")
        shown = f"{head} — из примечаний раздела; в самой позиции не указан" if from_notes else head
        add(f"**Порог для источника:** {shown if thr else '_не указан_'}")
        for extra in (tail.splitlines() if sep else []):
            if extra.strip():
                add(f"    {extra.strip()}")
        add("")
        add(f"**Что унаследовано ({len(ops)}):**")
        add("")
        # Вводная фраза блока (`_parent`) — ЧАСТЬ требования: без неё операции висят без условия,
        # под которым выполняются («осуществление НА ТЕРРИТОРИИ РФ следующих операций»). Рантайм её
        # печатает; лист сверки обязан показывать ровно то, что видит пользователь, иначе эксперт
        # подтвердит перечень, означающий не то, что ему показали.
        cur_intro = None
        for op in ops:
            intro = op.get("_parent")
            if intro and intro != cur_intro:
                add(f"- **{intro}:**")
            cur_intro = intro
            add(("  - " if intro else "- ") + _ops_line(op))
        add("")
        pts = [op.get("points") for op in ops if op.get("points")]
        if pts and not thr:
            add(f"> ⚠ **Отдельный вопрос по этому блоку.** У требований есть баллы ({', '.join(str(x) for x in pts)}),")
            add("> но порога нет ни в самой позиции-источнике, ни в примечаниях раздела. Если он для этой группы")
            add("> установлен во вводной части раздела или в примечании — подскажите, где именно:")
            add("> сейчас пользователь видит баллы без минимума, который нужно набрать.")
            add("")
        add(f"**Позиции, получившие эти требования ({len(items)}):**")
        add("")
        add("| Код ОКПД2 | Наименование |")
        add("|---|---|")
        for v in sorted(items, key=lambda v: (v["rec"].get("okpd2_codes") or [""])[0]):
            add(f"| `{_codes(v['rec'])}` | {_name(v['rec'])} |")
        add("")

    add("---")
    add("")

    # ---------- Б ----------
    orphan_recs = [r for r in recs if diag.n_operations(r) == 0]
    manual = []
    for rec in orphan_recs:
        rows = chunks.get(rec.get("section_roman") or "")
        if not rows:
            continue
        f = diag.classify(rec, rows, {})
        if f.category in ("NO_MATCH", "PARSER_LOSS", "EXCLUDED", "NO_PARENT") or \
                (f.category == "INHERIT" and f.confidence != "HIGH"):
            manual.append((rec, f.category, f.detail))

    ASK = {
        "NO_MATCH": "Строку не удалось сопоставить с первоисточником. Какая позиция приложения имеется в виду и какие у неё требования?",
        "PARSER_LOSS": "В первоисточнике требования есть, разбор их не увидел. Какие именно требования у этой позиции?",
        "EXCLUDED": "Позиция похожа на исключённую из приложения. Подтвердите, что требований у неё нет.",
        "NO_PARENT": "Ячейка требований пуста, и группы выше нет. Подтвердите, что требований действительно нет.",
        "родитель не найден в базе": "Требования, по-видимому, наследуются от позиции выше, но её не удалось найти. От какой позиции берутся требования?",
        "у родителя самого нет требований": "Позиция входит в группу, но и у первой позиции группы требований нет. Где искать требования этой группы?",
        # INHERIT попадает сюда только с уверенностью ниже HIGH — тогда наследование не применяется.
        # Сегодня таких нет, но без этой строки в графу «Что уточнить» уехал бы голый токен
        # «INHERIT»: `ASK.get(cat, cat)` подставляет ключ, и эксперт получил бы служебное слово.
        "INHERIT": "Похоже, требования наследуются от позиции выше по группе, но уверенности для автоматического переноса недостаточно. От какой позиции берутся требования?",
    }

    total_b = len(manual) + len(refused)
    add(f"## Б. Позиции без требований — {_plural(total_b, 'позиция', 'позиции', 'позиций')}")
    add("")
    add("**Что произошло.** Эти позиции система восстановить не смогла и честно отвечает")
    add("«требований не найдено». Замер на реальных вопросах июля: в 3.7 % ответов поиск приводит")
    add("именно к такой позиции.")
    add("")
    add("**Вопрос:** какие требования установлены приложением для каждой из них?")
    add("")
    add("| № | Разд. | Код ОКПД2 | Наименование (как в базе) | Что уточнить |")
    add("|---:|---|---|---|---|")
    n = 0
    def _also_in_v(rec) -> str:
        """Одна позиция может быть и без требований, и внутри расколотой группы. Не сказать об
        этом — значит получить два ответа под двумя разными теориями, уходящих в разные задачи."""
        return (" ⚠ Эта же позиция — в блоке В: её группа делит расколотую ячейку."
                if fragments.is_fragmented(rec.get("product_name")) else "")

    for rec, cat, _detail in manual:
        n += 1
        add(f"| {n} | {rec.get('section_roman')} | `{_codes(rec)}` | {_name_full(rec)} | "
            f"{ASK.get(cat, cat)}{_also_in_v(rec)} |")
    for r in refused:
        n += 1
        rec = r["rec"]
        add(f"| {n} | {rec.get('section_roman')} | `{_codes(rec)}` | {_name_full(rec)} | "
            f"{ASK.get(r['reason'], r['reason'])} Предполагаемый источник — «{' '.join((r['parent_name'] or '').split())[:60]}» |")
    add("")
    add("> Часть наименований в этой таблице выглядит повреждённой (начинается с «до 1 января")
    add("> 2019 г.: наличие у юридического лица…»). Это обрывки объединённой ячейки, попавшие в поле")
    add("> наименования, — и именно поэтому здесь нужен человек, а не алгоритм.")
    add("")
    add("---")
    add("")

    # Позиции блока Б по коду — чтобы блок В отметил те из них, что попали в оба списка. Без
    # перекрёстной ссылки эксперт отвечает на одну позицию дважды под двумя разными теориями
    # («разбор не увидел требований» и «перечень раскололся»), а ответы уходят в разные задачи.
    block_b_codes = {(rec.get("section_roman"), c)
                     for rec, _cat, _d in manual for c in (rec.get("okpd2_codes") or [])}
    block_b_codes |= {(r["rec"].get("section_roman"), c)
                      for r in refused for c in (r["rec"].get("okpd2_codes") or [])}

    # ---------- В ----------
    # Читаем ТОТ ЖЕ артефакт, что и рантайм (`app/rag/fragments`), а не пересчитываем по чанкам:
    # иначе документ описывал бы поведение, которого нет. Пересчёт всё равно делаем — но только
    # чтобы РАСХОЖДЕНИЕ стало громким: артефакт мог отстать от корпуса, как отстала карта
    # наследования в D6.
    frag_path = ROOT / "knowledge_base/pp719/fragmented_requirements.json"
    groups_json = json.loads(frag_path.read_text(encoding="utf-8")) if frag_path.exists() else []
    live = {(sec, " ".join(r.name.split()))
            for sec, rows in chunks.items() for run in diag.find_fragmented(rows) for r in run}
    stored = {(g["section"], " ".join((p["product_name"] or "").split()))
              for g in groups_json for p in g["positions"]}
    frag_drift = (live - stored, stored - live)

    n_pos = sum(len(g["positions"]) for g in groups_json)

    add(f"## В. Расколотая общая ячейка — {_plural(len(groups_json), 'группа', 'группы', 'групп')}, "
        f"{_plural(n_pos, 'позиция', 'позиции', 'позиций')}")
    add("")
    add("**Что произошло.** У этих групп общая ячейка требований не просто потерялась, а")
    add("**раскололась**: куски одного перечня разъехались по строкам соседних позиций. Получается")
    add("самый опасный вид ошибки — требование взято из приложения дословно, но приписано не той")
    add("продукции. Ни автоматическая проверка, ни беглое чтение такого не ловят.")
    add("")
    add("Поэтому там, где обрывок перечня всё же достался позиции, система показывает его с пометкой")
    add("«список операций неполный» и не делает вывода о наборе баллов. Позиции, которым не досталось")
    add("ничего, отвечают «требований не найдено» — они помечены ниже и продублированы в блоке Б.")
    add("Восстановить точно можно только сверкой.")
    add("")
    add("**Вопрос:** какие требования относятся к каждой позиции группы?")
    add("")
    for i, g in enumerate(sorted(groups_json, key=lambda g: g["section"]), 1):
        add(f"### В{i}. Раздел {g['section']}")
        add("")
        add("| Код ОКПД2 | Наименование | Что сейчас лежит в строке |")
        add("|---|---|---|")
        for p in g["positions"]:
            # Прочерк вместо курсива: внутри обратных кавычек markdown подчёркивания не съедает,
            # и «_(код в объединённой ячейке)_» читался бы как испорченное значение кода.
            code = f"`{', '.join(p['okpd2_codes'])}`" if p.get("okpd2_codes") else "— _код объединён_"
            nm = " ".join((p.get("product_name") or "").split())[:70]
            req = " ".join((p.get("req_preview") or "").split())[:110]
            if not req:
                req = "_пусто_"
            if {(g["section"], c) for c in (p.get("okpd2_codes") or [])} & block_b_codes:
                nm += " ⚠"
                req += " · **см. блок Б**: своих требований у позиции нет вовсе"
            add(f"| {code} | {nm} | {req} |")
        add("")
    if any(frag_drift):
        # Молчать нельзя: документ обещает, что собран из корпуса, а часть его — из артефакта.
        add("> ⚠ **Список групп и корпус разошлись** — `fragmented_requirements.json` отстал от")
        add("> чанков приложения. Пересоберите его (`diag_orphan_requirements.py --fragments`) и")
        add("> перегенерируйте этот документ: сейчас рантайм помечает не те позиции, что здесь.")
        add("")

    add("---")
    add("")

    # ---------- Г ----------
    hits = []
    for f in sorted(diag.CHUNKS_DIR.glob("*.txt")):
        txt = f.read_text(encoding="utf-8")
        c = len(re.findall("одномодов", txt, flags=re.I))
        if c:
            hits.append((f.name, c))

    add("## Г. Примечание 72.1 — позиция, которой нет в разделе")
    add("")
    add("**Что произошло.** Действующая редакция ввела примечание **72.1** с порогами для продукции")
    add("«Волокна оптические **одномодовые телекоммуникационные**»: с 01.08.2026 — не менее 50 баллов,")
    add("с 01.01.2032 — не менее 100. Самой такой позиции в разделе IV нет.")
    add("")
    if hits:
        add("Слово «одномодов» встречается в приложении только здесь:")
        add("")
        for fn, c in hits:
            add(f"- `{fn}` — {c} упом.")
    else:
        add("Слово «одномодов» в приложении сейчас не встречается вовсе.")
    add("")
    add("Соседние примечания 72.2 и 72.3 привязались нормально — их позиции в разделе есть.")
    add("")
    add("**Вопрос:** позиция должна быть в разделе IV (и потерялась при переносе), или примечание")
    add("введено «на вырост» под продукцию, которую в приложение ещё не включили?")
    add("")

    add("---")
    add("")
    add("_Собрано автоматически из корпуса приложения к ПП РФ №719._")
    return "\n".join(L) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="Лист сверки для эксперта ТПП (волна 3)")
    ap.add_argument("--out", default="docs/EXPERT_CHECKLIST_WAVE3.md")
    args = ap.parse_args()

    recs, chunks = load_corpus()
    buf = io.StringIO()
    with redirect_stdout(buf):  # классификатор разговорчив, нам нужен только результат
        fresh, flipped, refused, n_new, n_old = fresh_after_keyfix(recs, chunks)
    text = render(recs, chunks, fresh, refused, n_new, n_old)

    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")

    per_sec = Counter(v["rec"].get("section_roman") for v in fresh.values())
    print(f"[OK] {out}")
    print(f"  А. наследование после фикса ключа: {len(fresh)} позиций "
          f"({dict(per_sec.most_common())}), решений: "
          f"{len({diag._map_key(v['parent'].get('section_roman'), v['parent'].get('product_name')) for v in fresh.values()})}")
    print(f"  Б. без требований: {len(refused)} отказов сборки карты + категории ручного разбора")
    print(f"  наследников всего: старым ключом {n_old}, текущим {n_new}")
    if flipped:
        # Молчать здесь нельзя: у этих позиций требования сменились, а в лист сверки они не попали.
        print(f"  ⚠ СМЕНИЛИ ИСТОЧНИК, но в блок А не попадают ({len(flipped)}) — добавить вручную:")
        for v in flipped.values():
            print(f"      [{v['rec'].get('section_roman')}] {_name(v['rec'])[:60]} → «{_name(v['parent'])[:50]}»")


if __name__ == "__main__":
    main()
