"""Сверка редакций ПП №719: текущая база (pp719_full.txt) vs новый RTF.

Отвечает на вопрос «что изменилось и что нужно перепарсить», когда выходит новая
редакция постановления. НЕ трогает рабочую базу — только читает и печатает отчёт
(+ пишет полный диф в файл). Порядок применения найденных изменений — в
docs/ACTUALIZATION.md.

Что делает:
  1. Извлекает текст из нового RTF (striprtf, cp1251) — тем же способом, что parse_rtf.py.
  2. Диф множеств редакций «в ред. Постановлений … N XXXX» → какие постановления новые.
  3. Диф множеств кодов-позиций → добавленные / удалённые позиции (по разделам).
  4. Построчный диф с привязкой к разделу → сводка -/+ по разделам + full_diff.txt.

Запуск (из корня, через venv; кириллица → PYTHONUTF8=1 на Windows):
  PYTHONUTF8=1 .venv/Scripts/python.exe scripts/diff_edition.py \
      --new-rtf "Постановление ... от 01.07.2026.rtf"
  # по умолчанию --old = knowledge_base/pp719/pp719_full.txt, отчёты → scratchpad/edition_diff/
"""

from __future__ import annotations

import argparse
import difflib
import re
from collections import Counter
from pathlib import Path

from striprtf.striprtf import rtf_to_text

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OLD = ROOT / "knowledge_base" / "pp719" / "pp719_full.txt"

AMEND_RE = re.compile(r"от (\d{2}\.\d{2}\.\d{4}) N (\d+)")
ROMAN = (
    r"(?:XXIX|XXVIII|XXVII|XXVI|XXV|XXIV|XXIII|XXII|XXI|XX|XIX|XVIII|XVII|XVI|XV|"
    r"XIV|XIII|XII|XI|X|IX|VIII|VII|VI|V|IV|III|II|I)"
)
SEC_RE = re.compile(rf"^({ROMAN})\.\s+[А-ЯA-Zа-я]")
CODE_RE = re.compile(r"^(\d{2}(?:\.\d+)*)\s*\|")


def extract_rtf(path: Path) -> str:
    raw = path.read_text(encoding="cp1251", errors="replace")
    text = rtf_to_text(raw, encoding="cp1251", errors="replace")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text.strip()


def amend_set(text: str) -> set[tuple[str, str]]:
    return set(AMEND_RE.findall(text))


def sort_amend(k: tuple[str, str]):
    d, n = k
    dd, mm, yy = d.split(".")
    return (yy, mm, dd, int(n))


def codes_by_section(lines: list[str]) -> dict[str, tuple[str, str]]:
    cur, out = "?", {}
    for ln in lines:
        sm = SEC_RE.match(ln)
        if sm:
            cur = sm.group(1)
            continue
        cm = CODE_RE.match(ln)
        if cm:
            parts = ln.split("|")
            out.setdefault(cm.group(1), (cur, parts[1].strip() if len(parts) > 1 else ""))
    return out


def section_map(lines: list[str]) -> dict[int, str]:
    cur, m = "?", {}
    for i, ln in enumerate(lines):
        sm = SEC_RE.match(ln)
        if sm:
            cur = sm.group(1)
        m[i] = cur
    return m


def main() -> None:
    ap = argparse.ArgumentParser(description="Сверка редакций ПП №719 (текущая база vs новый RTF)")
    ap.add_argument("--new-rtf", required=True, help="путь к новому RTF постановления")
    ap.add_argument("--old", default=str(DEFAULT_OLD), help="текущий pp719_full.txt")
    ap.add_argument("--out-dir", default=str(ROOT / "scratchpad" / "edition_diff"),
                    help="куда писать извлечённый текст и full_diff.txt")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    old_text = Path(args.old).read_text(encoding="utf-8")
    new_text = extract_rtf(Path(args.new_rtf))
    (out_dir / "pp719_new_full.txt").write_text(new_text, encoding="utf-8")

    old_lines, new_lines = old_text.split("\n"), new_text.split("\n")

    # 1. Редакции
    only_new = sorted(amend_set(new_text) - amend_set(old_text), key=sort_amend)
    only_old = sorted(amend_set(old_text) - amend_set(new_text), key=sort_amend)
    print("=" * 70)
    print(f"РЕДАКЦИИ: в НОВОМ, но НЕ в текущей базе ({len(only_new)}):")
    for d, n in only_new:
        print(f"  от {d} N {n}")
    if only_old:
        print(f"⚠ в текущей базе, но НЕ в новом ({len(only_old)}): {only_old}")

    # 2. Позиции
    old_codes, new_codes = codes_by_section(old_lines), codes_by_section(new_lines)
    added = sorted(set(new_codes) - set(old_codes))
    removed = sorted(set(old_codes) - set(new_codes))
    print("\n" + "=" * 70)
    print(f"ПОЗИЦИИ добавлены ({len(added)}):")
    for c in added:
        print(f"  [{new_codes[c][0]:>5}] {c:20} {new_codes[c][1][:60]}")
    print(f"ПОЗИЦИИ удалены/переструктурированы ({len(removed)}):")
    for c in removed:
        print(f"  [{old_codes[c][0]:>5}] {c:20} {old_codes[c][1][:60]}")

    # 3. Построчный диф по разделам
    new_sm = section_map(new_lines)
    sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    add_by, del_by = Counter(), Counter()
    full = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        sec = new_sm.get(j1, "?")
        for k in range(j1, j2):
            add_by[new_sm.get(k, sec)] += 1
        for k in range(i1, i2):
            del_by[sec] += 1
        full.append(f"\n@@ {tag} old[{i1}:{i2}] new[{j1}:{j2}] section~{sec} @@")
        full += [f"- {old_lines[k]}" for k in range(i1, i2)]
        full += [f"+ {new_lines[k]}" for k in range(j1, j2)]
    (out_dir / "full_diff.txt").write_text("\n".join(full), encoding="utf-8")

    print("\n" + "=" * 70)
    print("ИЗМЕНЁННЫЕ СТРОКИ ПО РАЗДЕЛАМ (примечание: разделы I–IV после XXIX = Правила реестра):")
    for s in sorted(set(list(add_by) + list(del_by))):
        print(f"  раздел {s:>6}:  -{del_by.get(s, 0):<5} +{add_by.get(s, 0):<5}")
    print(f"\nПолный диф → {out_dir / 'full_diff.txt'}")
    print(f"Извлечённый текст новой редакции → {out_dir / 'pp719_new_full.txt'}")
    print("\nДальше: см. docs/ACTUALIZATION.md — какие structured/*.json перепарсить и реиндекс.")


if __name__ == "__main__":
    main()
