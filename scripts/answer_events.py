"""Сводка событий ответов из журнала боя — `O8` #144.

Читает строки `answer_event {json}` (их пишет `app/api/events.py`, одна на ответ) и печатает: сколько
ответов, доля веток по корпусу (`L2`), режимы источника (`L1`), срабатывания проверок, p50/p95 времени
ответа и времени до первого куска. В строках событий нет ни текста вопросов, ни логинов — сводку можно
снимать на боевом сервере и переносить без ПДн.

Запуск (на сервере):
  docker compose logs app --since 24h --no-log-prefix | python scripts/answer_events.py
  python scripts/answer_events.py журнал.txt
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
    ⚠ Через `ceil`, а не `round`: банковское округление Python давало p50 из двух значений — большее."""
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
    src = Path(sys.argv[1]).read_text(encoding="utf-8").splitlines() if len(sys.argv) > 1 else sys.stdin
    print(json.dumps(summary(parse(src)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.path.append(str(Path(__file__).resolve().parents[1]))  # корень репозитория — для app.core
    from app.core.console import enable_utf8  # #107: кириллица в выводе на консоли Windows
    enable_utf8()
    main()
