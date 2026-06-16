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
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
# Колонка-этап в таблицах пороговых баллов: «с 1 января 2026 г.», «до 30 июня 2023 г.»
DATE_STAGE_RE = re.compile(r"(?:с|до)\s+\d{1,2}\s+\S+\s+\d{4}\s*г")
BALL_RE = re.compile(r"\d+(?:[.,]\d+)?\s*балл")
# Прозовые пороги/потолки баллов в текстовых абзацах раздела (не строки-таблицы):
CAP_RE = re.compile(r"не\s+(?:более|менее|ниже)\s*\d+(?:[.,]\d+)?\s*балл")
YEAR_RE = re.compile(r"\b\d{4}\s*(?:год|г\.?)|\b(?:с|до|со)\s+\d{1,2}\s+\S+\s+\d{4}|\b(?:с|до)\s+\d{4}\s*год")


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
    joined = " ".join(c.strip() for c in fields)
    # Заголовок таблицы пороговых баллов: «Код…|Наименование продукции|с 1 января … г.|…».
    # Запоминаем ярлыки-этапы (колонки после наименования), чтобы привязать к ним баллы.
    # Настоящий заголовок не начинается с кода продукта — иначе это строка-продукт,
    # «склеенная» буфером с таблицей ниже (её отдаём как продукт, а не глотаем).
    if (
        not PRODUCT_START_RE.match(f0)
        and ("Наименование продукци" in joined or f0.startswith("Код по ОК"))
        and DATE_STAGE_RE.search(joined)
    ):
        return "stage_header", [c.strip() for c in fields[2:] if c.strip()]
    if PRODUCT_START_RE.match(f0):
        name = fields[1].strip() if len(fields) > 1 else ""
        # ВСЕ ячейки требований/баллов после наименования (а не только третья колонка —
        # в разделе X пороги идут несколькими столбцами по датам/этапам).
        req_cells = [c.strip() for c in fields[2:] if c.strip()]
        return "product", {"okpd2_field": f0, "name": name, "req_cells": req_cells}
    if f0.startswith("(") and ("ред." in f0 or "Постановлени" in f0):
        return "amendment", f0
    if f0 == "":
        req_cells = [c.strip() for c in fields[1:] if c.strip()]
        return ("component", req_cells) if req_cells else ("empty", None)
    # запасной случай: непустой первый столбец, но не код и не аннотация
    tail = fields[-1].strip()
    return ("component", [tail]) if tail else ("empty", None)


def make_req_text(req_cells: list[str], stage_labels: list[str]) -> str:
    """Склеивает ячейки требований одной строки в текст для LLM.

    Многоколоночная строка пороговых баллов («215 баллов|255 баллов|…») собирается в
    одну осмысленную строку с привязкой к датам-этапам из заголовка таблицы — чтобы
    модель по правилу 5 промпта положила её в min_threshold, а не выдумала операции.
    Обычная одиночная ячейка возвращается как есть (поведение для разделов I–IX).
    """
    ball_cells = [c for c in req_cells if BALL_RE.search(c)]
    if len(req_cells) >= 2 and len(ball_cells) == len(req_cells):
        if stage_labels and len(stage_labels) == len(ball_cells):
            parts = [f"{lbl} — {val}" for lbl, val in zip(stage_labels, ball_cells)]
        else:
            parts = ball_cells
        return "Пороговое количество баллов по этапам: " + "; ".join(parts)
    return "\n".join(req_cells)


def parse_section(text: str) -> tuple[str, list[Product]]:
    """Возвращает (заголовок раздела, список продуктов)."""
    header = ""
    products: list[Product] = []
    pending_amendments: list[str] = []
    stage_labels: list[str] = []  # колонки-этапы текущей таблицы пороговых баллов
    warnings = 0

    for row in iter_rows(text):
        kind, payload = classify(split_fields(row))
        if kind == "header":
            header = str(payload).lstrip("# ").strip()
        elif kind == "stage_header":
            stage_labels = list(payload)  # type: ignore[arg-type]
        elif kind == "product":
            p = Product(
                okpd2_field=payload["okpd2_field"],  # type: ignore[index]
                name_raw=payload["name"],  # type: ignore[index]
                okpd2_codes=OKPD2_RE.findall(payload["okpd2_field"]),  # type: ignore[index]
                position=len(products) + 1,
            )
            req_text = make_req_text(payload["req_cells"], stage_labels)  # type: ignore[index]
            if req_text:
                p.component_cells.append(req_text)
            p.amendments.extend(pending_amendments)
            pending_amendments = []
            products.append(p)
        elif kind == "component":
            if products:
                products[-1].component_cells.append(make_req_text(payload, stage_labels))  # type: ignore[arg-type]
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
    _lock: "threading.Lock" = field(default_factory=lambda: threading.Lock())

    def add(self, u) -> None:
        # потокобезопасно: при --workers>1 add зовётся из нескольких потоков
        with self._lock:
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


def collect_section_thresholds(text: str) -> list[str]:
    """Дословные прозовые пороги/потолки баллов из текстовых абзацев раздела.

    Это пороги уровня раздела, не привязанные к конкретной строке-продукту: потолки
    НИОКР («не более 1283 баллов в 2024 году»), ступенчатые годовые минимумы
    («с 2028 года - не менее 64 баллов»). Строки-таблицы (с разделителем `|`) сюда не
    берём — они уже структурированы по продуктам. Возвращает строки без дублей, по порядку.
    """
    out: list[str] = []
    seen: set[str] = set()
    for ln in text.splitlines():
        s = ln.strip()
        if "|" in s:
            continue
        if BALL_RE.search(s) and (CAP_RE.search(s) or YEAR_RE.search(s)) and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def methodology_record(roman: str, title: str, lines: list[str]) -> dict:
    """Секционная запись-методичка: дословные прозовые пороги раздела (для RAG и полноты)."""
    return {
        "section_roman": roman,
        "section_title": title,
        "record_type": "section_methodology",
        "product_name": f"Методологические пороги раздела {roman}",
        "okpd2_codes": [],
        "tnved_codes": [],
        "requirement_type": None,
        "min_threshold": None,
        "requirement_blocks": [],
        "methodology_thresholds": lines,
        "amendments": [],
        "source_anchor": f"Приложение к ПП №719, Раздел {roman} (методологические положения)",
        "notes": (
            "Пороги/потолки баллов из текстовых абзацев раздела, не привязанные к "
            "конкретному виду продукции (потолки НИОКР, ступенчатые годовые минимумы). "
            "Сохранены дословно. Точная привязка к продуктам — доразбор V2."
        ),
    }


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
    ap.add_argument(
        "--workers", type=int, default=6,
        help="параллельных потоков к API (1 = последовательно; при нестабильной сети снизь)",
    )
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
    results: list[dict | None] = [None] * len(products)

    try:
        from tqdm import tqdm
    except ImportError:
        tqdm = None  # type: ignore[assignment]

    bar = (
        tqdm(total=len(products), unit="прод", desc=f"Раздел {roman}", dynamic_ncols=True)
        if tqdm is not None
        else None
    )

    def tick(p: Product, res: dict) -> None:
        if bar is not None:
            bar.update(1)
            bar.set_postfix_str(
                f"{p.name_raw.strip()[:22]} | блоков={len(res['requirement_blocks'])} "
                f"| API={usage.calls} | ¥{usage.cost_usd() * USD_TO_CNY:.2f}"
            )
        else:
            print(
                f"→ [{p.position}/{len(products)}] {p.name_raw.strip()[:50]}… "
                f"ok: {len(res['requirement_blocks'])} блоков"
            )

    workers = max(1, args.workers)
    if workers == 1:
        for i, p in enumerate(products):
            res = normalize_product(client, p, roman, title, usage, args.max_chars)
            results[i] = res
            tick(p, res)
    else:
        # Продукты независимы → шлём в DeepSeek параллельно. Порядок результатов
        # восстанавливаем по индексу (as_completed возвращает в произвольном порядке).
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {
                ex.submit(normalize_product, client, p, roman, title, usage, args.max_chars): i
                for i, p in enumerate(products)
            }
            for fut in as_completed(futs):
                i = futs[fut]
                res = fut.result()
                results[i] = res
                tick(products[i], res)
    if bar is not None:
        bar.close()

    # Секционная запись-методичка: дословные прозовые пороги раздела (не привязаны к
    # продукту). Детерминированно, без LLM — гарантирует, что ни одно «N баллов» из
    # текстовых абзацев не теряется (потолки НИОКР, ступенчатые минимумы).
    metho = collect_section_thresholds(text)
    if metho:
        results.append(methodology_record(roman, title, metho))
        print(f"+ запись-методичка: {len(metho)} прозовых порогов раздела")

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
