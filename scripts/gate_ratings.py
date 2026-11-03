"""Гейт `1.0`, часть (в): доля оценок ≥4★ пользователей боя — по копии боевой базы (#80).

ЗАЧЕМ. Решение владельца 28.10.2026: гейт `1.0` вместо снятой третьей волны — на имеющихся данных,
и одна из его частей — доля ответов, оценённых на 4–5★, «после дедупликации #166, на выборке не
меньше заданной». Порог и минимальную выборку владелец назначает ПОСЛЕ первого замера (урок `K2`:
порог, назначенный до замера, подгоняет замер). Этот скрипт и есть первый замер: он печатает долю с
размером выборки и 95 % интервалом в разрезах, где она может отличаться, — по ветке ответа, месяцу и
роли оценившего.

ОДНО ПРАВИЛО С АДМИНКОЙ. Оценки отбираются ровно так, как их считает «Качество» `/admin`:
`admin_stats._superseded_ratings` (последняя оценка пользователя на ответ, #166), снятая оценка
(последняя = 0, решение владельца 27.10.2026) не считается, оценка-сирота (ответ удалён, R2) — тоже.
Итог сверяется с `admin_stats.build_admin_view` на той же базе; расхождение — код 1 и никаких чисел:
вторая реализация метрики, разошедшаяся с первой, хуже отсутствующей.

ВЕТКА ОТВЕТА. В базе её нет: колонка `messages.corpus` заполняется только с `L2` (28.10.2026).
Для прежних ответов ветка восстанавливается по источникам ответа: источник-пункт (`kind = rule`) —
процедурная; позиция приложения с кодом ОКПД2 — товарная; источников нет — процедурная, если вопрос
проходит процедурный гейт (`procedural.is_procedural`), иначе «прочее» (уточнение, отказ, приветствие).

⚠ 152-ФЗ. На вход — копия боевой базы с ПДн; она обязана лежать ВНЕ рабочего дерева репозитория и
открывается ТОЛЬКО ДЛЯ ЧТЕНИЯ (`mode=ro`). Печатаются только агрегаты: ни текстов, ни логинов, ни
регионов.

Запуск (Git Bash):
  NO_PROXY=localhost,127.0.0.1 .venv/Scripts/python scripts/gate_ratings.py \\
      --db D:/navigator-backups/navigator_app-<дата>.db [--json OUT_вне_репо.json]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BRANCHES = ("товарная", "процедурная", "прочее")
MSK = timedelta(hours=3)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95 % интервал Уилсона для доли k/n (у малых выборок честнее нормального)."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, mid - half), min(1.0, mid + half))


def branch_of(answer, question: str) -> str:
    """Ветка ответа (см. шапку). `answer` — объект с `corpus` и `sources_json`."""
    from app.rag import procedural

    used = (answer.corpus or "").partition("|")[0]
    if used:
        return "товарная" if used.startswith("appendix") else "процедурная"
    try:
        sources = json.loads(answer.sources_json or "[]") or []
    except (TypeError, ValueError):
        sources = []
    if any((s or {}).get("kind") == "rule" for s in sources) and not any(
            (s or {}).get("okpd2") for s in sources):
        return "процедурная"
    if any((s or {}).get("okpd2") for s in sources):
        return "товарная"
    return "процедурная" if procedural.is_procedural(question or "") else "прочее"


def counted_ratings(users, messages, feedback) -> list[dict]:
    """Оценки, которые идут в приёмочную долю, — по правилу «Качества» (`admin_stats`).

    Возвращает строки {rating, branch, month, role, low_relevance}; текстов в них нет."""
    from app.api.admin_stats import _superseded_ratings

    role_of = {u.id: u.role for u in users}
    msg_by_id = {m.id: m for m in messages}
    by_session: dict[str, list] = defaultdict(list)
    for m in sorted(messages, key=lambda m: (m.ts, m.id)):
        by_session[m.session_id].append(m)

    fb_by_user: dict[int, list] = defaultdict(list)
    for f in feedback:
        fb_by_user[f.user_id].append(f)
    superseded = _superseded_ratings(fb_by_user)

    rows = []
    for f in feedback:
        if (f.kind or "service") != "answer" or f.rating is None:
            continue
        ans = msg_by_id.get(f.message_id)
        if ans is None or f.id in superseded or f.rating == 0:
            continue                       # сирота (R2) · перебита (#166) · снята (27.10)
        question = ""
        for m in by_session.get(ans.session_id, ()):
            if m.id == ans.id:
                break
            if m.role == "user":
                question = m.content or ""
        ts = ans.ts + MSK if ans.ts else None
        rows.append({
            "rating": f.rating,
            "branch": branch_of(ans, question),
            "month": ts.strftime("%Y-%m") if ts else "—",
            "role": role_of.get(f.user_id, "?"),
            "low_relevance": bool(ans.low_relevance),
        })
    return rows


def share(rows: list[dict]) -> dict:
    n = len(rows)
    k = sum(1 for r in rows if r["rating"] >= 4)
    lo, hi = wilson(k, n)
    return {"n": n, "ok": k, "pct": round(100 * k / n, 1) if n else None,
            "ci95": [round(100 * lo, 1), round(100 * hi, 1)],
            "avg": round(sum(r["rating"] for r in rows) / n, 2) if n else None}


def summarize(rows: list[dict]) -> dict:
    out = {"всего": share(rows)}
    for key in ("branch", "month", "role", "low_relevance"):
        groups: dict = defaultdict(list)
        for r in rows:
            groups[r[key]].append(r)
        out[key] = {str(k): share(v) for k, v in sorted(groups.items(), key=lambda kv: str(kv[0]))}
    for a, b in (("branch", "month"), ("role", "month")):
        cross: dict = defaultdict(list)
        for r in rows:
            cross[(r[a], r[b])].append(r)
        out[f"{a}×{b}"] = {f"{x} · {y}": share(v) for (x, y), v in sorted(cross.items())}
    return out


def min_n_for_halfwidth(p: float, half: float) -> int:
    """Сколько оценок нужно, чтобы 95 % интервал вокруг доли p был не шире ±half."""
    return math.ceil((1.96 ** 2) * p * (1 - p) / (half ** 2))


def open_readonly(db_path: Path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    url = f"sqlite:///file:{db_path.resolve().as_posix()}?mode=ro&uri=true"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    return sessionmaker(bind=engine)()


def _fmt(s: dict) -> str:
    if not s["n"]:
        return "n=0"
    return (f"n={s['n']:>3}  ≥4★ {s['ok']:>3} = {s['pct']:5.1f} %  "
            f"[95 %: {s['ci95'][0]:.0f}–{s['ci95'][1]:.0f}]  ср. {s['avg']:.2f}★")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", required=True, help="копия боевой базы (вне репозитория!)")
    ap.add_argument("--json", type=Path, help="сохранить агрегаты (путь вне репозитория)")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        sys.exit(f"Нет файла базы: {db_path}")
    if ROOT in db_path.resolve().parents:
        sys.exit("База с ПДн лежит ВНУТРИ репозитория — вынеси её наружу (CLAUDE.md, 152-ФЗ).")

    from app.api.admin_stats import build_admin_view
    from app.db import queries as q

    db = open_readonly(db_path)
    try:
        users = q.list_users(db)
        messages = q.get_all_messages(db)
        feedback = q.get_all_feedback(db)
        rows = counted_ratings(users, messages, feedback)
        admin = build_admin_view(db)["stats"]
    finally:
        db.close()

    summary = summarize(rows)
    total = summary["всего"]
    # Сверка с «Качеством» админки: та же выборка, та же доля (там — округление до целого).
    admin_ok = (admin["answer_ratings"] == total["n"]
                and admin["accept_pct"] == (round(100 * total["ok"] / total["n"]) if total["n"] else None))
    if not admin_ok:
        print(f"✗ РАСХОЖДЕНИЕ С АДМИНКОЙ: здесь n={total['n']}, ≥4★ {total['pct']} %; "
              f"в «Качестве» n={admin['answer_ratings']}, {admin['accept_pct']} %. Чисел не печатаю.")
        return 1

    print("=" * 78)
    print(f"Гейт 1.0, часть (в): доля оценок ≥4★ — {db_path.name}")
    print("=" * 78)
    print(f"Сверка с «Качеством» /admin: n={admin['answer_ratings']}, {admin['accept_pct']} % — совпало.")
    print(f"Вне доли: перебито повторной оценкой {admin['repeat_ratings']}, снято "
          f"{admin['withdrawn_ratings']}, сирот {admin['orphan_ratings']}.\n")
    print(f"ВСЕГО           {_fmt(total)}")
    for key, title in (("branch", "ветка"), ("month", "месяц ответа"), ("role", "роль оценившего"),
                       ("low_relevance", "флаг «низкая релевантность»"), ("branch×month", "ветка × месяц"),
                       ("role×month", "роль × месяц")):
        print(f"\n— {title}")
        for k, s in summary[key].items():
            print(f"  {k:<26} {_fmt(s)}")
    p = (total["ok"] / total["n"]) if total["n"] else 0.5
    print("\n— сколько оценок нужно для интервала ±N п.п. при доле как сейчас "
          f"({100 * p:.0f} %): " + ", ".join(f"±{h} → {min_n_for_halfwidth(p, h / 100)}"
                                            for h in (5, 7, 10, 15)))
    if args.json:
        out = args.json.resolve()
        if ROOT in out.parents:
            sys.exit("Отчёт по боевой базе пишется ВНЕ репозитория (152-ФЗ).")
        out.write_text(json.dumps({"db": db_path.name, "summary": summary}, ensure_ascii=False,
                                  indent=1), encoding="utf-8")
        print(f"\nСохранено: {out}")
    return 0


if __name__ == "__main__":
    from app.core.console import enable_utf8  # #107

    enable_utf8()
    raise SystemExit(main())
