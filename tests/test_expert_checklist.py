"""Лист сверки для эксперта: защита от молчаливой порчи списка (офлайн).

Список блока А строится ДИФФОМ двух сборок карты наследования — текущим ключом и ключом до
фикса D6. Это единственный способ ответить «что добавил фикс», не переписывая число руками
(прежняя запись «46 позиций» именно так и разошлась с корпусом — на деле 44).

Риск у приёма ровно один и он тихий: если правило ключа поменяется ещё раз, `_old_map_key`
перестанет быть «ключом до фикса» и станет «ключом позапрошлой версии». Дифф при этом
продолжит считаться и выдаст правдоподобный, но неверный список — а эксперт сверит не то.
Отсюда первые два теста.

Запуск:  .venv\\Scripts\\python -m unittest discover -s tests
"""

from __future__ import annotations

import importlib.util
import re
import sys
import token
import tokenize
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


checklist = _load("build_expert_checklist", "scripts/build_expert_checklist.py")
diag = checklist.diag

# Имя-перечень из таблицы XVIII — на нём фикс и обнаружился: после снятия сноски остаётся « ;».
LIST_NAME = "Оборудование системы опознавания судов <9>;\nкодирующее устройство"

NAMES = [
    LIST_NAME,
    "Компас магнитный <9>;",
    "Этилен.\nЭта группировка включает этилен чистотой менее 95 процентов",
    "Круги шлифовальные",
    "Хладон-218 (октафторпропан),",
    "Суда обслуживающего флота.",
]


class TestHistoricalKey(unittest.TestCase):
    """`_old_map_key` обязан отличаться от текущего ровно на хвостовую пунктуацию."""

    def test_differs_only_by_trailing_punctuation(self):
        for name in NAMES:
            with self.subTest(name=name[:40]):
                old = checklist._old_map_key("XVIII", name)
                new = diag._map_key("XVIII", name)
                sec, _, tail = old.partition("|")
                self.assertEqual(f"{sec}|{tail.strip(';,.').strip()}", new)

    def test_the_fix_actually_changes_something(self):
        """Иначе дифф пуст, а список блока А тихо становится нулевым."""
        self.assertNotEqual(
            checklist._old_map_key("XVIII", LIST_NAME),
            diag._map_key("XVIII", LIST_NAME),
        )

    def test_current_key_matches_runtime(self):
        """Карту строит скрипт, а читает рантайм — расхождение = молчаливый отказ наследования."""
        from app.rag import inheritance
        for name in NAMES:
            with self.subTest(name=name[:40]):
                self.assertEqual(diag._map_key("XVIII", name), inheritance._key("XVIII", name))


class TestPlural(unittest.TestCase):
    """Документ уходит человеку: «44 позиции», а не «44 позиций»."""

    def test_forms(self):
        cases = {1: "1 позиция", 2: "2 позиции", 4: "4 позиции", 5: "5 позиций",
                 11: "11 позиций", 12: "12 позиций", 14: "14 позиций", 15: "15 позиций",
                 21: "21 позиция", 22: "22 позиции", 44: "44 позиции", 111: "111 позиций"}
        for n, expected in cases.items():
            with self.subTest(n=n):
                self.assertEqual(checklist._plural(n, "позиция", "позиции", "позиций"), expected)


class TestPointsForm(unittest.TestCase):
    """Баллы в листе сверки читает человек: «1 балл», а не «1 баллов»."""

    def test_integer_forms(self):
        for pts, expected in {1: "1 балл", 2: "2 балла", 4: "4 балла", 5: "5 баллов",
                              11: "11 баллов", 100: "100 баллов", 300: "300 баллов"}.items():
            with self.subTest(pts=pts):
                self.assertEqual(checklist._points(pts), expected)

    def test_fractional_points_survive(self):
        """Дробные баллы в корпусе есть (0,5, раздел VII) — целочисленная форма их исказит."""
        self.assertEqual(checklist._points(0.5), "0,5 балла")

    def test_zero_is_a_value_not_an_absence(self):
        """`if pts` съедал бы 0 вместе с None, а «0 баллов» — это значение градации."""
        self.assertEqual(checklist._ops_line({"text": "операция", "points": 0}),
                         "операция — **0 баллов**")
        self.assertEqual(checklist._ops_line({"text": "операция", "points": None}), "операция")


class TestNoServiceTokensLeak(unittest.TestCase):
    """В графу «Что уточнить» не должен попадать служебный токен категории."""

    TOKENS = ("NO_MATCH", "PARSER_LOSS", "EXCLUDED", "NO_PARENT", "INHERIT",
              "IN_COMPONENT", "NO_SOURCE", "HIGH", "MEDIUM")

    def test_generated_document_has_no_raw_categories(self):
        doc = ROOT / "docs/EXPERT_CHECKLIST_WAVE3.md"
        if not doc.exists():
            self.skipTest("лист сверки не сгенерирован")
        text = doc.read_text(encoding="utf-8")
        for token in self.TOKENS:
            with self.subTest(token=token):
                self.assertNotIn(token, text)


class TestDamagedNameFallback(unittest.TestCase):
    """У повреждённых записей первая строка — обрывок ячейки, по нему позицию не опознать."""

    def test_short_head_gets_next_line(self):
        rec = {"product_name": "или\nналичие у юридического лица прав на документацию"}
        self.assertIn("наличие у юридического лица", checklist._name_full(rec))

    def test_colon_head_gets_next_line(self):
        rec = {"product_name": "до 1 января 2019 г.:\nналичие у юридического лица прав"}
        self.assertIn("наличие", checklist._name_full(rec))

    def test_normal_name_left_alone(self):
        rec = {"product_name": "Аппараты автоматического плазмафереза донорского\nвторая строка"}
        self.assertNotIn("вторая строка", checklist._name_full(rec))


class TestSourceThresholdMatchesRuntime(unittest.TestCase):
    """Порог источника лист обязан считать ТЕМ ЖЕ правилом, что и рантайм.

    Регресс, найденный ревью 14.08: лист читал только `min_threshold` записи, а
    `pipeline.format_context` берёт `min_threshold or thresholds.lookup_threshold(...)`.
    У блока А1 (раздел XXI, 26 позиций) поле пусто, но примечание 7 даёт «не менее 300 баллов» —
    документ печатал «порог не указан», поднимал врезку «пользователь видит баллы без минимума»
    и просил эксперта разобраться с этим В ПЕРВУЮ ОЧЕРЕДЬ. Просьба закрыть несуществующий пробел
    дороже любой опечатки: внимание эксперта — самый дефицитный ресурс волны, а после рассылки
    документ уже не исправить."""

    def test_document_threshold_equals_runtime_threshold(self):
        from app.rag.thresholds import lookup_threshold
        doc = ROOT / "docs/EXPERT_CHECKLIST_WAVE3.md"
        if not doc.exists():
            self.skipTest("лист сверки не сгенерирован")
        text = doc.read_text(encoding="utf-8")
        by_key = {}
        for rec in diag.load_structured():
            by_key.setdefault((rec.get("section_roman"), checklist._name(rec)), rec)

        heads = re.findall(r"^### (А\d+)\. Раздел (\S+) · источник «(.+?)»", text, re.M)
        self.assertTrue(heads, "блок А исчез из документа")
        for tag, sec, src in heads:
            with self.subTest(block=tag):
                rec = by_key.get((sec, src))
                self.assertIsNotNone(rec, f"{tag}: источник «{src[:40]}» не найден в корпусе")
                expected = rec.get("min_threshold") or lookup_threshold(
                    rec.get("okpd2_codes") or [], checklist._name(rec), rec.get("section_roman"))
                block = text.split(f"### {tag}.", 1)[1].split("\n### ", 1)[0]
                line = next(ln for ln in block.splitlines()
                            if ln.startswith("**Порог для источника:**"))
                if expected:
                    # ⚠ Порог бывает ДВУСТРОЧНЫМ: с 20.08.2026 `lookup_threshold` выносит
                    # оговорку прим. 17 («⚠ ИНОЙ порог — за исключением судов…») отдельной
                    # строкой. Сверять многострочное значение с ОДНОЙ markdown-строкой нельзя —
                    # такой `assertIn` не найдёт совпадения НИКОГДА. Сегодня тест зелен лишь
                    # потому, что ни один из блоков А не берёт источник из прим. 17; первое же
                    # судно в блоке А сделало бы сторожа документа структурно непроходимым, и
                    # это читалось бы как поломка генератора, а не как устаревшая проверка.
                    # Сверяем ОБЩИЙ график — он и стоит в строке «Порог для источника:».
                    head = expected.split("\n", 1)[0]
                    self.assertIn(head, line, f"{tag}: рантайм показывает «{head}»")
                    self.assertNotIn("пользователь видит баллы без минимума", block)
                else:
                    self.assertIn("_не указан_", line)


class TestDBlockMatchesClassifier(unittest.TestCase):
    """Блок Д (issue #95): числа в письме эксперту обязаны ПЕРЕСЧИТЫВАТЬСЯ, а не переписываться.

    Урок проекта, стоивший двух отзывов опубликованных чисел за сутки: число, которое нечем
    пересчитать, — не критерий, а цитата. Здесь цена ошибки выше обычной: документ уходит
    НАРУЖУ, эксперту ТПП, и отозвать его нельзя.
    """

    @classmethod
    def setUpClass(cls):
        cls.items = checklist.d13_items()
        cls.block = "\n".join(checklist.render_d_block(cls.items))
        cls.appendix = checklist.render_d_appendix(cls.items)

    def test_counts_in_text_match_the_classifier(self):
        for key, tag, _ in checklist.D13_CLASSES:
            n = sum(1 for i in self.items if i["class"] == key)
            with self.subTest(tag=tag):
                self.assertRegex(self.block, rf"\*\*{tag} —[^*]*: {n} ")

    def test_appendix_lists_every_position(self):
        """Рендер мог бы тихо потерять строки — считаем их, а не верим заголовку."""
        expected = sum(1 for i in self.items
                       if i["class"] in {k for k, _, _ in checklist.D13_CLASSES})
        rows = sum(1 for line in self.appendix.splitlines()
                   if re.match(r"^\|\s*(\d+|—)\s*\|", line))
        self.assertEqual(rows, expected)

    def test_defect_classes_never_go_to_the_expert(self):
        """⚠ Дефект разбора — работа для кода. Спрашивать про него эксперта значит просить его
        чинить наш баг и тратить единственный дефицитный ресурс проекта."""
        clf = sys.modules["classify_missing"]
        self.assertFalse(
            {k for k, _, _ in checklist.D13_CLASSES} & set(clf.DEFECT_CLASSES),
            "в блок Д попал дефектный класс — его чинят кодом, а не письмом")

    def test_position_number_distinguishes_rows_with_the_same_code(self):
        """Код и наименование позицию НЕ определяют: «Устройства наведения промышленные»
        (26.40.33) стоят в разделе IV дважды, позиции 11 и 34, с разными требованиями.
        Без номера эксперт видит дубль и не знает, о какой строке речь.

        ⚠ Номер уникален В ПРЕДЕЛАХ РАЗДЕЛА, не глобально: тот же код 26.40.33 есть в разделе IX
        и там тоже позиция 34. Первая редакция теста сравнивала номера по всему корпусу и упала
        на ВЕРНОМ коде — приложение группирует таблицы по разделам, там сравнение и уместно."""
        twins = [i for i in self.items if i["code"] == "26.40.33" and i["section"] == "IV"]
        self.assertGreater(len(twins), 1, "образец задачи исчез из корпуса — тест пересобрать")
        numbers = {checklist._pos_no(i.get("anchor")) for i in twins}
        self.assertEqual(len(numbers), len(twins), f"номера позиций не различают строки: {numbers}")
        self.assertNotIn("—", numbers, "у записи нет якоря первоисточника")

    def test_position_numbers_are_unique_inside_every_section_table(self):
        """Инвариант, на котором держится адресность приложения: в одной таблице раздела номер
        позиции встречается один раз. Иначе «проверьте позицию 34» снова неоднозначно."""
        for key, tag, _ in checklist.D13_CLASSES:
            rows = [i for i in self.items if i["class"] == key]
            for sec in {r["section"] for r in rows}:
                nums = [checklist._pos_no(r.get("anchor")) for r in rows if r["section"] == sec]
                with self.subTest(tag=tag, section=sec):
                    self.assertEqual(len(nums), len(set(nums)), f"повтор номера в разделе {sec}")


class TestPython311Compatible(unittest.TestCase):
    """Обратный слэш внутри выражения f-строки разрешён только с Python 3.12 (PEP 701).

    `SETUP.md` обещает «3.11+ тоже работает», а CI гоняет один 3.12 — расхождение тихое.
    В `_old_map_key` такой f-string стоял, и этот файл импортирует скрипт на уровне модуля:
    на 3.11 упала бы ВСЯ батарея на этапе сборки тестов, а не один тест."""

    def test_no_backslash_inside_fstring_expressions(self):
        if not hasattr(token, "FSTRING_START"):
            self.skipTest("токенизатор до 3.12 не разбирает f-строки на части")
        files = [*(ROOT / "app").rglob("*.py"), *(ROOT / "scripts").glob("*.py"),
                 *(ROOT / "tests").glob("*.py"), ROOT / "main.py"]
        offenders = []
        for path in files:
            depth = 0
            try:
                with tokenize.open(path) as fh:
                    for tok in tokenize.generate_tokens(fh.readline):
                        name = tokenize.tok_name.get(tok.type, "")
                        if name == "FSTRING_START":
                            depth += 1
                        elif name == "FSTRING_END":
                            depth = max(0, depth - 1)
                        elif depth and name == "STRING" and "\\" in tok.string:
                            offenders.append(f"{path.relative_to(ROOT)}:{tok.start[0]}")
            except (tokenize.TokenError, SyntaxError):  # файл не разбирается — не наша проверка
                continue
        self.assertEqual(offenders, [], f"f-строки со слэшем внутри выражения: {offenders}")


if __name__ == "__main__":
    unittest.main()
