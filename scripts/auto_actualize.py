"""Ежедневный запуск актуализации (`A10`): слежение → конвейер → черновой PR и задача владельцу.

ЗАЧЕМ. О трёх редакциях подряд узнавали вручную, а акты с отложенным сроком не видели вовсе. Этот
скрипт стоит в Планировщике Windows: смотрит Контур и, если вышла новая редакция, сам собирает ветку
`auto/edition-<id>` конвейером `actualize_edition.py`.

ЧТО ДАЛЬШЕ ПРОИСХОДИТ БЕЗ ЧЕЛОВЕКА: ничего. Прошёл конвейер — черновой PR в `dev`. Остановился —
задача, ветка остаётся локально. В `dev`, в `main` и на бой этот скрипт не ходит.

РЕПОЗИТОРИЙ ПУБЛИЧЕН. Текст задачи — короткая причина без вывода процессов; подробности остаются в
локальном отчёте. `.env` в рабочее дерево не копируется: конвейеру нужен один ключ, он идёт окружением.

МОЛЧАНИЕ — ТОЖЕ ОТКАЗ. Сломанное слежение выглядит как «нового нет». Поэтому неделя без успешной
проверки сама становится задачей (`STALE_DAYS`), а любой сбой запуска — задачей с типом ошибки.
⚠ О том, что сам запуск не состоялся, Python сообщить не может: это видно только по журналу.

    python scripts/auto_actualize.py            # раз в сутки
    python scripts/auto_actualize.py --dry-run  # только показать, что было бы сделано
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import watch_edition as we  # noqa: E402
from app.core.console import enable_utf8  # noqa: E402

STALE_DAYS = 7
MAX_ISSUES_PER_RUN = 3     # смена разметки не должна породить пачку задач
DEFAULT_SOURCES = Path("D:/navigator-sources")
MAIN_ENV = Path("D:/navigator-719/.env")
KEY = "DEEPSEEK_API_KEY"
# Что конвейер имеет право менять. `git add` всего дерева забрал бы лишнее.
PUBLISH_PATHS = ("knowledge_base", "app/rag/edition.py", "landing/index.html")


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def load_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_state(path: Path, state: dict) -> None:
    """Атомарно: запись, оборванная посреди файла, не должна стирать дату последней проверки."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def days_silent(state: dict, moment: dt.datetime) -> int | None:
    """Сколько дней не было успешной проверки. `None` — проверок ещё не было, считать не от чего."""
    last = state.get("последняя успешная проверка")
    return (moment - dt.datetime.fromisoformat(last)).days if last else None


def gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")


class Issues:
    """Задачи за один запуск: с потолком и без дублей."""

    def __init__(self, dry_run: bool, runner=None, limit: int = MAX_ISSUES_PER_RUN):
        self.dry_run, self.runner, self.limit, self.made = dry_run, runner or gh, limit, 0

    def once(self, title: str, body: str) -> str:
        """⚠ Поиск не ответил — задачи не заводим: лучше повторить завтра, чем завести дубль."""
        if self.dry_run:
            return f"была бы создана задача: {title}"
        if self.made >= self.limit:
            return f"потолок задач за запуск, повтор завтра: {title}"
        found = self.runner("issue", "list", "--state", "all", "--search", f'"{title}" in:title', "--json", "number")
        if found.returncode != 0:
            return f"поиск задач не ответил, повтор завтра: {found.stderr.strip()[:120]}"
        if json.loads(found.stdout or "[]"):
            return f"уже заведено: {title}"
        made = self.runner("issue", "create", "--title", title, "--body", body)
        if made.returncode != 0:
            return f"задача не создана: {made.stderr.strip()[:160]}"
        self.made += 1
        return made.stdout.strip()


def git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout


def edition_built(doc_id: str, sources: Path) -> bool:
    """Ветка или каталог уже есть: сборка ждёт разбора, повторять её каждый день незачем."""
    branch = f"auto/edition-{doc_id}"
    return ((sources / f"wt-{doc_id}").exists() or bool(git("branch", "--list", branch).strip())
            or bool(git("ls-remote", "--heads", "origin", branch).strip()))


def model_env(env_file: Path = MAIN_ENV) -> dict[str, str]:
    """Окружение конвейера: один ключ модели вместо копии `.env` в рабочем дереве."""
    env = dict(os.environ)
    if KEY not in env and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{KEY}="):
                env[KEY] = line.split("=", 1)[1].strip().strip("\"'")
    return env


def build_edition(doc_id: str, sources: Path) -> tuple[bool, dict]:
    """Ветка `auto/edition-<id>` от `origin/dev` в своём каталоге, конвейер внутри неё."""
    tree = sources / f"wt-{doc_id}"
    git("fetch", "origin", "--quiet")
    git("worktree", "add", "-b", f"auto/edition-{doc_id}", str(tree), "origin/dev")
    run = subprocess.run([sys.executable, "scripts/actualize_edition.py", "--doc-id", doc_id,
                          "--sources", str(sources)], cwd=tree, env=model_env())
    path = sources / doc_id / "report.json"
    report = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return run.returncode == 0, report


def publish(doc_id: str, tree: Path, report_md: Path) -> str:
    """Черновой PR в `dev`. Черновик нельзя слить случайно: его сначала переводит в готовый человек."""
    branch = f"auto/edition-{doc_id}"
    git("add", "-A", "--", *PUBLISH_PATHS, cwd=tree)
    git("commit", "-m", f"data(auto): редакция ПП 719, документ Контура {doc_id}", cwd=tree)
    git("push", "-u", "origin", branch, cwd=tree)
    made = subprocess.run(["gh", "pr", "create", "--draft", "--base", "dev", "--head", branch, "--title",
                           f"data(auto): редакция ПП 719, документ Контура {doc_id}", "--body-file", str(report_md)],
                          cwd=tree, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if made.returncode != 0:
        raise RuntimeError(f"gh pr create: код {made.returncode}")
    return made.stdout.strip()


def handle_found(result: dict, sources: Path, issues: Issues) -> list[str]:
    """Новое на Контуре: отложенный акт — задача; новый документ — конвейер и PR либо задача с причиной."""
    log = []
    for act in result["новые отложенные акты"]:
        log.append(issues.once(f"[A10] Новый отложенный акт ПП 719: {act}",
                               f"Контур, блок «Примечания», «ИЗМЕНЕН»: {act}. Внести в задачник и в "
                               "`knowledge_base/pp719/edition_watch.json`."))
    for act in result["актов нет в тексте"]:
        log.append(issues.once(f"[A10] Акт есть на Контуре, но нет в корпусе: {act}",
                               "Текст действующей редакции назвал акт, которого нет в `pp719_full.txt`."))
    for doc_id, link in result["новые документы"].items():
        if issues.dry_run:
            log.append(f"был бы запущен конвейер: документ {doc_id} ({link})")
            continue
        if edition_built(doc_id, sources):
            log.append(f"документ {doc_id}: сборка уже есть и ждёт разбора")
            continue
        ok, report = build_edition(doc_id, sources)
        where = sources / doc_id / "report.md"
        if not ok:
            step = report.get("последний шаг", "неизвестен")
            log.append(issues.once(f"[A10] Новая редакция ПП 719 {doc_id}: конвейер остановился на шаге {step}",
                                   f"Новая редакция, документ Контура {doc_id}. Подробности — в локальном отчёте "
                                   f"`{where.name}` каталога первоисточников."))
            continue
        try:
            log.append(publish(doc_id, sources / f"wt-{doc_id}", where))
        except (subprocess.CalledProcessError, RuntimeError) as e:
            print(f"публикация не удалась: {e}", flush=True)
            log.append(issues.once(f"[A10] Новая редакция ПП 719 {doc_id}: сборка готова, PR не создан",
                                   f"Конвейер прошёл, публикация не удалась ({type(e).__name__}). Работа лежит в "
                                   f"локальной ветке `auto/edition-{doc_id}`."))
    return log


def run_once(args: argparse.Namespace, sources: Path, state: dict, issues: Issues) -> None:
    result = we.check_kontur(we.current_doc_id(), we.known_pending())
    moment = now()
    print(f"{moment:%Y-%m-%d %H:%M} UTC: {json.dumps(result, ensure_ascii=False)}", flush=True)
    if result["исход"] == "не проверено":
        silent = days_silent(state, moment)
        if silent is None:
            state.setdefault("последняя успешная проверка", moment.isoformat())
        elif silent >= STALE_DAYS:
            print(issues.once(f"[A10] Слежение за редакциями молчит с {state['последняя успешная проверка'][:10]}",
                              f"Проверка Контура не удаётся {silent} дн. Причина: {result['причина'][:200]}"))
        return
    state["последняя успешная проверка"] = moment.isoformat()
    if result["исход"] == "новое":
        for line in handle_found(result, sources, issues):
            print(line, flush=True)


def main() -> None:
    enable_utf8()
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--sources", default=str(DEFAULT_SOURCES), help="первоисточники, отчёты, рабочие деревья")
    ap.add_argument("--dry-run", action="store_true", help="ничего не создавать")
    args = ap.parse_args()

    sources = Path(args.sources)
    state_path = sources / "watch_state.json"
    state = load_state(state_path)
    issues = Issues(args.dry_run)
    try:
        run_once(args, sources, state, issues)
    except Exception as e:  # noqa: BLE001 — запуск без человека: любой сбой обязан стать задачей
        traceback.print_exc()
        print(issues.once(f"[A10] Сбой ежедневного запуска: {type(e).__name__}",
                          "Текст ошибки — в локальном журнале `watch.log`, в задачу он не идёт: репозиторий публичен."))
    finally:
        if not args.dry_run:
            save_state(state_path, state)


if __name__ == "__main__":
    main()
