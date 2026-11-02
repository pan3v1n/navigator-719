"""Конвейер актуализации корпуса под новую редакцию ПП №719 (`A10`).

ЗАЧЕМ. `docs/ACTUALIZATION.md` — это 24 урока и десяток скриптов, которые связывал человек. Здесь они
связаны в один прогон: номер документа Контура на входе, ветка с данными и отчёт на выходе.

ЧЕГО КОНВЕЙЕР НЕ ДЕЛАЕТ. На бой не выкатывает и в `dev` не сливает: решение о новой редакции принимает
человек (`AGENTS_PLAN` §A6). Запускается только на отдельной ветке, остальное защищено `step_guard`.

ПОЧЕМУ СТОП, А НЕ ПОПЫТКА. Цена тихой ошибки — неверный балл в ответе эксперту. Условие, которое можно
доказать, останавливает прогон. То, что доказать нельзя, попадает в отчёт для чтения глазами: `verify`
сверяет множества чисел и подмены баллов между операциями не видит, поэтому по каждой изменённой записи
отчёт сверяет пары «текст операции — балл» с ячейкой источника и называет неподтверждённые.

ОТЧЁТ ПУБЛИЧЕН. Репозиторий открытый, отчёт идёт в тело PR: всё, что попадает в отчёт, проходит `scrub`.

    python scripts/actualize_edition.py --doc-id 508574 --until chunks     # без модели и без затрат
    python scripts/actualize_edition.py --doc-id 508574                    # весь путь, DeepSeek
    python scripts/actualize_edition.py --doc-id 508574 --rtf a.rtf --page a.html   # из файлов
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import edition_source as es  # noqa: E402
import watch_edition as we  # noqa: E402
from app.core.console import enable_utf8  # noqa: E402

KB = ROOT / "knowledge_base" / "pp719"
FULL = KB / "pp719_full.txt"
CHUNKS = KB / "chunks"
STRUCTURED = KB / "structured"
MANIFEST = ROOT / "knowledge_base" / "manifest.yaml"
LANDING = ROOT / "landing" / "index.html"
PROTECTED_BRANCHES = ("main", "dev", "prod")
STEPS = ("guard", "source", "text", "control", "chunks", "link", "plan", "structure", "post", "gates", "tests")
# Сверх этого числа позиций раздел переписан массово: такой перепарс смотрит человек (XVIII, N 1002).
DEFAULT_MAX_LLM = 60
_SECTION_RE = re.compile(r"^# ([IVXL]+)\. ")
_LABEL_RE = re.compile(r"ред\. от (\d{2}\.\d{2}\.\d{4}) N (\d+)")
_SECRET_RE = re.compile(r"""input_value=(['"]).*?\1|sk-[A-Za-z0-9_-]{8,}""")
_SECRET_ENV = ("DEEPSEEK_API_KEY",)
_RESERVED = ("source_anchor", "amendments")     # меняются у неизменённой позиции: нумерация и пометки актов
# Тесты, закреплённые за конкретной редакцией и релизом: на новой редакции им положено падать, правит человек.
# Всё остальное красное — неожиданность: иначе «красная» стала бы нормой и настоящий сигнал утонул.
_PINNED_TESTS = re.compile(r"^(?:tests\.)?test_(?:edition_\d{4}_|deploy_script|ev24_paraphrase_anchors"
                           r"|k15_metodrek_corpus\.TestPassport\.test_radius_editions)")
_REQUIREMENT_RE = re.compile(r"налич|прав[ао]? на|сервисн|обязательн|запрет", re.I)


class Stop(RuntimeError):
    """Условие шага не доказано: дальше без человека нельзя."""


@dataclass
class Ctx:
    doc_id: str
    args: argparse.Namespace
    facts: dict = field(default_factory=dict)
    log: list[dict] = field(default_factory=list)
    new_text: str = ""
    sections: list[str] = field(default_factory=list)      # изменённые разделы приложения
    modes: dict[str, str] = field(default_factory=dict)    # раздел → keep|renumber
    plans: dict[str, dict] = field(default_factory=dict)   # раздел → план `restructure_changed`
    before: dict = field(default_factory=dict)             # снимки базы до изменений
    warnings: list[str] = field(default_factory=list)      # не доказано автоматически: глазами
    surprises: int = 0                                     # падения батареи вне закреплённых за редакцией


def scrub(text: str) -> str:
    """Вывод чужого процесса может нести секрет (`Settings` печатает `input_value`), а отчёт публичен."""
    out = _SECRET_RE.sub("[скрыто]", text)
    for name in _SECRET_ENV:
        value = os.environ.get(name)
        if value:
            out = out.replace(value, "[скрыто]")
    return out


def run(cmd: list[str], check: bool = True, timeout: int = 3600, utf8: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    if utf8:
        env["PYTHONUTF8"] = "1"
    else:
        env.pop("PYTHONUTF8", None)
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=env, timeout=timeout)
    if check and r.returncode != 0:
        raise Stop(f"{' '.join(cmd[:4])}: код {r.returncode}\n{scrub((r.stdout + r.stderr).strip()[-900:])}")
    return r


def py(script: str, *args: str, check: bool = True, timeout: int = 3600) -> subprocess.CompletedProcess:
    return run([sys.executable, f"scripts/{script}", *args], check=check, timeout=timeout)


def git(*args: str, check: bool = True) -> str:
    return run(["git", *args], check=check).stdout


def kb_changes() -> list[str]:
    """Изменённые файлы базы относительно HEAD, включая новые.

    ⚠ Не `git status`: при `core.autocrlf=true` он метит файл с LF в дереве изменённым, хотя блоб тот же.
    """
    diff = git("-c", "core.quotepath=off", "diff", "--name-only", "HEAD", "--", "knowledge_base")
    new = git("-c", "core.quotepath=off", "ls-files", "--others", "--exclude-standard", "--", "knowledge_base")
    return [line.strip() for line in (diff + new).splitlines() if line.strip()]


def write_chunks(text: str) -> None:
    """Чанки из полного текста. Часть пишет `rechunk_appendix`, остальные — `# заголовок + тело`."""
    import parse_rtf
    import rechunk_appendix as rc

    FULL.write_text(text, encoding="utf-8", newline="\n")
    py("rechunk_appendix.py", "--write")
    covered = {f"{v[0]}_{v[1]}.txt" for v in rc.TARGETS.values()} | {rc.NOTES_CHUNK.name, rc.FOOTNOTES_CHUNK.name}
    sections = parse_rtf.split_sections(text)
    for path in sorted(CHUNKS.glob("*.txt")):
        if path.name in covered:
            continue
        header = path.read_text(encoding="utf-8").split("\n", 1)[0].removeprefix("# ")
        found = [body for h, body in sections if h == header]
        if len(found) != 1:
            raise Stop(f"{path.name}: заголовок «{header[:60]}» найден в тексте {len(found)} раз")
        path.write_text(f"# {header}\n\n{found[0]}", encoding="utf-8", newline="\n")


def section_of(path: Path) -> str | None:
    """Римский номер раздела приложения. Правила реестра — свой корпус, в JSON не идут."""
    from watch_edition import _RULES_CHUNKS

    if path.parent != CHUNKS or path.name in _RULES_CHUNKS:
        return None
    m = _SECTION_RE.match(path.read_text(encoding="utf-8").split("\n", 1)[0])
    return m.group(1) if m else None


def known_sections() -> set[str]:
    return {r for r in map(section_of, CHUNKS.glob("*.txt")) if r}


def check_sections(found: list[str], known: set[str]) -> None:
    """Новый или пропавший раздел приложения: чанка под него нет, и позиции молча потерялись бы."""
    new, gone = sorted(set(found) - known), sorted(known - set(found))
    if new or gone:
        raise Stop(f"состав разделов приложения изменился: новые {new}, пропавшие {gone}. "
                   "Новому разделу нужна строка в rechunk_appendix.TARGETS, это делает человек")


def uncovered_lines() -> int:
    """Непустые строки полного текста вне всех чанков: их рост значит, что нарезка что-то потеряла."""
    chunked: set[str] = set()
    for path in CHUNKS.glob("*.txt"):
        chunked.update(line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return sum(1 for line in FULL.read_text(encoding="utf-8").splitlines() if line.strip() and line.strip() not in chunked)


def threshold_snapshot() -> dict[str, str]:
    """anchor → пороги, которые видит модель, включая закупочный. Отдельный процесс: у модулей кэши."""
    code = (
        "import json,sys,glob\n"
        "from app.rag.thresholds import lookup_threshold,lookup_procurement_threshold as lp\n"
        "out={}\n"
        "for f in sorted(glob.glob('knowledge_base/pp719/structured/*.json')):\n"
        "    for r in json.load(open(f,encoding='utf-8')):\n"
        "        if r.get('record_type')=='section_methodology': continue\n"
        "        c,n,s=r.get('okpd2_codes') or [],r.get('product_name') or '',r.get('section_roman')\n"
        "        mt=r.get('min_threshold') or lookup_threshold(c,n,s)\n"
        "        pr=lp(c,n,s)\n"
        "        out[r.get('source_anchor') or n]=f\"{mt or ''} | закупка: {pr or ''}\"\n"
        "sys.stdout.write(json.dumps(out,ensure_ascii=False))\n")
    return json.loads(run([sys.executable, "-c", code], timeout=900).stdout)


def records_by_anchor() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for f in sorted(STRUCTURED.glob("*.json")):
        for r in json.loads(f.read_text(encoding="utf-8")):
            if r.get("record_type") != "section_methodology":
                out[r.get("source_anchor") or r.get("product_name")] = r
    return out


def methodology_snapshot() -> dict[str, str]:
    """Запись-методичка каждого раздела: файл → её текст."""
    out = {}
    for f in sorted(STRUCTURED.glob("*.json")):
        for r in json.loads(f.read_text(encoding="utf-8")):
            if r.get("record_type") == "section_methodology":
                out[f.stem] = json.dumps(r, ensure_ascii=False, sort_keys=True)
    return out


def verify_summary() -> dict[str, int]:
    """Проверки с кодом возврата 0 при любом исходе: итог берём из их строк. Нет строки — стоп."""
    records = py("verify_structured.py", "--records", timeout=900).stdout
    m = re.search(r"ИТОГО: (\d+) расхождений", records)
    if not m:
        raise Stop("verify_structured --records: нет итоговой строки, проверка не состоялась")
    sections = py("verify_structured.py", timeout=900).stdout
    if "Потеряно полностью" not in sections or "Выдуманные баллы" not in sections:
        raise Stop("verify_structured: нет строк сверки разделов, проверка не состоялась")
    reconcile = py("reconcile_kb.py", timeout=900).stdout
    r = re.search(r"ИТОГО чисто: (\d+)/(\d+)", reconcile)
    if not r:
        raise Stop("reconcile_kb: нет итоговой строки, проверка не состоялась")
    return {"расхождений в записях": int(m.group(1)),
            "разделов с потерями": len(re.findall(r"Потеряно полностью \(нет нигде в JSON\): (?!НЕТ)", sections)),
            "разделов с выдуманными баллами": len(re.findall(r"Выдуманные баллы \(нет в законе\): (?!НЕТ)", sections)),
            "разделов не чисто": int(r.group(2)) - int(r.group(1))}


def regressions(before: dict[str, int], after: dict[str, int]) -> list[str]:
    return [f"{k}: {before[k]} → {after[k]}" for k in after if after[k] > before.get(k, 0)]


def record_key(rec: dict) -> str:
    """Запись целиком, без якоря и пометок актов: `renumber` сдвигает номера у всего раздела.

    ⚠ Имя и коды ключом не годятся: в корпусе есть разные записи с одинаковыми именем и кодами, и они
    склеивались бы в одну, давая ложную тревогу.
    """
    return json.dumps({k: v for k, v in rec.items() if k not in _RESERVED}, ensure_ascii=False, sort_keys=True)


def changed_anchors(before: dict[str, dict], after: dict[str, dict], renumbered: frozenset[str] = frozenset()) -> set[str]:
    """Новые и изменённые позиции.

    В перенумерованном разделе якорь сдвигается, поэтому запись ищется среди всех прежних. В остальных
    сравнение по якорю: поиск по всем не заметил бы обмена содержимым двух записей.
    """
    old = {record_key(r) for r in before.values()}
    out = set()
    for anchor, rec in after.items():
        if rec.get("section_roman") in renumbered:
            if record_key(rec) not in old:
                out.add(anchor)
        elif anchor not in before or record_key(before[anchor]) != record_key(rec):
            out.add(anchor)
    return out


def foreign_thresholds(before: dict, after: dict, recs_before: dict, recs_after: dict) -> list[str]:
    """Запись та же, а порог другой: чужой порог. Так поймали магниты и прим. 10 (N 1257).

    Порог живёт в примечаниях, а не в записи, поэтому правка примечания задевает неизменённые позиции.
    """
    old_by_key = {record_key(r): before.get(a, "") for a, r in recs_before.items()}
    bad = []
    for anchor, rec in recs_after.items():
        k = record_key(rec)
        if k in old_by_key and old_by_key[k] != after.get(anchor, ""):
            bad.append(f"{anchor}: «{old_by_key[k][:60]}» → «{after.get(anchor, '')[:60]}»")
    return bad


def source_points(raw_block: str) -> Counter:
    """Баллы операций в ячейке источника. Порог («не менее N баллов») операцией не считается."""
    text = re.sub(r"не\s+менее\s+\d+(?:[.,]\d+)?\s*балл\w*", " ", raw_block, flags=re.I)
    return Counter(float(n.replace(",", ".")) for n in re.findall(r"(\d+(?:[.,]\d+)?)\s*балл", text, flags=re.I))


def _number(value) -> float:
    """Балл записи: число или строка, в том числе с запятой («2,5»)."""
    return float(str(value).replace(",", "."))


def record_points(rec: dict) -> Counter:
    return Counter(_number(o["points"]) for o in record_operations(rec) if o.get("points") is not None)


def record_operations(rec: dict) -> list[dict]:
    return [o for b in rec.get("requirement_blocks") or [] for o in b.get("operations") or []]


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]{0,12}>", " ", text or "")).lower().strip(" ;,.")


def pair_confirmed(source: str, text: str, points: float) -> bool | None:
    """Стоит ли рядом с текстом операции в ячейке источника именно этот балл. `None` — текста там нет.

    `verify` сверяет множества чисел и обмена баллами двух операций не видит. Замер на корпусе: пара
    подтверждается у 85.9 % из 15 065 операций, смоделированные обмены пойманы 820 из 820.
    """
    needle = _norm(text)
    i = source.find(needle) if needle else -1
    if i < 0:
        return None
    number = f"{points:g}".replace(".", ",")
    return bool(re.match(r".{0,12}?(?<![\d,.])" + re.escape(number) + r"\s*\(?\s*балл",
                         source[i + len(needle):i + len(needle) + 40]))


def unconfirmed_pairs(raw_block: str, rec: dict) -> list[str]:
    """Операции с баллом, чью пару «текст — балл» источник не подтвердил."""
    source = _norm(raw_block)
    return [f"«{(o.get('text') or '')[:50]}» = {o['points']}" for o in record_operations(rec)
            if o.get("points") is not None and not pair_confirmed(source, o.get("text") or "", _number(o["points"]))]


def source_lines(raw_block: str, rec: dict) -> tuple[list[str], list[str]]:
    """Длинные строки ячейки источника и те из них, начала которых нет в записи."""
    parts = [_norm(rec.get("product_name")), *(_norm(o.get("text")) for o in record_operations(rec))]
    for block in rec.get("requirement_blocks") or []:
        parts += [_norm(block.get("component")), _norm(block.get("note"))]
    joined = " ".join(parts)
    lines = [n for n in map(_norm, raw_block.split("\n")) if len(n) > 25]
    return lines, [n for n in lines if n[:60] not in joined]


def lost_requirements(raw_block: str, rec: dict) -> list[str]:
    """Строки с обязательным требованием, которых нет в записи: сверка чисел их не видит, баллов у них нет."""
    return [n[:70] for n in source_lines(raw_block, rec)[1] if _REQUIREMENT_RE.search(n)]


def previous_record(name: str, codes: list[str] | None, previous: dict[str, dict]) -> dict | None:
    """Прежняя запись той же позиции: якорь при `renumber` сдвигается, поэтому поиск по имени."""
    same = [r for r in previous.values() if r.get("product_name") == name]
    return ([r for r in same if r.get("okpd2_codes") == codes] or same or [None])[0]


def review_rows(anchors: set[str], records: dict[str, dict], thresholds: dict[str, str],
                previous: dict[str, dict] | None = None) -> tuple[list[str], list[str]]:
    """Строки таблицы изменённых записей и замечания: баллы источника рядом с баллами записи.

    Строки требований вне записи есть у четверти корпуса и без всякой правки, поэтому они делятся на две
    группы. «Было и пропало» — адресное замечание. Строку, которой нет и в прежней записи, отличить от новой
    строки редакции нельзя: она не пропадает из отчёта, но идёт одной сводной строкой.
    """
    import structure_kb as sk
    from restructure_changed import clean_name

    products: dict[str, list] = {}
    rows, notes, old_gaps = [], [], []
    for anchor in sorted(anchors):
        rec = records[anchor]
        roman = rec.get("section_roman")
        if roman not in products:
            _, products[roman] = sk.parse_section(sk.find_chunk(roman, None).read_text(encoding="utf-8"))
        name = rec.get("product_name") or ""
        same = [p for p in products[roman] if clean_name(p) == name]
        if len(same) > 1:
            same = [p for p in same if p.okpd2_codes == rec.get("okpd2_codes")]
        ours = record_points(rec)
        theirs, lines = None, "?"
        if len(same) != 1:
            notes.append(f"{anchor}: запись не сопоставлена с ячейкой источника ({len(same)} кандидатов)")
        else:
            raw = same[0].raw_block()
            theirs = source_points(raw)
            if theirs != ours:
                lost, extra = sorted((theirs - ours).elements()), sorted((ours - theirs).elements())
                notes.append(f"{anchor}: баллы расходятся с источником: нет в записи {lost}, лишние {extra}")
            if bad := unconfirmed_pairs(raw, rec):
                notes.append(f"{anchor}: пара «операция — балл» не подтверждена у {len(bad)}: {bad[:3]}")
            lost_req = lost_requirements(raw, rec)
            old = previous_record(name, rec.get("okpd2_codes"), previous or {})
            before_gap = lost_requirements(raw, old) if old else []
            if gone := [n for n in lost_req if n not in before_gap]:
                notes.append(f"{anchor}: требования из ячейки не нашлись в записи, {len(gone)}: {gone[:2]}")
            elif lost_req:
                old_gaps.append(anchor)
            all_lines, absent = source_lines(raw, rec)
            lines = f"{len(all_lines) - len(absent)} / {len(all_lines)}"
        if re.search(r"налич|обязательн", rec.get("notes") or "", re.I):
            notes.append(f"{anchor}: требование попало в notes, до ответа оно не дойдёт")
        src_ops = "?" if theirs is None else sum(theirs.values())
        src_sum = "?" if theirs is None else f"{sum(theirs.elements()):g}"
        rows.append(table_row(name[:60], roman, f"{len(record_operations(rec))} ({sum(ours.values())} / {src_ops})",
                              f"{sum(ours.elements()):g} / {src_sum}", lines, thresholds.get(anchor, "")[:90]))
    if old_gaps:
        notes.append(f"Ещё {len(old_gaps)} записей: строк требований из ячейки нет ни в прежней, ни в новой записи "
                     f"(старый пробел или новая строка редакции): {old_gaps[:6]}")
    return rows, notes


def table_row(*cells: str) -> str:
    """Строка таблицы Markdown: перенос и `|` внутри ячейки ломают разметку всей таблицы."""
    return "| " + " | ".join(re.sub(r"\s+", " ", str(c)).replace("|", "/").strip() for c in cells) + " |"


# --- шаги ---------------------------------------------------------------------------------------


def step_guard(ctx: Ctx) -> str:
    branch = git("rev-parse", "--abbrev-ref", "HEAD").strip()
    if branch in PROTECTED_BRANCHES or branch == "HEAD":
        raise Stop(f"ветка «{branch}»: конвейер работает только на своей ветке, например auto/edition-{ctx.doc_id}")
    if kb_changes():
        raise Stop(f"база уже изменена: {kb_changes()[:5]}")
    ctx.facts["ветка"] = branch
    ctx.facts["база"] = git("rev-parse", "--short", "HEAD").strip()
    return f"ветка {branch}"


def step_source(ctx: Ctx) -> str:
    a = ctx.args
    src = Path(a.sources) / ctx.doc_id
    src.mkdir(parents=True, exist_ok=True)
    for attr, url, name in (("rtf", es.KONTUR_RTF, "edition.rtf"), ("page", es.KONTUR_DOC, "edition.html")):
        if getattr(a, attr):
            continue
        data, why = es.fetch(url.format(doc_id=ctx.doc_id))
        if data is None:
            raise Stop(f"не проверено: {name} не скачан ({why})")
        (src / name).write_bytes(data)
        setattr(a, attr, str(src / name))
    info = es.parse_page(Path(a.page).read_text(encoding="utf-8", errors="replace"))
    if info is None:
        raise Stop("разметка страницы Контура не разобрана: статус редакции не найден")
    ctx.facts.update(редакция=info.title, статус=info.status,
                     отложенные=[f"{x.key} (с {x.effective})" for x in info.pending])
    if not (info.is_actual or info.is_future or a.allow_outdated):
        raise Stop(f"страница говорит «{info.title}»: это не действующая редакция, собирать из неё корпус нельзя "
                   f"(документы на странице: {sorted(info.jumps)})")
    return info.title


def header_acts(text: str) -> set:
    """Акты из шапки постановления: до слова «постановляет». Шапка только растёт, ссылки в тексте — нет."""
    m = re.search(r"постановляет", text, re.I)
    return we.acts(text[:m.start()]) if m else set()


def step_text(ctx: Ctx) -> str:
    old = FULL.read_text(encoding="utf-8")
    text = es.rtf_to_corpus_text(Path(ctx.args.rtf).read_bytes())
    try:
        text, restored = es.restore_lost_blocks(text, old)
    except es.SourceError as e:
        raise Stop(str(e)) from e
    if es.LOST_MARK in text:
        raise Stop("маркер потери таблицы остался в тексте")
    # Номера документов Контура не хронологичны (508574 новее 508767), поэтому откат виден только по актам.
    was, now = header_acts(old), header_acts(text)
    if not now:
        raise Stop("шапка постановления не разобрана: сравнить редакции не с чем")
    if was - now:
        raise Stop(f"откат: в кандидате нет актов корпуса {[we.label(a) for a in sorted(was - now)]}. "
                   "Документ старше редакции, на которой стоит корпус")
    ctx.facts["новые акты"] = [we.label(a) for a in sorted(now - was)]
    w = es.witness(text, Path(ctx.args.page).read_text(encoding="utf-8", errors="replace"))
    ctx.facts["восстановлено блоков"] = [f"{r.chars} зн: {r.head[:50]}" for r in restored]
    ctx.facts["сверено символов с HTML"] = w.compared
    if not w.equal:
        raise Stop(f"текст не совпал с HTML той же редакции: {w.note}\n  наш:  {w.ours}\n  HTML: {w.theirs}\n"
                   "Если расхождение внутри восстановленного блока — акт изменил таблицу, которую экспортёр "
                   "RTF теряет: блок собирается из HTML вручную.")
    ctx.new_text = text
    return f"сверено с HTML: {w.compared} символов, блоков восстановлено: {len(restored)}"


def corpus_label() -> tuple[str, str, str | None]:
    """Подпись редакции, ключ таблицы ссылок и её документ. Отдельный процесс: у модуля кэш."""
    code = ("import json,sys\nfrom app.rag import edition as e\n"
            "sys.stdout.write(json.dumps([e.corpus_edition(),e.edition_key(),e.KONTUR_719_BY_EDITION.get(e.edition_key())]))")
    return tuple(json.loads(run([sys.executable, "-c", code]).stdout))


def step_control(ctx: Ctx) -> str:
    """Положительный контроль: нарезка прежнего текста обязана дать ровно прежние чанки."""
    write_chunks(FULL.read_text(encoding="utf-8"))
    changed = kb_changes()
    if changed:
        git("checkout", "--", "knowledge_base")
        raise Stop(f"нарезка не воспроизводит прежний корпус: {changed[:6]}")
    ctx.before = {"пороги": threshold_snapshot(), "записи": records_by_anchor(), "сверки": verify_summary(),
                  "вне чанков": uncovered_lines(), "подпись": corpus_label()[0], "разделы": known_sections(),
                  "методички": methodology_snapshot()}
    return "нарезка прежнего текста побайтно та же"


def step_chunks(ctx: Ctx) -> str:
    import rechunk_appendix as rc

    if ctx.new_text == FULL.read_text(encoding="utf-8"):
        raise Stop("НЕТ ИЗМЕНЕНИЙ: текст редакции равен корпусу")
    check_sections([r for r, _, _ in rc.find_appendix_sections(ctx.new_text)], ctx.before["разделы"])
    write_chunks(ctx.new_text)
    if (lost := uncovered_lines()) > ctx.before["вне чанков"]:
        raise Stop(f"строк текста вне чанков стало больше: {ctx.before['вне чанков']} → {lost}, нарезка что-то потеряла")
    changed = kb_changes()
    ctx.sections = [r for r in (section_of(ROOT / name) for name in changed) if r]
    ctx.facts["изменённые чанки"] = [Path(c).name for c in changed]
    ctx.facts["разделы"] = ctx.sections
    return f"чанков изменено {len(changed)}, разделов приложения: {ctx.sections or 'нет'}"


def document_label(sources: list[str], corpus: str) -> str | None:
    """Редакция документа манифеста — по его собственным файлам: сноски акт мог и не задеть.

    Источник-маска — товарный корпус `structured/*.json`: его подпись по определению равна редакции корпуса.
    """
    if any("*" in name for name in sources):
        return corpus
    found: set = set()
    for name in sources:
        found |= we.acts((ROOT / "knowledge_base" / name).read_text(encoding="utf-8"))
    if not found:
        return None
    y, m, d, n = max(found)
    return f"ред. от {d:02d}.{m:02d}.{y} N {n}"


def retarget_manifest(corpus: str, changed: list[str]) -> bool:
    """`edition_expected` документов постановления, чьи чанки изменились: у каждого своя подпись.

    Только `pp719/chunks/`: у Соглашения СНГ и приказов своя история, их подпись по актам считать нельзя.
    """
    touched = {name.removeprefix("knowledge_base/") for name in changed}
    blocks = re.split(r"(?m)^(?=  - doc_type: )", MANIFEST.read_text(encoding="utf-8"))
    moved = False
    for i, block in enumerate(blocks):
        declared = re.search(r'edition_expected: "(ред\. от [^"]+)"', block)
        sources = re.findall(r"(?m)^\s+- (pp719/\S+)", block)
        own = any("*" in s or (s.startswith("pp719/chunks/") and s in touched) for s in sources)
        if not declared or not own:
            continue
        label = document_label(sources, corpus)
        if label and label != declared[1]:
            blocks[i] = block.replace(declared[0], f'edition_expected: "{label}"')
            moved = True
    if moved:
        MANIFEST.write_text("".join(blocks), encoding="utf-8", newline="\n")
    return moved


def retarget_landing(old: str, new: str) -> bool:
    """Подпись редакции на лендинге: тест требует её равенства с корпусом."""
    was, now = _LABEL_RE.fullmatch(old), _LABEL_RE.fullmatch(new)
    if not was or not now:
        raise Stop(f"подпись редакции не разобрана: «{old}» → «{new}»")
    page = LANDING.read_text(encoding="utf-8")
    out = page.replace(f"от {was[1]} № {was[2]}", f"от {now[1]} № {now[2]}").replace(f"ред. от {was[1]}<", f"ред. от {now[1]}<")
    if out != page:
        LANDING.write_text(out, encoding="utf-8", newline="\n")
    return out != page


def step_link(ctx: Ctx) -> str:
    """Ссылка «открыть редакцию» обязана вести в тот документ Контура, на котором стоит корпус."""
    label, key, known = corpus_label()
    ctx.facts["подпись"] = label
    old = ctx.before.get("подпись", label)
    moved = [MANIFEST.name] if retarget_manifest(label, kb_changes()) else []
    if label != old and retarget_landing(old, label):
        moved.append(LANDING.name)
    if known == ctx.doc_id:
        return f"{key} → {known}; подпись обновлена в: {moved or 'не менялась'}"
    if known:
        raise Stop(f"ключ редакции «{key}» уже ведёт на {known}, а документ {ctx.doc_id}. Акт, датированный раньше "
                   "последнего, подпись не меняет: нужна строка в LATER_IN_FORCE (ACTUALIZATION, п. 22).")
    path = ROOT / "app" / "rag" / "edition.py"
    src = path.read_text(encoding="utf-8")
    anchor = "KONTUR_719_BY_EDITION: dict[str, str] = {\n"
    if anchor not in src:
        raise Stop("таблица KONTUR_719_BY_EDITION не найдена")
    row = f'    "{key}": "{ctx.doc_id}",  # {ctx.facts.get("редакция", "")}\n'
    path.write_text(src.replace(anchor, anchor + row, 1), encoding="utf-8", newline="\n")
    return f"добавлена ссылка: {key} → {ctx.doc_id}; подпись обновлена в: {moved or 'не менялась'}"


def plan_path(ctx: Ctx, roman: str) -> Path:
    return Path(ctx.args.sources) / ctx.doc_id / f"plan_{roman}.json"


def step_plan(ctx: Ctx) -> str:
    """Сколько позиций уйдёт в модель. `keep` — заплатка; новая позиция требует `renumber`."""
    lines = []
    for roman in ctx.sections:
        path = plan_path(ctx, roman)
        path.unlink(missing_ok=True)
        mode = "keep"
        if py("restructure_changed.py", roman, "--anchors", "keep", "--plan-json", str(path), check=False).returncode:
            mode = "renumber"
            py("restructure_changed.py", roman, "--anchors", "renumber", "--plan-json", str(path))
        if not path.exists():
            raise Stop(f"раздел {roman}: план не записан, проверка числа позиций не состоялась")
        ctx.modes[roman], ctx.plans[roman] = mode, json.loads(path.read_text(encoding="utf-8"))
        plan = ctx.plans[roman]
        lines.append(f"{roman}: {mode}, в модель {len(plan['в модель'])}, без модели {len(plan['без модели'])}, "
                     f"исключено {len(plan['исключены'])}")
    ctx.facts["план перепарса"] = lines
    ctx.facts["исключённые позиции"] = [n for p in ctx.plans.values() for n in p["исключены"]]
    total = sum(len(p["в модель"]) for p in ctx.plans.values())
    if total > ctx.args.max_llm:
        raise Stop(f"в модель уходит {total} позиций при пороге {ctx.args.max_llm}: раздел переписан массово, "
                   "такой перепарс проверяет человек")
    return "; ".join(lines) or "разделы приложения не менялись"


def step_structure(ctx: Ctx) -> str:
    done = []
    for roman in ctx.sections:
        extra = []
        if ctx.modes[roman] == "renumber":
            amap = Path(ctx.args.sources) / ctx.doc_id / f"anchors_{roman}.json"
            extra = ["--map-out", str(amap)]
            ctx.facts.setdefault("карты якорей: перевести наборы замеров", []).append(amap.name)
        r = py("restructure_changed.py", roman, "--anchors", ctx.modes[roman], "--write", *extra, timeout=3600)
        m = re.search(r"вызовов API (\d+), ≈ ¥([\d.]+)", r.stdout)
        done.append(f"{roman}: {m.group(0) if m else 'без вызовов модели'}")
    ctx.facts["перепарс"] = done
    return "; ".join(done) or "нечего перепарсивать"


def step_post(ctx: Ctx) -> str:
    """Производные файлы. Порядок — из вывода `backfill`: сначала он, потом карты и список порогов."""
    backfill = py("backfill_block_thresholds.py", "--write").stdout
    ctx.warnings += [line.strip() for line in backfill.splitlines() if "ОТКАТ" in line]
    py("diag_orphan_requirements.py", "--fragments", "knowledge_base/pp719/fragmented_requirements.json")
    py("diag_orphan_requirements.py", "--inheritance", "knowledge_base/pp719/inherited_requirements.json")
    py("verify_structured.py", "--json", "knowledge_base/pp719/incomplete_thresholds.json")
    cells = py("build_code_cells.py").stdout
    ctx.facts["кодов не определено"] = cells.count("НЕ ОПРЕДЕЛЕНО")
    return "производные файлы пересобраны"


def step_gates(ctx: Ctx) -> str:
    before, after = ctx.before["пороги"], threshold_snapshot()
    recs_before, recs_after = ctx.before["записи"], records_by_anchor()
    bad = foreign_thresholds(before, after, recs_before, recs_after)
    if bad:
        raise Stop(f"чужие пороги ({len(bad)}): {bad[:5]}")
    # Изменено ровно то, что планировалось: иначе шаг задел записи, которых модель не видела.
    renumbered = frozenset(r for r, mode in ctx.modes.items() if mode == "renumber")
    fresh = changed_anchors(recs_before, recs_after, renumbered)
    allowed = sum(len(p["в модель"]) + len(p["без модели"]) for p in ctx.plans.values())
    if len(fresh) > allowed:
        raise Stop(f"изменено записей {len(fresh)}, а в плане {allowed}: {sorted(fresh)[:6]}")
    names = {n for p in ctx.plans.values() for n in p["в модель"] + p["без модели"] + p.get("прежние имена", [])}
    # У пары позиций имени в ячейке нет (III/157, IX/91): столько записей может остаться с именем модели.
    nameless = sum(n == "" for p in ctx.plans.values() for n in p["в модель"] + p["без модели"])
    stray = sorted(a for a in fresh if recs_after[a].get("product_name") not in names)
    if len(stray) > nameless:
        raise Stop(f"изменены записи вне плана: {stray[:6]}")
    planned_gone = sum(len(p["исключены"]) for p in ctx.plans.values())
    if len(recs_after) < len(recs_before) - planned_gone:
        raise Stop(f"записей было {len(recs_before)}, стало {len(recs_after)}, в плане исключено {planned_gone}")
    was, now = ctx.before["сверки"], verify_summary()
    ctx.facts["сверки"] = {"до": was, "после": now}
    if worse := regressions(was, now):
        raise Stop(f"сверки ухудшились: {worse}")
    corpus = json.loads(py("watch_edition.py", "--json", check=False).stdout)["корпус"]
    if not corpus["части согласованы"]:
        raise Stop("watch_edition: тело постановления и разделы приложения не согласованы")
    rows, notes = review_rows(fresh, recs_after, after, recs_before)
    ctx.facts["изменённые записи"] = rows
    ctx.warnings += notes
    # Методичка раздела идёт в индекс, а шлюзы её не сверяют: правка видна только глазами.
    if moved := sorted(k for k, v in methodology_snapshot().items() if ctx.before.get("методички", {}).get(k) != v):
        ctx.warnings.append(f"методички разделов изменились: {moved}")
    return f"чужих порогов 0, изменено записей {len(fresh)} из {allowed} в плане, сверки {now}, замечаний {len(notes)}"


def step_tests(ctx: Ctx) -> str:
    """Батарея на новых данных. Закреплённым за редакцией тестам положено падать: их правит человек в PR.

    Неожиданное падение меняет вердикт: это либо дефект данных, либо новый закреплённый тест.
    ⚠ Без `PYTHONUTF8`: с ним батарея зеленеет там, где документированная среда падает.
    """
    r = run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], check=False, timeout=3600, utf8=False)
    if r.returncode == 0:
        return "батарея зелёная"
    failed = [mod for _, mod in re.findall(r"^(?:FAIL|ERROR): (\S+) \((\S+)\)", r.stderr + r.stdout, flags=re.M)]
    if not failed:
        raise Stop(f"батарея упала без списка тестов, код {r.returncode}\n{scrub(r.stderr[-600:])}")
    pinned = [t for t in failed if _PINNED_TESTS.match(t)]
    surprises = [t for t in failed if t not in pinned]
    ctx.surprises = len(surprises)
    ctx.facts["тесты, закреплённые за редакцией"] = pinned
    ctx.facts["неожиданные падения тестов"] = surprises
    if surprises:
        ctx.warnings.append(f"неожиданных падений тестов: {len(surprises)}, это мог быть дефект данных")
    return f"закреплённых падений {len(pinned)}, неожиданных {len(surprises)}"


HANDLERS = {"guard": step_guard, "source": step_source, "text": step_text, "control": step_control,
            "chunks": step_chunks, "link": step_link, "plan": step_plan, "structure": step_structure,
            "post": step_post, "gates": step_gates, "tests": step_tests}


def verdict_of(stopped: str | None, last_step: str, warnings: list[str], surprises: int = 0) -> str:
    if stopped:
        return f"ОСТАНОВЛЕНО: {stopped}"
    if last_step != STEPS[-1]:
        return f"ЧАСТИЧНЫЙ ПРОГОН до шага {last_step}: это не готовность"
    if surprises:
        return f"НУЖЕН РАЗБОР: неожиданных падений тестов {surprises}, замечаний {len(warnings)}"
    if warnings:
        return f"ГОТОВО К ПРОВЕРКЕ, ЗАМЕЧАНИЙ: {len(warnings)}"
    return "ГОТОВО К ПРОВЕРКЕ ЧЕЛОВЕКОМ"


def render_report(doc_id: str, verdict: str, steps: list[dict], facts: dict, warnings: list[str] | None = None) -> str:
    """Отчёт для человека: что проверено автоматически и что осталось глазами. Публичен: только через `scrub`."""
    lines = [f"# Актуализация ПП 719: документ Контура {doc_id}", "", f"**{verdict}**", ""]
    lines += [f"Редакция: {facts.get('редакция', 'не определена')}. База ветки: `{facts.get('база', '?')}`.", ""]
    if warnings:
        lines += ["## ⚠ Посмотреть в первую очередь", "", *[f"- {w}" for w in warnings], ""]
    lines += ["| Шаг | Итог | Деталь |", "|---|---|---|"]
    for s in steps:
        detail = str(s["деталь"]).replace("\n", " ").replace("|", "\\|")[:300]
        lines.append(f"| {s['шаг']} | {s['итог']} | {detail} |")
    if facts.get("изменённые записи"):
        lines += ["", "## Изменённые записи", "",
                  "Операций: всего (с баллами в записи / в источнике). Баллы: сумма в записи / в источнике. "
                  "Строки: нашлось в записи / длинных строк ячейки, требования без баллов видны только здесь.", "",
                  "| Позиция | Раздел | Операций | Баллы | Строки | Порог |", "|---|---|---|---|---|---|",
                  *facts["изменённые записи"]]
    for title, key in (("Новые акты редакции", "новые акты"), ("Изменённые разделы", "разделы"),
                       ("План перепарса", "план перепарса"), ("Перепарс", "перепарс"),
                       ("Исключённые позиции", "исключённые позиции"),
                       ("Карты якорей: перевести наборы замеров (п. 15)", "карты якорей: перевести наборы замеров"),
                       ("Отложенные акты", "отложенные"),
                       ("Неожиданные падения тестов", "неожиданные падения тестов"),
                       ("Тесты, закреплённые за редакцией: править", "тесты, закреплённые за редакцией")):
        if facts.get(key):
            lines += ["", f"**{title}:**", *[f"- {x}" for x in facts[key]]]
    acts = ", ".join(facts.get("новые акты") or []) or "см. шапку редакции"
    if facts.get("кодов не определено"):
        lines += ["", f"**Кодов с неопределённой пометкой «из»:** {facts['кодов не определено']}, разобрать поимённо."]
    lines += ["", "## Что остаётся человеку",
              f"- [ ] Сверить изменённые и исключённые позиции с текстом акта: {acts}. Источник — "
              "publication.pravo.gov.ru: вёрстки Контура расходятся между собой, а сверка с HTML этого не видит.",
              f"- [ ] Сравнить с редакцией на Контуре: {es.KONTUR_DOC.format(doc_id=doc_id)}",
              "- [ ] Переиндексировать в параллельные коллекции и снять замер одним кодом (ACTUALIZATION, п. 8).",
              "- [ ] Сделать профиль выкатки и выбрать ночь: окна зависят от размера корпуса.", ""]
    return scrub("\n".join(lines))


def clear_report(sources: str, doc_id: str) -> None:
    """Прогон, прерванный без отчёта, не должен оставить прежний успешный: автомат опубликовал бы его данные."""
    for name in ("report.json", "report.md"):
        (Path(sources) / doc_id / name).unlink(missing_ok=True)


def write_report(ctx: Ctx, stopped: str | None, last_step: str) -> Path:
    out = Path(ctx.args.sources) / ctx.doc_id / "report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    verdict = verdict_of(stopped, last_step, ctx.warnings, ctx.surprises)
    data = {"документ": ctx.doc_id, "вердикт": verdict, "последний шаг": last_step, "шаги": ctx.log,
            "замечания": ctx.warnings, "факты": ctx.facts}
    out.write_text(scrub(json.dumps(data, ensure_ascii=False, indent=2)), encoding="utf-8")
    out.with_suffix(".md").write_text(render_report(ctx.doc_id, verdict, ctx.log, ctx.facts, ctx.warnings), encoding="utf-8")
    return out


def main() -> None:
    enable_utf8()
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--doc-id", required=True, help="documentId редакции у Контура")
    ap.add_argument("--rtf", help="RTF из файла вместо скачивания")
    ap.add_argument("--page", help="страница Контура из файла вместо скачивания")
    ap.add_argument("--sources", default=str(ROOT.parent / "navigator-sources"), help="куда класть первоисточники и отчёт")
    ap.add_argument("--until", choices=STEPS, default=STEPS[-1], help="последний шаг")
    ap.add_argument("--max-llm", type=int, default=DEFAULT_MAX_LLM, help="порог позиций в модель")
    ap.add_argument("--allow-outdated", action="store_true",
                    help="принять страницу недействующей редакции: только для реплея прошлых редакций")
    args = ap.parse_args()

    clear_report(args.sources, args.doc_id)
    ctx = Ctx(doc_id=args.doc_id, args=args)
    stopped, last = None, STEPS[0]
    for name in STEPS:
        t0 = time.time()
        last = name
        try:
            detail = HANDLERS[name](ctx)
            ctx.log.append({"шаг": name, "итог": "ok", "деталь": detail, "сек": round(time.time() - t0, 1)})
            print(f"[{name}] ok ({time.time() - t0:.0f} с): {detail}", flush=True)
        except Exception as e:  # noqa: BLE001 — любой сбой обязан оставить отчёт: прогон идёт без человека
            reason = str(e) if isinstance(e, Stop) else f"сбой {type(e).__name__}: {e}"
            stopped = f"{name}: {scrub(reason)}"
            ctx.log.append({"шаг": name, "итог": "стоп", "деталь": scrub(reason), "сек": round(time.time() - t0, 1)})
            print(f"[{name}] СТОП: {scrub(reason)}", flush=True)
            break
        if name == args.until:
            break
    report = write_report(ctx, stopped, last)
    print(f"\nотчёт: {report}")
    sys.exit(1 if stopped else 0)


if __name__ == "__main__":
    main()
