r"""Кейс о ЧУЖОЙ продукции не попадает в контекст — `EV16` (#102).

ЗАМЕР, ЗАВЕДШИЙ ЗАДАЧУ (21.08.2026, `scripts/eval_cases_influence.py`): на запросе «сколько
баллов для металлорежущих станков с ЧПУ» подтягивался кейс по ОКПД2 26.20.13 «Машины
вычислительные электронные цифровые» (раздел IX) с косинусом **0.851** — ВЫШЕ, чем 0.843 у
безусловно уместного кейса про автобусы. Сработала близость «ЧПУ ↔ вычислительные / цифровые /
программное управление», а не близость продукции.

⚠ ПОРОГОМ ЭТО НЕ ЛЕЧИТСЯ: посторонний кейс лежит ВЫШЕ законного, полоса калибровки `R30`
(«уместные от 0.839, посторонние до 0.803») перестала разделять.

⚠ ЦЕНА ОШИБКИ ВЫШЕ ОБЫЧНОГО ШУМА, поэтому задача и заведена: кейс уходит в контекст с ВЫСШИМ
приоритетом (правило 1а промпта, выше первоисточника), его числа попадают в `grounding` — то есть
faithfulness считает их ЗАКОННЫМИ, — и он же гасит гард out-of-scope (`confident = bool(cases)`).

РЕШЕНИЕ — второй сигнал ДРУГОЙ ПРИРОДЫ, как в `scope.py` для out-of-scope: позиционный кейс (у
него есть код и раздел) применим только к продукции того же раздела или той же ветки кода.
Семантическая близость осталась необходимой, но перестала быть достаточной.

Эффект, замеренный тем же инструментом: срабатываний на наборах 2 → **1** (станки отсечены,
автобусы остались).

Запуск:  .venv\Scripts\python -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.rag.retriever import case_fits_scope  # noqa: E402

COMPUTERS = {"okpd2": "26.20.13", "section_roman": "IX",
             "product_name": "Машины вычислительные электронные цифровые"}
BUSES = {"okpd2": "29.10.4", "section_roman": "II",
         "product_name": "Средства автотранспортные (легковые M1…)"}
METHOD = {"okpd2": None, "section_roman": None,
          "product_name": "Методика подбора позиции по коду ОКПД2"}


class TestForeignCaseRejected(unittest.TestCase):
    def test_case_of_another_section_and_branch_is_rejected(self):
        """Тот самый дефект: станки (разделы I/XVI/XXVII) и кейс про ЭВМ раздела IX."""
        self.assertFalse(case_fits_scope(COMPUTERS, {"I", "XVI", "XXVII"},
                                         ["28.41.1", "28.49.12"]))

    def test_case_of_the_same_section_is_kept(self):
        """Кейс про автотранспорт раздела II уместен для автобусов того же раздела."""
        self.assertTrue(case_fits_scope(BUSES, {"II", "III"}, ["29.10.3"]))

    def test_case_of_the_same_code_branch_is_kept(self):
        """Раздел не совпал, но ветка кода — та же: кейс про 26.20.13 и позиция 26.20.13.110."""
        self.assertTrue(case_fits_scope(COMPUTERS, {"XX"}, ["26.20.13.110"]))

    def test_broader_query_code_still_matches(self):
        """Совпадение проверяется в ОБЕ стороны: код окна может быть шире кода кейса."""
        self.assertTrue(case_fits_scope(COMPUTERS, {"XX"}, ["26.20"]))


class TestMethodicalCasesNeverFiltered(unittest.TestCase):
    """Пятеро из двенадцати кейсов кода не имеют и применимы к любой продукции.

    Отсечь их по разделу значило бы выключить половину петли обучения ради дефекта, которого
    у них нет: «состав документов СТ-1», «методика подбора кода», «нет в приложении ≠ нельзя
    в реестр» не привязаны ни к какому разделу.
    """

    def test_case_without_code_passes_any_scope(self):
        self.assertTrue(case_fits_scope(METHOD, {"XXIX"}, ["05.10.10"]))
        self.assertTrue(case_fits_scope(METHOD, set(), []))


class TestFilterOffWithoutWindow(unittest.TestCase):
    """⚠ Окно не передано — фильтр ВЫКЛЮЧЕН.

    Первая редакция без окна отбрасывала ВСЕ позиционные кейсы (`any(...)` по пустому списку
    ложно), то есть делала обратное тому, что обещал докстринг. Поймано положительным контролем
    `eval_cases_influence.py`: 12 из 12 кейсов находились по своим вопросам — стало 11.
    Диагностические скрипты зовут `search_cases` без окна, и молчаливое отключение петли в них
    исказило бы любой замер её влияния.
    """

    def test_no_window_means_no_filtering(self):
        self.assertTrue(case_fits_scope(COMPUTERS, None, None))

    def test_empty_window_still_filters(self):
        """Пустое окно — это ответ «ничего не нашли», а не «окна нет»: фильтр работает."""
        self.assertFalse(case_fits_scope(COMPUTERS, set(), []))


class TestNamedCodeBeatsScope(unittest.TestCase):
    """Код, НАЗВАННЫЙ пользователем, авторитетнее эвристики о разделе.

    Путь `_case_code_hit` идёт мимо и порога, и этого фильтра: спросили про конкретный код —
    значит про эту продукцию. Проверяется на связке, а не на функции: важно, что `search_cases`
    отдаёт кейс по названному коду при любом окне.
    """

    def test_case_found_by_named_code_survives_foreign_window(self):
        from app.rag.embeddings import embed_query
        from app.rag.retriever import search_cases

        q = "что означает код 27.40.33.130"
        got = search_cases(q, limit=3, qvec=embed_query(q),
                           sections={"XXIX"}, codes=["05.10.10"])
        self.assertTrue(any(str(c.get("okpd2")) == "27.40.33.130" for c in got),
                        "кейс по НАЗВАННОМУ коду отсечён фильтром области")


if __name__ == "__main__":
    unittest.main()
