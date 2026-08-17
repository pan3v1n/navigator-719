"""Текстовая заземлённость ответа — то, чего не ловит числовой гард (EV4, issue #81).

ЗАЧЕМ. `unverified_numbers` сверяет с контекстом только числа рядом с «балл» и «%». Искажение
НАЗВАНИЯ операции, документа или формулировки требования не поймает никто — а это ровно тот класс
«правдоподобно, но неверно», который в проекте объявлен главным врагом. Пример, который гард
пропустит молча: в контексте «сварка кузова», в ответе «сварка и окраска кузова» — числа целы,
факт искажён.

КАК МЕРИМ (методика гайда, §1.3: «n-граммное пересечение с контекстом + ручная валидация на
20 кейсах»). Утверждение ответа заземлено, если его словесные 3-граммы встречаются в контексте.
Три решения, без которых метрика меряла бы не то:

1. **Проверяем только утверждения со ссылкой [N].** Правило 3б промпта требует ставить ссылку на
   позицию-источник у КАЖДОГО факта, а правило 4 разрешает связки «пиши живо». Значит текст сам
   размечен автором: [N] — заявка на факт, без [N] — связка. Проверяй мы всё подряд — метрика
   штрафовала бы за «Смотрите, что нашлось по вашей продукции», то есть за предписанное поведение
   (тот же капкан, в который попала первая версия метрики полноты).
2. **Сравниваем по стеммам** (`app/rag/sparse.tokenize`, тот же Snowball, что в BM25): русская
   морфология иначе даст ложные расхождения на падежах — «сварка кузова» против «сварки кузова».
3. **Считаем ДОЛЮ покрытых 3-грамм, а не факт совпадения**: пересказ своими словами законен,
   выдумка — нет, и разделяет их именно доля.

Побочно считается детерминированная проверка правила 3б: предложения, которые выглядят фактом
(баллы, проценты, «требуется/должен», названия документов), но ссылки [N] не несут.

⚠ ПОРОГ. Значение `--threshold` откалибровано вручную на 20 кейсах (см. docs/eval_runs) и без
повторной ручной сверки не двигается: метрика без валидации порога — это генератор шума.

Запуск (нужны Qdrant и DEEPSEEK_API_KEY):
  .venv/Scripts/python.exe scripts/eval_text_faithfulness.py --report docs/eval_text_faith_report.md
  .venv/Scripts/python.exe scripts/eval_text_faithfulness.py --show-claims   # ручная валидация
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.rag.pipeline import answer, format_context  # noqa: E402
from app.rag.sparse import tokenize  # noqa: E402

GOLDEN = ROOT / "scripts" / "eval_golden.json"

CITE_RE = re.compile(r"\[\s*\d+\s*\]")
# Предложение «пахнет фактом»: балл/процент/модальность требования/документ. Нужно только для
# побочной проверки правила 3б — какие утверждения остались без ссылки.
FACTUAL_RE = re.compile(
    r"\bбалл\w*|\bпроцент\w*|\b%|требуется|должн\w+|обязан\w*|не менее|не более|"
    r"акт\w*\s+эксперт|сертификат|ГОСТ|операци\w+", re.I)
# Служебные строки, которые фактом не являются: заголовки блоков и приглашения уточнить.
SKIP_RE = re.compile(r"^\s*\*\*[^*]+:?\*\*\s*$|^\s*[-–—]\s*$", re.M)

# ⚠ МЕТА-УТВЕРЖДЕНИЯ — про САМ ОТВЕТ, а не про продукцию, и почти все ПРЕДПИСАНЫ промптом:
# «Порог: в контексте не указан» (правило 2), «Что уточнить» (шаблон правила 4), «показаны 60 из
# 676 операций» (правило 2а), «уточните категорию» (правило 1г), «Хотите, поясню…» (правило 1ж).
# Первая версия метрики честно посчитала их незаземлёнными — и получила 0.568, то есть объявила
# сломанным ровно то поведение, которого инструкция требует. Тот же капкан уже был у метрики
# полноты («полнота 0.78» штрафовала уточняющие ответы) — и он же обесценил бы эту метрику:
# оптимизируй по ней, и система перестанет говорить «порога нет» и «список неполный».
META_RE = re.compile(
    r"в контексте|уточните|что уточнить|хотите,|предварительно|проверьте|"
    r"показан\w*\s+\d+\s+из\s+\d+|неполн|обратите внимание|как я привёл|те же, что", re.I)

# 2-граммы, а не 3: цель — поймать ВЫДУМАННУЮ СВЯЗКУ («сварка и окраска кузова» там, где в
# первоисточнике только «сварка кузова»), не наказывая за законное сжатие перечня. На 3-граммах
# ручная валидация показала, что перечень, свёрнутый в скобки, теряет покрытие целиком, хотя
# каждый его термин заземлён.
N = 2


def _ngrams(text: str, n: int = N) -> set[tuple[str, ...]]:
    toks = tokenize(text)
    if len(toks) < n:
        return {(t,) for t in toks}          # короткое утверждение сверяем по словам
    return {tuple(toks[i:i + n]) for i in range(len(toks) - n + 1)}


def claims(text: str) -> list[str]:
    """Утверждения ответа: строки списка и предложения, несущие ссылку [N].

    ⚠ Строки Markdown-таблицы исключены намеренно. Длинный перечень баллов печатает КОД
    (`pipeline.points_table`, P4) — дословно из контекста, поэтому исказить там нечего, а ссылка
    [N] у такой строки не нужна. Без этого исключения метрика тонула в 60 строках таблицы и
    объявляла «фактом без ссылки» то, что заземлено по построению."""
    out = []
    for raw in (text or "").split("\n"):
        line = raw.strip(" -•\t")
        if not line or SKIP_RE.match(raw) or line.startswith("|"):
            continue
        # длинную строку режем на предложения: ссылка [N] относится к своему утверждению
        parts = re.split(r"(?<=[.;])\s+(?=[А-ЯA-Z«\-])", line) if len(line) > 200 else [line]
        out.extend(p.strip() for p in parts if p.strip())
    return out


def grounded_share(claim: str, ctx_grams: set[tuple[str, ...]]) -> float:
    grams = _ngrams(claim)
    if not grams:
        return 1.0
    return len(grams & ctx_grams) / len(grams)


# Второй сигнал, более узкий и потому более точный: СЛОВА, которых в контексте нет вовсе.
# Законный пересказ переставляет и сжимает слова первоисточника, выдумка приносит НОВЫЕ. Общая
# лексика («который», «например», «также») к делу не относится и вычитается — иначе сигнал утонет
# в связках, которые правило 4 промпта прямо разрешает.
GENERIC = set(tokenize(
    "который которая которое если это тот такой также например поэтому значит нужно надо предстоит "
    "смотрите обратите внимание важно учесть можно стоит будет является относится входит зависит "
    "ваш ваша ваше наш свой каждый другой прочий остальной либо здесь там сейчас далее выше ниже "
    "позиция раздел код продукция требование операция балл процент первоисточник эксперт документ "
    "приложение постановление контекст ответ вопрос случай вид тип часть перечень список"))


def novel_words(claim: str, ctx_words: set[str]) -> list[str]:
    """Содержательные слова утверждения, которых нет в контексте (кандидаты в выдумку)."""
    return sorted({w for w in tokenize(claim)
                   if len(w) >= 4 and w not in ctx_words and w not in GENERIC and not w.isdigit()})


def evaluate(limit: int, cases_limit: int, threshold: float) -> list[dict]:
    cases = [c for c in json.loads(GOLDEN.read_text(encoding="utf-8"))["cases"] if c["in_scope"]]
    if cases_limit:
        cases = cases[:cases_limit]

    rows = []
    for c in cases:
        ans = answer(c["query"], okpd2=c.get("okpd2") or None, limit=limit)
        text = ans.text or ""
        # Заземление — ровно то, что видела модель: контекст позиций плюс кейсы эксперта.
        ctx = format_context(ans.hits, c["query"])
        ctx_grams = _ngrams(ctx) | _ngrams(c["query"])  # вопрос пользователя — тоже законный источник
        ctx_words = set(tokenize(ctx)) | set(tokenize(c["query"]))
        scored, unmarked, meta, novel = [], [], 0, []
        for cl in claims(text):
            if META_RE.search(cl):     # утверждение про сам ответ/контекст — не факт о продукции
                meta += 1
                continue
            if CITE_RE.search(cl):
                bare = CITE_RE.sub(" ", cl)
                scored.append((cl, grounded_share(bare, ctx_grams)))
                nw = novel_words(bare, ctx_words)
                if nw:
                    novel.append((cl, nw))
            elif FACTUAL_RE.search(cl):
                unmarked.append(cl)
        rows.append({
            "id": c["id"], "query": c["query"],
            "claims": scored,
            "n_claims": len(scored),
            "meta": meta,
            "weak": [(cl, s) for cl, s in scored if s < threshold],
            "unmarked": unmarked,
            "novel": novel,
        })
    return rows


def report(rows: list[dict], threshold: float, show_claims: bool) -> list[str]:
    all_scores = [s for r in rows for _cl, s in r["claims"]]
    n_claims = len(all_scores)
    weak = sum(len(r["weak"]) for r in rows)
    unmarked = sum(len(r["unmarked"]) for r in rows)
    L = ["=" * 78,
         f"ТЕКСТОВАЯ ЗАЗЕМЛЁННОСТЬ (EV4) — {len(rows)} кейсов, {n_claims} утверждений со ссылкой [N]",
         "=" * 78]
    if n_claims:
        L += [f"  ЗАЗЕМЛЁННЫХ (доля {N}-грамм в контексте ≥ {threshold}): "
              f"{n_claims - weak}/{n_claims} = {(n_claims - weak)/n_claims:.3f}",
              f"  медиана покрытия: {statistics.median(all_scores):.3f} · "
              f"минимум: {min(all_scores):.3f}",
              f"  БЕЗ ССЫЛКИ [N], но похоже на факт (правило 3б): {unmarked}",
              f"  исключено мета-утверждений (про сам ответ, предписаны промптом): "
              f"{sum(r['meta'] for r in rows)}",
              ""]
    L += ["  ⚠ Метрика читается ТОЛЬКО в паре с полнотой (eval_completeness.py): «ничего лишнего»",
          "     тривиально доводится до 1.00 молчанием.",
          ""]
    novel_n = sum(len(r["novel"]) for r in rows)
    L.insert(5, f"  СО СЛОВАМИ ВНЕ КОНТЕКСТА (второй сигнал, кандидаты в выдумку): "
                f"{novel_n}/{n_claims}" if n_claims else "")
    if show_claims:
        if novel_n:
            L.append("СЛОВА, КОТОРЫХ НЕТ В КОНТЕКСТЕ (ручная сверка — здесь и живёт искажение):")
            for r in rows:
                for cl, nw in r["novel"]:
                    L.append(f"  {r['id']}: {nw} ← {cl[:110]}")
            L.append("")
        L.append("СЛАБО ЗАЗЕМЛЁННЫЕ УТВЕРЖДЕНИЯ (для ручной валидации порога):")
        for r in rows:
            for cl, s in sorted(r["weak"], key=lambda x: x[1]):
                L.append(f"  [{s:.2f}] {r['id']}: {cl[:150]}")
        L.append("")
        if unmarked:
            L.append("ФАКТ БЕЗ ССЫЛКИ [N]:")
            for r in rows:
                for cl in r["unmarked"]:
                    L.append(f"  {r['id']}: {cl[:150]}")
            L.append("")
    else:
        L.append("Слабых утверждений по кейсам:")
        for r in rows:
            mark = f"  ⚠ {len(r['weak'])}" if r["weak"] else ""
            L.append(f"  {r['id']:>10}: утверждений {r['n_claims']:>3}{mark}  {r['query'][:44]}")
        L.append("")
    return L


def main() -> None:
    ap = argparse.ArgumentParser(description="EV4 текстовая заземлённость (вызывает DeepSeek)")
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--cases", type=int, default=0, help="ограничить число кейсов")
    ap.add_argument("--threshold", type=float, default=0.5,
                    help="доля покрытых 3-грамм, ниже которой утверждение считается незаземлённым")
    ap.add_argument("--show-claims", action="store_true", help="печатать сами утверждения (валидация)")
    ap.add_argument("--report", type=str, default="")
    args = ap.parse_args()

    try:
        from app.rag.retriever import _client
        _client().get_collections()
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Qdrant недоступен ({e}).")

    rows = evaluate(args.limit, args.cases, args.threshold)
    text = "\n".join(report(rows, args.threshold, args.show_claims))
    print("\n" + text)
    if args.report:
        path = (ROOT / args.report) if not Path(args.report).is_absolute() else Path(args.report)
        path.write_text("# EV4 текстовая заземлённость\n\n```\n" + text + "\n```\n", encoding="utf-8")
        print(f"\nОтчёт сохранён: {path}")


if __name__ == "__main__":
    main()
