"""Хаос-часть уровня 4: что видит пользователь, когда отваливается зависимость (`EV22` #130).

ЗАЧЕМ. `EVAL_GUIDE` требует двух проверок с порогом 1.00 — «LLM недоступна → честный фолбэк, не
выдумка» и «Qdrant недоступен → внятная ошибка, не пустой ответ». В базе сравнения обе помечены
«покрыто юнит-тестами уровня 0», то есть НА БОЮ не проверялись ни разу. Юнит-тест проверяет
ветку кода; здесь проверяется система — с реальным контейнером, реальным рестартом и реальным
ответом, который увидит эксперт ТПП.

⚠⚠ ГЛАВНОЕ ТРЕБОВАНИЕ К ЭТОМУ СКРИПТУ — ВОССТАНОВЛЕНИЕ НЕ В РУКАХ ОПЕРАТОРА.
Хаос-проверка НАМЕРЕННО ломает боевой сервис, которым пользуются живые люди. Канал до VM рвётся
(28.08.2026: SSH не прошёл 3 раза из 3, семь обрывов за одну выкатку накануне). Обрыв посреди
ручного `docker compose stop qdrant` оставил бы бой сломанным, а связи, чтобы это исправить,
могло не быть десять минут. Поэтому:

  1. восстановление стоит в `finally` — оно случится и при исключении, и при Ctrl-C;
  2. ПОМИМО этого запускается ОТВЯЗАННЫЙ сторожевой таймер (`setsid`), который восстановит
     зависимость, даже если этот процесс убьют сигналом, который `finally` не переживает;
  3. восстановление ПРОВЕРЯЕТСЯ запросом, а не считается выполненным по коду возврата команды.

⚠⚠ ВТОРОЕ ТРЕБОВАНИЕ: ПРОВЕРЯТЬ, ЧТО ПОЛОМКА ВООБЩЕ СЛУЧИЛАСЬ.
Сценарий, который не сработал, снаружи неотличим от сценария, который прошёл: и там и там
«ошибок нет». Поэтому каждый сценарий сначала доказывает, что зависимость ДЕЙСТВИТЕЛЬНО отвалилась,
и только потом судит о качестве фолбэка. Не доказал — «НЕ ВОСПРОИЗВЕДЕНО», а не «пройдено». Это то
же правило, по которому у любого инструмента с результатом «ноль» обязан быть положительный
контроль.

⚠⚠ ЗДОРОВЬЕ МЕРЯЕТСЯ ЧИСЛОМ ИСТОЧНИКОВ В ОТВЕТЕ, и это выяснилось РЕПЕТИЦИЕЙ на настоящем
Docker, а не рассуждением. Замерено 28.08.2026: здоровый ответ — `класс=ok, источников=6`;
при остановленном Qdrant — `класс=ok, источников=0`. Сервис отвечает честным фолбэком с кодом
200, поэтому НИ КОД ОТВЕТА, НИ КЛАСС двух состояний не различают. Первая редакция судила о
поломке неравенством ТЕКСТА — а текст у LLM отличается между любыми двумя вызовами, так что
«поломка» подтверждалась всегда; та же редакция объявила «сервис вернулся», когда он ещё лежал.
Оба раза вердикт был бы ЛОЖНО ПОЛОЖИТЕЛЬНЫМ — худший исход для проверки, которая ломает бой.

⚠ ПОЧЕМУ НЕ ВТОРОЙ КОНТЕЙНЕР. Гайд предлагает поднимать «контейнер без зависимости». На боевой
машине 8 ГБ ОЗУ, и e5 уже резидентен в основном процессе; второй экземпляр приложения тянет ещё
~2.1 ГБ и рискует OOM'нуть тот самый сервис, который мы проверяем. Дешевле и честнее уронить
зависимость у работающего сервиса на несколько секунд.

⚠ LLM РОНЯЕТСЯ БЕЗ РЕСТАРТА: в `/etc/hosts` контейнера дописывается строка, уводящая хост
DeepSeek в 127.0.0.1. `DEEPSEEK_BASE_URL` менять нельзя без перезапуска приложения, а перезапуск
— это ещё и повторная загрузка e5, то есть минуты недоступности вместо секунд. Запись в hosts
действует на новые соединения сразу и снимается одной строкой.
⚠ Оговорка честная: пул соединений может пережить подмену, и тогда запрос пройдёт НОРМАЛЬНО.
Скрипт это увидит (класс не изменился) и скажет «не воспроизведено» — а не запишет «пройдено».

ЗАПУСК (на боевой машине, из каталога с `docker-compose.yml`):
    python3 scripts/eval_chaos.py --accounts 'lt.load1:PASS' --base-url http://127.0.0.1 \
        --json /tmp/level4_chaos.json
    # только один сценарий:
    python3 scripts/eval_chaos.py --only qdrant --accounts '...'
    # ⚠ сухой прогон — ничего не ломает, показывает план и проверяет доступность:
    python3 scripts/eval_chaos.py --dry-run --accounts '...'
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

try:
    from app.core.console import enable_utf8
    enable_utf8()
except Exception:  # noqa: BLE001 - на голом хосте пакета `app` рядом нет
    pass

import eval_level4 as L4  # noqa: E402

DEEPSEEK_HOST = "api.deepseek.com"
WATCHDOG_SECONDS = 300  # сторож восстановит зависимость, даже если этот процесс убьют


def sh(cmd: list[str], timeout: float = 120.0) -> tuple[int, str]:
    """Команда оболочки. Возвращаем КОД И ВЫВОД: судить по коду возврата мало — `docker` умеет
    печатать ошибку и возвращать 0 (уже наступали на это в выкатке 20.08, когда упавшая проверка
    не остановила процесс)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, f"таймаут {timeout} с: {' '.join(cmd)}"
    except FileNotFoundError as e:
        return 127, str(e)


class Chaos:
    """Один сценарий: сломать → доказать поломку → опросить → восстановить → доказать возврат."""

    name = "?"
    human = "?"

    def __init__(self, docker: list[str], compose_dir: str, app: str, qdrant: str):
        self.docker, self.compose_dir, self.app, self.qdrant = docker, compose_dir, app, qdrant
        # ⚠⚠ ФАЙЛ КОМПОУЗА УКАЗЫВАЕТСЯ ЯВНО. Первая редакция принимала `--compose-dir` и НЕ
        # ИСПОЛЬЗОВАЛА его: команды шли просто `docker compose stop`, то есть подхватили бы
        # проект из текущего каталога — на боевой машине это «какой попало» сервис. Аргумент,
        # который принимается и игнорируется, хуже отсутствующего: оператор считает, что указал.
        # ⚠ Путь склеивается СТРОКОЙ, а не `Path`. Цель этого скрипта всегда Linux (боевая VM),
        # а `Path` на Windows дал бы `\home\yc-user\...\docker-compose.yml` — и команда, которую
        # разработчик проверил у себя, оказалась бы не той, что уедет на бой. Инструмент, чью
        # команду нельзя посмотреть с рабочей машины, проверяется только на бою — поздно.
        self.compose = self.docker + ["compose", "--project-directory", compose_dir,
                                      "-f", compose_dir.rstrip("/") + "/docker-compose.yml"]

    def break_it(self) -> tuple[int, str]:
        raise NotImplementedError

    def restore(self) -> tuple[int, str]:
        raise NotImplementedError

    def watchdog(self) -> list[str]:
        """Команда для отвязанного сторожа. ⚠ Он обязан быть ИДЕМПОТЕНТНЫМ: сработает и после
        того, как штатное восстановление уже прошло."""
        raise NotImplementedError


class QdrantDown(Chaos):
    name, human = "qdrant", "Qdrant недоступен"

    def break_it(self):
        return sh(self.compose + ["stop", self.qdrant])

    def restore(self):
        return sh(self.compose + ["start", self.qdrant])

    def watchdog(self):
        return self.compose + ["start", self.qdrant]


class LLMDown(Chaos):
    name, human = "llm", "LLM (DeepSeek) недоступна"
    _LINE = f"127.0.0.1 {DEEPSEEK_HOST}"

    def break_it(self):
        return sh(self.compose + ["exec", "-T", self.app, "sh", "-c",
                                 f"grep -q '{self._LINE}' /etc/hosts || echo '{self._LINE}' >> /etc/hosts"])

    def restore(self):
        # ⚠ sed по ТОЧНОЙ строке, не по имени хоста: снести чужую запись мы права не имеем.
        return sh(self.compose + ["exec", "-T", self.app, "sh", "-c",
                                 f"sed -i '\\|^{self._LINE}$|d' /etc/hosts"])

    def watchdog(self):
        return self.compose + ["exec", "-T", self.app, "sh", "-c",
                              f"sed -i '\\|^{self._LINE}$|d' /etc/hosts"]


WATCHDOG_LOG = "/tmp/eval_chaos_watchdog.log"


def watchdog_command(cmd: list[str], seconds: int) -> str:
    """Строка для `sh -c`. Вынесена отдельно, чтобы её можно было ПРОВЕРИТЬ тестом, не запуская.

    ⚠⚠ ЭКРАНИРОВАНИЕ ЧЕРЕЗ `shlex.quote`, А НЕ «обернуть в кавычки, если есть пробел». Первая
    редакция делала второе — и ломалась ровно на команде восстановления LLM, потому что та сама
    содержит одинарные кавычки: `sed -i '\\|^127.0.0.1 api.deepseek.com$|d' /etc/hosts`. Внутренние
    кавычки закрыли бы внешние, сторож упал бы с синтаксической ошибкой оболочки — и НИКТО БЫ ОБ
    ЭТОМ НЕ УЗНАЛ, потому что вывод уходил в /dev/null. Второй контур восстановления, молча
    сломанный, — худший вид предохранителя: оператор ломает бой, считая себя подстрахованным."""
    import shlex
    return f"sleep {seconds}; " + " ".join(shlex.quote(c) for c in cmd)


def arm_watchdog(cmd: list[str], seconds: int) -> str:
    """⚠⚠ ВТОРОЙ КОНТУР ВОССТАНОВЛЕНИЯ, независимый от этого процесса. `finally` не переживает
    SIGKILL и не помогает, если оператора отрезало вместе с терминалом. Сторож отвязывается
    (`setsid`), спит и чинит — даже если штатное восстановление уже прошло (команды идемпотентны)."""
    # ⚠ Вывод — В ФАЙЛ, а не в /dev/null: упавший сторож обязан оставить след. Раньше его
    # падение было бы неотличимо от успешной работы.
    inner = f"{{ {watchdog_command(cmd, seconds)} ; }} >> {WATCHDOG_LOG} 2>&1"
    try:
        subprocess.Popen(["setsid", "sh", "-c", inner],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return f"сторож взведён на {seconds} с, его вывод — {WATCHDOG_LOG}"
    except FileNotFoundError:
        # ⚠ Нет `setsid` (не-Linux) — говорим ПРЯМО. Молчаливое отсутствие второго контура хуже
        # его отсутствия: оператор будет думать, что подстраховка есть.
        return "⚠⚠ СТОРОЖ НЕ ВЗВЕДЁН: нет `setsid`. Второго контура восстановления НЕТ"


def probe(base: str, cookie: str, question: str, timeout: float) -> dict:
    """Опрос ОБОИМИ путями: стрим и то, что реально видит человек при фолбэке."""
    stream = L4.ask(base, cookie, question, timeout)
    plain = L4.ask_plain(base, cookie, question, timeout)
    return {"stream_class": stream["class"], "plain_class": plain["class"],
            "plain_status": plain["status"], "text": (plain.get("text") or "")[:600],
            "unverified": plain.get("unverified") or [], "sources": plain.get("sources", 0)}


def healthy(p: dict) -> bool:
    """⚠⚠ ЗДОРОВЬЕ МЕРЯЕТСЯ ИСТОЧНИКАМИ, А НЕ КОДОМ ОТВЕТА И НЕ ТЕКСТОМ. Это выяснилось
    репетицией 28.08.2026 на настоящем Docker, и первая редакция `judge` на этом сгорела дважды.

    Замерено: здоровый ответ — `класс=ok, источников=6`; при остановленном Qdrant —
    `класс=ok, источников=0`. То есть сервис отвечает ЧЕСТНЫМ ФОЛБЭКОМ с кодом 200, и по коду
    два состояния НЕРАЗЛИЧИМЫ. Первая редакция ловила «поломку» неравенством ТЕКСТА — а текст у
    LLM отличается между двумя вызовами всегда, поэтому «поломка» подтверждалась и там, где её
    не было. Она же объявила «сервис вернулся», когда он ещё лежал: класс-то был `ok`.

    Источники — признак СТРУКТУРНЫЙ: они берутся из выдачи ретрива, и пока Qdrant лежит, их
    неоткуда взять."""
    return p["plain_class"] == L4.OK and p.get("sources", 0) > 0


def judge(before: dict, during: dict, after: dict, recovery_seconds: float | None = None) -> dict:
    """Вердикт сценария. ⚠ Три состояния, а не два: пройдено / провалено / НЕ ВОСПРОИЗВЕДЕНО."""
    if healthy(during):
        return {"verdict": "НЕ ВОСПРОИЗВЕДЕНО",
                "why": f"сервис остался здоров (источников {during.get('sources')}) — "
                       "зависимость, похоже, доступна (пул соединений, кэш, другая точка входа). "
                       "Это НЕ «пройдено»."}
    if during["unverified"]:
        return {"verdict": "ПРОВАЛЕН",
                "why": f"в ответе незаземлённые числа {during['unverified']} — это выдумка, "
                       "а гайд требует честного фолбэка"}
    text = (during["text"] or "").strip()
    if not text:
        return {"verdict": "ПРОВАЛЕН",
                "why": f"пользователь не получил текста вовсе (класс {during['plain_class']}) — "
                       "гайд требует ВНЯТНОЙ ошибки, а не пустого ответа"}
    recovered = healthy(after)
    return {"verdict": "ПРОЙДЕН" if recovered else "ПРОВАЛЕН",
            "why": (f"фолбэк внятный, сервис вернулся за {recovery_seconds} с" if recovered
                    else "⚠⚠ СЕРВИС НЕ ВЕРНУЛСЯ ПОСЛЕ ВОССТАНОВЛЕНИЯ — разбирать немедленно"),
            "recovered": recovered, "recovery_seconds": recovery_seconds}


def run_scenario(sc: Chaos, base: str, cookie: str, question: str, timeout: float,
                 settle: float, recovery_deadline: float = 90.0) -> dict:
    print(f"\n── сценарий «{sc.human}» ─────────────────────────────────")
    before = probe(base, cookie, question, timeout)
    print(f"   до:      стрим={before['stream_class']} чат={before['plain_class']} "
          f"источников={before['sources']}")
    if not healthy(before):
        return {"scenario": sc.name, "verdict": "НЕ ЗАПУЩЕН",
                "why": f"сервис нездоров ДО поломки (класс {before['plain_class']}, источников "
                       f"{before['sources']}) — ломать нельзя, вердикт был бы бессмысленным",
                "before": before}

    print(f"   {arm_watchdog(sc.watchdog(), WATCHDOG_SECONDS)}")
    during = after = None
    try:
        code, out = sc.break_it()
        print(f"   ломаю:   код {code} {out[:100]}")
        time.sleep(settle)
        during = probe(base, cookie, question, timeout)
        print(f"   во время: стрим={during['stream_class']} чат={during['plain_class']} "
              f"источников={during['sources']} текст={during['text'][:60]!r}")
    finally:
        code, out = sc.restore()
        print(f"   чиню:    код {code} {out[:100]}")
        # ⚠⚠ ЖДЁМ ЗДОРОВЬЯ ОПРОСОМ, А НЕ ФИКСИРОВАННОЙ ПАУЗОЙ. Репетиция 28.08 показала: Qdrant
        # поднимает коллекции ~6 с после `start`, и пауза в 4 с давала «сервис не вернулся» на
        # исправном сервисе. Фиксированная пауза — это либо ложная тревога, либо потерянное
        # время; опрос даёт ещё и ЧИСЛО — сколько сервис возвращался, а оно тоже относится
        # к надёжности и в гайде своей строки не имеет.
        t_restore = time.monotonic()
        recovery_seconds = None
        for _ in range(int(max(1, recovery_deadline // max(settle, 1)))):
            time.sleep(settle)
            after = probe(base, cookie, question, timeout)
            if healthy(after):
                recovery_seconds = round(time.monotonic() - t_restore, 1)
                break
        print(f"   после:   стрим={after['stream_class']} чат={after['plain_class']} "
              f"источников={after['sources']} возврат={recovery_seconds} с")
        if not healthy(after):
            print("   ⚠⚠ СЕРВИС НЕ ВЕРНУЛСЯ. Сторож сработает сам, но проверьте руками.")

    res = {"scenario": sc.name, "human": sc.human,
           "before": before, "during": during, "after": after}
    res.update(judge(before, during, after, recovery_seconds))
    print(f"   ВЕРДИКТ: {res['verdict']} — {res['why']}")
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="Хаос уровня 4 (EV22 #130)")
    ap.add_argument("--base-url", default="http://127.0.0.1")
    ap.add_argument("--accounts", required=True, help="'логин:пароль' — достаточно одного")
    ap.add_argument("--compose-dir", default=".", help="каталог с docker-compose.yml")
    ap.add_argument("--docker", default="sudo -n docker", help="как звать docker")
    ap.add_argument("--app-service", default="app")
    ap.add_argument("--qdrant-service", default="qdrant")
    ap.add_argument("--only", choices=("qdrant", "llm"), default="")
    ap.add_argument("--question", default="какие критерии подтверждения производства продукции")
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--settle", type=float, default=3.0, help="пауза после поломки/починки")
    ap.add_argument("--recovery-deadline", type=float, default=90.0,
                    help="сколько ждать возврата сервиса опросом (Qdrant поднимает коллекции ~6 с)")
    ap.add_argument("--dry-run", action="store_true", help="НИЧЕГО НЕ ЛОМАТЬ: план и готовность")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    docker = args.docker.split()
    user, _, pw = args.accounts.split(",")[0].partition(":")
    cookie = L4.login(args.base_url, user, pw, timeout=args.timeout)
    print(f"вход: {user} на {args.base_url}")

    scenarios = [QdrantDown(docker, args.compose_dir, args.app_service, args.qdrant_service),
                 LLMDown(docker, args.compose_dir, args.app_service, args.qdrant_service)]
    if args.only:
        scenarios = [s for s in scenarios if s.name == args.only]

    if args.dry_run:
        print("\nСУХОЙ ПРОГОН — ничего не ломается.")
        code, out = sh(scenarios[0].compose + ["ps", "--format", "{{.Service}} {{.State}}"])
        print(f"  контейнеры: код {code}\n{out[:400]}")
        base = probe(args.base_url, cookie, args.question, args.timeout)
        print(f"  сервис сейчас: стрим={base['stream_class']} чат={base['plain_class']}")
        for s in scenarios:
            print(f"  сценарий «{s.human}»\n     восстановление: {' '.join(s.watchdog())}")
        print(f"  сторож: {WATCHDOG_SECONDS} с, независимо от этого процесса")
        return 0 if base["stream_class"] == L4.OK else 1

    results = [run_scenario(s, args.base_url, cookie, args.question, args.timeout, args.settle,
                            args.recovery_deadline) for s in scenarios]

    # ⚠ ДВА РАЗНЫХ СООБЩЕНИЯ — отдельное требование базы сравнения: «временный сбой» и
    # «выключено настройкой» не должны выглядеть одинаково, иначе эксперт не поймёт, ждать ему
    # или звать администратора.
    texts = {r["scenario"]: (r.get("during") or {}).get("text", "") for r in results}
    distinct = len({t.strip() for t in texts.values() if t.strip()}) == len(
        [t for t in texts.values() if t.strip()])
    if len(results) > 1:
        print(f"\nсообщения сценариев РАЗНЫЕ: {distinct}")

    ok = all(r["verdict"] == "ПРОЙДЕН" for r in results)
    print(f"\n{'=' * 70}\nитог: " + ", ".join(f"{r['scenario']}={r['verdict']}" for r in results))
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"results": results, "messages_distinct": distinct}, ensure_ascii=False, indent=1),
            encoding="utf-8")
        print(f"   → {args.json}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
