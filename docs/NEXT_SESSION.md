# План на следующую сессию — Навигатор ПП №719

> Составлен 2026-07-20 в конце сессии доработок по итогам открытого теста №1.
> Полный план задач — `docs/POST_TEST1_TZ.md`. Состояние движка — память проекта.

## Состояние (что сделано в этой сессии)

**`dev = 48aa487` = origin/dev** (11 коммитов). `main = prod = e4bf3ef` — **VM у экспертов на СТАРОМ
коде**, доработки не задеплоены (редеплой отложен по решению юзера).

- ✅ **Волна 0** — T1 (СТ-1, не «невозможно»), T7 (терминология), T3 (обязательные/балльные 2 блока),
  T6 (якорь-код диалога), петля кейсов (5 verified_cases).
- ✅ **Волна 1** — Шаг 1: тело ПП 719 (критерии/СТ-1) + Приказ ТПП №52 в `pp719_rules` (58→242);
  Шаг 2: простые пороги «не менее N баллов» из примечаний (`thresholds.py`); Шаг 3: T3 закрыт Волной 0,
  T11 системный → 2.0 (parent/child), боль авто → verified_case.
- ✅ **Волна 2 / T5** — поиск по частичному коду ОКПД2 (класс-код → дочерние позиции; поле
  `okpd2_prefixes`). recall@1=1.00 без регресса.
- ✅ **Волна 3 / T17** — табличный вывод (Markdown-таблицы в ответах + рендер + скролл).

Локальный Qdrant живой (Docker-контейнер `qdrant`, том `D:/navigator-719/qdrant_storage`, порт 6533):
`pp719=1379` (+поле okpd2_prefixes), `pp719_rules=242`, `verified_cases=5`.

## Приоритет 1 — T18 стриминг ответа (дизайн готов, разведка сделана)

Постепенный вывод ответа (как Claude/ChatGPT). Полный full-stack. **Разведка выполнена — вот план:**

**Текущий поток (non-stream):** `chat.js ask()` (стр. 376) → POST `/api/chat` → JSON
`{answer, sources, message_id}` → `renderMarkdown` → `addFeedbackBar(message_id)`.
`pipeline.answer()` (стр. 316): пред-работа (meta/`_contextualize`/процедурный гейт/`search`/rerank/
cases/low_rel/`format_context`/сборка `messages`) → DeepSeek non-stream → пост-проверка
(`_strip_emoji`,`unverified_numbers`) → `Answer`. `chat.py chat()` (стр. 81): `answer()` →
`log_message`(user+assistant, получить `message_id`) → `ChatResponse`.

**Что делать:**
1. **Бэкенд `pipeline.py`:** вынести пред-работу в `_plan_answer(query, okpd2, limit, history)` →
   вернуть либо ранний `Answer` (meta/процедурный/нет-позиции), либо план (`messages, grounding, hits,
   cases, low_rel, effective_okpd2`). `answer()` использует его (поведение НЕ меняется — прогнать eval).
   Новый генератор `answer_stream(...)`: ранний ответ → отдать одним куском; LLM-путь → `stream=True` +
   `stream_options={"include_usage":True}`, копить текст, в КОНЦЕ `_strip_emoji`+`unverified_numbers`,
   отдать финал-метаданные. (Процедурный путь `_answer_procedural` в v1 можно отдавать non-stream одним
   куском; стрим для него — фолоу-ап.)
2. **Эндпоинт `chat.py`:** новый `POST /api/chat/stream` → `StreamingResponse`
   `media_type="text/event-stream"`. События SSE: `data:{"type":"delta","text":"…"}` … затем
   `data:{"type":"done","message_id":N,"sources":[…],"low_relevance":…,"unverified_numbers":[…],
   "session_id":"…"}`. `log_message`(user+assistant) и `message_id` — в конце стрима (нужен полный текст).
3. **Фронт `chat.js ask()`:** `fetch("/api/chat/stream")` → читать `res.body.getReader()`, парсить SSE,
   дописывать `delta` в пузырь с инкрементальным `renderMarkdown`, на `done` — `sources` +
   `addFeedbackBar(message_id)` + флаг `unverified`. **Фолбэк:** при ошибке/обрыве стрима — вызвать
   старый `/api/chat` (сохранить его!). ⚠ Рваная РФ-сеть: длинный SSE хрупок → фолбэк обязателен.
4. **Проверка:** curl SSE (увидеть delta+done), `node --check chat.js`, юнит-тесты, ручной прогон UI
   (`main.py` локально, Qdrant поднят). Faithfulness/лог/фидбек не потерять.

## Приоритет 2 — Редеплой (когда юзер стартует VM)

Всё в `dev`, экспертам не видно. Редеплой (юзер стартует VM в консоли → новый IP):
- `git archive dev | ssh tar-x` на VM → `sudo docker compose up -d --build app` (пересобрать код-слой).
- `sudo docker compose run -T --rm app python scripts/load_rules_kb.py` — **корпус вырос 58→242** (тело
  719 + Приказ 52) — ОБЯЗАТЕЛЬНО.
- `sudo docker compose run -T --rm app python scripts/seed_cases.py` — **verified_cases 5** — ОБЯЗАТЕЛЬНО.
- `sudo docker compose run -T --rm app python scripts/backfill_okpd2_prefixes.py` — поле для T5 (частичный
  код) БЕЗ 20-мин reindex. (Полный `load_kb` НЕ нужен — структура pp719 не менялась, только payload-поле.)
- e2e снаружи (СТ-1/пороги/таблица/частичный код) → FF `prod`/`main` на `dev` → приёмка ≥70%.
- ⚠ `run` только с `-T` (иначе SSH-канал рвётся на рваной сети); docker через `sudo` (yc-user не в группе).

## Остаток бэклога (по убыванию готовности)

- **T2-хвост** (данные, локально): описательные примечания (прим. 8(1) лопасти — форма «не менее N для
  <описание>», без кода → матч по имени), условное прим.55 (белорусские/Союзное гос-во), сноски-условия
  `<45>`/`<9>` к операциям (parse_rtf их режет). Инъекция в контекст, без реиндекса.
- **T5-хвост / T9** (нужен внешний датасет): справочник ОКПД2 (подсказка кода по наименованию, глубокий
  recall сверла→25.73.4) + переходные ключи ОКПД2↔ТН ВЭД (alta.ru).
- **T12** (нужны исходники от заказчика): Методические рекомендации, Методика расчёта стоимости экспертизы.
- **T13** редакции во времени · **T14** меры поддержки (ПП 1875) · **T15** генерация документов/чек-листов
  · **T16** навигация по запросам в сессии · **T10** загрузка ТУ.
- **T11 системный** (2.0): parent/child auto-merge для авто разд. II / судов XVIII (матрица
  категория×компонент — только осторожно, риск неверных баллов).

## Среда / как продолжить

- Локальный Qdrant: `Start-Process 'C:\Program Files\Docker\Docker\Docker Desktop.exe'` → контейнер
  `qdrant` поднимется сам (restart-policy), ~1-2 мин. Проверка: `curl http://localhost:6533/collections`.
- Прогон движка: `.venv/Scripts/python.exe -m app.rag.pipeline "вопрос" [код]` (нужен Qdrant + DEEPSEEK_API_KEY в .env).
- Тесты: `.venv/Scripts/python.exe -m unittest discover -s tests` (79, офлайн). Eval: `scripts/eval_retrieval.py`.
- ⚠ Сеть в проект рваная (DPI); длинные коннекты к DeepSeek/SSH роняются — ретраи 3-5 попыток.
