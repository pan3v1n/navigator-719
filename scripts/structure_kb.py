"""Структуризация базы знаний ПП №719: текст раздела -> JSON по видам продукции.

Пайплайн (см. docs/V1_SPEC.md, Спринт 2):
  1. Регулярки режут «расплющенную» таблицу раздела на кандидатов-записей
     (продукты + их компоненты + аннотации «в ред.»).
  2. DeepSeek нормализует «грязный» текст каждого продукта в строгий JSON.
  3. Результат -> knowledge_base/pp719/structured/<раздел>.json (коммитится в репо,
     повторно не парсится).

Запуск (из корня проекта, через venv):
  .venv/Scripts/python.exe scripts/structure_kb.py --section II --dry-run
  .venv/Scripts/python.exe scripts/structure_kb.py --section II            # с DeepSeek
  .venv/Scripts/python.exe scripts/structure_kb.py --section II --limit 1  # только 1-й продукт

Флаги:
  --dry-run     не звать API: показать разбор на кандидатов и размеры.
  --limit N     обработать только первые N продуктов (дешёвая проверка промпта).
  --max-chars   порог размера блока, выше — продукт режется на батчи (по умолч. 9000).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# --- доступ к пакету app при запуске как скрипта ---
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings  # noqa: E402
from app.core.prompts import (  # noqa: E402
    KB_PARSER_SYSTEM_PROMPT,
    build_kb_parser_user_prompt,
)

CHUNKS_DIR = ROOT / "knowledge_base" / "pp719" / "chunks"
OUT_DIR = ROOT / "knowledge_base" / "pp719" / "structured"

# Ориентировочные цены DeepSeek (deepseek-chat). ПРОВЕРЯТЬ актуальность на
# platform.deepseek.com — меняются. USD за 1M токенов; ¥ считаем по курсу ниже.
PRICE_IN_USD_PER_1M = 0.27
PRICE_OUT_USD_PER_1M = 1.10
USD_TO_CNY = 7.2

OKPD2_RE = re.compile(r"\d{2}\.\d{2}(?:\.\d+)*")
PRODUCT_START_RE = re.compile(r"^(из\s+)?\d{2}\.\d{2}")


# --------------------------------------------------------------------------- #
# Модель данных
# --------------------------------------------------------------------------- #
@dataclass
class Product:
    okpd2_field: str
    name_raw: str
    okpd2_codes: list[str] = field(default_factory=list)
    component_cells: list[str] = field(default_factory=list)
    amendments: list[str] = field(default_factory=list)
    position: int = 0

    def raw_block(self) -> str:
        """Сырой текст продукта для подачи в LLM."""
        parts = [self.name_raw.strip()]
        parts.extend(c.strip() for c in self.component_cells if c.strip())
        return "\n".join(p for p in parts if p)


# --------------------------------------------------------------------------- #
# Разбор раздела на кандидатов (без LLM)
# --------------------------------------------------------------------------- #
def iter_rows(text: str):
    """Группирует физические строки в логические строки таблицы.

    Логическая строка завершается, когда физическая строка заканчивается на `|`
    (ячейки могут содержать переносы строк). Последняя строка файла может не
    иметь хвостового `|` — сбрасываем буфер на EOF.
    """
    buffer: list[str] = []
    for line in text.splitlines():
        buffer.append(line)
        if line.rstrip().endswith("|"):
            yield "\n".join(buffer)
            buffer = []
    if buffer and any(s.strip() for s in buffer):
        yield "\n".join(buffer)


def split_fields(row: str) -> list[str]:
    core = row.rstrip()
    if core.endswith("|"):
        core = core[:-1]  # снять один хвостовой разделитель строки
    return core.split("|")


def classify(fields: list[str]) -> tuple[str, object]:
    f0 = fields[0].strip()
    if f0.startswith("#"):
        return "header", f0
    if PRODUCT_START_RE.match(f0):
        name = fields[1].strip() if len(fields) > 1 else ""
        req = fields[2].strip() if len(fields) > 2 else ""
        return "product", {"okpd2_field": f0, "name": name, "req": req}
    if f0.startswith("(") and ("ред." in f0 or "Постановлени" in f0):
        return "amendment", f0
    if f0 == "":
        req = ""
        if len(fields) > 2:
            req = fields[2].strip()
        if not req and len(fields) > 1:
            req = fields[1].strip()
        return ("component", req) if req else ("empty", None)
    # запасной случай: непустой первый столбец, но не код и не аннотация
    return "component", fields[-1].strip()


def parse_section(text: str) -> tuple[str, list[Product]]:
    """Возвращает (заголовок раздела, список продуктов)."""
    header = ""
    products: list[Product] = []
    pending_amendments: list[str] = []
    warnings = 0

    for row in iter_rows(text):
        kind, payload = classify(split_fields(row))
        if kind == "header":
            header = str(payload).lstrip("# ").strip()
        elif kind == "product":
            p = Product(
                okpd2_field=payload["okpd2_field"],  # type: ignore[index]
                name_raw=payload["name"],  # type: ignore[index]
                okpd2_codes=OKPD2_RE.findall(payload["okpd2_field"]),  # type: ignore[index]
                position=len(products) + 1,
            )
            if payload["req"]:  # type: ignore[index]
                p.component_cells.append(payload["req"])  # type: ignore[index]
            p.amendments.extend(pending_amendments)
            pending_amendments = []
            products.append(p)
        elif kind == "component":
            if products:
                products[-1].component_cells.append(str(payload))
            else:
                warnings += 1
        elif kind == "amendment":
            if products:
                products[-1].amendments.append(str(payload))
            else:
                pending_amendments.append(str(payload))
        # 'empty' игнорируем

    if warnings:
        print(f"  ⚠ {warnings} строк-компонентов до первого продукта (пропущены)")
    return header, products


# --------------------------------------------------------------------------- #
# Нормализация через DeepSeek
# --------------------------------------------------------------------------- #
@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0

    def add(self, u) -> None:
        self.calls += 1
        self.prompt_tokens += getattr(u, "prompt_tokens", 0) or 0
        self.completion_tokens += getattr(u, "completion_tokens", 0) or 0

    def cost_usd(self) -> float:
        return (
            self.prompt_tokens / 1_000_000 * PRICE_IN_USD_PER_1M
            + self.completion_tokens / 1_000_000 * PRICE_OUT_USD_PER_1M
        )


def make_client():
    from openai import OpenAI

    if not settings.DEEPSEEK_API_KEY:
        sys.exit("DEEPSEEK_API_KEY пуст — заполни .env")
    return OpenAI(
        api_key=settings.DEEPSEEK_API_KEY,
        base_url=settings.DEEPSEEK_BASE_URL,
    )


def call_llm(client, user_prompt: str, usage: Usage, retries: int = 3) -> dict:
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            resp = client.chat.completions.create(
                model=settings.DEEPSEEK_MODEL,
                messages=[
                    {"role": "system", "content": KB_PARSER_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0,
                max_tokens=8000,
            )
            usage.add(resp.usage)
            return json.loads(resp.choices[0].message.content)
        except Exception as e:  # noqa: BLE001 — сеть нестабильна, ретраим всё
            last_err = e
            wait = 2 * attempt
            print(f"    попытка {attempt}/{retries} не удалась: {e}; жду {wait}с")
            time.sleep(wait)
    raise RuntimeError(f"DeepSeek недоступен после {retries} попыток: {last_err}")


def batch_cells(cells: list[str], max_chars: int) -> list[list[str]]:
    batches: list[list[str]] = []
    cur: list[str] = []
    size = 0
    for c in cells:
        if cur and size + len(c) > max_chars:
            batches.append(cur)
            cur, size = [], 0
        cur.append(c)
        size += len(c)
    if cur:
        batches.append(cur)
    return batches or [[]]


def empty_stub(product: Product, roman: str, title: str) -> dict:
    """Продукт без требований во фрагменте — отдаём без вызова LLM (иначе модель
    дофантазирует несуществующие операции и баллы)."""
    return {
        "section_roman": roman,
        "section_title": title,
        "product_name": product.name_raw.strip(),
        "okpd2_codes": product.okpd2_codes,
        "tnved_codes": [],
        "requirement_type": None,
        "min_threshold": None,
        "requirement_blocks": [],
        "amendments": product.amendments,
        "source_anchor": f"Приложение к ПП №719, Раздел {roman}, позиция {product.position}",
        "notes": "Во фрагменте приложения требования (операции/баллы) для этого вида продукции не указаны.",
    }


def normalize_product(
    client, product: Product, roman: str, title: str, usage: Usage, max_chars: int
) -> dict:
    # Нет требований во фрагменте → не зовём LLM (защита от галлюцинаций на пустом блоке)
    if not any(c.strip() for c in product.component_cells):
        return empty_stub(product, roman, title)

    batches = batch_cells(product.component_cells, max_chars)
    merged: dict | None = None
    for i, batch in enumerate(batches):
        raw = product.name_raw.strip()
        if batch:
            raw += "\n" + "\n\n".join(batch)
        user = build_kb_parser_user_prompt(roman, title, product.okpd2_codes, raw)
        data = call_llm(client, user, usage)
        if merged is None:
            merged = data
            merged.setdefault("requirement_blocks", [])
        else:
            merged["requirement_blocks"].extend(data.get("requirement_blocks", []) or [])

    assert merged is not None
    # Санитизация: «0 баллов» в ПП719 не бывает — это модель так кодирует «нет балла».
    for b in merged.get("requirement_blocks", []):
        for o in b.get("operations") or []:
            p = o.get("points")
            if isinstance(p, (int, float)) and p <= 0:
                o["points"] = None
    return {
        "section_roman": roman,
        "section_title": title,
        "product_name": merged.get("product_name") or product.name_raw.strip(),
        "okpd2_codes": product.okpd2_codes,
        "tnved_codes": [],
        "requirement_type": merged.get("requirement_type"),
        "min_threshold": merged.get("min_threshold"),
        "requirement_blocks": merged.get("requirement_blocks", []),
        "amendments": product.amendments,
        "source_anchor": f"Приложение к ПП №719, Раздел {roman}, позиция {product.position}",
        "notes": merged.get("notes"),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def find_chunk(section: str | None, file_arg: str | None) -> Path:
    if file_arg:
        p = Path(file_arg)
        return p if p.is_absolute() else (ROOT / p)
    matches = sorted(CHUNKS_DIR.glob(f"*_{section}_*.txt"))
    if not matches:
        sys.exit(f"Не нашёл чанк для раздела '{section}' в {CHUNKS_DIR}")
    if len(matches) > 1:
        # Разделы-приложения с продукцией — файлы 02..10; процедурные части — 11+.
        # Для структуризации нужны приложения, поэтому предпочитаем их.
        appendix = [m for m in matches if (m.name[:2].isdigit() and 2 <= int(m.name[:2]) <= 10)]
        if len(appendix) == 1:
            return appendix[0]
        names = ", ".join(m.name for m in matches)
        sys.exit(f"Неоднозначно для '{section}' ({len(matches)} файлов): {names}. Уточни --file.")
    return matches[0]


def out_name(chunk_path: Path) -> str:
    # "03_II_Продукция_..." -> "II_Продукция_..."
    return re.sub(r"^\d+_", "", chunk_path.stem) + ".json"


def roman_from_name(chunk_path: Path) -> str:
    m = re.match(r"^\d+_([IVX]+)_", chunk_path.name)
    return m.group(1) if m else "?"


def main() -> None:
    ap = argparse.ArgumentParser(description="Структуризация раздела ПП №719 в JSON")
    ap.add_argument("--section", help="римский номер раздела, напр. II")
    ap.add_argument("--file", help="путь к чанку (альтернатива --section)")
    ap.add_argument("--dry-run", action="store_true", help="без вызова API")
    ap.add_argument("--limit", type=int, default=0, help="обработать первые N продуктов")
    ap.add_argument("--max-chars", type=int, default=9000, help="порог батча на продукт")
    args = ap.parse_args()

    if not args.section and not args.file:
        ap.error("укажи --section II или --file <путь>")

    chunk = find_chunk(args.section, args.file)
    roman = args.section or roman_from_name(chunk)
    text = chunk.read_text(encoding="utf-8")
    header, products = parse_section(text)
    title = header.split(".", 1)[-1].strip() if header else chunk.stem

    print(f"Раздел: {roman} — {title}")
    print(f"Файл:   {chunk.relative_to(ROOT)}")
    print(f"Найдено продуктов: {len(products)}")
    for p in products:
        size = len(p.raw_block())
        print(
            f"  [{p.position}] {p.name_raw.strip()[:60]!r} "
            f"| коды={p.okpd2_codes} | компонентов={len(p.component_cells)} "
            f"| {size} симв | поправок={len(p.amendments)}"
        )

    if args.limit:
        products = products[: args.limit]
        print(f"\n(--limit {args.limit}: обрабатываю только первые {len(products)})")

    if args.dry_run:
        print("\n[dry-run] API не вызывался. Разбор на кандидатов готов.")
        return

    client = make_client()
    usage = Usage()
    results = []
    for p in products:
        print(f"\n→ Продукт [{p.position}] {p.name_raw.strip()[:50]}…")
        results.append(normalize_product(client, p, roman, title, usage, args.max_chars))
        print(f"  ok: {len(results[-1]['requirement_blocks'])} блоков требований")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / out_name(chunk)
    out_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    cost_usd = usage.cost_usd()
    print("\n" + "=" * 60)
    print(f"Записано: {out_path.relative_to(ROOT)} ({len(results)} продуктов)")
    print(f"Вызовов API: {usage.calls}")
    print(f"Токены: вход={usage.prompt_tokens}, выход={usage.completion_tokens}")
    print(
        f"Оценка стоимости: ${cost_usd:.4f}  ≈  ¥{cost_usd * USD_TO_CNY:.2f}  "
        f"(по ценам {PRICE_IN_USD_PER_1M}/{PRICE_OUT_USD_PER_1M} USD за 1M)"
    )


if __name__ == "__main__":
    main()
