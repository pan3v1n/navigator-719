"""D14 #141: иерархия ОКПД2 поразрядная, а не посегментная; целевая — ближайшая по коду позиция.

Бой, сентябрь 2026, два эксперта: н-бутан 20.14.11.112 → «требования 20.14.11.110 применять нельзя»,
хладон 20.14.11.122 → катализаторы. Корень — два дефекта на одном пути:
  1) `okpd2_match` сравнивал коды по сегментам и не видел, что «…YZ0» — группировка «…YZW»
     (а также подгруппа «XX.XX.X» и подкласс «XX.X» — замер `scripts/diag_okpd2_climb.py`);
  2) среди совпавших по коду целевой становилась первая по score, а не ближайшая по коду:
     14 «Катализаторов» с кодом класса «20» обходили подкатегорию 20.14.11.110.
Офлайн: Qdrant и ключ не нужны (`_hybrid`, `embed_query`, `_client` подменены)."""
from __future__ import annotations

import json
import random
import unittest
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.rag import okpd2_ref, pipeline, retriever
from app.rag.retriever import Hit, code_relation, okpd2_match

ROOT = Path(__file__).resolve().parents[1]


def _corpus_codes() -> list[str]:
    out = []
    for f in sorted((ROOT / "knowledge_base" / "pp719" / "structured").glob("*.json")):
        for r in json.loads(f.read_text(encoding="utf-8")):
            out += r.get("okpd2_codes") or []
    return sorted(set(out))


def _old_match(rec: str, q: str) -> bool:
    """Правило до D14 — посегментный префикс в любую сторону (копия для проверки надмножества)."""
    a = [s for s in rec.split(".") if s]
    b = [s for s in q.split(".") if s]
    n = min(len(a), len(b))
    return bool(n) and a[:n] == b[:n]


def _seg_prefixes(code: str) -> list[str]:
    s = [x for x in code.split(".") if x]
    return [".".join(s[: i + 1]) for i in range(len(s))]


class TestHierarchy(unittest.TestCase):
    def test_key_strips_only_ninth_level_padding(self):
        self.assertEqual(okpd2_ref.okpd2_key("20.14.11.110"), "20141111")
        self.assertEqual(okpd2_ref.okpd2_key("27.12.31.000"), "271231")
        self.assertEqual(okpd2_ref.okpd2_key("01.11.31.100"), "0111311")
        # ⚠ ноль вида — разряд, а не заполнитель: «27.12.10» не равен подгруппе «27.12.1»
        self.assertEqual(okpd2_ref.okpd2_key("27.12.10"), "271210")
        self.assertEqual(okpd2_ref.okpd2_key("не код"), "")

    def test_relation_on_expert_cases(self):
        rel = okpd2_ref.okpd2_relation
        self.assertEqual(rel("20.14.11.110", "20.14.11.112"), 1)     # н-бутан → подкатегория
        self.assertEqual(rel("20.14.11.120", "20.14.11.122"), 1)     # хладон (пропилен)
        self.assertEqual(rel("20.14.11.112", "20.14.11.110"), -1)    # обратное — потомок
        self.assertEqual(rel("27.12.31", "27.12.31.000"), 0)         # «…000» — тот же вид
        self.assertEqual(rel("27.3", "27.32.13.110"), 5)             # подкласс: «273» → «27321311»
        self.assertEqual(rel("29.10.2", "29.10.21.000"), 1)          # подгруппа: легковые
        # по разрядам «…131» — подкатегория «…13», а не «…12»: подъёма к 120 нет
        self.assertIsNone(rel("20.14.11.120", "20.14.11.131"))
        self.assertIsNone(rel("27.12.10", "27.12.11"))               # соседние виды
        self.assertTrue(okpd2_match(["20.14.11.110"], "20.14.11.112"))
        self.assertTrue(okpd2_match(["29.20.23"], "29.20.23.110"))   # прежний подъём цел

    def test_new_rule_is_superset_of_old_on_whole_classifier(self):
        """Ни одна связь, которую видело старое правило, не потеряна (весь справочник × корпус)."""
        corpus = _corpus_codes()
        by_class: dict[str, list[str]] = {}
        for c in corpus:
            by_class.setdefault(c[:2], []).append(c)
        lost = [(rc, q) for q in okpd2_ref._okpd2_names() for rc in by_class.get(q[:2], [])
                if _old_match(rc, q) and not okpd2_match([rc], q)]
        self.assertEqual(lost, [])

    def test_filter_forms_agree_with_relation(self):
        """⚠ ДВЕ ТАБЛИЦЫ ПРО ОДНО: формы для фильтра Qdrant (`okpd2_codes` ∋ предок) обязаны находить
        РОВНО те позиции, которые `okpd2_relation` считает предками (и тем же кодом). Иначе
        отношение верное, а добор слепой."""
        corpus = _corpus_codes()
        names = list(okpd2_ref._okpd2_names())
        sample = random.Random(141).sample(names, 3000) + [
            "20.14.11.112", "20.14.11.122", "27.12.31.000", "29.10.21", "27.3", "20.60.1"]
        bad = []
        for q in sample:
            anc = set(okpd2_ref.okpd2_ancestor_forms(q))
            for rc in corpus:
                if rc[:2] != q[:2]:
                    continue
                r = okpd2_ref.okpd2_relation(rc, q)
                if (rc in anc) != (r is not None and r >= 0):
                    bad.append(("предок", rc, q))
        self.assertEqual(bad[:10], [])

    def test_okpd2_name_fallback_goes_to_nearest_ancestor(self):
        names = okpd2_ref._okpd2_names()
        self.assertEqual(okpd2_ref.okpd2_name("20.14.11.118"), names["20.14.11.110"])
        self.assertIsNone(okpd2_ref.okpd2_name("00.00.00"))


def _hit(anchor, codes, score, match=True, name=None):
    return Hit(score=score, section_roman="XXI", section_title="т", product_name=name or anchor,
               okpd2_codes=codes, min_threshold=None, requirement_blocks=[], source_anchor=anchor,
               okpd2_match=match)


class TestTargetChoice(unittest.TestCase):
    def test_nearest_ancestor_beats_higher_score(self):
        hits = [_hit("катализатор", ["20"], 0.9), _hit("подкатегория", ["20.14.11.110"], 0.2)]
        self.assertEqual([h.source_anchor for h in pipeline.target_hits(hits, "20.14.11.112")],
                         ["подкатегория"])

    def test_exact_beats_nearer_ancestor(self):
        hits = [_hit("категория", ["26.11.22.210"], 0.9), _hit("своя", ["26.11.22.216"], 0.1)]
        self.assertEqual([h.source_anchor for h in pipeline.target_hits(hits, "26.11.22.216")],
                         ["своя"])

    def test_padded_vid_is_exact(self):
        hits = [_hit("реле", ["27.12"], 0.9), _hit("панели", ["27.12.31", "27.12.32"], 0.1)]
        self.assertEqual([h.source_anchor for h in pipeline.target_hits(hits, "27.12.31.000")],
                         ["панели"])

    def test_all_exact_records_stay_targets_across_spellings(self):
        """Точное совпадение — по ключу, и целевыми остаются ВСЕ точные записи, как до D14
        (у одного кода бывает несколько позиций): «27.12.31» и «27.12.31.000» — один вид."""
        hits = [_hit("А", ["27.12.31"], 0.9), _hit("Б", ["27.12.31.000"], 0.5), _hit("реле", ["27.12"], 0.4)]
        self.assertEqual([h.source_anchor for h in pipeline.target_hits(hits, "27.12.31.000")], ["А", "Б"])

    def test_only_descendants_keep_rank_order(self):
        """Код шире позиций («28.13») — выбор прежний: первая совпавшая по рангу."""
        hits = [_hit("насос А", ["28.13.14.110"], 0.9), _hit("насос Б", ["28.13.1"], 0.1)]
        self.assertEqual([h.source_anchor for h in pipeline.target_hits(hits, "28.13")], ["насос А"])

    def test_broad_code_keeps_pre_d14_ranking(self):
        """УЗКИЙ D14 (решение владельца 08.10): широкий код («20.14») — ближайшего предка не поднимаем
        (потомки есть), остальное ПО РАНГУ, как до D14. Раунд 2 ставил потомков выше предков — замер
        показал цену (21.20.10.110 → медизделие, «Мониторы» → «Гидрофоны»): score разных запросов были
        несравнимы (#179), и порядок широких кодов этим правилом не чинился. На сравнимых score (R31)
        перемерено (#228): «потомки выше» дают 10 «сломано» в climb — правило держится на смысле."""
        hits = [_hit(f"кат{i}", ["20"], 0.9 - i / 100) for i in range(5)] +                [_hit("углеводороды", ["20.14.11.110"], 0.3)]
        self.assertEqual([h.source_anchor for h in pipeline.target_hits(hits, "20.14")], ["кат0"])
        self.assertEqual(retriever.code_tiers([h.okpd2_codes for h in hits], "20.14"), [3] * 6)

    def test_grouping_position_wins_inside_nearest_tier(self):
        """Ревью PR #177, раунд 3: у 28.25.11.110 три позиции — «Теплообменники» (XV/5, сама
        группировка) и две с названными товарами (III/177, III/178). Для «кожухотрубчатых»
        28.25.11.111 целевая — группировка, даже если у пищевой аппаратуры score больше."""
        hits = [_hit("III/177", ["28.25.11.110"], 0.9,
                     name="Аппараты теплообменные пластинчатые для пищевой промышленности"),
                _hit("XV/5", ["28.25.11.110"], 0.1, name="Теплообменники")]
        self.assertEqual([h.source_anchor for h in pipeline.target_hits(hits, "28.25.11.111")], ["XV/5"])

    def test_scope_qualifiers_stay_both_without_sibling_swap(self):
        """Ревью PR #177, раунд 3: «Светодиоды прочие» 26.11.22.219 поднимаются к двум позициям
        26.11.22.210 с противоположными областями. Код область не различает — целевые ОБЕ, и без
        подмены «в части белого» на 26.11.22.216 «Светодиоды белого диапазона»."""
        white = _hit("IV/3", ["26.11.22.216"], 0.4, name="Светодиоды белого диапазона", match=False)
        # ⚠ У белых в корпусе 15 операций — без них подмена сиблингом не срабатывала бы вовсе, и тест
        # её не видел (мутация «подмена и у квалификаторов» выжила).
        white.requirement_blocks = [{"component": "операции", "operations": [
            {"text": f"операция {i}", "points": 10} for i in range(15)]}]
        hits = [_hit("IV/2", ["26.11.22.210"], 0.9, name="Светодиоды (в части светодиодов белого диапазона)"),
                _hit("IV/5", ["26.11.22.210"], 0.5, name="Светодиоды (за исключением светодиодов белого диапазона)"),
                white]
        self.assertEqual(sorted(h.source_anchor for h in pipeline.target_hits(hits, "26.11.22.219")),
                         ["IV/2", "IV/5"])

    def test_subgroup_ancestor_competes_with_descendants_by_rank(self):
        """Замер радиуса 08.10: «потомки выше ВСЕХ предков» отдавало «Препараты для лечения ЖКТ»
        21.20.10.110 медизделию VII/73 (подкод .111) вместо VIII/1 «Препараты лекарственные»
        (21.20.1). Предок от подгруппы — содержательная группировка: с потомком по рангу."""
        drugs = _hit("VIII/1", ["21.10.51", "21.20.1"], 0.9, name="Препараты лекарственные, сыворотки и вакцины")
        device = _hit("VII/73", ["21.20.10.111"], 0.3, name="Рассасывающиеся гемостатические материалы")
        self.assertEqual([h.source_anchor for h in pipeline.target_hits([drugs, device], "21.20.10.110")],
                         ["VIII/1"])
        self.assertEqual(retriever.code_tiers([drugs.okpd2_codes, device.okpd2_codes], "21.20.10.110"), [3, 3])

    def test_ancestor_gives_one_record(self):
        """Без точного кода — ОДНА целевая. Находку раунда 1 («предок отдаёт все записи») раунд 2
        опроверг: под одним кодом разные изделия, а записи 26.11.22.210 — противоположные области."""
        split = [_hit("210a", ["26.11.22.210"], 0.9), _hit("210b", ["26.11.22.210"], 0.5)]
        self.assertEqual([h.source_anchor for h in pipeline.target_hits(split, "26.11.22.219")], ["210a"])


def _point(anchor, codes, score):
    return SimpleNamespace(id=anchor, score=score, payload={
        "section_roman": "XXI", "section_title": "т", "product_name": anchor,
        "okpd2_codes": codes, "source_anchor": anchor, "requirement_blocks": []})


CAT = "Углеводороды ациклические насыщенные"      # официальное имя 20.14.11.110 — позиция-группировка


class TestSearchFetchesNewRelations(unittest.TestCase):
    """`search` добирает новые связи отдельными запросами и ставит ближайшего предка первым."""

    def _run(self, code, pool_has_category=False, corpus=None, hidden=(CAT,), query="Н-бутан очищенный",
             fail_must=False, inflate=False, rescore=None, sparse=None):
        """Подмена `_hybrid` ФИЛЬТРУЕТ маленький корпус по правилам Qdrant (MatchAny по
        `okpd2_codes` и по посегментным префиксам, `must` и `should`), а не отвечает заготовкой:
        иначе тест проверял бы подмену. Катализаторы класса «20» — score выше, чем у подкатегории.
        `hidden` — позиции, которые текстом пул не находит; `fail_must` — новые доборы (фильтр
        `must`) падают, как упал бы Qdrant; функция от значений фильтра — падают только те, на
        которых она истинна."""
        calls = []
        corpus = corpus or [(f"катализатор {i}", ["20"], 0.9 - i / 100) for i in range(14)] + \
            [(CAT, ["20.14.11.110"], 0.05)]

        def cond_ok(codes, cond):
            field = codes if cond.key == "okpd2_codes" else [p for c in codes for p in _seg_prefixes(c)]
            return bool(set(cond.match.any) & set(field))

        def ok(codes, qfilter):
            if qfilter is None:
                return True
            if qfilter.must:
                return all(cond_ok(codes, c) for c in qfilter.must)
            return any(cond_ok(codes, c) for c in qfilter.should or [])

        self.rescores = []
        self.rescore_payload = []

        self.rescore_requests = []

        def fake_batch(collection_name, requests):
            # R31: пересчёт score окна — dense и sparse по id одним пакетом. Подмена отвечает КАЖДОМУ
            # запросу по его `using`, фильтру и `limit`, как Qdrant (ревью PR #248, MED-3: прежняя читала
            # фильтр первого и отдавала всё — «только dense», «sparse без фильтра», чужой `limit` и
            # перепутанный `using` проходили). dense — score «настоящие» (третий элемент строки
            # корпуса), `rescore` подменяет его (сбой, неполный ответ); sparse по умолчанию пуст,
            # `sparse` — функция строк → точки. Без фильтра запрос видит весь корпус.
            self.rescore_requests.append(requests)
            self.rescore_payload.append([r.with_payload for r in requests])
            out = []
            for req in requests:
                ids = [i for c in (getattr(req.filter, "must", None) or []) for i in (getattr(c, "has_id", None) or [])]
                rows = [r for r in corpus if r[0] in ids] if req.filter is not None else list(corpus)
                if req.using == retriever.DENSE:
                    self.rescores.append(ids)
                    pts = rescore(rows) if rescore is not None else \
                        [_point(*r) for r in sorted(rows, key=lambda r: -r[2])]
                elif req.using == retriever.SPARSE:
                    pts = sparse(rows) if sparse else []
                else:
                    raise ValueError(f"нет вектора {req.using!r}")    # Qdrant ответил бы 400
                out.append(SimpleNamespace(points=pts[:req.limit]))
            return out

        def fake_hybrid(query, limit, qfilter=None, collection=None, qvec=None, with_payload=True):
            conds = (getattr(qfilter, "should", None) or []) + (getattr(qfilter, "must", None) or [])
            calls.append([v for cond in conds for v in cond.match.any])
            if fail_must and getattr(qfilter, "must", None) and (
                    fail_must is True or fail_must(calls[-1])):
                raise RuntimeError("Qdrant недоступен")
            rows = [r for r in corpus if ok(r[1], qfilter)]
            if qfilter is None and not pool_has_category:
                rows = [r for r in rows if r[0] not in hidden]    # текстом пул их не находит
            rows = sorted(rows, key=lambda r: -r[2])[:limit]
            if inflate and qfilter is not None:
                # RRF ранговый: первая позиция МАЛЕНЬКОЙ отфильтрованной выдачи получает максимальный
                # score при любом тексте (#179) — так и отвечает подмена добора.
                return [_point(r[0], r[1], 0.99 - k / 100) for k, r in enumerate(rows)]
            return [_point(*r) for r in rows]

        with mock.patch.object(retriever, "_hybrid", side_effect=fake_hybrid),                 mock.patch.object(retriever, "embed_query", return_value=[0.0] * 8),                 mock.patch.object(retriever, "_client",
                                  return_value=SimpleNamespace(query_batch_points=fake_batch)):
            hits = retriever.search(query, okpd2=code, limit=8)
        return hits, calls

    def test_broad_code_with_descendant_fetches_no_ancestors(self):
        """Ревью PR #177, раунд 4 (HIGH), замер 09.10: «Мониторы» 26.40.34 — потомок 26.40.34.110 в
        выдаче есть, а добор новых предков приносил «Гидрофоны» 26.40.3 с высоким score маленькой
        выдачи (#179), и они становились целевой вместо мониторов. Предков добирают, только когда
        потомков среди совпавших нет — у широкого кода новых предков в окне нет, как на бою."""
        corpus = [("Гидрофоны", ["26.40.3"], 0.95), ("Мониторы", ["26.40.34.110"], 0.5),
                  ("Прочее", ["28.13.14.110"], 0.7)]
        hits, calls = self._run("26.40.34", corpus=corpus, hidden=("Гидрофоны",),
                                query="Мониторы и проекторы")
        self.assertEqual(hits[0].source_anchor, "Мониторы")
        self.assertNotIn("Гидрофоны", [h.source_anchor for h in hits])
        self.assertTrue(all("26.40.3" not in c for c in calls[2:]), calls)

    def test_failed_new_fetch_keeps_the_answer(self):
        """Раунд 4: новые доборы уточняют окно; сбой Qdrant на них не роняет поиск — окно остаётся
        прежним (до D14), без подкатегории."""
        hits, calls = self._run("20.14.11.112", fail_must=True)
        self.assertGreaterEqual(len(calls), 3)            # новый добор был и упал
        self.assertNotIn(CAT, [h.source_anchor for h in hits])
        self.assertEqual(hits[0].source_anchor, "катализатор 0")

    def test_broad_code_gets_no_new_descendant_fetch(self):
        """Узкий D14: новые запросы добирают только точные формы и новых ПРЕДКОВ, потомков — нет; у
        «20.14» окно собирается как до D14 (прежний добор не изменился)."""
        hits, calls = self._run("20.14")
        self.assertNotIn(CAT, [h.source_anchor for h in hits])
        self.assertEqual(hits[0].source_anchor, "катализатор 0")      # по score, как до D14
        self.assertTrue(all("20.14.11.110" not in c for c in calls[2:]))

    def test_subcode_pulls_and_ranks_its_category_first(self):
        hits, calls = self._run("20.14.11.112")
        self.assertEqual(hits[0].source_anchor, CAT)
        self.assertEqual(hits[0].code_tier, 1)            # позиция — сама группировка
        self.assertTrue(all(h.okpd2_match for h in hits))
        # прежний добор не изменился: в нём нет новых форм; подкатегорию приносит новый запрос
        self.assertNotIn("20.14.11.110", calls[1])
        self.assertTrue(any("20.14.11.110" in c for c in calls[2:]))

    def test_class_code_makes_no_extra_call(self):
        _hits, calls = self._run("20")
        self.assertEqual(len(calls), 2)        # «Катализаторы» с кодом «20» — точные: новых запросов нет

    def test_exact_position_found_makes_no_extra_call(self):
        """Ревью PR #177: запрос новых связей (~43 мс) не нужен, когда точная позиция уже найдена."""
        hits, calls = self._run("20.14.11.110", pool_has_category=True)
        self.assertEqual(hits[0].source_anchor, CAT)
        self.assertEqual(len(calls), 2)

    def test_exact_position_crowded_out_is_recovered(self):
        """Прежний добор делит лимит 12 между предками и потомками: 14 катализаторов класса «20» с
        большим score вытесняли из него точную позицию. Запрос ТОЧНЫХ форм кода её возвращает, и
        предков после этого не спрашиваем."""
        hits, calls = self._run("20.14.11.110")
        self.assertEqual(hits[0].source_anchor, CAT)
        self.assertEqual(hits[0].code_tier, 0)
        self.assertEqual(len(calls), 3)

    def test_odd_key_code_gets_no_descendant_fetch(self):
        """Ревью PR #177, раунд 4 (второй ревьюер, MED-1): запрет «потомков новых уровней не
        добираем» проверялся только на «20.14» — у ключа ЧЁТНОЙ длины поразрядные формы потомков
        совпадают с прежним посегментным добором, и третий добор раунда 3 тест не различал. У
        21.20.10.110 (ключ нечётной длины, своей позиции нет) медизделие VII/73 «Рассасывающиеся
        гемостатические материалы» (21.20.10.111) приходит ТОЛЬКО таким добором — и с высоким score
        маленькой выдачи становилось целевой вместо VIII/1 «Препараты лекарственные» (отчёт §5.1)."""
        corpus = [("Препараты лекарственные", ["21.20.1"], 0.6),
                  ("Рассасывающиеся гемостатические материалы", ["21.20.10.111"], 0.9)]
        hits, _calls = self._run("21.20.10.110", corpus=corpus,
                                 hidden=("Рассасывающиеся гемостатические материалы",),
                                 query="Препараты для лечения заболеваний пищеварительного тракта")
        self.assertNotIn("Рассасывающиеся гемостатические материалы", [h.source_anchor for h in hits])
        self.assertEqual(hits[0].source_anchor, "Препараты лекарственные")

    def test_descendant_from_prefix_fetch_also_blocks_ancestors(self):
        """Раунд 4 (LOW-1): у 26.40.34 и 28.13.14 на бою потомков приносит ПРЕЖНИЙ добор, а не пул.
        Условие «предков без потомков» обязано видеть и их: пул из 40 посторонних, мониторы — только
        прежним добором, и «Гидрофоны» всё равно не добираются."""
        corpus = [(f"посторонняя {i}", ["99.99"], 0.99 - i / 1000) for i in range(40)] + [
            ("Гидрофоны", ["26.40.3"], 0.95), ("Мониторы", ["26.40.34.110"], 0.5)]
        hits, calls = self._run("26.40.34", corpus=corpus, hidden=("Гидрофоны", "Мониторы"),
                                query="Мониторы и проекторы")
        self.assertEqual(hits[0].source_anchor, "Мониторы")
        self.assertNotIn("Гидрофоны", [h.source_anchor for h in hits])
        self.assertTrue(all("26.40.3" not in c for c in calls[2:]), calls)

    def test_failed_exact_fetch_skips_ancestors(self):
        """Раунд 4 (LOW-3): упал только запрос ТОЧНЫХ форм — предков не спрашиваем. Иначе своя
        позиция кода могла не дойти, а группировка-предок стала бы целевой со строкой «входит в
        группировку» (`_group_notes` полагается на добор точных форм)."""
        hits, calls = self._run("20.14.11.112", fail_must=lambda vals: "20.14.11.112" in vals)
        self.assertEqual(len(calls), 3, calls)            # пул, прежний добор, упавший точный
        self.assertNotIn(CAT, [h.source_anchor for h in hits])


class TestRescoreR31(unittest.TestCase):
    """R31 #179: score кандидатов из разных запросов несравнимы — окно пересчитывается одним запросом.

    Широкий код «26.40»: «Мониторы» (26.40.34.110) пул находит сам по тексту, «Приёмники» (26.40.20)
    — только добор по ветке, и маленькая отфильтрованная выдача даёт им score 0.99 против 0.5 у
    мониторов. Оба — потомки кода, ярус один, порядок решает score; до R31 первыми шли «Приёмники»."""

    _run = TestSearchFetchesNewRelations._run          # та же подмена Qdrant, без повтора её тестов
    CORPUS = [("Мониторы", ["26.40.34.110"], 0.5), ("Приёмники", ["26.40.20"], 0.2),
              ("Прочее", ["28.13.14.110"], 0.4)]

    def _go(self, **kw):
        return self._run("26.40", corpus=self.CORPUS, hidden=("Приёмники",), query="Мониторы",
                         inflate=True, **kw)

    def test_window_is_rescored_by_one_query(self):
        hits, _calls = self._go()
        self.assertEqual(len(self.rescores), 1)
        self.assertEqual(set(self.rescores[0]), {h.point_id for h in hits})
        self.assertEqual(hits[0].source_anchor, "Мониторы")
        # score из ОДНОЙ выдачи: ранги 0 и 2 dense-списка (между ними «Прочее» 0.4), 1/(2 + ранг)
        self.assertEqual([h.score for h in hits[:2]], [1 / 2, 1 / 4])

    def test_without_rescore_inflated_dobor_wins(self):
        """Отрицательный контроль: сбой пересчёта — прежние score, и «Приёмники» снова первыми.
        Ответ не роняется: пересчёт уточняет порядок, без него окно — как до R31."""
        def fail(rows):
            raise RuntimeError("Qdrant недоступен")
        hits, _calls = self._go(rescore=fail)
        self.assertEqual(hits[0].source_anchor, "Приёмники")

    def test_partial_answer_keeps_old_scores(self):
        """Вернулись не все кандидаты — новые score со старыми не смешиваем (та же несравнимость).
        ⚠ Ревью R31 (LOW-1): вернувшимся «Мониторам» подмена даёт ДРУГОЙ score (0.1), иначе «оставили
        старые» и «смешали» неразличимы — мутация `got.get(id, h.score)` выживала."""
        # ⚠ Ревью PR #248 (MED-2): score вернувшимся — РАЗНЫЕ. При равных (0.1 и 0.1) ничья давала
        # «Мониторам» ранг 0, то есть 1/2 = их старому 0.5, и смешение снова было неотличимо.
        given = {"Мониторы": 0.1, "Прочее": 0.3}                       # «Мониторы» — ранг 1, 1/3
        hits, _calls = self._go(rescore=lambda rows: [_point(r[0], r[1], given[r[0]]) for r in rows
                                                      if r[0] != "Приёмники"])
        self.assertEqual(hits[0].source_anchor, "Приёмники")
        self.assertGreater(hits[0].score, 0.9)                         # score маленькой выдачи
        self.assertEqual(next(h.score for h in hits if h.source_anchor == "Мониторы"), 0.5)

    def test_partial_dense_is_not_completed_by_sparse(self):
        """dense вернул не всех — прежние score, даже если недостающий пришёл через sparse: иначе пересчёт
        молча шёл бы на урезанном dense (ревью PR #248, мутация `limit` у dense-запроса)."""
        given = {"Мониторы": 0.1, "Прочее": 0.3}
        hits, _calls = self._go(
            rescore=lambda rows: [_point(r[0], r[1], given[r[0]]) for r in rows if r[0] != "Приёмники"],
            sparse=lambda rows: [_point(r[0], r[1], 5.0) for r in rows if r[0] == "Приёмники"])
        self.assertEqual(hits[0].source_anchor, "Приёмники")
        self.assertGreater(hits[0].score, 0.9)                         # score маленькой выдачи
        self.assertEqual(next(h.score for h in hits if h.source_anchor == "Мониторы"), 0.5)

    def test_rescore_asks_no_payload(self):
        """Ревью R31 (LOW-5): пересчёту нужны только id и score; прочим запросам — payload."""
        self._go()
        self.assertEqual(self.rescore_payload, [[False, False]])     # одно обращение, два списка

    def test_rescore_requests_shape(self):
        """Ревью PR #248 (MED-3): оба списка — по id ВСЕХ кандидатов окна, каждый со своим вектором и
        лимитом не меньше числа кандидатов (иначе часть не вернётся и пересчёт молча выключится)."""
        hits, _calls = self._go()
        ids = {h.point_id for h in hits}
        [batch] = self.rescore_requests
        self.assertEqual([r.using for r in batch], [retriever.DENSE, retriever.SPARSE])
        for r in batch:
            self.assertEqual({i for c in r.filter.must for i in c.has_id}, ids)
            self.assertEqual(r.limit, len(ids))
        self.assertEqual(batch[0].query, [0.0] * 8)                       # вектор вопроса, не пересчитан

    def test_sparse_list_takes_part(self):
        """sparse-список участвует в порядке и спрашивается ТОЛЬКО по кандидатам. dense: Мониторы >
        Приёмники > Проекторы; sparse: Приёмники > Проекторы > Мониторы → по RRF первыми Приёмники
        (5/6 против 3/4). Только dense — первыми Мониторы; sparse без фильтра — «Шум» (не кандидат,
        sparse 20) сдвигает ранги, и снова Мониторы."""
        corpus = [("Мониторы", ["26.40.34.110"], 0.5), ("Приёмники", ["26.40.20"], 0.2),
                  ("Проекторы", ["26.40.34.120"], 0.15), ("Шум", ["99.99"], 0.01)]
        sp = {"Приёмники": 9.0, "Проекторы": 8.0, "Мониторы": 1.0, "Шум": 20.0}
        hits, _calls = self._run("26.40", corpus=corpus, hidden=("Приёмники", "Шум"), query="Мониторы",
                                 inflate=True, sparse=lambda rows: sorted(
                                     (_point(r[0], r[1], sp[r[0]]) for r in rows), key=lambda p: -p.score))
        self.assertNotIn("Шум", [h.source_anchor for h in hits])
        self.assertEqual([h.source_anchor for h in hits[:3]], ["Приёмники", "Мониторы", "Проекторы"])

    def test_no_new_candidates_no_extra_query(self):
        """Доборы ничего не добавили — все score из пула, сравнимы: лишнего запроса нет."""
        self._run("20")
        self.assertEqual(self.rescores, [])


def _rrf(*ranks: int) -> float:
    """Сумма RRF по рангам (с нуля, k = 2) — дробью, как в `_rrf_tied`."""
    return float(sum(Fraction(1, 2 + r) for r in ranks))


class TestRescoreTiesR312(unittest.TestCase):
    """R31-2 #245: окно одного вопроса плавало между прогонами. «Краски полиграфические 20.30.24»:
    sparse-score XXI/27 и XXI/30 равны до бита, `FusionQuery` раздавал этой ничьей ранги в
    произвольном порядке — первой за 12 прогонов была то одна позиция (7), то другая (5)."""

    def test_tie_in_one_list_does_not_depend_on_arrival_order(self):
        dense = [("a", 0.87), ("b", 0.86), ("c", 0.80)]
        tie = [("b", 16.11), ("a", 16.11), ("c", 9.4)]
        for sp in (tie, [tie[1], tie[0], tie[2]]):
            got = retriever._rrf_tied([dense, sp])
            self.assertEqual(got["a"], _rrf(0, 0))                     # ничья — лучший из рангов
            self.assertEqual(got["b"], _rrf(1, 0))
            self.assertEqual(got["c"], _rrf(2, 2))                     # после ничьей двух — ранг 2

    def test_any_permutation_gives_same_scores(self):
        dense = [("a", 0.5), ("b", 0.5), ("c", 0.4), ("d", 0.4), ("e", 0.1)]
        sparse = [("e", 3.0), ("d", 3.0), ("a", 1.0)]
        rnd = random.Random(719)
        want = retriever._rrf_tied([dense, sparse])
        for _ in range(30):
            d, s = dense[:], sparse[:]
            rnd.shuffle(d)
            rnd.shuffle(s)
            self.assertEqual(retriever._rrf_tied([d, s]), want)

    def test_without_ties_matches_qdrant_fusion(self):
        """Без ничьих — формула Qdrant (1/(2 + ранг), ранг с нуля): окно, где ничьих нет, не меняется."""
        got = retriever._rrf_tied([[("a", 0.9), ("b", 0.8)], [("b", 5.0), ("a", 1.0), ("c", 0.5)]])
        self.assertEqual(got, {"a": _rrf(0, 1), "b": _rrf(1, 0), "c": _rrf(2)})

    def test_equal_sums_of_different_ranks_are_equal(self):
        """Ревью PR #248 (LOW-2): ранги (0, 10) и (1, 2) — обе суммы 7/12, а в float они расходились
        на 1 ulp, и порядок решало округление. Равные суммы обязаны быть равны — дальше решает якорь."""
        dense = [(f"x{i}", 1.0 - i / 100) for i in range(11)]          # a — ранг 0, b — ранг 1
        dense[0], dense[1] = ("a", 1.0), ("b", 0.99)
        sparse = [("p", 3.0), ("q", 2.5), ("b", 2.0)] + [(f"y{i}", 1.0 - i / 100) for i in range(7)] + \
            [("a", 0.1)]                                                  # b — ранг 2, a — ранг 10
        got = retriever._rrf_tied([dense, sparse])
        self.assertEqual(got["a"], got["b"])

    def test_window_order_stable_when_sparse_tie_arrives_reversed(self):
        """Через `search`: та же ничья sparse в двух порядках прихода — то же окно. По dense выше
        XXI/30, якорь меньше у XXI/27: с рангами по месту прихода ничья давала то равную сумму (и
        первой XXI/27 по якорю), то перевес XXI/30 — ровно картина боя."""
        corpus = [("XXI/27", ["20.30.24"], 0.29), ("XXI/30", ["20.30.24"], 0.30),
                  ("XIV/11", ["20.30.2"], 0.10)]
        orders = []
        for rev in (False, True):
            def sparse(rows, rev=rev):
                tied = [r for r in rows if r[0].startswith("XXI")]
                tied = tied[::-1] if rev else tied
                return [_point(r[0], r[1], 16.11) for r in tied]
            hits, _ = TestSearchFetchesNewRelations._run(
                self, "20.30.24", corpus=corpus, hidden=("XXI/27",), query="Краски полиграфические",
                inflate=True, sparse=sparse)
            self.assertEqual(len(self.rescores), 1)
            orders.append([h.source_anchor for h in hits])
        self.assertEqual(orders[0], orders[1])
        self.assertEqual(orders[0][0], "XXI/30")                          # dense выше, sparse — ничья


class TestHasExactPosition(unittest.TestCase):
    """Раунд 4 (MED-3): барьер раунда 2 «у кода из прошлого хода своя позиция» тестами подменялся
    целиком, сама проверка не тестировалась. Подмена `_client` считает записи по фильтру, как Qdrant."""

    def _check(self, code, rec_codes=("27.12.31",), fail=False):
        def count(collection_name, exact, count_filter):
            if fail:
                raise RuntimeError("Qdrant недоступен")
            forms = set(count_filter.must[0].match.any)
            return SimpleNamespace(count=sum(bool(forms & set(c)) for c in [list(rec_codes)]))

        client = SimpleNamespace(count=count)
        with mock.patch.object(retriever, "_client", return_value=client):
            return retriever.has_exact_position(code)

    def test_finds_position_by_key_not_string(self):
        """«27.12.31.000» и «27.12.31» — один вид (раунд 3: по ключу, а не по строке)."""
        self.assertTrue(self._check("27.12.31.000"))
        self.assertTrue(self._check("27.12.31", rec_codes=("27.12.31.000",)))

    def test_other_code_has_no_position(self):
        self.assertFalse(self._check("27.12.32"))
        self.assertFalse(self._check("27.12.31.190"))      # подкод — не тот же вид

    def test_qdrant_failure_means_true(self):
        """Сбой → True: вызывающий тогда строку о группировке не печатает."""
        self.assertTrue(self._check("27.12.32", fail=True))


class TestGroupNoteInContext(unittest.TestCase):
    def _ctx(self, code, dialog=None, codes=("20.14.11.110",)):
        h = _hit("Приложение, Раздел XXI, позиция 142", list(codes), 0.5,
                 name="Углеводороды ациклические насыщенные")
        # ⚠⚠ ОФЛАЙН (`O3` #104): строка для кода из истории спрашивает Qdrant «есть ли у кода своя
        # позиция» (`has_exact_position`). Без подмены тест был зелёным у меня (Qdrant поднят) и
        # КРАСНЫМ в CI (Qdrant нет → сбой → строку не печатать) — пять коммитов PR #177 подряд.
        with mock.patch.object(retriever, "has_exact_position", lambda code: False):
            return pipeline.format_context([h], "вопрос", code, dialog_codes=dialog)

    def test_subcode_in_question_gets_note(self):
        ctx = self._ctx("20.14.11.112")
        self.assertIn("20.14.11.112 ВХОДИТ В ГРУППИРОВКУ ОКПД2 20.14.11.110", ctx)

    def test_note_only_for_target(self):
        """Раунд 4 (MED-2): строка «входит в группировку» — только у ЦЕЛЕВОЙ. У кода со своей
        позицией группировка-предок в окне — не целевая, и строка «её требования применяются»
        противоречила бы своей позиции (в корпусе таких кодов 163 из 1040: 20.16.10.111 и
        «Полиэтилен» 20.16.10.110).
        ⚠ Свойство держат ДВА механизма: условие `is_target` у `_group_notes` и свёрнутый хвост
        нецелевых (K6: имя, код, якорь — без строк блока). Снятие одного условия (мутация M7) тест
        не красит: строка блока нецелевого до контекста не доходит. Условие решает только у «строки
        той же расколотой ячейки», а её код группировкой кода вопроса не бывает."""
        own = _hit("Приложение, Раздел XXI, позиция 1", ["20.14.11.112"], 0.3, name="Н-бутан")
        group = _hit("Приложение, Раздел XXI, позиция 142", ["20.14.11.110"], 0.9,
                     name="Углеводороды ациклические насыщенные")
        with mock.patch.object(retriever, "has_exact_position", lambda code: True):
            ctx = pipeline.format_context([own, group], "вопрос", "20.14.11.112")
        self.assertIn("позиция 142", ctx)                  # группировка в контексте есть
        self.assertNotIn("ГРУППИРОВКУ", ctx)

    def test_dialog_code_gets_note(self):
        ctx = self._ctx("20.14.11.110", dialog=["20.14.11.112"])
        self.assertIn("названный ранее в диалоге, 20.14.11.112 ВХОДИТ В ГРУППИРОВКУ", ctx)

    def test_no_note_for_shallow_ancestor(self):
        """Ревью PR #177: «из 20 Катализаторы» не группировка н-бутана — строки быть не должно."""
        self.assertNotIn("ГРУППИРОВКУ", self._ctx("20.14.11.112", codes=("20",)))

    def test_no_note_for_named_goods_position(self):
        """Ревью PR #177, раунд 2: «из» стоит и у глубоких кодов. Позиция «из 27.12.31 Панели … (ГРЩ …)»
        — названные товары внутри вида, а не сам вид: строки «распространяется» нет."""
        h = _hit("V/31", ["27.12.31"], 0.5, name="Панели и прочие комплекты (главные распределительные щиты)")
        ctx = pipeline.format_context([h], "вопрос", "27.12.31.190")
        self.assertNotIn("ГРУППИРОВКУ", ctx)

    def test_grouping_name_is_the_official_one(self):
        self.assertTrue(okpd2_ref.is_grouping_name("20.14.11.110", "Углеводороды ациклические насыщенные <11>"))
        # раунд 3: по ключу — «…000» в корпусе, «32.50.12» в справочнике
        self.assertTrue(okpd2_ref.is_grouping_name("32.50.12.000", "Стерилизаторы хирургические или лабораторные"))
        self.assertFalse(okpd2_ref.is_grouping_name("20", "Катализаторы гидроочистки"))
        self.assertFalse(okpd2_ref.is_grouping_name("99.99.99", "что угодно"))

    def test_dialog_code_does_not_attach_to_a_new_product(self):
        """Ревью PR #177: «шкаф 27.12.31.000», затем «требования у реле 27.12» — реле не получает
        строку «распространяется на шкаф» (27.12 — группа, в приложении это строки «из»)."""
        self.assertNotIn("ГРУППИРОВКУ", self._ctx("27.12", dialog=["27.12.31.000"], codes=("27.12",)))

    def test_dialog_code_only_when_question_gave_none(self):
        ctx = self._ctx("20.14.11.112", dialog=["20.14.11.111"])
        self.assertIn("из вопроса 20.14.11.112", ctx)
        self.assertNotIn("20.14.11.111", ctx)

    def test_no_note_for_exact_or_unrelated(self):
        self.assertNotIn("ГРУППИРОВКУ", self._ctx("20.14.11.110"))
        self.assertNotIn("ГРУППИРОВКУ", self._ctx("20.14.11.110", dialog=["27.12.31.000"]))
        # у позиции И точный код, И код-предок: совпадение точное, строка о группировке лжёт бы
        self.assertNotIn("ГРУППИРОВКУ", self._ctx("27.12.31", codes=("27.12", "27.12.31")))


class TestDialogCodeReachesPrompt(unittest.TestCase):
    """⚠ ПУТЬ, А НЕ КОМПОНЕНТ: строку о группировке видит модель, только если `_plan_answer`
    передал код продукции из истории в `format_context`. Тест `format_context` напрямую этого
    не проверяет — на бою «нельзя» прозвучало именно на втором ходе диалога."""

    def _plan(self, own_position, name="Углеводороды ациклические насыщенные"):
        hit = _hit("Приложение, Раздел XXI, позиция 142", ["20.14.11.110"], 0.5, name=name)
        history = [{"role": "user", "content": "Н-бутан очищенный 20.14.11.112"},
                   {"role": "assistant", "content": "Точной позиции не нашёл."}]
        with mock.patch.object(pipeline, "embed_query", lambda *a, **k: [0.0] * 8), \
                mock.patch.object(pipeline, "search", lambda *a, **k: [hit]), \
                mock.patch.object(pipeline, "search_cases", lambda *a, **k: []), \
                mock.patch.object(pipeline, "dense_top1", lambda *a, **k: 0.95), \
                mock.patch.object(retriever, "has_exact_position", own_position if callable(own_position) else (lambda code: own_position)), \
                mock.patch.object(pipeline.settings, "RERANK_ENABLED", False):
            plan = pipeline._plan_answer("можно ли применять к нему требования позиции 20.14.11.110?",
                                         okpd2="20.14.11.110", history=history)
        return plan.messages[-1]["content"]

    def test_second_turn_with_group_code_mentions_product_code(self):
        self.assertIn("названный ранее в диалоге, 20.14.11.112 ВХОДИТ В ГРУППИРОВКУ", self._plan(False))

    def test_own_position_check_is_lazy(self):
        """Раунд 3: запрос к Qdrant «есть ли своя позиция» — только когда строка напечаталась бы.
        Позиция не группировка — строки нет, и проверка не нужна."""
        asked = []
        self._plan(lambda code: asked.append(code) or False, name="Катализаторы гидроочистки")
        self.assertEqual(asked, [])

    def test_dialog_code_with_own_position_gets_no_note(self):
        """Ревью PR #177, раунд 2: у кода из прошлого хода СВОЯ позиция («прибор 26.51.12.130», затем
        «а эхолот 26.51.12?») — группировка нового вопроса на него не распространяется."""
        self.assertNotIn("ГРУППИРОВКУ", self._plan(True))


class TestExpertSetIsGrounded(unittest.TestCase):
    """EV23: ожидания набора экспертов сверены с корпусом и первоисточником, а не с ответом."""

    def test_anchors_exist_and_numbers_are_in_source(self):
        cases = json.loads((ROOT / "scripts" / "eval_golden_experts.json").read_text(encoding="utf-8"))["cases"]
        anchors = set()
        for f in (ROOT / "knowledge_base" / "pp719" / "structured").glob("*.json"):
            anchors |= {r.get("source_anchor") for r in json.loads(f.read_text(encoding="utf-8"))}
        source = (ROOT / "knowledge_base" / "pp719" / "pp719_full.txt").read_text(encoding="utf-8")
        self.assertGreaterEqual(len(cases), 9)
        for c in cases:
            for a in (c.get("expect_target_any") or []) + (c.get("expect_window_all") or []):
                self.assertIn(a, anchors, c["id"])
            for needle in c.get("expect_prompt") or []:
                # число (порог) обязано стоять в первоисточнике как «не менее N»; код — в справочнике
                if "." in needle and okpd2_ref.okpd2_key(needle):
                    self.assertIn(needle, okpd2_ref._okpd2_names(), c["id"])
                else:
                    self.assertTrue(f"не менее {needle}" in source, f"{c['id']}: «не менее {needle}»")

    def test_d14_targets_are_ancestors_of_question_code(self):
        """Ожидаемая позиция D14 действительно предок кода вопроса — по корпусу, не по набору."""
        recs = {}
        for f in (ROOT / "knowledge_base" / "pp719" / "structured").glob("*.json"):
            for r in json.loads(f.read_text(encoding="utf-8")):
                recs[r["source_anchor"]] = r
        cases = json.loads((ROOT / "scripts" / "eval_golden_experts.json").read_text(encoding="utf-8"))["cases"]
        for c in cases:
            if c["task"] != "#141" or c["id"].startswith("control"):
                continue
            q = okpd2_ref.extract_codes(c["query"])[0]
            for a in c["expect_target_any"]:
                rel = code_relation(recs[a]["okpd2_codes"], q)
                self.assertIsNotNone(rel, c["id"])
                self.assertGreaterEqual(rel, 0, c["id"])


if __name__ == "__main__":
    unittest.main()
