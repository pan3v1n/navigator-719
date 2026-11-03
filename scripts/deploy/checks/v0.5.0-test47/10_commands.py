"""test47 — готовые команды `L8-1` #153: рантаймовый релиз, миграция `ADD COLUMN`.

ЗАЧЕМ. Корпус и индекс те же, что у test46: счётчики точек ничего не скажут. Команда — пресет: текст
запроса собирает сервер, источник задаёт команда, ключ пишется у ответа (`messages.command`). Доехало ли —
видно только по тому, что получает движок, что пишется в базу и что видит человек. Проверка идёт в
контейнере приложения; лендинга и Caddy она не касается.

Что проверяется (провал любого пункта — провал выкатки):
  1. каталог: 12 команд с ключами, которые пишутся в базу (переименованный ключ теряет подпись у
     сохранённых ответов), 2 «Скоро», 8 на главной, 5 в сайдбаре; каждая собирается без места подстановки;
  2. миграция на старте: колонка `command` у `messages` и `guest_messages` боевой базы (база только ЧИТАЕТСЯ);
  3. вошедший пользователь — настоящие маршруты (вход по паролю, https, кука Secure), база в памяти, движок
     подменён (DeepSeek не зовётся): в движок уходит собранный вопрос, корпус команды, поиск по значению;
     подпись «источник задан командой»; ключ у ответа; стрим отдаёт собранный вопрос; неизвестная команда и
     пустое значение — 422 без движка и без записи; уточнение по источнику команды — подпись командная,
     ключ `held:`; вопрос без команды — прежний путь (без `search_text`, «выбран автоматически»);
  4. гость: команда идёт пробным вопросом и пишет ключ. Режим выключен в `.env` — «НЕ ПРОВЕРЕНО», не провал;
  5. страница `/chat`: карточки всех команд каталога и обе «Скоро»;
  6. срез «Команды» в «Качестве»: строка на каждую команду, запуск засчитан, уточнение — нет;
  7. пайплайн (поиск подменён, сети нет): поиск и гейт релевантности товарной команды видят значение поля,
     модель — вопрос целиком; обычный вопрос ищется самим вопросом, как до релиза. Без этого блока проверка
     видела бы только то, что маршрут ПЕРЕДАЛ значение, а не то, что движок им воспользовался.

⚠ Проверка НИЧЕГО не пишет в боевую базу: `get_session` подменён во всех модулях `app.api`, которые его
держат, проверка идёт отдельным процессом. Положительный контроль — прогон на коде `dev` (результат в
профиле релиза): старый код обязан отчитаться провалами, а не упасть на импорте.
"""
from app.core.console import enable_utf8

enable_utf8()  # #107: проверка печатает значки вне cp1251

import json  # noqa: E402

# Ключи пишутся в базу с первого запуска: переименовать — значит потерять подпись у записанных ответов
KEYS = ("req", "docs", "exp", "buy", "plain", "prep", "parts", "ops", "terms", "registry", "points", "st1")
CODE = "28.92.21.110"  # бульдозеры: код в значении поля доходит до буста


def main() -> int:
    bad = 0

    def check(ok: bool, good: str, fail: str) -> None:
        nonlocal bad
        if ok:
            print(f"   OK: {good}")
        else:
            bad += 1
            print(f"   ❌ {fail}")

    print("--- 1. Каталог команд")
    try:
        from app.core import commands
    except ImportError as e:
        commands = None
        check(False, "", f"модуля команд нет: {e}")
    if commands:
        keys = tuple(c.key for c in commands.COMMANDS)
        check(keys == KEYS, "12 команд, ключи прежние", f"ключи каталога: {keys}")
        check(len(commands.SOON) == 2, "две карточки «Скоро»", f"«Скоро»: {len(commands.SOON)}")
        home, pinned = sum(c.home for c in commands.COMMANDS), sum(c.pinned for c in commands.COMMANDS)
        check((home, pinned) == (8, 5), "8 на главной, 5 в сайдбаре", f"на главной {home}, в сайдбаре {pinned}")
        broken = []
        for c in commands.COMMANDS:
            out = c.render("насосы {0}")
            if commands.VALUE in out or (c.field is not None) != ("насосы {0}" in out):
                broken.append(c.key)
        check(not broken, "каждая команда собирает запрос, фигурная скобка в значении не ломает сборку",
              f"сборка запроса: {broken}")

    print("--- 2. Миграция: колонка command в боевой базе")
    try:
        from sqlalchemy import inspect

        from app.db.engine import engine
        cols = {t: {c["name"] for c in inspect(engine).get_columns(t)} for t in ("messages", "guest_messages")}
        missing = [t for t, c in cols.items() if "command" not in c]
        check(not missing, "messages.command и guest_messages.command есть", f"колонки command нет: {missing}")
    except Exception as e:  # noqa: BLE001
        check(False, "", f"схема базы не прочиталась: {type(e).__name__}: {e}")

    try:
        routes(check)
    except Exception as e:  # noqa: BLE001
        check(False, "", f"сценарии кодом образа не выполнились: {type(e).__name__}: {e}")
    try:
        engine_search(check)
    except Exception as e:  # noqa: BLE001
        check(False, "", f"пайплайн с заглушками поиска не выполнился: {type(e).__name__}: {e}")
    return report(bad)


def engine_search(check) -> None:
    """Блок 7: что видят поиск, гейт и модель. Подменяются только функции поиска в модуле пайплайна и флаг
    реранкера — в процессе проверки, не в работающем приложении."""
    from app.core import commands
    from app.rag import corpora
    from app.rag import pipeline as pl
    from app.rag.retriever import Hit

    print("--- 7. Пайплайн: поиск по значению поля, модель видит вопрос")
    hit = Hit(score=0.9, section_roman="XIX", section_title="Насосы", product_name="Насосы",
              okpd2_codes=[], min_threshold=None, requirement_blocks=[], source_anchor="Раздел XIX, позиция 1")
    seen: list[tuple[str, str]] = []
    prompts: list[str] = []
    names = ("embed_query", "search", "search_cases", "dense_top1", "product_low_relevance",
             "build_navigator_user_prompt")
    orig = {n: getattr(pl, n) for n in names}
    rerank = pl.settings.RERANK_ENABLED

    def spy_prompt(*a, **k):
        prompts.append(a[0])
        return orig["build_navigator_user_prompt"](*a, **k)

    pl.embed_query = lambda t: (seen.append(("embed", t)), [0.1] * 4)[1]
    pl.search = lambda q_, **k: (seen.append(("search", q_)), [hit])[1]
    pl.search_cases = lambda *a, **k: []
    pl.dense_top1 = lambda *a, **k: 0.9
    pl.product_low_relevance = lambda s, *a, **k: (seen.append(("gate", s)), False)[1]
    pl.build_navigator_user_prompt = spy_prompt
    pl.settings.RERANK_ENABLED = False
    try:
        question = commands.find("req").render("насосы")
        pl._plan_answer(question, corpus=corpora.APPENDIX, search_text="насосы")
        got = {what: t for what, t in seen}
        check(got == {"embed": "насосы", "search": "насосы", "gate": "насосы"},
              "поиск, вектор и гейт релевантности видят значение поля", f"поиск видел: {got}")
        check(prompts[:1] == [question], "модель видит вопрос команды целиком",
              f"в промпт ушло: {str(prompts[:1])[:80]}")
        seen.clear()
        pl._plan_answer("требования к насосам", corpus=corpora.APPENDIX)
        check({t for _, t in seen} == {"требования к насосам"}, "обычный вопрос ищется самим вопросом",
              f"обычный вопрос: {seen}")
    finally:
        for n, f in orig.items():
            setattr(pl, n, f)
        pl.settings.RERANK_ENABLED = rerank


def routes(check) -> None:
    """Блоки 3–6: настоящие маршруты на базе в памяти, движок подменён."""
    import importlib
    import pkgutil
    from unittest import mock

    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import app.api as api_pkg
    from app.api import admin_stats, auth, chat, guest, web
    from app.api.ratelimit import SlidingWindow
    from app.core import commands
    from app.core.config import settings
    from app.db import queries as q
    from app.db.models import Base, GuestMessage, Message
    from app.rag import corpora
    from main import app

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng, expire_on_commit=False)
    pw = "Check-Expert-47"
    with S() as db:
        q.create_user(db, "check-expert47", auth.hash_password(pw), role="expert")

    calls: list[tuple[str, dict]] = []

    class Ans:  # форма ответа движка, которую читают маршруты
        text = "Ответ проверки"
        hits: list = []
        cases: list = []
        rule_sources: list = []
        low_relevance = False
        unverified_numbers: list = []
        prompt_tokens = 1
        completion_tokens = 1
        input_hint = ""
        metered = True
        phantom_documents: list = []
        corpus = corpora.USED_APPENDIX

    def fake(message, **kw):
        calls.append((message, kw))
        return Ans()

    def fake_stream(message, **kw):
        calls.append((message, kw))
        yield "delta", Ans.text
        yield "done", Ans()

    mods = [importlib.import_module(f"app.api.{m.name}") for m in pkgutil.iter_modules(api_pkg.__path__)]
    patches = [mock.patch.object(m, "get_session", S) for m in mods if hasattr(m, "get_session")]
    patches += [mock.patch.object(chat, "answer", fake), mock.patch.object(chat, "answer_stream", fake_stream),
                mock.patch.object(chat, "_chat_limit", SlidingWindow(1000, window=60.0)),
                mock.patch.object(guest, "answer", fake), mock.patch.object(guest, "answer_stream", fake_stream),
                mock.patch.object(guest, "_ip_limit", SlidingWindow(1000, window=86400.0)),
                mock.patch.object(guest, "_retry_once", SlidingWindow(1, window=300.0))]
    for p in patches:
        p.start()
    try:
        web._login_limit.reset()
        # https: на сервере COOKIE_SECURE=true, кука сессии Secure — по http клиент её не вернул бы, и все
        # /api/* ответили бы 401 на верном коде (ревью test40, HIGH). Без `with` — lifespan не запускается.
        user = TestClient(app, base_url="https://testserver")
        login = user.post("/login", data={"username": "check-expert47", "password": pw}, follow_redirects=False)
        web._login_limit.reset()
        check(login.status_code == 302, "вход по паролю (https)", f"вход: {login.status_code}")

        def bot(model=Message):
            with S() as db:
                return db.execute(select(model).where(model.role == "assistant")
                                  .order_by(model.id.desc()).limit(1)).scalars().first()

        def rows(model=Message) -> int:
            with S() as db:
                return len(list(db.execute(select(model)).scalars()))

        req, docs = commands.find("req"), commands.find("docs")

        print("--- 3. Вошедший пользователь: команда, стрим, отказы, уточнение, обычный вопрос")
        r = user.post("/api/chat", json={"message": CODE, "command": "req", "session_id": "chk47"})
        d = r.json() if r.status_code == 200 else {}
        message, kw = calls[-1] if calls else ("", {})
        check(r.status_code == 200 and message == req.render(CODE),
              "в движок ушёл собранный вопрос", f"/api/chat {r.status_code}, в движок: {message[:70]!r}")
        check(kw.get("corpus") == corpora.APPENDIX and kw.get("search_text") == CODE,
              "корпус команды, поиск по значению поля",
              f"корпус {kw.get('corpus')!r}, search_text {kw.get('search_text')!r}")
        check(d.get("question") == req.render(CODE), "ответ отдаёт собранный вопрос", "в ответе нет вопроса")
        check("источник задан командой «Требования к продукции»" in d.get("corpus_label", ""),
              "подпись: источник задан командой", f"подпись: {d.get('corpus_label', '')!r}")
        last = bot()
        check(getattr(last, "command", None) == "req", "ключ команды записан у ответа",
              f"у ответа ключ {getattr(last, 'command', '—')!r}")

        r = user.post("/api/chat/stream", json={"command": "docs", "session_id": "chk47s"})
        events = [json.loads(x[6:]) for x in r.text.split("\n\n") if x.startswith("data: ")]
        done = events[-1] if events else {}
        check(r.status_code == 200 and done.get("question") == docs.render()
              and getattr(bot(), "command", None) == "docs",
              "стрим: команда без поля, собранный вопрос, ключ записан",
              f"стрим {r.status_code}, вопрос {str(done.get('question'))[:50]!r}")

        before_calls, before_rows = len(calls), rows()
        unknown = user.post("/api/chat", json={"message": "x", "command": "нет-такой", "session_id": "chk47"})
        empty = user.post("/api/chat", json={"message": "", "command": "req", "session_id": "chk47"})
        detail = empty.json().get("detail") if empty.status_code == 422 else ""
        check(unknown.status_code == 422 and empty.status_code == 422 and str(detail).startswith("Укажите")
              and len(calls) == before_calls and rows() == before_rows,
              "неизвестная команда и пустое значение — 422 без движка и без записи",
              f"неизвестная {unknown.status_code}, пустое {empty.status_code} {detail!r}, "
              f"движок +{len(calls) - before_calls}, записей +{rows() - before_rows}")

        r = user.post("/api/chat", json={"message": "а для насосов?", "corpus": corpora.APPENDIX,
                                         "session_id": "chk47"})
        label = r.json().get("corpus_label", "") if r.status_code == 200 else ""
        held = getattr(bot(), "command", None)
        check("задан командой" in label and held == commands.held("req"),
              "уточнение по источнику команды: подпись командная, ключ held:",
              f"уточнение: подпись {label!r}, ключ {held!r}")

        r = user.post("/api/chat", json={"message": "Требования к насосам 28.13", "session_id": "chk47p"})
        label = r.json().get("corpus_label", "") if r.status_code == 200 else ""
        message, kw = calls[-1]
        check(r.status_code == 200 and "search_text" not in kw and "выбран автоматически" in label
              and getattr(bot(), "command", "—") is None,
              "вопрос без команды — прежний путь", f"обычный вопрос: {r.status_code}, {sorted(kw)}, {label!r}")

        print("--- 4. Гость")
        if not settings.GUEST_TRIAL_ENABLED:
            print("   ⚠ НЕ ПРОВЕРЕНО: пробный режим выключен в настройках сервера")
        else:
            g = TestClient(app, base_url="https://testserver")
            g.get("/chat")  # гость открывает сервис — ставится кука
            r = g.post("/api/guest/chat", json={"message": "насосы", "command": "req", "session_id": "chk47g"})
            gd = r.json() if r.status_code == 200 else {}
            check(r.status_code == 200 and gd.get("question") == req.render("насосы")
                  and calls[-1][1].get("corpus") == corpora.APPENDIX
                  and getattr(bot(GuestMessage), "command", None) == "req",
                  "гость: команда пробным вопросом, корпус команды, ключ записан",
                  f"гость {r.status_code}: {str(gd.get('detail') or gd.get('question'))[:70]!r}")

        print("--- 5. Страница /chat")
        html = user.get("/chat").text
        lost = [k for k in KEYS if f'data-command="{k}"' not in html]
        soon = [s.title for s in commands.SOON if s.title not in html]
        check(not lost and not soon and "Готовые команды" in html, "карточки всех команд и «Скоро» на странице",
              f"нет на странице: команд {lost}, «Скоро» {soon}")

        print("--- 6. Срез «Команды» в «Качестве»")
        with S() as db:
            stats = admin_stats.build_admin_view(db)["stats"]
        # Список, а не словарь по названию: уточнение `held:req` носит то же название, что и команда, и в
        # словаре его отдельная строка схлопнулась бы с настоящей (мутация 03.11 так и выжила).
        cmd_rows = stats.get("commands", [])
        req_uses = [x["uses"] for x in cmd_rows if x["title"] == req.title]
        check(len(cmd_rows) == len(KEYS) and req_uses == [1],
              "строка на каждую команду; запуск засчитан, уточнение — нет",
              f"строк {len(cmd_rows)}, запусков «{req.title}»: {req_uses}")
    finally:
        for p in patches:
            p.stop()


def report(bad: int) -> int:
    print(f"=== релизная проверка test47: провалов {bad} ===")
    return 1 if bad else 0


raise SystemExit(main())
