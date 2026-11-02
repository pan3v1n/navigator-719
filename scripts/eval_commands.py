"""Живой прогон готовых команд (`L8-1` #153, `docs/L8_COMMANDS_TZ.md` §11). ПЛАТНЫЙ: зовёт DeepSeek.

ЗАЧЕМ. Тест маршрута проверяет, куда команда ушла, но не что ответила модель. Здесь каждая команда
проходит настоящим путём запроса (`ChatRequest` → `_apply_command` → движок), поэтому текст, источник и
значение поиска те же, что на бою. Отдельной сборки запроса в замере нет: разойтись с сервисом нечему.

ЧТО СЧИТАЕМ. Незаземлённые числа той же проверкой, что в рантайме · несуществующее «заключение ТПП» двумя
независимыми чтениями · корпус и подпись «источник задан командой» · вне сферы: отказ или флаг · баллы в
ответах команды «Баллы и порог» · устойчивость трёх прогонов.

ОБЪЁМ. У команды с полем три значения, у команды без поля одно; всё ×3 прогона: 72 ответа (§11 считал
108, но у шести команд из двенадцати значения нет).

⚠ `declines` откалиброван на товарной ветке: у процедурных ответов он срабатывает на обычных словах и
показывал бы «отказ» у полных ответов. Поэтому отказ считаем только у команд приложения, а процедурные
ответы читаем глазами: `--from` пересчитывает сводку из сохранённых ответов без новых вызовов.

ЗАПУСК (нужны локальный Qdrant, модель e5 и ключ DeepSeek в окружении):
    python scripts/eval_commands.py --out <файл вне репозитория>.json --report docs/eval_runs/<дата>_l8_commands_live.md
    python scripts/eval_commands.py --from <тот файл>.json   → сводка без вызовов модели
⚠ Файл `--out` держит тексты ответов: в репозиторий кладём только сводку.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from app.core.console import enable_utf8  # noqa: E402

enable_utf8()

from eval_answers import CITE_RE, declines, leaked_cjk  # noqa: E402
from eval_documents import conclusion_mentions  # noqa: E402

from app.api.chat import ChatRequest, _apply_command  # noqa: E402
from app.core import commands  # noqa: E402
from app.core.costs import cost_rub  # noqa: E402
from app.rag import corpora  # noqa: E402
from app.rag.pipeline import answer, claim_numbers, unverified_numbers  # noqa: E402
from app.tools.navigator import extract_okpd2  # noqa: E402

RUNS = 3
OUT_OF_SCOPE = "хлебобулочные изделия"
FILLS: dict[str, tuple[str, ...]] = {
    "product": ("бульдозеры гусеничные", "28.22.16.111", OUT_OF_SCOPE),
    "tnved": ("8471 30 000 0", "8544 70 000 0", "8517 62 000 9"),
    "point": ("сноска <12(1)>", "подпункт «г» пункта 1", "пункт 32 Правил реестра"),
}


def build_cases() -> list[tuple[commands.Command, str]]:
    return [(c, v) for c in commands.COMMANDS for v in (FILLS[c.field.kind] if c.field else ("",))]


def run_one(cmd: commands.Command, value: str, run: int) -> dict:
    req = ChatRequest(message=value, command=cmd.key)
    key = _apply_command(req)
    started = time.monotonic()
    ans = answer(req.message, okpd2=extract_okpd2(req.message), history=[], corpus=req.corpus,
                 **req.engine_extra())
    text = ans.text or ""
    return {
        "key": key, "value": value, "run": run, "corpus_mode": req.corpus, "used": ans.corpus,
        "text": text, "seconds": round(time.monotonic() - started, 1),
        "label": corpora.answered_by(ans.corpus, req.corpus, commands.title_of(key)),
        "hallucinated": unverified_numbers(text, ans.grounding, req.message),
        "guard": list(ans.unverified_numbers or []),
        "phantom_guard": list(ans.phantom_documents or []),
        "phantom_oracle": conclusion_mentions(text)[1],
        "claims": len(claim_numbers(text)), "cited": bool(CITE_RE.search(text)),
        "declined": declines(text), "low_relevance": bool(ans.low_relevance),
        "cjk": leaked_cjk(text, ans),
        "prompt_tokens": ans.prompt_tokens or 0, "completion_tokens": ans.completion_tokens or 0,
    }


def summarize(rows: list[dict]) -> list[str]:
    n = len(rows)
    cost = sum(cost_rub(r["prompt_tokens"], r["completion_tokens"]) for r in rows)
    wrong_corpus = [r for r in rows if r["used"] and r["corpus_mode"] != corpora.APPENDIX
                    and r["used"] != corpora.used_for_mode(r["corpus_mode"])]
    wrong_corpus += [r for r in rows if r["corpus_mode"] == corpora.APPENDIX
                     and r["used"] not in (corpora.USED_APPENDIX, "")]
    no_label = [r for r in rows if r["used"] and "источник задан командой" not in r["label"]]
    no_corpus = [r for r in rows if not r["used"]]
    outside = [r for r in rows if r["value"] == OUT_OF_SCOPE]
    outside_ok = [r for r in outside if r["declined"] or r["low_relevance"]]
    points = [r for r in rows if r["key"] == "points" and r["value"] != OUT_OF_SCOPE]
    points_ok = [r for r in points if r["claims"]]
    groups: dict[tuple, list[str]] = {}
    for r in rows:
        groups.setdefault((r["key"], r["value"]), []).append(r["text"])
    same = sum(1 for texts in groups.values() if len(set(texts)) == 1)
    seconds = sorted(r["seconds"] for r in rows)
    L = [f"Ответов: {n} ({len(groups)} запросов × {RUNS}), стоимость {cost:.2f} ₽, "
         f"медиана {seconds[n // 2]} с, максимум {seconds[-1]} с", "",
         "| Что | Итог |", "|---|---|",
         f"| Незаземлённые числа | {sum(bool(r['hallucinated']) for r in rows)} ответов из {n} |",
         f"| Пометка гарда чисел | {sum(bool(r['guard']) for r in rows)} |",
         f"| «Заключение ТПП» утверждается: замер / гард | {sum(bool(r['phantom_oracle']) for r in rows)} / "
         f"{sum(bool(r['phantom_guard']) for r in rows)} |",
         f"| Чужой корпус | {len(wrong_corpus)} |",
         f"| Ответ с корпусом без подписи команды | {len(no_label)} |",
         f"| Ответ без корпуса (отказ, сбой) | {len(no_corpus)} |",
         f"| Вне сферы: отказ или флаг | {len(outside_ok)} из {len(outside)} |",
         f"| «Баллы и порог»: числа в ответе | {len(points_ok)} из {len(points)} |",
         f"| Ссылки [N] в ответе | {sum(r['cited'] for r in rows)} из {n} |",
         f"| Иероглифы в ответе | {sum(bool(r['cjk']) for r in rows)} |",
         f"| Побайтно тот же текст во всех прогонах | {same} запросов из {len(groups)} |", "",
         "| Команда | Значение | Корпус | Отказ (только приложение) | Флаг | Чисел в ответе | Ссылки |",
         "|---|---|---|---|---|---|---|"]
    for (key, value), texts in groups.items():
        rs = [r for r in rows if r["key"] == key and r["value"] == value]
        refused = (f"{sum(r['declined'] for r in rs)}/{RUNS}" if rs[0]["corpus_mode"] == corpora.APPENDIX
                   else "—")
        L.append(f"| `{key}` | {value or '—'} | {rs[0]['used'] or '—'} | {refused} | "
                 f"{sum(r['low_relevance'] for r in rs)}/{RUNS} | {min(r['claims'] for r in rs)}–"
                 f"{max(r['claims'] for r in rs)} | {sum(r['cited'] for r in rs)}/{RUNS} |")
    return L


def main() -> int:
    ap = argparse.ArgumentParser(description="Живой прогон готовых команд (L8-1), платный")
    ap.add_argument("--out", type=Path, help="JSON с текстами ответов, ВНЕ репозитория")
    ap.add_argument("--from", dest="source", type=Path, help="пересчитать сводку из файла, без модели")
    ap.add_argument("--report", type=Path)
    args = ap.parse_args()
    if args.source:
        print("\n".join(summarize(json.loads(args.source.read_text(encoding="utf-8")))))
        return 0
    if not args.out:
        ap.error("нужен --out или --from")
    cases = build_cases()
    print(f"запросов {len(cases)}, ответов {len(cases) * RUNS}", flush=True)
    rows, failures = [], 0
    for run in range(1, RUNS + 1):
        for cmd, value in cases:
            try:
                rows.append(run_one(cmd, value, run))
            except Exception as e:  # noqa: BLE001 — сбой считаем, но деньги на сломанный прогон не тратим
                failures += 1
                print(f"СБОЙ {cmd.key} {value!r}: {type(e).__name__}", flush=True)
                if failures >= 3:
                    print("три сбоя: остановка", flush=True)
                    args.out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
                    return 2
            print(f"{len(rows)}/{len(cases) * RUNS} {cmd.key}", flush=True)
            args.out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    out = "\n".join(summarize(rows))
    print(out)
    if args.report:
        args.report.write_text("# Живой прогон готовых команд\n\n" + out + "\n", encoding="utf-8")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
