"""Перечанковка приложения ПП №719 из pp719_full.txt (фикс дефекта нарезки).

ПРОБЛЕМА: исходный `parse_rtf.py` распознавал заголовки разделов регекспом
`I{1,3}|IV|V|...|X` (обрыв на X) + лимит длины заголовка `.{5,80}`. Из-за этого
разделы XI–XXIX склеились в «X» (851 чужая запись с ярлыком «стройматериалы»), а
раздел V (длинный заголовок) влился в IV. См. docs/RAG_EXPLAINED.md / разбор в IDEAS.

ЭТОТ скрипт нарезает приложение заново ПРЯМО из pp719_full.txt (исходный RTF удалён),
корректным детектором римских заголовков любой длины. Перезаписывает только
недостающие/сломанные разделы (V, X, XI–XXIX); 8 уже корректных (I,II,III,IV,VI,VII,
VIII,IX) НЕ трогает. Чанки получают латинские слаги (правило проекта — без кириллицы
в именах) и заголовок `# {ROMAN}. {полное русское название}` (его читает structure_kb).

Запуск:  .venv\\Scripts\\python scripts\\rechunk_appendix.py [--write]
Без --write — только показывает нарезку (dry-run, ничего не пишет).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FULL = ROOT / "knowledge_base" / "pp719" / "pp719_full.txt"
CHUNKS = ROOT / "knowledge_base" / "pp719" / "chunks"

# Заголовок раздела приложения: римское число (любой длины) + точка + название.
HEADER_RE = re.compile(r"(?m)^([IVXLC]+)\.[ \t]+(\S[^\n]{3,})$")

_ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}


def roman_to_int(s: str) -> int:
    total, prev = 0, 0
    for ch in reversed(s):
        v = _ROMAN.get(ch, 0)
        total += -v if v < prev else v
        prev = max(prev, v)
    return total


# Целевые разделы (что перечанковываем) → латинский слаг и числовой префикс файла.
# Префикс 1NN выбран, чтобы не сталкиваться с существующими 00..15 чанками.
TARGETS: dict[str, tuple[int, str]] = {
    "V": (105, "V_energomash_elektrotehnika"),
    "X": (110, "X_stroymaterialy"),
    "XI": (111, "XI_mebel_derevoobrabotka"),
    "XII": (112, "XII_zhd_mashinostroenie"),
    "XIII": (113, "XIII_armaturostroenie"),
    "XIV": (114, "XIV_himveschestva_dobycha"),
    "XV": (115, "XV_oborud_uglevodorody"),
    "XVI": (116, "XVI_kompressor_holod"),
    "XVII": (117, "XVII_legkaya_prom"),
    "XVIII": (118, "XVIII_sudostroenie"),
    "XIX": (119, "XIX_nasosy"),
    "XX": (120, "XX_burilnye_ustanovki"),
    "XXI": (121, "XXI_neftegazohimiya"),
    "XXII": (122, "XXII_pribory_izmereniya"),
    "XXIII": (123, "XXIII_metallurgiya"),
    "XXIV": (124, "XXIV_azs_szhizh_gaz"),
    "XXV": (125, "XXV_muz_instrumenty_zvuk"),
    "XXVI": (126, "XXVI_oborud_spg"),
    "XXVII": (127, "XXVII_detskie_tovary"),
    "XXVIII": (128, "XXVIII_aviaprom"),
    "XXIX": (129, "XXIX_razvedka_neftegaz"),
}
# Старый сломанный чанк, который нужно удалить (X склеивал XI–XXIX).
OLD_X = CHUNKS / "10_X_stroymaterialy.txt"


def find_appendix_sections(text: str):
    """Возвращает [(roman, title, body)] для приложения = первого монотонного
    прогона I, II, …, XXIX. Останавливается на сбросе нумерации (тело постановления
    «I. Общие положения»). Сторонние ложные совпадения пропускаются."""
    matches = list(HEADER_RE.finditer(text))
    starts = [m.start() for m in matches]
    run = []  # (roman, title, start, end)
    expected = 1
    for m in matches:
        roman, title = m.group(1), m.group(2).strip().rstrip("|").strip()
        if roman_to_int(roman) == expected:
            run.append([roman, title, m.start(), None])
            expected += 1
    for i, item in enumerate(run):
        if i + 1 < len(run):
            item[3] = run[i + 1][2]
        else:
            # конец ПОСЛЕДНЕГО раздела (XXIX) = старт СЛЕДУЮЩЕГО заголовка после него.
            # Это «I. Общие положения» тела постановления — иначе XXIX проглотил бы
            # весь хвост документа (тот же баг, что был у X). Fallback — конец файла.
            nxt = [s for s in starts if s > item[2]]
            item[3] = nxt[0] if nxt else len(text)
    return [(r, t, text[s:e].strip()) for r, t, s, e in run]


def count_products(body: str) -> int:
    # строки-кандидаты продукта начинаются с кода ОКПД2 (NN.N…)
    return len(re.findall(r"(?m)^\d{2}\.\d", body))


def main() -> None:
    ap = argparse.ArgumentParser(description="Перечанковка приложения ПП №719 из pp719_full.txt")
    ap.add_argument("--write", action="store_true", help="записать чанки (без флага — dry-run)")
    args = ap.parse_args()

    if not FULL.exists():
        sys.exit(f"Нет {FULL}")
    text = FULL.read_text(encoding="utf-8")
    sections = find_appendix_sections(text)

    print(f"Найдено разделов приложения: {len(sections)} (ожидаем 29: I..XXIX)\n")
    print(f"{'рим':>5} {'преф':>5} {'прод':>5}  заголовок")
    print("-" * 78)
    total = 0
    written = []
    for roman, title, body in sections:
        n = count_products(body)
        total += n
        tgt = TARGETS.get(roman)
        pref = str(tgt[0]) if tgt else "—"
        mark = "" if tgt else "  (корректен — не трогаем)"
        print(f"{roman:>5} {pref:>5} {n:>5}  {title[:52]}{mark}")
        if tgt and args.write:
            slug = tgt[1]
            path = CHUNKS / f"{tgt[0]}_{slug}.txt"
            path.write_text(f"# {roman}. {title}\n\n{body}\n", encoding="utf-8")
            written.append(path.name)
    print("-" * 78)
    print(f"ИТОГО строк-продуктов в приложении: {total}")

    if args.write:
        if OLD_X.exists():
            OLD_X.unlink()
            print(f"Удалён старый склеенный чанк: {OLD_X.name}")
        print(f"Записано чанков: {len(written)}")
        for n in written:
            print("  +", n)
    else:
        print("\n(dry-run: ничего не записано. Повтори с --write, когда нарезка устроит.)")


if __name__ == "__main__":
    main()
