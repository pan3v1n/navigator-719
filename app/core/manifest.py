"""Манифест корпуса — единственный источник правды о его составе (`K8` #39, `#106`).

ЗАЧЕМ ОТДЕЛЬНЫЙ МОДУЛЬ. Читалка родилась внутри `scripts/load_rules_kb.py`, а манифест нужен уже
ТРЁМ загрузчикам: процедурный корпус, товарный (приложение) и кейсы экспертов. Копия правил
разбора в каждом означала бы три расходящихся понимания того, что такое «состав корпуса», —
ровно то, ради устранения чего манифест и заводился. Здесь же, в `app/core/`, он доступен и
рантайму: `legal_force` понадобится `K13` для иерархии источников в промпте.

ЧТО ЭТОТ МОДУЛЬ НЕ ДЕЛАЕТ: не читает сами документы и не знает их форматов. Разбор — забота
загрузчика (у каждого корпуса свой парсер), манифест отвечает на вопросы «какие документы»,
«какой они силы», «что действует» и «где лежат файлы».
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "knowledge_base" / "manifest.yaml"
KB_DIR = ROOT / "knowledge_base"

# Поля паспорта, которые уезжают в payload КАЖДОЙ записи документа. `edition` сюда не входит:
# её считает загрузчик из текста, а манифест лишь заявляет ожидаемую (`edition_expected`).
PASSPORT_FIELDS = ("authority", "legal_force", "doc_kind", "topic", "key_type",
                   "status", "valid_from", "valid_to", "supersedes")
RETIRED = "утратил силу"
ACTIVE = "действует"


class ManifestError(RuntimeError):
    """Манифест не читается или противоречив. Скрипты превращают её в `sys.exit`.

    Исключение, а не `sys.exit` внутри: модуль зовётся и из тестов, и из рантайма, а библиотека,
    гасящая процесс, — плохой сосед."""


def load_manifest(path: Path | None = None) -> dict:
    """Читает манифест и проверяет значения по его же словарям.

    ⚠ Проверка словарём — не бюрократия. Опечатка в `status` («утратила силу») тихо превратила бы
    исключённый документ в действующий, а `legal_force: "2"` строкой сломала бы сравнение силы
    источников при `K13`. Ошибка здесь останавливает загрузку: индексировать корпус с неизвестным
    паспортом хуже, чем не индексировать вовсе."""
    import yaml  # локально: парсеры документов тестируются без внешних зависимостей

    path = path or MANIFEST_PATH
    if not path.exists():
        raise ManifestError(f"нет манифеста корпуса: {path} (состав корпуса задаётся им)")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    docs = data.get("documents") or []
    if not docs:
        raise ManifestError(f"в манифесте {path.name} нет ни одного документа")
    vocab = data.get("vocabularies") or {}
    seen: set[str] = set()
    for d in docs:
        dt = d.get("doc_type")
        if not dt:
            raise ManifestError(f"документ без doc_type: {d.get('title')!r}")
        if dt in seen:
            raise ManifestError(f"doc_type {dt!r} встречается дважды — идентификатор обязан быть уникален")
        seen.add(dt)
        if not d.get("collection"):
            raise ManifestError(f"{dt}: не указана коллекция (collection)")
        for field, allowed in vocab.items():
            if field not in d:
                continue
            value = d[field]
            if value is None and None in allowed:
                continue
            if value not in allowed:
                raise ManifestError(f"{dt}: недопустимое {field}={value!r}; словарь: {allowed}")
        missing = [f for f in PASSPORT_FIELDS if f not in d]
        if missing:
            raise ManifestError(f"{dt}: в паспорте нет полей {missing}")
    return data


def documents(collection: str, path: Path | None = None) -> list[dict]:
    """Документы одной коллекции, в порядке манифеста.

    Разделение по коллекциям обязательно: `load_rules_kb` итерирует документы и зовёт для каждого
    свой парсер — без фильтра он попытался бы разобрать товарное приложение парсером норм."""
    return [d for d in load_manifest(path)["documents"] if d.get("collection") == collection]


def passport(doc: dict) -> dict:
    """Поля паспорта одной записи + человекочитаемое имя документа для атрибуции."""
    out = {f: doc.get(f) for f in PASSPORT_FIELDS}
    out["doc_title"] = doc.get("short") or doc.get("title") or doc.get("doc_type")
    return out


def resolve_sources(doc: dict, kb_dir: Path | None = None) -> list[Path]:
    """Файлы документа: точные пути и/или шаблоны, минус исключения.

    ⚠ ШАБЛОН РАЗРЕШЁН ОСОЗНАННО, но не везде. У процедурного корпуса файлы перечислены поимённо:
    их пять, они разные по структуре, и подстановка `1[1-5]*.txt` однажды уже цепляла товарные
    чанки. У приложения секций 29, они ГЕНЕРИРУЮТСЯ — перечислять их значит заводить второй
    список, который начнёт расходиться с каталогом молча.

    `exclude` нужен, чтобы решение «эти файлы не индексируем» жило в манифесте, а не в условии
    внутри загрузчика: так исключение видно тому, кто читает состав корпуса."""
    base = kb_dir or KB_DIR
    files: list[Path] = []
    for pattern in doc.get("sources") or []:
        if any(ch in pattern for ch in "*?["):
            found = sorted(base.glob(pattern))
            if not found:
                raise ManifestError(f"{doc.get('doc_type')}: шаблон {pattern!r} не нашёл ни одного файла")
            files.extend(found)
        else:
            p = base / pattern
            if not p.exists():
                raise ManifestError(f"{doc.get('doc_type')}: нет файла {p} (перечислен в манифесте)")
            files.append(p)
    for pattern in doc.get("exclude") or []:
        dropped = {p for p in files if p.match(pattern)}
        files = [p for p in files if p not in dropped]
    return files
