"""Подготовка гейта `1.0` (#80) и листов разметки (#178, #175) — офлайн, без Qdrant и DeepSeek.

Замерный код опаснее продуктового: по его числам владелец назначит пороги гейта. Поэтому здесь
закреплено то, на чём числа держатся:
  * доля оценок ≥4★ считается ТЕМ ЖЕ правилом, что «Качество» админки (#166: последняя оценка
    пользователя на ответ, снятая не считается, сирота не считается) — сверка с `build_admin_view`;
  * эталон `gs_natural` берётся по последней оценке, только у позиции приложения и сверяется с
    текущим корпусом (номера позиций у редакций не вечны);
  * ничего с текстами вопросов не пишется в репозиторий — он публичный (152-ФЗ).

Запуск:  .venv\\Scripts\\python -m unittest tests.test_gate_prep
"""
from __future__ import annotations

import csv
import json
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

POS = "Приложение к ПП №719, Раздел IV, позиция 12"


def _src(anchor=POS, name="Насосы гидравлические", okpd2=("28.13.14",), kind=None):
    s = {"source_anchor": anchor, "product_name": name, "okpd2": list(okpd2), "section": "IV"}
    if kind:
        s["kind"] = kind
    return s


class _Db:
    """Синтетическая база со схемой боя (feedback с user_id / ts / kind)."""

    def __init__(self, tmp: str):
        self.path = Path(tmp) / "copy.db"
        db = sqlite3.connect(self.path)
        db.execute("create table messages (id integer primary key, session_id text, ts text, "
                   "role text, content text, sources_json text, low_relevance integer)")
        db.execute("create table feedback (id integer primary key, user_id integer, ts text, "
                   "kind text, message_id integer, rating integer)")
        db.commit()
        db.close()
        self.n = 0

    def pair(self, question: str, sources: list, low: int = 0) -> int:
        """Вопрос + ответ в своей беседе; возвращает id ответа."""
        db = sqlite3.connect(self.path)
        self.n += 1
        sid = f"s{self.n}"
        db.execute("insert into messages (session_id, ts, role, content, sources_json, low_relevance) "
                   "values (?,?,?,?,?,?)", (sid, f"2026-10-0{self.n % 9 + 1} 10:00", "user", question,
                                            None, 0))
        cur = db.execute("insert into messages (session_id, ts, role, content, sources_json, "
                         "low_relevance) values (?,?,?,?,?,?)",
                         (sid, f"2026-10-0{self.n % 9 + 1} 10:01", "assistant", "ответ",
                          json.dumps(sources, ensure_ascii=False), low))
        db.commit()
        mid = cur.lastrowid
        db.close()
        return mid

    def rate(self, mid: int, rating: int, ts: str, user: int = 1) -> None:
        db = sqlite3.connect(self.path)
        db.execute("insert into feedback (user_id, ts, kind, message_id, rating) values (?,?,?,?,?)",
                   (user, ts, "answer", mid, rating))
        db.commit()
        db.close()


class TestGsNaturalRatings(unittest.TestCase):
    """Эталон — ПОСЛЕДНЯЯ оценка пользователя (#166), снятая не считается."""

    def setUp(self):
        import build_gs_natural as m
        self.m = m
        self._tmp = tempfile.TemporaryDirectory()
        self.db = _Db(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_last_rating_wins_and_withdrawn_is_dropped(self):
        downgraded = self.db.pair("делаем гидравлические насосы для нефтепереработки", [_src()])
        self.db.rate(downgraded, 5, "2026-10-01 11:00")
        self.db.rate(downgraded, 2, "2026-10-01 12:00")      # переоценка вниз — эталона нет
        upgraded = self.db.pair("выпускаем насосы шестерённые для гидросистем", [_src()])
        self.db.rate(upgraded, 2, "2026-10-01 11:00")
        self.db.rate(upgraded, 5, "2026-10-01 12:00")        # переоценка вверх — эталон есть
        withdrawn = self.db.pair("производим насосы аксиально-поршневые серийно", [_src()])
        self.db.rate(withdrawn, 5, "2026-10-01 11:00")
        self.db.rate(withdrawn, 0, "2026-10-01 12:00")       # повторный клик — оценка снята
        cases, stats = self.m.collect(self.db.path, 4, "бой")
        self.assertEqual([c["query"] for c in cases], ["выпускаем насосы шестерённые для гидросистем"])
        self.assertEqual(stats["оценённых ответов"], 1)

    def test_disputed_answer_takes_the_lowest_user_rating(self):
        mid = self.db.pair("делаем насосы гидравлические для станков", [_src()])
        self.db.rate(mid, 3, "2026-10-01 11:00", user=2)     # несогласный оценил ПЕРВЫМ
        self.db.rate(mid, 5, "2026-10-01 11:05", user=1)
        cases, _ = self.m.collect(self.db.path, 4, "бой")
        self.assertEqual(cases, [], "оспоренный ответ эталоном не идёт, в каком бы порядке ни оценили")

    def test_withdrawn_rating_of_another_user_is_not_a_dispute(self):
        """Снятая оценка — «оценки нет», а не «0★»: она не оспаривает 5★ другого пользователя."""
        mid = self.db.pair("делаем насосы гидравлические для станков", [_src()])
        self.db.rate(mid, 5, "2026-10-01 11:00", user=1)
        self.db.rate(mid, 4, "2026-10-01 11:01", user=2)
        self.db.rate(mid, 0, "2026-10-01 11:02", user=2)     # второй снял свою оценку
        cases, _ = self.m.collect(self.db.path, 4, "бой")
        self.assertEqual(len(cases), 1)

    def test_old_schema_without_user_and_kind_still_reads(self):
        """Июльская база (тесты `test_eval_tools`) без колонок user_id / ts / kind."""
        path = Path(self._tmp.name) / "old.db"
        db = sqlite3.connect(path)
        db.execute("create table messages (id integer primary key, session_id text, ts text, "
                   "role text, content text, sources_json text, low_relevance integer)")
        db.execute("create table feedback (id integer primary key, message_id integer, rating integer)")
        db.executemany("insert into messages values (?,?,?,?,?,?,?)", [
            (1, "s1", "t1", "user", "делаем гидравлические насосы для нефтепереработки", None, 0),
            (2, "s1", "t2", "assistant", "ответ", json.dumps([_src()], ensure_ascii=False), 0)])
        db.execute("insert into feedback (message_id, rating) values (2, 5)")
        db.commit()
        db.close()
        cases, _ = self.m.collect(path, 4, "бой")
        self.assertEqual(len(cases), 1)


class TestGsNaturalAnchor(unittest.TestCase):
    """Якорь — только позиции приложения и только сверенный с текущим корпусом."""

    def setUp(self):
        import build_gs_natural as m
        self.m = m
        self._tmp = tempfile.TemporaryDirectory()
        self.db = _Db(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _one(self, sources, records):
        mid = self.db.pair("делаем гидравлические насосы для нефтепереработки", sources)
        self.db.rate(mid, 5, "2026-10-01 11:00")
        return self.m.collect(self.db.path, 4, "бой", records)

    def test_rule_point_is_never_a_position_anchor(self):
        rule = _src("Приказ ТПП РФ №52, п. 4.4.3", "Пункт приказа", ("28.13.14",), kind="rule")
        recs = [{"source_anchor": POS, "product_name": "Насосы гидравлические", "okpd2_codes": ["28.13.14"]}]
        cases, _ = self._one([rule, _src()], recs)
        self.assertEqual(cases[0]["expect"]["anchor"], POS)

    def test_source_without_code_is_not_a_position(self):
        recs = [{"source_anchor": POS, "product_name": "Насосы гидравлические", "okpd2_codes": ["28.13.14"]}]
        cases, _ = self._one([_src(okpd2=())], recs)
        self.assertEqual(cases, [])

    def test_renumbered_position_is_remapped_by_name(self):
        moved = "Приложение к ПП №719, Раздел IV, позиция 40"
        recs = [{"source_anchor": POS, "product_name": "Кронблоки для буровых судов", "okpd2_codes": ["30.11"]},
                {"source_anchor": moved, "product_name": "Насосы  гидравлические", "okpd2_codes": ["28.13.14"]}]
        cases, stats = self._one([_src()], recs)
        self.assertEqual(cases[0]["expect"]["anchor"], moved)
        self.assertEqual(stats["якорь перепривязан по наименованию"], 1)

    def test_vanished_position_is_dropped(self):
        recs = [{"source_anchor": POS, "product_name": "Кронблоки для буровых судов", "okpd2_codes": ["30.11"]}]
        cases, stats = self._one([_src()], recs)
        self.assertEqual(cases, [])
        self.assertEqual(stats["отброшено: якорь устарел"], 1)

    def test_same_name_twice_is_resolved_by_code_or_dropped(self):
        a = "Приложение к ПП №719, Раздел IV, позиция 40"
        b = "Приложение к ПП №719, Раздел IX, позиция 7"
        recs = [{"source_anchor": POS, "product_name": "Другое", "okpd2_codes": ["30.11"]},
                {"source_anchor": a, "product_name": "Насосы гидравлические", "okpd2_codes": ["28.13.14"]},
                {"source_anchor": b, "product_name": "Насосы гидравлические", "okpd2_codes": ["26.51"]}]
        self.assertEqual(self.m.resolve_anchor(_src(), recs), (a, "перепривязан"))
        recs[2]["okpd2_codes"] = ["28.13.14"]
        self.assertEqual(self.m.resolve_anchor(_src(), recs), (None, "неоднозначен"))


class TestNoTextsInRepo(unittest.TestCase):
    """Набор, листы и детали с текстами вопросов в репозиторий не пишутся: он публичный."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = _Db(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _argv(self, mod, *args):
        with unittest.mock.patch.object(sys, "argv", [mod.__file__, *args]):
            with self.assertRaises(SystemExit) as cm:
                mod.main()
        return str(cm.exception)

    def test_gs_natural_out_inside_repo_refused(self):
        import build_gs_natural as m
        msg = self._argv(m, "--db", str(self.db.path), "--out", str(ROOT / "scripts" / "x.json"))
        self.assertIn("ВНЕ репозитория", msg)

    def test_eval_gs_natural_refuses_set_inside_repo(self):
        import eval_gs_natural as m
        msg = self._argv(m, "--set", str(ROOT / "scripts" / "x.json"))
        self.assertIn("вне репозитория", msg)

    def test_labeling_out_dir_inside_repo_refused(self):
        import build_labeling_sheets as m
        msg = self._argv(m, "--db", str(self.db.path), "--out-dir", str(ROOT / "docs"))
        self.assertIn("вне репозитория", msg)

    def test_gate_ratings_db_inside_repo_refused(self):
        import gate_ratings as m
        inside = ROOT / "scripts" / "eval_golden.json"      # существующий файл внутри дерева
        msg = self._argv(m, "--db", str(inside))
        self.assertIn("ВНУТРИ репозитория", msg)


class TestGateRatings(unittest.TestCase):
    """Часть (в): та же выборка и та же доля, что «Качество» `/admin`."""

    @classmethod
    def setUpClass(cls):
        import gate_ratings as m
        cls.m = m

    def test_wilson(self):
        self.assertEqual(self.m.wilson(0, 0), (0.0, 0.0))
        lo, hi = self.m.wilson(5, 10)
        self.assertAlmostEqual(lo + hi, 1.0, places=6)
        self.assertTrue(0.2 < lo < 0.25 and 0.75 < hi < 0.8)
        self.assertEqual(self.m.min_n_for_halfwidth(0.5, 0.1), 97)

    def test_branch_of(self):
        b = self.m.branch_of
        ns = lambda corpus=None, src=None: SimpleNamespace(  # noqa: E731
            corpus=corpus, sources_json=json.dumps(src or [], ensure_ascii=False))
        self.assertEqual(b(ns("appendix|auto"), ""), "товарная")
        self.assertEqual(b(ns("rules|rules", [_src()]), ""), "процедурная", "корпус ответа важнее источников")
        self.assertEqual(b(ns(None, [_src("п. 7", "", (), kind="rule")]), ""), "процедурная")
        self.assertEqual(b(ns(None, [_src()]), ""), "товарная")
        self.assertEqual(b(ns(None, []), "какой порядок внесения продукции в реестр"), "процедурная")
        self.assertEqual(b(ns(None, []), "спасибо"), "прочее")

    def test_same_sample_and_share_as_admin_quality(self):
        """Своя выборка обязана совпасть с `build_admin_view`: вторая реализация метрики,
        разошедшаяся с первой, хуже отсутствующей."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.api.admin_stats import build_admin_view
        from app.db import queries as q
        from app.db.models import Base, Feedback, Message, User

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        t0 = datetime(2026, 10, 1, 9, 0)
        db.add_all([User(id=1, username="u1", password_hash="x", role="user"),
                    User(id=2, username="e1", password_hash="x", role="expert")])
        mid = 0
        plan = [  # (кто, [оценки по порядку]) — на отдельный ответ каждая строка
            (1, [5]), (1, [3, 4]), (1, [4, 2]), (1, [5, 0]), (2, [1]), (2, [4]), (2, [5, 5]),
        ]
        for uid, ratings in plan:
            mid += 2
            sid = f"s{mid}"
            db.add(Message(id=mid - 1, user_id=uid, session_id=sid, ts=t0 + timedelta(minutes=mid),
                           role="user", content="вопрос о насосах"))
            db.add(Message(id=mid, user_id=uid, session_id=sid, ts=t0 + timedelta(minutes=mid, seconds=5),
                           role="assistant", content="ответ",
                           sources_json=json.dumps([_src()], ensure_ascii=False)))
            for j, r in enumerate(ratings):
                db.add(Feedback(user_id=uid, kind="answer", message_id=mid, rating=r,
                                ts=t0 + timedelta(hours=1, minutes=mid * 3 + j)))
        db.add(Feedback(user_id=1, kind="answer", message_id=999, rating=5, ts=t0))  # сирота
        db.commit()

        rows = self.m.counted_ratings(q.list_users(db), q.get_all_messages(db), q.get_all_feedback(db))
        admin = build_admin_view(db)["stats"]
        self.assertEqual(len(rows), admin["answer_ratings"])
        self.assertEqual(sorted(r["rating"] for r in rows), [1, 2, 4, 4, 5, 5])
        s = self.m.share(rows)
        self.assertEqual(round(100 * s["ok"] / s["n"]), admin["accept_pct"])
        self.assertEqual({r["role"] for r in rows}, {"user", "expert"})
        db.close()


class TestEvalGsNaturalSummary(unittest.TestCase):

    def test_rates_are_over_all_cases_not_only_planned(self):
        import eval_gs_natural as m
        rows = [{"id": "a", "has_code": False, "route": "товарная", "target": True, "r1": True,
                 "r3": True, "window": True},
                {"id": "b", "has_code": False, "route": "ушёл бы в модель до плана"},
                {"id": "c", "has_code": True, "route": "товарная", "target": False, "r1": False,
                 "r3": True, "window": True}]
        s = m.summarize(rows)
        self.assertEqual(s["без кода"]["n"], 2)
        self.assertEqual(s["без кода"]["target"], {"hit": 1, "rate": 0.5},
                         "кейс, ушедший не в товарную ветку, — промах, а не исключение из выборки")
        self.assertEqual(s["без кода"]["не товарная"], 1)
        self.assertEqual(s["всего"]["r3"]["hit"], 2)

    def test_paid_call_is_an_error_not_a_bill(self):
        import eval_gs_natural as m
        with self.assertRaises(m.PaidCall):
            m._no_llm()


class TestLabelingSheets(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import build_labeling_sheets as m
        cls.m = m

    def test_ev81_takes_codes_with_three_or_more_positions(self):
        rec = lambda i, code: {"source_anchor": f"A{i}", "okpd2_codes": [code]}  # noqa: E731
        idx = self.m.code_index([rec(1, "26.51.12"), rec(2, "26.51.12"), rec(3, "26.51.12"),
                                 rec(4, "28.13.14"), rec(5, "28.13.14")])
        qs = [{"src": "chat", "mid": 1, "q": "требования по 26.51.12 к теодолитам"},
              {"src": "chat", "mid": 2, "q": "насосы 28.13.14"},
              {"src": "guest", "mid": 3, "q": "что нужно для внесения в реестр"}]
        picked, stats = self.m.ev81_rows(qs, idx)
        self.assertEqual([(p["mid"], p["code"], len(p["recs"])) for p in picked], [(1, "26.51.12", 3)])
        self.assertEqual(stats["вопросов с кодом"], 2)

    def test_significant_stems_drop_digits_and_frequent_words(self):
        stems = self.m.significant_stems("Требования к полотенцам 2024 года", stop={"требован"})
        self.assertNotIn("требован", stems)
        self.assertFalse(any(ch.isdigit() for s in stems for ch in s))
        self.assertTrue(any(s.startswith("полотенц") for s in stems))

    def test_csv_opens_in_russian_excel(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "sheet.csv"
            self.m.write_csv(f, ["инструкция; с точкой с запятой"], ["№", "Вопрос"],
                             [[1, "вопрос; с разделителем\nи переносом"]])
            self.assertEqual(f.read_bytes()[:3], b"\xef\xbb\xbf", "BOM — иначе Excel откроет кракозябры")
            with f.open(encoding="utf-8-sig") as fh:
                rows = list(csv.reader(fh, delimiter=";"))
            self.assertEqual(rows[0], ["инструкция; с точкой с запятой"])
            self.assertEqual(rows[2], ["№", "Вопрос"])
            self.assertEqual(rows[3][1], "вопрос; с разделителем\nи переносом")

    def test_k18_pool_is_product_branch_without_any_code(self):
        qs = [{"src": "chat", "mid": i, "q": q} for i, q in enumerate([
            "выпускаем полотенца махровые из хлопка",            # берём
            "требования по 26.51.12 к теодолитам",               # код ОКПД2
            "условия по коду 8544 70 000 0 для кабеля",           # код ТН ВЭД
            "какой порядок внесения продукции в реестр",          # процедурный гейт
            "да, покажи подробнее про это",                       # продолжение
            "привет",                                             # коротко / meta
        ])]
        self.assertEqual([p["mid"] for p in self.m.k18_pool(qs)], [0])

    def test_tnved_code_question_is_not_in_k18_pool(self):
        self.assertTrue(self.m.TNVED_RE.search("условия по коду 8544 70 000 0 для кабеля"))
        self.assertTrue(self.m.TNVED_RE.search("ТН ВЭД 8471300000 ноутбуки"))
        self.assertFalse(self.m.TNVED_RE.search("требования по 26.51.12 к теодолитам"))
        self.assertFalse(self.m.TNVED_RE.search("выпускаем полотенца махровые"))


if __name__ == "__main__":
    unittest.main()
