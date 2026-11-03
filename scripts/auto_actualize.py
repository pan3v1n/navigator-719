"""Ежедневный запуск актуализации (`A10`): слежение → конвейер → черновой PR и задача владельцу.

ЗАЧЕМ. О трёх редакциях подряд узнавали вручную, а акты с отложенным сроком не видели вовсе. Этот
скрипт стоит в Планировщике Windows: смотрит Контур и, если вышла новая редакция, сам собирает ветку
`auto/edition-<id>` конвейером `actualize_edition.py`.

ЧТО ДАЛЬШЕ ПРОИСХОДИТ БЕЗ ЧЕЛОВЕКА: ничего. Прошёл конвейер — черновой PR в `dev`. Остановился —
задача, ветка остаётся локально. В `dev`, в `main` и на бой этот скрипт не ходит.

РЕПОЗИТОРИЙ ПУБЛИЧЕН. Текст задачи — короткая причина без вывода процессов; подробности остаются в
локальном отчёте. `.env` в рабочее дерево не копируется: конвейеру нужен один ключ, он идёт окружением.

СООБЩЕНИЕ ОБЯЗАНО ДОЙТИ. Запуск идёт раз в сутки, поэтому каждый шаг повторяем: сборка уже есть, но PR или
задачи нет — завтра попытка повторится. Иначе сбой `gh` в день сборки оставил бы оплаченную работу молча.

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

import edition_source as es  # noqa: E402
import watch_edition as we  # noqa: E402
from app.core.console import enable_utf8  # noqa: E402

STALE_DAYS = 7
MAX_ISSUES_PER_RUN = 3     # смена разметки не должна породить пачку задач
DEFAULT_SOURCES = Path("D:/navigator-sources")
MAIN_ENV = Path("D:/navigator-719/.env")
KEY = "DEEPSEEK_API_KEY"
# Что конвейер имеет право менять. `git add` всего дерева забрал бы лишнее.
PUBLISH_PATHS = ("knowledge_base", "app/rag/edition.py", "landing/index.html")
# SHA коммита автомата, рядом с отчётом. Тема коммита не годится: `--amend --no-edit` её сохраняет.
OWN_COMMIT = "auto_commit.txt"
# Признаки настоящего ответа: заглушка с кодом 200 иначе дошла бы до конвейера и заняла ветку.
SOURCE_FILES = ((es.KONTUR_RTF, "edition.rtf", b"{\\rtf"), (es.KONTUR_DOC, "edition.html", b"js-doc-frame-container"))


class HumanCommits(RuntimeError):
    """На ветке автомата есть чужие коммиты: уносить их в публичный репозиторий нельзя."""


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def load_state(path: Path) -> dict:
    """Битый файл состояния — это начало отсчёта, а не повод не проверить Контур."""
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError:
        print(f"файл состояния не читается, начинаю заново: {path.name}", flush=True)
        return {}


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

    def once(self, title: str, body: str, urgent: bool = False, reopen: bool = False) -> str:
        """Одна задача на заголовок. ⚠ Поиск не ответил — не заводим: лучше повторить завтра, чем дубль.

        `urgent` — вне потолка: о сборке и сбое нельзя молчать из-за трёх новых актов.
        `reopen` — смотреть только открытые: причина не устранена, и закрытая задача не должна её глушить.
        """
        if self.dry_run:
            return f"была бы создана задача: {title}"
        if self.made >= self.limit and not urgent:
            return f"потолок задач за запуск, повтор завтра: {title}"
        state = "open" if reopen else "all"
        found = self.runner("issue", "list", "--state", state, "--search", f'"{title}" in:title', "--json", "number")
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
    """Ветка или каталог уже есть: сборку не повторяем, она оплачена."""
    branch = f"auto/edition-{doc_id}"
    return ((sources / f"wt-{doc_id}").exists() or bool(git("branch", "--list", branch).strip())
            or bool(git("ls-remote", "--heads", "origin", branch).strip()))


def prefetch(doc_id: str, sources: Path) -> str | None:
    """Первоисточник — до ветки и дерева: при отказе сети убирать нечего. Причина отказа или `None`.

    ⚠ Автомат ничего не удаляет: прежняя уборка неудачной сборки могла снести правку человека.
    """
    src = sources / doc_id
    src.mkdir(parents=True, exist_ok=True)
    for url, name, mark in SOURCE_FILES:
        data, why = es.fetch(url.format(doc_id=doc_id))
        if not data:
            return why or "пустой ответ"
        if mark not in data:
            return f"{name}: ответ не похож на документ"
        (src / name).write_bytes(data)
    return None


def model_env(env_file: Path = MAIN_ENV) -> dict[str, str]:
    """Окружение конвейера: один ключ модели вместо копии `.env` в рабочем дереве."""
    env = dict(os.environ)
    if KEY not in env and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{KEY}="):
                env[KEY] = line.split("=", 1)[1].strip().strip("\"'")
    return env


def read_report(doc_id: str, sources: Path) -> tuple[bool, dict]:
    """Успех — только полный прогон на своей ветке: частичный запуск руками не должен уйти в публичный PR."""
    path = sources / doc_id / "report.json"
    report = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    ok = (report.get("последний шаг") == "tests" and not str(report.get("вердикт", "")).startswith("ОСТАНОВЛЕНО")
          and report.get("факты", {}).get("ветка") == f"auto/edition-{doc_id}")
    return ok, report


def build_edition(doc_id: str, sources: Path) -> tuple[bool, dict]:
    """Ветка `auto/edition-<id>` от `origin/dev` в своём каталоге, конвейер внутри неё."""
    tree, src = sources / f"wt-{doc_id}", sources / doc_id
    # Упавший при запуске конвейер не должен прочесть чужой отчёт, а публикация — принять чужой коммит за свой.
    for name in ("report.json", "report.md", OWN_COMMIT):
        (src / name).unlink(missing_ok=True)
    git("fetch", "origin", "--quiet")
    git("worktree", "add", "-b", f"auto/edition-{doc_id}", str(tree), "origin/dev")
    run = subprocess.run([sys.executable, "scripts/actualize_edition.py", "--doc-id", doc_id, "--sources", str(sources),
                          "--rtf", str(src / "edition.rtf"), "--page", str(src / "edition.html")], cwd=tree, env=model_env())
    ok, report = read_report(doc_id, sources)
    return ok and run.returncode == 0, report


def pr_opened(doc_id: str, runner=None) -> bool | None:
    """Есть ли PR этой сборки. `None` — GitHub не ответил: решать нечего, повтор завтра.

    Открытый PR — всегда да: его правит человек, и вершины локальной ветки и PR расходятся законно.
    Коммит сверяется только у закрытых: PR прошлой сборки не должен заглушить пересборку.
    ⚠ `--head` фильтрует только по имени ветки, а репозиторий публичен: PR из форка с тем же именем — не наш.
    """
    branch = f"auto/edition-{doc_id}"
    found = (runner or gh)("pr", "list", "--head", branch, "--state", "all", "--json",
                           "headRefOid,state,isCrossRepository")
    if found.returncode != 0:
        return None
    prs = [p for p in json.loads(found.stdout or "[]") if not p.get("isCrossRepository")]
    if any(p["state"] == "OPEN" for p in prs):
        return True
    tip = git("branch", "--list", "--format=%(objectname)", branch).strip()
    return tip in {p["headRefOid"] for p in prs} if tip else bool(prs)


def publish(doc_id: str, tree: Path, report_md: Path) -> str:
    """Черновой PR в `dev`, повторяемо: коммит и пуш могли пройти в прошлый запуск, а PR — нет.

    Черновик нельзя слить случайно: его сначала переводит в готовый человек.
    ⚠ Ветку мог забрать в работу человек, а репозиторий публичен. Публикуется ветка ПО ИМЕНИ, поэтому проверяется
    она, а не HEAD дерева: дерево обязано стоять на ней, а на ней — только коммит автомата, опознанный по SHA
    (тему сохраняет `--amend`). Иначе отказ. Свой коммит уже есть — сборка в нём, и всё незакоммиченное в дереве
    принадлежит человеку: его не трогаем.
    """
    branch = f"auto/edition-{doc_id}"
    title = f"data(auto): редакция ПП 719, документ Контура {doc_id}"
    own_file = report_md.with_name(OWN_COMMIT)
    own = own_file.read_text(encoding="utf-8").strip() if own_file.exists() else ""
    head = subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=tree, capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout.strip()
    if head != f"refs/heads/{branch}":
        raise HumanCommits(f"дерево сборки не стоит на ветке {branch}")
    made_before = git("log", "--format=%H", f"origin/dev..refs/heads/{branch}", cwd=tree).split()
    if made_before and made_before != [own]:
        raise HumanCommits(f"на ветке {branch} есть коммиты не автомата")
    if not made_before:
        git("add", "-A", "--", *PUBLISH_PATHS, cwd=tree)
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=tree).returncode != 0:
            git("commit", "-m", title, cwd=tree)
            own_file.write_text(git("rev-parse", "HEAD", cwd=tree).strip(), encoding="utf-8")
    git("push", "-u", "origin", branch, cwd=tree)
    made = subprocess.run(["gh", "pr", "create", "--draft", "--base", "dev", "--head", branch, "--title",
                           title, "--body-file", str(report_md)],
                          cwd=tree, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if made.returncode != 0:
        raise RuntimeError(f"gh pr create: код {made.returncode}")
    return made.stdout.strip()


def handle_document(doc_id: str, link: str, sources: Path, issues: Issues) -> str:
    """Новая редакция. Каждый шаг повторяем: сообщение обязано дойти, даже если сегодня упал `gh`."""
    if issues.dry_run:
        return f"был бы запущен конвейер: документ {doc_id} ({link})"
    branch = f"auto/edition-{doc_id}"
    if edition_built(doc_id, sources):
        # PR — раньше отчёта. Пока сборка существует, автомат её не пересобирает, и отчёт меняет только ручной
        # прогон человека в том же каталоге: частичный или прерванный, он не повод для ежедневной срочной задачи.
        opened = pr_opened(doc_id)
        if opened is None:
            return f"документ {doc_id}: GitHub не ответил, повтор завтра"
        if opened:
            return f"документ {doc_id}: PR уже открыт"
        ok, report = read_report(doc_id, sources)
    else:
        if (why := prefetch(doc_id, sources)) is not None:
            print(f"документ {doc_id}: первоисточник не скачался: {why}", flush=True)
            return issues.once(f"[A10] Новая редакция ПП 719 {doc_id}: первоисточник не скачивается",
                               "Контур показал новую редакцию, а её текст не скачался. Причина — в локальном журнале, "
                               "попытка повторяется каждый день.", urgent=True, reopen=True)
        ok, report = build_edition(doc_id, sources)
    if not ok:
        step = report.get("последний шаг", "неизвестен")
        return issues.once(f"[A10] Новая редакция ПП 719 {doc_id}: конвейер остановился на шаге {step}",
                           f"Новая редакция, документ Контура {doc_id}. Подробности — в локальном отчёте "
                           "`report.md` каталога первоисточников. Сборку автомат не повторяет, пока есть дерево "
                           f"`wt-{doc_id}` в каталоге первоисточников или ветка `{branch}` (локальная или на GitHub). "
                           "Для пересборки после правки уберите дерево и обе ветки, затем закройте задачу: иначе "
                           "назавтра она вернётся по тому же отчёту.", urgent=True, reopen=True)
    # Свежая сборка PR иметь не может: ветки не было ни здесь, ни на GitHub, а PR из форков не в счёт.
    try:
        return publish(doc_id, sources / f"wt-{doc_id}", sources / doc_id / "report.md")
    except HumanCommits as e:
        print(f"публикация не удалась: {e}", flush=True)
        return issues.once(f"[A10] Новая редакция ПП 719 {doc_id}: на ветке сборки чужая работа, PR не создан",
                           f"На ветке `{branch}` есть коммиты не автомата, или её дерево снято с ветки "
                           f"({type(e).__name__}). Публиковать её автомат не будет, и каждый день исход будет тот же, "
                           "пока вы сами не откроете PR с этой веткой или не уберёте свои коммиты.",
                           urgent=True, reopen=True)
    except (subprocess.CalledProcessError, RuntimeError, OSError) as e:
        print(f"публикация не удалась: {e}", flush=True)
        return issues.once(f"[A10] Новая редакция ПП 719 {doc_id}: сборка готова, PR не создан",
                           f"Конвейер прошёл, публикация не удалась ({type(e).__name__}). Завтра попытка повторится, "
                           f"работа лежит в локальной ветке `auto/edition-{doc_id}`.", urgent=True, reopen=True)


def handle_found(result: dict, sources: Path, issues: Issues) -> list[str]:
    """Новое на Контуре. Новая редакция — первой: её сообщение не должно уступить потолку задач."""
    log = [handle_document(doc_id, link, sources, issues) for doc_id, link in result["новые документы"].items()]
    for act in result["новые отложенные акты"]:
        log.append(issues.once(f"[A10] Новый отложенный акт ПП 719: {act}",
                               f"Контур, блок «Примечания», «ИЗМЕНЕН»: {act}. Внести в задачник и в "
                               "`knowledge_base/pp719/edition_watch.json`."))
    for act in result["сроки изменились"]:
        log.append(issues.once(f"[A10] Срок вступления акта ПП 719 изменился: {act}",
                               f"Контур: {act}. Сверить и обновить `knowledge_base/pp719/edition_watch.json`."))
    for act in result["актов нет в тексте"]:
        log.append(issues.once(f"[A10] Акт есть на Контуре, но нет в корпусе: {act}",
                               "Текст действующей редакции назвал акт, которого нет в `pp719_full.txt`."))
    return log


def run_once(sources: Path, state: dict, issues: Issues) -> None:
    result = we.check_kontur(we.current_doc_id(), we.known_pending())
    moment = now()
    print(f"{moment:%Y-%m-%d %H:%M} UTC: {json.dumps(result, ensure_ascii=False)}", flush=True)
    if result["исход"] == "не проверено":
        silent = days_silent(state, moment)
        if silent is None:
            state.setdefault("последняя успешная проверка", moment.isoformat())
        elif silent >= STALE_DAYS:
            print(issues.once(f"[A10] Слежение за редакциями молчит с {state['последняя успешная проверка'][:10]}",
                              f"Проверка Контура не удаётся {silent} дн. Причина: {result['причина'][:200]}",
                              urgent=True, reopen=True), flush=True)
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
        run_once(sources, state, issues)
    except Exception as e:  # noqa: BLE001 — запуск без человека: любой сбой обязан стать задачей
        traceback.print_exc()
        try:
            print(issues.once(f"[A10] Сбой ежедневного запуска: {type(e).__name__}",
                              "Текст ошибки — в локальном журнале `watch.log`, в задачу он не идёт: репозиторий публичен.",
                              urgent=True, reopen=True), flush=True)
        except OSError as gh_error:   # нет `gh`: задачи не будет, журнал остаётся единственным следом
            print(f"задача о сбое не создана: {gh_error}", flush=True)
    finally:
        if not args.dry_run:
            save_state(state_path, state)


if __name__ == "__main__":
    main()
