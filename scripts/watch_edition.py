"""`A6` — слежение за редакциями: не отстал ли корпус от первоисточника.

ЗАЧЕМ. ПП №719 правится 6+ раз в год, и отставание даже на одну редакцию = формально неверный
ответ по баллам, порогам и позициям. 12.08.2026 выяснилось, что корпус отстал на ДВЕ редакции
(N 899 от 16.07 и N 923 от 22.07), причём тело постановления оказалось СИЛЬНЕЕ приложения, —
и заметили это ВРУЧНУЮ, при сверке владельца с КонсультантПлюс. Задача этого скрипта — сделать
такую проверку машинной и воспроизводимой.

ЧТО ОН ДЕЛАЕТ И ЧЕГО НЕ ДЕЛАЕТ. Решение о переиндексации принимает ЧЕЛОВЕК (`AGENTS_PLAN` §A6):
скрипт только показывает расхождения. Автоматически ничего не скачивает и не перезаписывает.

ДВЕ ПРОВЕРКИ РАЗНОЙ ПРИРОДЫ:

  A. ВНУТРЕННЯЯ СОГЛАСОВАННОСТЬ (офлайн, без сети). Сравниваются ДВА НЕЗАВИСИМЫХ СВИДЕТЕЛЯ:
     тело постановления (`01_postanovlenie.txt`, его шапка перечисляет все изменяющие акты) и
     РАЗДЕЛЫ приложения (каждый чанк несёт пометки только тех актов, что правили именно его).
     Разошлись — значит одну часть актуализировали, а другую забыли; это и есть форма дефекта
     12.08. Сюда же — проверка, что для действующей редакции есть строка в
     `KONTUR_719_BY_EDITION` (иначе ссылка на первоисточник ведёт на недействующий текст,
     дефект 16.08), и что все части ПРОЧИТАНЫ: нечитаемый файл — это не «согласовано».

     ⚠ Первая версия сравнивала тело с `pp719_full.txt` — а тот ОТКРЫВАЕТСЯ той же шапкой
     (110 актов в ней, 0 в остальном файле). Две «части» несли один перечень, проверка не могла
     провалиться, и дефект 12.08 она бы не поймала (ревью PR #94).

  B. ОТСТАВАНИЕ ОТ ПЕРВОИСТОЧНИКА (нужен текст действующей редакции). ⚠ Скачивание НЕ встроено
     в рантайм и не делается по умолчанию: канал рвётся (DPI), а стек проекта сознательно
     RF-first и без внешних зависимостей. Текст даётся файлом (`--source`), который владелец
     сохраняет из КонсультантПлюс/Контура, либо адресом (`--url`) — тогда скрипт честно скажет,
     если сеть не пустила. Сравниваются ПЕРЕЧНИ изменяющих актов: какие есть в первоисточнике и
     отсутствуют в корпусе.

КАК ЧИТАТЬ ВЫХОД. Код возврата 1 = найдено расхождение (годится для cron и CI). Ноль ложных
тревог важнее полноты: отчёт, который «всегда что-то нашёл», перестают читать (`AGENTS_PLAN` §9).

    .venv\\Scripts\\python scripts\\watch_edition.py
    .venv\\Scripts\\python scripts\\watch_edition.py --source D:\\719_deystvuyushchaya.txt
    .venv\\Scripts\\python scripts\\watch_edition.py --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.rag.edition import (  # noqa: E402
    KONTUR_719_BY_EDITION,
    UNKNOWN,
    corpus_edition,
)

# «(в ред. Постановлений Правительства РФ от 22.07.2026 N 923, …)» — та же форма, что в
# `app/rag/edition.py` и `scripts/load_rules_kb.py`.
# ⚠ И «N», и «№», и неразрывный пробел: экспорты из правовых систем пишут номер по-разному, а
# первая версия принимала только латинскую «N» с обычным пробелом — источник с «№ 923» разбирался
# в НОЛЬ актов, и скрипт печатал «корпус не отстаёт» (ревью PR #94).
_AMEND_RE = re.compile(r"от (\d{2})\.(\d{2})\.(\d{4})\s*[N№]\s*(\d+)")

# ⚠ ЧАСТИ ОБЯЗАНЫ БЫТЬ НЕЗАВИСИМЫМИ СВИДЕТЕЛЯМИ, иначе проверка не может провалиться.
#
# Первая версия сравнивала `01_postanovlenie.txt` с `pp719_full.txt` — а полный текст ОТКРЫВАЕТСЯ
# той же шапкой постановления со всеми 110 актами (замер: 110 актов в шапке, 0 в остальном файле).
# То есть обе «части» несли ОДИН перечень, «части согласованы» было истинным по построению, и
# дефект 12.08.2026 (тело актуализировано, приложение — нет) этот инструмент НЕ ПОЙМАЛ БЫ. Тест,
# утверждавший согласованность на реальном корпусе, зеленел по той же причине (ревью PR #94).
#
# Настоящий независимый свидетель приложения — ЕГО РАЗДЕЛЫ: каждый чанк несёт пометки только тех
# актов, что правили именно его (замер: `02_I` — последний акт 13.04.2026 N 400, `05_IV` —
# 22.07.2026 N 923). Поэтому сравниваем тело с МАКСИМУМОМ по разделам.
#
# Приказ ТПП №52 — ОТДЕЛЬНЫЙ документ со своей историей, к редакции 719 не привязан (проверено
# 16.08.2026), в сверку 719 не идёт.
_CHUNKS = ROOT / "knowledge_base" / "pp719" / "chunks"
BODY_FILE = _CHUNKS / "01_postanovlenie.txt"


def section_files() -> list[Path]:
    """Разделы приложения — всё, кроме тела постановления и вводной части."""
    return [f for f in sorted(_CHUNKS.glob("*.txt"))
            if f.name != BODY_FILE.name and not f.name.startswith("00_")]


PARTS: dict[str, Path] = {"тело постановления": BODY_FILE}


def acts(text: str) -> set[tuple[int, int, int, int]]:
    """Множество изменяющих актов текста: (год, месяц, день, номер) — сравнимо и сортируемо."""
    return {(int(m.group(3)), int(m.group(2)), int(m.group(1)), int(m.group(4)))
            for m in _AMEND_RE.finditer(text)}


def label(act: tuple[int, int, int, int]) -> str:
    y, mth, d, num = act
    return f"от {d:02d}.{mth:02d}.{y} N {num}"


def read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def fetch(url: str, tries: int = 3) -> str | None:
    """Скачать текст первоисточника. ⚠ Канал рвётся — говорим об этом прямо, а не молча пустеем."""
    import urllib.error
    import urllib.request
    for i in range(1, tries + 1):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:  # noqa: S310 — адрес задаёт человек
                return r.read().decode("utf-8", errors="ignore")
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            print(f"  попытка {i}/{tries} не прошла: {type(e).__name__}: {e}")
    return None


def check_corpus() -> dict:
    """Проверка A: согласованы ли части корпуса между собой и с таблицей ссылок."""
    per_part: dict[str, dict] = {}
    for name, path in PARTS.items():
        text = read(path)
        if text is None:
            per_part[name] = {"файл": str(path), "ошибка": "не читается"}
            continue
        a = acts(text)
        per_part[name] = {"файл": path.name, "актов": len(a),
                          "последний": label(max(a)) if a else None,
                          "_acts": a}
    # Разделы приложения — независимый свидетель: у каждого свои пометки актов.
    sec_acts: set = set()
    sec_last: dict[str, str] = {}
    unreadable = [n for n, v in per_part.items() if "ошибка" in v]
    for f in section_files():
        text = read(f)
        if text is None:
            unreadable.append(f.name)
            continue
        a = acts(text)
        if a:
            sec_acts |= a
            sec_last[f.name] = label(max(a))
    per_part["разделы приложения"] = {
        "файл": f"chunks/*.txt ({len(sec_last)} разделов с пометками)",
        "актов": len(sec_acts),
        "последний": label(max(sec_acts)) if sec_acts else None,
        "_acts": sec_acts,
    }
    lasts = {n: v.get("последний") for n, v in per_part.items() if v.get("последний")}
    # ⚠ Нечитаемая часть — это НЕ «согласовано»: сравнение просто не состоялось. Первая версия
    # выбрасывала такую часть из сравнения и печатала «части согласованы: да» с кодом 0, хотя
    # единственная проверка, ради которой существует, не выполнялась (ревью PR #94). Файлы данных,
    # не доехавшие до архива выкатки, — живой риск из чек-листа REVIEW_TZ §1а.
    consistent = len(set(lasts.values())) <= 1 and not unreadable
    ed = corpus_edition()
    newest = max(sec_acts) if sec_acts else None
    return {
        "части": per_part,
        "редакция корпуса": ed,
        "части согласованы": consistent,
        "нечитаемые части": unreadable,
        "разделов с последним актом": sum(1 for v in sec_last.values() if newest and v == label(newest)),
        "ссылка на первоисточник известна": ed in KONTUR_719_BY_EDITION,
    }


def check_source(text: str, corpus_acts: set) -> dict:
    """Проверка B: чего из первоисточника нет в корпусе (и наоборот)."""
    src = acts(text)
    missing = sorted(src - corpus_acts)
    extra = sorted(corpus_acts - src)
    return {
        # ⚠ Ноль разобранных актов = проверка НЕ состоялась, а не «отставания нет». Первая версия
        # в этом случае печатала «корпус не отстаёт» и выходила с кодом 0 — на источнике с «№»
        # вместо «N», на HTML со страницы и на нечитаемом файле (ревью PR #94).
        "разбор удался": bool(src),
        "актов в первоисточнике": len(src),
        "нет в корпусе": [label(a) for a in missing],
        "есть только в корпусе": [label(a) for a in extra],
        "последний в первоисточнике": label(max(src)) if src else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="A6: слежение за редакциями ПП №719")
    ap.add_argument("--source", metavar="FILE", help="текст ДЕЙСТВУЮЩЕЙ редакции (сохранённый файл)")
    ap.add_argument("--url", help="адрес действующей редакции (⚠ канал рвётся, файл надёжнее)")
    ap.add_argument("--json", action="store_true", help="машиночитаемый вывод")
    args = ap.parse_args()

    report: dict = {"корпус": check_corpus()}
    corpus = report["корпус"]
    corpus_acts: set = set()
    for v in corpus["части"].values():
        corpus_acts |= v.pop("_acts", set())

    src_text = None
    if args.source:
        src_text = read(Path(args.source))
        if src_text is None:
            print(f"⚠ файл первоисточника не читается: {args.source}")
    elif args.url:
        src_text = fetch(args.url)
        if src_text is None:
            print("⚠ первоисточник не скачался — проверка отставания НЕ выполнена "
                  "(сохраните текст файлом и передайте --source)")
    if src_text:
        report["первоисточник"] = check_source(src_text, corpus_acts)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print("=" * 78)
        print("A6 — СЛЕЖЕНИЕ ЗА РЕДАКЦИЯМИ")
        print("=" * 78)
        print(f"  редакция корпуса: {corpus['редакция корпуса']}")
        for name, v in corpus["части"].items():
            if "ошибка" in v:
                print(f"    {name:26} — {v['ошибка']} ({v['файл']})")
            else:
                print(f"    {name:26} актов {v['актов']:3}, последний {v['последний']}")
        print(f"  части согласованы между собой: {'да' if corpus['части согласованы'] else 'НЕТ'}"
              f"   (разделов с последним актом: {corpus['разделов с последним актом']})")
        if corpus["нечитаемые части"]:
            print(f"  ⚠ НЕ ПРОЧИТАНЫ (сравнение не состоялось): {', '.join(corpus['нечитаемые части'])}")
        print(f"  ссылка на первоисточник для этой редакции известна: "
              f"{'да' if corpus['ссылка на первоисточник известна'] else 'НЕТ'}")
        if "первоисточник" in report:
            s = report["первоисточник"]
            print(f"\n  первоисточник: актов {s['актов в первоисточнике']}, "
                  f"последний {s['последний в первоисточнике']}")
            if not s["разбор удался"]:
                print("  ⚠ В ПЕРВОИСТОЧНИКЕ НЕ РАЗОБРАН НИ ОДИН АКТ — проверка отставания НЕ "
                      "выполнена (не тот файл? другая форма номера?). Это НЕ «отставания нет».")
            elif s["нет в корпусе"]:
                print("  ⚠ ЕСТЬ В ПЕРВОИСТОЧНИКЕ, НО НЕ В КОРПУСЕ:")
                for a in s["нет в корпусе"]:
                    print(f"      {a}")
            else:
                print("  корпус не отстаёт: все акты первоисточника в нём есть")

    src_report = report.get("первоисточник")
    problems = (not corpus["части согласованы"]
                or bool(corpus["нечитаемые части"])
                or corpus["редакция корпуса"] == UNKNOWN
                or not corpus["ссылка на первоисточник известна"]
                or bool(src_report and not src_report["разбор удался"])
                or bool(src_report and src_report["нет в корпусе"])
                # источник просили, но он не доехал — тоже отказ проверки, а не «всё хорошо»
                or bool((args.source or args.url) and src_report is None))
    if problems and not args.json:
        print("\n⚠ РАСХОЖДЕНИЕ НАЙДЕНО. Решение о переиндексации принимает человек: сверьте с "
              "первоисточником, при необходимости актуализируйте корпус и обновите\n"
              "  KONTUR_719_BY_EDITION в app/rag/edition.py.")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
