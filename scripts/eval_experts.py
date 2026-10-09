"""Прогон приёмочного набора исправлений экспертов (`scripts/eval_golden_experts.json`, EV23 #142).

Меряет то, что эксперт видит первым и что модель не исправит сама: КАКАЯ позиция стала целевой,
что попало в окно и что доехало до текста запроса к модели. Ответ модели не генерируется — путь
до вызова LLM общий у обычного и потокового ответа (`pipeline._plan_answer`), он и прогоняется.

⚠ Реранкер выключен: он зовёт DeepSeek. В бою он работает только без совпадения по коду и
переставляет окно, но не пополняет его, — поэтому кейсы «чего нет в окне» он не спасает, а кейсы
с кодом не трогает вовсе. Если кейс без кода зелёный только благодаря реранкеру, этот прогон
покажет его красным — это честнее обратного.

Запуск: `.venv\\Scripts\\python scripts/eval_experts.py` (нужен Qdrant; `--json PATH` — детали).
Код возврата: число непрошедших кейсов (0 — все прошли).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.core.console import enable_utf8  # noqa: E402

enable_utf8()
GOLDEN = ROOT / "scripts" / "eval_golden_experts.json"


def load_cases() -> list[dict]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))["cases"]


def check(case: dict, plan) -> list[str]:
    """Список нарушенных ожиданий кейса (пусто — кейс прошёл)."""
    from app.rag import pipeline

    if isinstance(plan, pipeline.Answer):
        return [f"ранний ответ без плана: {plan.text[:80]!r}"]
    fails: list[str] = []
    targets = [h.source_anchor for h in pipeline.target_hits(plan.hits, plan.codes)]
    window = [h.source_anchor for h in plan.hits]
    if case.get("expect_target_any") and not set(case["expect_target_any"]) & set(targets):
        fails.append(f"целевая {short(targets)} вместо {short(case['expect_target_any'])}")
    for a in case.get("expect_window_all") or []:
        if a not in window:
            fails.append(f"в окне нет {short([a])}")
    bad = [h.section_roman for h in plan.hits if h.section_roman in set(case.get("forbid_window_sections") or [])]
    if bad:
        fails.append(f"в окне чужие разделы {sorted(set(bad))}")
    prompt = plan.messages[-1]["content"]
    for needle in case.get("expect_prompt") or []:
        if needle not in prompt:
            fails.append(f"в запросе к модели нет «{needle}»")
    return fails


def short(anchors: list[str]) -> str:
    return ", ".join((a or "—").replace("Приложение к ПП №719, ", "") for a in anchors) or "—"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", type=Path, help="сохранить детали прогона")
    args = ap.parse_args()

    from app.core.config import settings
    settings.RERANK_ENABLED = False
    from app.rag import pipeline, retriever
    from app.tools.navigator import extract_okpd2
    retriever.EXACT_SEARCH = True   # M1: повторяемость замера

    out, failed = [], 0
    for case in load_cases():
        plan = pipeline._plan_answer(case["query"], okpd2=extract_okpd2(case["query"]),
                                     history=case.get("history"))
        fails = check(case, plan)
        failed += bool(fails)
        tg = ([] if isinstance(plan, pipeline.Answer)
              else [h.source_anchor for h in pipeline.target_hits(plan.hits, plan.codes)])
        print(f"{'OK  ' if not fails else 'FAIL'} {case['id']:24} целевая: {short(tg)[:70]}")
        for f in fails:
            print(f"       ✗ {f}")
        out.append({"id": case["id"], "ok": not fails, "fails": fails, "targets": tg})
    print(f"\nИТОГ: прошло {len(out) - failed} из {len(out)}")
    if args.json:
        args.json.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
