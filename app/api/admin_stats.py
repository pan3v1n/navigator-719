"""Сбор данных для админ-панели: приёмочный скоркард, срез по регионам, триаж «плохих»
ответов, детальные логи и агрегаты токенов/₽ — за ОДИН проход по батч-выборкам (без N+1).

Вынесено из ``app/api/web.py``, чтобы:
  * расчёт был тестируемым в отрыве от HTTP (см. ``tests/test_admin_stats.py``);
  * его переиспользовал admin-экспорт (``/api/admin/export``);
  * сам роут остался тонким.

Фильтры (все опциональны, пустое = «все»):
  * ``date_from`` / ``date_to`` — период по дате реплики/фидбека (изоляция когорты теста);
  * ``region`` — регион пользователя (сравнение ТПП);
  * ``role`` — роль (user | expert | admin).

Возвращает единый ``dict`` (``data`` / ``totals`` / ``stats``), который роут распаковывает в
шаблон. Все прежние ключи ``stats`` сохранены (регресс-безопасно), новое — ``regions``,
``bad_answers``, эхо фильтров и ``generated_at``.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from app.core.costs import cost_rub
from app.db import queries as q
from app.rag import procedural

# Оценки 0..2★ считаем «плохими» (гейт приёмки: ≥4★ — пройдено, 3★ — пограничные).
BAD_RATING_MAX = 2
# Порог приёмки: доля ответов с оценкой ≥4★, при которой гейт 1.0 пройден.
GATE_PCT = 70


def _fmt(ts) -> str:
    return ts.strftime("%d.%m.%Y %H:%M") if ts else ""


def _in_window(ts, date_from: date | None, date_to: date | None) -> bool:
    """Попадает ли отметка времени в окно [date_from; date_to] (границы включительно).
    Без даты — учитываем только когда окно вовсе не задано (иначе не спутаем с когортой)."""
    if ts is None:
        return date_from is None and date_to is None
    d = ts.date()
    if date_from and d < date_from:
        return False
    if date_to and d > date_to:
        return False
    return True


def _accept_pct(ratings: list[int]) -> int | None:
    """Доля оценок ≥4★ в процентах (метрика приёмочного гейта). None — если оценок нет."""
    return round(100 * sum(1 for r in ratings if r >= 4) / len(ratings)) if ratings else None


def _fmt_trend(cur: float, prev: float, good_up: bool | None = True) -> dict:
    """Готовый к отрисовке тренд-чип (текущее окно к предыдущему): стрелка, %-подпись, css-класс.
    Зелёный = «хорошо»: направление сверяется с good_up (для затрат good_up=False — рост это плохо;
    good_up=None → нейтральный серый). prev=0 → 'новое' (без деления на ноль)."""
    title = f"текущий {round(cur, 2)} · предыдущий {round(prev, 2)}"
    if prev == 0 and cur == 0:
        return {"label": "0%", "arrow": "→", "cls": "tr-flat", "delta": 0, "title": title}
    if prev == 0:
        return {"label": "новое", "arrow": "•", "cls": "tr-new", "delta": None, "title": title}
    d = round((cur - prev) / prev * 100)
    if d == 0:
        return {"label": "0%", "arrow": "→", "cls": "tr-flat", "delta": 0, "title": title}
    up = d > 0
    good = None if good_up is None else (up == good_up)
    cls = "tr-flat" if good is None else ("tr-good" if good else "tr-bad")
    return {"label": f"{abs(d)}%", "arrow": "▲" if up else "▼", "cls": cls, "delta": d, "title": title}


def _question_for(answer_msg, sess_msgs: dict) -> str:
    """Вопрос эксперта, на который отвечает ``answer_msg`` — ближайшая реплика role='user'
    перед ним в той же беседе (список бесед отсортирован по возрастанию ts)."""
    prev = ""
    for m in sess_msgs.get(answer_msg.session_id, ()):
        if m.id == answer_msg.id:
            break
        if m.role == "user":
            prev = m.content
    return prev


def build_admin_view(
    db,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    region: str = "",
    role: str = "",
    generated_at: datetime | None = None,
) -> dict:
    """Единый проход по данным приложения → структура для админ-панели и экспорта."""
    users = q.list_users(db)
    # Значения для выпадашек фильтра — по ВСЕМ пользователям (до применения фильтра).
    all_regions = sorted({(u.region or "").strip() for u in users if (u.region or "").strip()})
    all_roles = sorted({u.role for u in users})

    region = (region or "").strip()
    role = (role or "").strip()
    sel_users = [
        u for u in users
        if (not role or u.role == role)
        and (not region or (u.region or "").strip() == region)
    ]
    sel_ids = {u.id for u in sel_users}

    # Батч-выборки (обе отсортированы по ts) вместо N+1 из старого admin_page.
    all_msgs = q.get_all_messages(db)
    all_fb = q.get_all_feedback(db)
    msg_by_id = {m.id: m for m in all_msgs}
    sess_msgs: dict[str, list] = defaultdict(list)
    for m in all_msgs:
        sess_msgs[m.session_id].append(m)

    # Раскладка по пользователю с учётом окна дат (регион/роль уже отфильтрованы через sel_ids).
    msgs_by_user: dict[int, list] = defaultdict(list)
    for m in all_msgs:
        if m.user_id in sel_ids and _in_window(m.ts, date_from, date_to):
            msgs_by_user[m.user_id].append(m)
    fb_by_user: dict[int, list] = defaultdict(list)
    for f in all_fb:
        if f.user_id in sel_ids and _in_window(f.ts, date_from, date_to):
            fb_by_user[f.user_id].append(f)

    # --- аккумуляторы ------------------------------------------------------
    data = []                       # детальные логи (юзер → беседы → реплики)
    g_prompt = g_completion = 0
    total_requests = total_answers = total_conversations = 0
    flags_unverified = flags_lowrel = 0
    per_day: dict[str, dict] = defaultdict(lambda: {"requests": 0, "tokens": 0})  # ключ ISO YYYY-MM-DD
    per_user_stat = []
    matched_counts = {"да": 0, "частично": 0, "нет": 0}
    ratings: list[int] = []                                  # service-оценки 1..5
    ans_ratings: list[int] = []                              # звёзды ответов 0..5 (kind='answer')
    orphan_ratings = 0                                       # оценки без найденного ответа — вне метрики (R2)
    ans_ratings_by_role: dict[str, list[int]] = defaultdict(list)
    region_acc: dict[str, dict] = defaultdict(
        lambda: {"users": 0, "ratings": [], "requests": 0, "tokens": 0, "cost": 0.0}
    )
    corrections, answer_comments, dialog_comments = [], [], []
    procedural_questions, answer_rows, recent = [], [], []  # answer_rows — ВСЕ оценённые ответы (триаж/экспорт)

    for u in sel_users:
        msgs = msgs_by_user.get(u.id, [])
        u_region = (u.region or "").strip()
        by_sid, order = {}, []
        u_req = 0
        for m in msgs:
            s = by_sid.get(m.session_id)
            if s is None:
                s = {"sid": m.session_id, "title": None, "date": "", "messages": [],
                     "prompt": 0, "completion": 0}
                by_sid[m.session_id] = s
                order.append(m.session_id)
            if s["title"] is None and m.role == "user":
                s["title"] = m.content
            if not s["date"] and m.ts:
                s["date"] = _fmt(m.ts)
            tok = (m.prompt_tokens or 0) + (m.completion_tokens or 0)
            s["messages"].append({
                "role": m.role, "content": m.content, "ts": _fmt(m.ts),
                "low_relevance": m.low_relevance, "unverified": m.unverified_json, "tokens": tok,
            })
            s["prompt"] += m.prompt_tokens or 0
            s["completion"] += m.completion_tokens or 0
            day = m.ts.strftime("%Y-%m-%d") if m.ts else None  # ISO-ключ → корректная сортировка
            if m.role == "user":
                u_req += 1
                if day:
                    per_day[day]["requests"] += 1
                recent.append({"_ts": m.ts, "user": u.username, "region": u_region,
                               "q": (m.content or "")[:120], "ts": _fmt(m.ts)})
                if procedural.is_procedural(m.content or ""):
                    procedural_questions.append(
                        {"user": u.username, "region": u_region,
                         "q": (m.content or "")[:140], "ts": _fmt(m.ts)}
                    )
            else:
                total_answers += 1
                if day:
                    per_day[day]["tokens"] += tok
                if m.unverified_json:
                    flags_unverified += 1
                if m.low_relevance:
                    flags_lowrel += 1

        u_prompt = u_completion = 0
        sessions = []
        for sid in reversed(order):  # новые беседы сверху
            s = by_sid[sid]
            s["title"] = (s["title"] or "Диалог")[:60]
            s["tokens"] = s["prompt"] + s["completion"]
            s["cost"] = cost_rub(s["prompt"], s["completion"])
            u_prompt += s["prompt"]
            u_completion += s["completion"]
            sessions.append(s)

        # --- фидбек пользователя (три канала по Feedback.kind) ---
        feedback = []
        for f in fb_by_user.get(u.id, []):
            kind = f.kind or "service"
            if kind == "service":
                feedback.append({"rating": f.rating, "matched": f.matched,
                                 "comment": f.comment, "ts": _fmt(f.ts)})
                if f.rating is not None:
                    ratings.append(f.rating)
                if f.matched in matched_counts:
                    matched_counts[f.matched] += 1
            elif kind == "answer":
                orig = msg_by_id.get(f.message_id)
                # R2: оценка-СИРОТА — ответ, к которому она привязана, не найден (беседа удалена
                # либо легаси-строка). Такую оценку в ПРИЁМОЧНУЮ МЕТРИКУ не берём: раньше она
                # учитывалась безусловно, и гейт 1.0 тихо искажался ответами, которых уже нет.
                # Считаем отдельно и показываем в «Сигналах качества» — потеря данных должна быть
                # видимой, а не молчаливой. Комментарии и исправления сохраняем в любом случае:
                # они ценны сами по себе (обучающий материал петли кейсов).
                orphan = orig is None
                ans_txt = orig.content if orig else ""
                snippet = (ans_txt[:220] + "…") if len(ans_txt) > 220 else ans_txt
                if f.rating is not None and orphan:
                    orphan_ratings += 1
                elif f.rating is not None:
                    ans_ratings.append(f.rating)
                    ans_ratings_by_role[u.role].append(f.rating)
                    region_acc[u_region]["ratings"].append(f.rating)
                    answer_rows.append({  # каждый оценённый ответ (триаж ≤2★ и экспорт-скоркард)
                        "user": u.username, "region": u_region, "role": u.role,
                        "rating": f.rating, "ts": _fmt(f.ts), "_sort_ts": f.ts,
                        "question": _question_for(orig, sess_msgs),
                        "answer": ans_txt,
                        "unverified": orig.unverified_json,
                        "low_relevance": bool(orig.low_relevance),
                        "comment": f.comment or "", "correction": f.correction or "",
                        "session_id": f.session_id,
                    })
                if f.correction:
                    corrections.append({"user": u.username, "region": u_region,
                                        "correction": f.correction, "ts": _fmt(f.ts), "answer": snippet})
                if f.comment:
                    answer_comments.append({"user": u.username, "region": u_region,
                                            "comment": f.comment, "ts": _fmt(f.ts), "answer": snippet})
            elif kind == "dialog" and f.comment:
                dialog_comments.append({"user": u.username, "region": u_region,
                                        "comment": f.comment, "ts": _fmt(f.ts)})

        g_prompt += u_prompt
        g_completion += u_completion
        total_requests += u_req
        total_conversations += len(order)
        u_tokens, u_cost = u_prompt + u_completion, cost_rub(u_prompt, u_completion)
        data.append({
            "username": u.username, "role": u.role, "region": u_region,
            "full_name": (u.full_name or "").strip(), "msg_count": len(msgs),
            "conversations": len(order), "requests": u_req,
            "sessions": sessions, "feedback": feedback, "tokens": u_tokens, "cost": u_cost,
        })
        per_user_stat.append({"username": u.username, "region": u_region,
                              "requests": u_req, "tokens": u_tokens, "cost": u_cost})
        ra = region_acc[u_region]
        ra["users"] += 1
        ra["requests"] += u_req
        ra["tokens"] += u_tokens
        ra["cost"] += u_cost

    # --- финальные агрегаты ------------------------------------------------
    per_user_stat.sort(key=lambda x: x["requests"], reverse=True)
    # Ключ per_day — ISO YYYY-MM-DD → сортировка строк уже хронологическая (в т.ч. через границу
    # месяца/года; старый ключ «%d.%m» сортировался неверно и коллизил дни). Ось подписываем «дд.мм».
    per_day_list = []
    for iso, v in sorted(per_day.items())[-14:]:
        _, mm, dd = iso.split("-")
        per_day_list.append({"date": f"{dd}.{mm}", "iso": iso,
                             "requests": v["requests"], "tokens": v["tokens"]})
    tokens_total, cost_total = g_prompt + g_completion, cost_rub(g_prompt, g_completion)
    star_dist = [(i, sum(1 for r in ans_ratings if r == i)) for i in range(5, -1, -1)]
    avg_stars = round(sum(ans_ratings) / len(ans_ratings), 2) if ans_ratings else None

    # Приёмка по регионам (доля ≥4★), отсортировано: сначала где оценок больше.
    region_stats = []
    for name, acc in region_acc.items():
        if not acc["ratings"] and acc["requests"] == 0:
            continue  # нет ни оценок, ни запросов — шум (напр. admin без региона): в разбивку не берём
        region_stats.append({
            "region": name or "— без региона —",
            "users": acc["users"], "requests": acc["requests"],
            "tokens": acc["tokens"], "cost": round(acc["cost"], 4),
            "answer_ratings": len(acc["ratings"]),
            "accept_pct": _accept_pct(acc["ratings"]),
        })
    region_stats.sort(key=lambda r: (r["answer_ratings"], r["requests"]), reverse=True)

    answer_rows.sort(key=lambda r: (r["_sort_ts"] or datetime.min), reverse=True)  # новые сверху
    bad_answers = sorted((r for r in answer_rows if r["rating"] <= BAD_RATING_MAX),
                         key=lambda b: b["rating"])  # триаж: худшие первыми (stable → внутри по ts desc)
    for r in answer_rows:  # bad_answers ссылается на те же dict-объекты → чистим один раз
        r.pop("_sort_ts", None)

    recent.sort(key=lambda r: (r["_ts"] or datetime.min), reverse=True)  # живая лента: новые сверху
    recent = recent[:14]
    for r in recent:
        r.pop("_ts", None)

    accept_pct = _accept_pct(ans_ratings)

    # --- тренды: текущее окно vs предыдущее равной длины (моментум использования) ---
    ref = (generated_at or datetime.now()).date()
    if date_from and date_to:
        cur_from, cur_to = date_from, date_to
        span = (cur_to - cur_from).days + 1
        prev_to = cur_from - timedelta(days=1)
        prev_from = prev_to - timedelta(days=span - 1)
        trend_label = f"период {span} дн. vs предыдущий"
    else:  # без фильтра дат — скользящая неделя к предыдущей
        cur_to, cur_from = ref, ref - timedelta(days=6)
        prev_to = cur_from - timedelta(days=1)
        prev_from = prev_to - timedelta(days=6)
        trend_label = "7 дней vs предыдущие 7"
    cur_w = {"req": 0, "p": 0, "c": 0, "users": set()}
    prev_w = {"req": 0, "p": 0, "c": 0, "users": set()}
    for m in all_msgs:  # тренд считает СВОИ окна (не завязан на фильтр дат представления)
        if m.user_id not in sel_ids or m.ts is None:
            continue
        d = m.ts.date()
        w = cur_w if cur_from <= d <= cur_to else (prev_w if prev_from <= d <= prev_to else None)
        if w is None:
            continue
        if m.role == "user":
            w["req"] += 1
            w["users"].add(m.user_id)
        else:
            w["p"] += m.prompt_tokens or 0
            w["c"] += m.completion_tokens or 0
    trends = {
        "requests": _fmt_trend(cur_w["req"], prev_w["req"], good_up=True),
        "users": _fmt_trend(len(cur_w["users"]), len(prev_w["users"]), good_up=True),
        "tokens": _fmt_trend(cur_w["p"] + cur_w["c"], prev_w["p"] + prev_w["c"], good_up=None),
        "cost": _fmt_trend(cost_rub(cur_w["p"], cur_w["c"]), cost_rub(prev_w["p"], prev_w["c"]), good_up=False),
    }

    stats = {
        "users_total": len(data),
        "users": sum(1 for x in data if x["role"] == "user"),
        "experts": sum(1 for x in data if x["role"] == "expert"),
        "admins": sum(1 for x in data if x["role"] == "admin"),
        "conversations": total_conversations,
        "requests": total_requests,
        "answers": total_answers,
        "tokens": tokens_total,
        "cost": cost_total,
        "avg_tokens": round(tokens_total / total_answers) if total_answers else 0,
        "avg_cost": round(cost_total / total_answers, 3) if total_answers else 0,
        "feedback_count": sum(len(x["feedback"]) for x in data),
        "avg_rating": round(sum(ratings) / len(ratings), 1) if ratings else None,
        "matched": matched_counts,
        "answer_ratings": len(ans_ratings),
        "avg_stars": avg_stars,
        "accept_pct": accept_pct,
        "accept_user": _accept_pct(ans_ratings_by_role.get("user", [])),
        "accept_user_n": len(ans_ratings_by_role.get("user", [])),
        "accept_expert": _accept_pct(ans_ratings_by_role.get("expert", [])),
        "accept_expert_n": len(ans_ratings_by_role.get("expert", [])),
        "gate": GATE_PCT,
        "gate_pass": accept_pct is not None and accept_pct >= GATE_PCT,
        "trends": trends,
        "trend_label": trend_label,
        "star_dist": star_dist,
        "regions": region_stats,
        "bad_answers": bad_answers,
        "bad_count": len(bad_answers),
        "answer_rows": answer_rows,
        "corrections": corrections,
        "answer_comments": answer_comments,
        "dialog_comments": dialog_comments,
        "procedural_questions": procedural_questions,
        "recent": recent,
        "demand_product": max(total_requests - len(procedural_questions), 0),
        "demand_procedural": len(procedural_questions),
        "flags_unverified": flags_unverified,
        "flags_lowrel": flags_lowrel,
        # Оценки, чей ответ не найден: в приёмку НЕ включены (R2). Ненулевое значение — сигнал,
        # что часть сигнала качества потеряна вместе с удалёнными беседами.
        "orphan_ratings": orphan_ratings,
        "per_user": per_user_stat,
        "per_day": per_day_list,
        "max_requests": per_user_stat[0]["requests"] if per_user_stat else 0,
        "max_cost": max((x["cost"] for x in per_user_stat), default=0),
        "max_day": max((d["requests"] for d in per_day_list), default=0),
        # эхо фильтров + значения для выпадашек (шаблон рисует панель фильтров)
        "filter_from": date_from.strftime("%Y-%m-%d") if date_from else "",
        "filter_to": date_to.strftime("%Y-%m-%d") if date_to else "",
        "filter_region": region,
        "filter_role": role,
        "filter_active": bool(date_from or date_to or region or role),
        "all_regions": all_regions,
        "all_roles": all_roles,
        "generated_at": (generated_at or datetime.now()).strftime("%d.%m.%Y %H:%M"),
    }
    totals = {"tokens": tokens_total, "cost": cost_total}
    return {"data": data, "totals": totals, "stats": stats}


def system_health() -> dict:
    """Оперативный статус инфраструктуры для дашборда (I/O — отдельно от чистого build_admin_view).
    Полностью защищён: недоступность Qdrant/сети → коллекции 'недоступно', страница НЕ падает."""
    from app.core.config import settings

    health = {
        "version": settings.APP_VERSION, "env": settings.APP_ENV,
        "server_time": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "deepseek": bool(settings.DEEPSEEK_API_KEY), "deepseek_model": settings.DEEPSEEK_MODEL,
        "qdrant_ok": False, "qdrant_url": settings.QDRANT_URL, "collections": [],
    }
    wanted = [
        ("Продукция 719", settings.QDRANT_COLLECTION),
        ("Правила реестра", settings.QDRANT_RULES_COLLECTION),
        ("Кейсы экспертов", settings.QDRANT_CASES_COLLECTION),
    ]
    try:
        from qdrant_client import QdrantClient
        client = QdrantClient(url=settings.QDRANT_URL, timeout=2)
        for label, name in wanted:
            try:
                pts = client.get_collection(name).points_count
                health["collections"].append({"label": label, "name": name, "points": pts, "ok": True})
            except Exception:
                health["collections"].append({"label": label, "name": name, "points": None, "ok": False})
        health["qdrant_ok"] = any(c["ok"] for c in health["collections"])
    except Exception:  # клиент даже не создался (нет пакета/адреса) — всё недоступно, но не падаем
        health["collections"] = [{"label": l, "name": n, "points": None, "ok": False} for l, n in wanted]
    return health

