"""Прогон набора приёмочных кейсов через navigate() и выгрузка результата.

Готовит для эксперта Курской ТПП: docs/test_cases.csv (Excel) + docs/test_cases.md
(читаемо, с полным текстом «Анализа» по каждому кейсу). Колонки оценки эксперта —
пустые, заполняются вручную.

Запуск (нужен поднятый Qdrant и DEEPSEEK_API_KEY в .env):
  .venv\\Scripts\\python scripts\\run_test_cases.py
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.tools.navigator import navigate  # noqa: E402

# (описание продукции для ввода, код ОКПД2 или "" , что проверяем)
CASES: list[tuple[str, str, str]] = [
    ("Производим станки для обработки камня, керамики и бетона",
     "28.49.11",
     "Станкоинструмент, балльно-смешанный тип (порог растёт по годам)"),
    ("Изготавливаем шариковые и роликовые подшипники",
     "28.15.10",
     "Спецмашиностроение, балльный тип"),
    ("Выпускаем светодиоды и светодиодные модули по технологии chip-on-board",
     "26.11.22.200",
     "Фотоника/светотехника, перечень операций"),
    ("Производим башенные и портальные грузоподъёмные краны",
     "29.22.14.400",
     "Тяжёлое машиностроение, доля иностранных товаров (%)"),
    ("Шьём медицинские маски одноразовые",
     "32.50.50.190",
     "Медизделия, смешанный тип, не менее 25 баллов"),
    ("Собираем ноутбуки и планшетные компьютеры",
     "26.20.11",
     "Радиоэлектроника, смешанный, 20 баллов"),
    ("Производим лекарственные препараты, сыворотки и вакцины",
     "21.20.1",
     "Фармацевтика, перечень операций"),
    ("Выпускаем автомобили скорой медицинской помощи",
     "29.10.2",
     "Автомобилестроение, балльный тип"),
    ("Производим оптические волокна для связи",
     "27.31.12.110",
     "Фотоника, балльный тип, не менее 60 баллов"),
    # код НЕ задан — проверка семантического поиска + извлечения позиции
    ("Наше предприятие выпускает стреловые грузоподъёмные краны",
     "",
     "Без кода ОКПД2: проверка семантического поиска"),
    # код внутри текста — проверка извлечения кода из свободного текста
    ("Производим пеностекло в форме блоков и плит, код ОКПД2 23.19.12.160",
     "",
     "Код в тексте: проверка извлечения ОКПД2"),
    # заведомо вне сферы 719 — проверка поведения на нерелевантном вводе
    ("Выпекаем хлеб и кондитерские изделия",
     "",
     "Вне сферы 719: продукция не промышленная — ожидаем корректную оговорку"),
]


def short(text: str, n: int = 280) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def main() -> None:
    rows = []
    md = ["# Приёмочные кейсы — Навигатор ПП №719\n",
          "Реальный вывод сервиса (V1-MVP). Колонки «Оценка эксперта» и «Комментарий» — "
          "заполняются экспертом Курской ТПП. Критерий приёмки: ≥70% корректных.\n",
          "> Ответы сервиса — предварительный анализ, не заключение.\n"]

    for i, (query, okpd2, checks) in enumerate(CASES, 1):
        print(f"[{i}/{len(CASES)}] {query[:50]} …", flush=True)
        try:
            nav = navigate(query, okpd2=okpd2 or None, limit=5)
            top = nav.sources[0] if nav.sources else None
            top_name = top.product_name if top else "—"
            if top is None:
                match = "—"
            elif top.okpd2_match:
                match = "✅ по коду"
            else:
                match = f"score {top.score:.2f}"
            thr = (top.min_threshold if top and top.min_threshold else "—")
            n_src = len(nav.sources)
            answer_full = nav.answer or ""
            err = ""
        except Exception as e:  # noqa: BLE001
            top_name = match = thr = "ОШИБКА"
            n_src = 0
            answer_full = ""
            err = str(e)

        rows.append({
            "№": i,
            "Ввод: продукция": query,
            "Ввод: ОКПД2": okpd2 or "(не задан)",
            "Что проверяем": checks,
            "Выход: позиция 719 (топ-1)": top_name,
            "Выход: совпадение": match,
            "Выход: порог/баллы": thr,
            "Найдено позиций": n_src,
            "Выход: краткий анализ": short(answer_full),
            "Оценка эксперта (верно/частично/неверно)": "",
            "Комментарий эксперта": "",
        })

        # подробный блок в MD
        md.append(f"\n## Кейс {i}\n")
        md.append(f"**Ввод (продукция):** {query}  ")
        md.append(f"\n**Ввод (ОКПД2):** {okpd2 or '(не задан)'}  ")
        md.append(f"\n**Что проверяем:** {checks}\n")
        if err:
            md.append(f"\n**ОШИБКА:** {err}\n")
        else:
            md.append(f"\n**Применимая позиция 719 (топ-1):** {top_name} — {match}  ")
            md.append(f"\n**Порог/баллы:** {thr} · **Найдено позиций:** {n_src}\n")
            md.append("\n**Анализ сервиса:**\n\n")
            md.append(answer_full + "\n")
        md.append("\n**Оценка эксперта:** ____________  **Комментарий:** ____________\n")
        md.append("\n---\n")

    out_csv = ROOT / "docs" / "test_cases.csv"
    out_md = ROOT / "docs" / "test_cases.md"

    with out_csv.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    out_md.write_text("\n".join(md), encoding="utf-8")

    print(f"\nГотово:\n  {out_csv}\n  {out_md}")


if __name__ == "__main__":
    main()
