# Навигатор ПП РФ №719 — Проектная база для Claude Code

## Контекст проекта
Сервис для Курской торгово-промышленной палаты (ТПП).
Помогает предприятиям разобраться в требованиях **Постановления Правительства РФ №719**
о подтверждении производства промышленной продукции на территории РФ (критерии локализации,
балльная система, документальное подтверждение).

**Заказчик**: Курская ТПП  
**Цель**: снизить нагрузку на экспертов ТПП, ускорить прохождение процедуры для предприятий  
**Ограничение**: ИИ — черновик, финальный вердикт всегда за экспертом ТПП

---

## Стек (только RF-доступные инструменты)

| Слой | Технология | Причина выбора |
|---|---|---|
| LLM | DeepSeek V3 (`deepseek-chat`) | API доступен из РФ, дёшево, качественно |
| Embeddings | `sentence-transformers` (локально) | Без внешних API, бесплатно |
| Vector DB | Qdrant (self-hosted Docker) | Open source, РФ-доступен, production-ready |
| RAG | LlamaIndex | Python, open source, гибкий |
| Backend | FastAPI (Python 3.11+) | Стандарт, async, хорошая документация |
| База данных | SQLite (dev) → PostgreSQL (prod) | Простой старт, масштабируется |
| Frontend MVP | Streamlit | Быстро, Python, без фронтенд-разработчика |
| Frontend Prod | React + Vite (V3+) | Только если MVP подтвердит ценность |
| Хостинг | Selectel / Timeweb / Yandex Cloud | РФ-юрисдикция, ФЗ-152 |
| Telegram UI | aiogram 3 (опционально) | Быстрый доступ для экспертов ТПП |

### Запуск Python
```bash
py -3 -m venv .venv
# Windows:
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py
# Linux/Mac:
source .venv/bin/activate && pip install -r requirements.txt && python main.py
```

---

## Секреты (никогда не коммитить)
Файл `.env` в `.gitignore`. Копировать из `.env.example`.
- `DEEPSEEK_API_KEY` — от platform.deepseek.com
- `DATABASE_URL` — строка подключения к БД
- `QDRANT_URL` — адрес Qdrant (локально: http://localhost:6333)
- `TELEGRAM_BOT_TOKEN` — если используется Telegram-интерфейс

---

## Структура репозитория

```
719-post/
├── CLAUDE.md              ← ты здесь (главный контекст проекта)
├── ROADMAP.md             ← фазы разработки и статус
├── .env.example           ← шаблон переменных окружения
├── .gitignore
├── requirements.txt       ← зависимости Python
├── main.py                ← точка запуска (FastAPI или Streamlit)
│
├── app/
│   ├── core/
│   │   ├── config.py      ← настройки из .env (pydantic-settings)
│   │   └── prompts.py     ← системные промпты для LLM
│   ├── rag/
│   │   ├── loader.py      ← загрузка документов в Qdrant
│   │   ├── retriever.py   ← поиск по базе знаний
│   │   └── pipeline.py    ← RAG-пайплайн (запрос → контекст → ответ)
│   ├── tools/
│   │   ├── navigator.py   ← определение применимой группы товаров по 719
│   │   ├── checklist.py   ← генерация чек-листа по группе
│   │   ├── validator.py   ← проверка документов (V2)
│   │   └── act_gen.py     ← генератор черновиков актов (V3)
│   ├── api/
│   │   ├── routes.py      ← FastAPI роутеры
│   │   └── schemas.py     ← Pydantic схемы запросов/ответов
│   └── db/
│       ├── models.py      ← SQLAlchemy модели
│       ├── engine.py      ← подключение к БД
│       └── queries.py     ← CRUD операции
│
├── knowledge_base/
│   ├── pp719/             ← PDF и TXT файлы ПП №719 + приложения
│   ├── cases/             ← верифицированные кейсы от эксперта ТПП
│   └── templates/         ← шаблоны актов экспертизы ТПП
│
├── frontend/              ← Streamlit UI (MVP)
│   └── app.py
│
├── scripts/
│   ├── load_kb.py         ← скрипт: загрузить knowledge_base в Qdrant
│   └── seed_cases.py      ← скрипт: загрузить кейсы экспертов
│
├── tests/
│   ├── test_rag.py
│   └── test_tools.py
│
└── docs/
    └── expert_guide.md    ← инструкция для эксперта ТПП
```

---

## Ключевые принципы разработки

1. **AI — черновик, эксперт — вердикт.** Каждый ответ системы содержит пометку,
   что это предварительный анализ, требующий проверки уполномоченным экспертом ТПП.

2. **Петля обучения.** Ошибки, которые находит эксперт, фиксируются в `knowledge_base/cases/`
   и переиндексируются — точность растёт без дообучения модели.

3. **RF-first стек.** Никаких зависимостей от сервисов, заблокированных или недоступных в РФ.
   Все внешние API — только DeepSeek (работает из РФ напрямую).

4. **Portable dev environment.** Весь код в GitHub. CLAUDE.md содержит полный контекст.
   Любой Claude Code на любом ПК с доступом к репо может продолжить работу.

---

## Текущий статус
**V1-MVP функционально готов** (код пунктов 1.0–1.5): база в Qdrant (1602 позиции),
гибрид-поиск + буст по ОКПД2, навигатор, `POST /navigate`, Streamlit UI, петля кейсов
(`verified_cases`). Остался приёмочный тест с экспертом ТПП (критерий ≥70%) — не код.
Запуск: [docs/LAUNCH.md](docs/LAUNCH.md). Детальный статус по пунктам — в **ROADMAP.md**.

---

## Как продолжить работу на новом устройстве
```bash
git clone https://github.com/panev1n/navigator-719.git
cd navigator-719
py -3 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
# Заполнить .env секретами
# Запустить Qdrant: docker run -p 6333:6333 qdrant/qdrant
.venv\Scripts\python scripts/load_kb.py   # загрузить базу знаний
.venv\Scripts\python main.py              # запустить сервис
```
