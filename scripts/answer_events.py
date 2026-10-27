"""Сводка событий ответов из журнала боя — `O8` #144.

Читает строки `answer_event {json}` (их пишет `app/api/events.py`, одна на ответ движка) и печатает:
сколько ответов, корпуса (`L2`), режимы источника (`L1`), срабатывания проверок, p50/p95 времени
ответа и времени до первого куска. `procedural_share` — доля ответов, СОБРАННЫХ по корпусу норм
(rules/metodrek); честные отказы процедурной ветки и приветствия корпуса не имеют («-») и в долю не
входят. В строках событий нет ни текста вопросов, ни логинов — сводку можно снимать на сервере и
переносить без ПДн.

Скрипт самостоятельный (только стандартная библиотека) — запускается и системным `python3` сервера:
  docker compose logs app --since 24h --no-log-prefix | python3 scripts/answer_events.py
  python3 scripts/answer_events.py журнал.txt
"""

from __future__ import annotations

import json
import math
import sys
from collections import Counter
from pathlib import Path

PREFIX = "answer_event "
PROCEDURAL = {"rules", "metodrek"}


def parse(lines) -> list[dict]:
    out = []
    for line in lines:
        i = line.find(PREFIX)
        if i < 0:
            continue
        try:
            out.append(json.loads(line[i + len(PREFIX):].strip()))
        except json.JSONDecodeError:
            continue
    return out


def pct(values: list[int], q: float) -> int | None:
    """Перцентиль ближайшего ранга (ceil(q·n)-й по возрастанию); None — данных нет.
    Важно: через `ceil`, а не `round` — банковское округление Python давало p50 из двух значений большим.
    Скрипт без `app.core.console`, чтобы шёл системным `python3` сервера: значков вне cp1251 в нём нет."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    return vals[max(0, min(len(vals), math.ceil(q * len(vals))) - 1)]


def summary(events: list[dict]) -> dict:
    n = len(events)
    corpus = Counter(e.get("corpus", "-") for e in events)
    return {
        "answers": n,
        "corpus": dict(corpus),
        "procedural_share": round(sum(corpus[c] for c in PROCEDURAL) / n, 3) if n else None,
        "mode": dict(Counter(e.get("mode", "auto") for e in events)),
        "guest": sum(1 for e in events if e.get("guest")),
        "unverified": sum(1 for e in events if e.get("unverified")),
        "phantom": sum(1 for e in events if e.get("phantom")),
        "low_relevance": sum(1 for e in events if e.get("low_relevance")),
        "total_ms_p50": pct([e.get("total_ms") for e in events], 0.5),
        "total_ms_p95": pct([e.get("total_ms") for e in events], 0.95),
        "first_token_ms_p50": pct([e.get("first_token_ms") for e in events], 0.5),
        "first_token_ms_p95": pct([e.get("first_token_ms") for e in events], 0.95),
    }


def main() -> None:
    # Журнал — UTF-8; на консоли Windows stdin по умолчанию cp1251 и падал на кириллице (ревью этапа 2).
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    src = Path(sys.argv[1]).read_text(encoding="utf-8").splitlines() if len(sys.argv) > 1 else sys.stdin
    print(json.dumps(summary(parse(src)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
