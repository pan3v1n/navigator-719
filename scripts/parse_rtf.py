"""
Парсинг RTF-файла ПП №719 → чистый текст в knowledge_base/pp719/
Запуск: .venv\\Scripts\\python scripts\\parse_rtf.py
"""

import re
import sys
from pathlib import Path

from striprtf.striprtf import rtf_to_text

ROOT = Path(__file__).parent.parent
# Текущий авторитетный RTF ПП №719 (ред., действующая с 01.07.2026) лежит в корне репо; имя менялось
# между редакциями, поэтому берём единственный .rtf с «719» в имени, а не хардкод старого имени.
_RTFS = [p for p in ROOT.glob("*.rtf") if "719" in p.name]
RTF_FILE = _RTFS[0] if _RTFS else ROOT / "Постановление 719.rtf"
OUT_DIR = ROOT / "knowledge_base" / "pp719"
OUT_FULL = OUT_DIR / "pp719_full.txt"
OUT_CHUNKS_DIR = OUT_DIR / "chunks"


def clean(text: str) -> str:
    # убрать множественные пустые строки
    text = re.sub(r"\n{3,}", "\n\n", text)
    # убрать висячие пробелы
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text.strip()


def split_sections(text: str) -> list[tuple[str, str]]:
    """
    Разбивает текст на секции по заголовкам вида:
    'Приложение N X', 'ПЕРЕЧЕНЬ', 'Раздел', номерным пунктам верхнего уровня.
    Возвращает список (заголовок, текст_секции).
    """
    pattern = re.compile(
        r"(?m)^((?:Приложение\s+N\s*\d+|ПОСТАНОВЛЕНИЕ|ПЕРЕЧЕНЬ|Раздел\s+\d+|"
        r"(?:I{1,3}|IV|V|VI|VII|VIII|IX|X)\.\s+.{5,80})\s*)$"
    )
    parts = pattern.split(text)

    sections: list[tuple[str, str]] = []
    if parts[0].strip():
        sections.append(("Вводная часть", parts[0].strip()))

    it = iter(parts[1:])
    for header in it:
        body = next(it, "")
        header = header.strip()
        if header and body.strip():
            sections.append((header, body.strip()))

    return sections


def main() -> None:
    if not RTF_FILE.exists():
        print(f"[ERROR] Файл не найден: {RTF_FILE}", file=sys.stderr)
        sys.exit(1)

    print(f"Читаю: {RTF_FILE.name}")
    raw = RTF_FILE.read_text(encoding="cp1251", errors="replace")
    text = rtf_to_text(raw, encoding="cp1251", errors="replace")
    text = clean(text)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_FULL.write_text(text, encoding="utf-8")
    print(f"[OK] Полный текст -> {OUT_FULL}  ({len(text):,} симв.)")

    sections = split_sections(text)
    print(f"Найдено секций: {len(sections)}")

    OUT_CHUNKS_DIR.mkdir(parents=True, exist_ok=True)
    for i, (header, body) in enumerate(sections):
        slug = re.sub(r"[^\w\s-]", "", header)[:50].strip().replace(" ", "_")
        fname = OUT_CHUNKS_DIR / f"{i:02d}_{slug}.txt"
        fname.write_text(f"# {header}\n\n{body}", encoding="utf-8")
        print(f"  {fname.name}  ({len(body):,} симв.)")

    print("\nГотово. Следующий шаг: scripts/load_kb.py - индексация в Qdrant.")


if __name__ == "__main__":
    main()
