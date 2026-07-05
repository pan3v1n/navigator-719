# Запуск Навигатора ПП №719 (0.5.0 · движок + веб-чат мини-1.0)

Краткая инструкция: от свежего клона до работающего интерфейса. Подробная настройка
окружения с нуля — в [SETUP.md](../SETUP.md); протокол теста с экспертом — в
[TESTING.md](TESTING.md).

---

## Что нужно

- **Python 3.12** (на этом ПК интерпретатор: `C:\Users\<user>\AppData\Local\Programs\Python\Python312\python.exe`; лаунчер `py` может отсутствовать).
- **Docker Desktop** — для Qdrant.
- **Ключ DeepSeek** (`platform.deepseek.com` → API Keys) — для генерации ответов.

---

## Первичная настройка (один раз)

```powershell
git clone https://github.com/pan3v1n/navigator-719.git
cd navigator-719
py -3 -m venv .venv                       # или полным путём к python.exe -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env                     # затем вписать DEEPSEEK_API_KEY
```

Структурированная база (`knowledge_base/pp719/structured/*.json`) уже в репозитории —
заново парсить через DeepSeek не нужно.

---

## Поднять Qdrant

1. Запустить **Docker Desktop**, дождаться старта демона (контейнер Qdrant поднимется сам).
2. Если контейнера ещё нет — создать (данные хранятся в `D:/qdrant_storage`):
   ```powershell
   docker run -d --name qdrant --restart unless-stopped -p 6333:6333 -v D:/qdrant_storage:/qdrant/storage qdrant/qdrant
   ```
3. Проверка:
   ```
   curl http://localhost:6333/healthz        → healthz check passed
   ```

---

## Разовая индексация в Qdrant

Коллекции хранятся постоянно — повторять только при смене схемы/данных.

```powershell
.venv\Scripts\python scripts\load_kb.py        # база знаний → коллекция pp719 (~1379 точек)
.venv\Scripts\python scripts\seed_cases.py     # кейсы эксперта → verified_cases (на старте пусто)
```
Первый запуск `load_kb.py` считает эмбеддинги e5 на CPU (~15–25 мин). Повторные —
быстрые за счёт кэша в `.emb_cache/` (пересчитываются только изменённые чанки).
Перед этим разово скачивается сама модель e5 (~2 ГБ); при обрывах на нестабильной
сети / из РФ — см. [SETUP.md](../SETUP.md), Шаг 5 (`hf_xet` / зеркало / ручная папка).

---

## Запуск интерфейса

### Вариант A — Веб-приложение мини-1.0 (главный: чат + роли + админка)

```powershell
.venv\Scripts\python scripts\seed_users.py --admin-password admin   # разово: создать логины
.venv\Scripts\python main.py                                        # uvicorn на http://localhost:8000
```
Открыть `http://localhost:8000` → вход (по умолчанию `admin` / `admin`). Дальше — окно чата
(история диалогов, экспорт, источники), для роли `admin` — `/admin` (логи диалогов, дашборд
KPI: токены/₽/активность, фидбек). Роли: `user` (чат) · `expert` (+фидбек) · `admin` (+логи/дашборд).

### Вариант B — Streamlit-форма (ранний MVP, вытеснен веб-чатом)

```powershell
.venv\Scripts\python -m streamlit run frontend\streamlit_app.py
```
Откроется `http://localhost:8501`: поле ввода продукции + код ОКПД2 → анализ, источники, чек-лист.

### Вариант C — API `POST /navigate` (быстрый смоук)

```powershell
curl http://localhost:8000/ping
curl -X POST http://localhost:8000/navigate -H "Content-Type: application/json" ^
     -d "{\"query\":\"производим прицепы для легковых авто\",\"okpd2\":\"29.20.23\"}"
```
Интерактивная документация: `http://localhost:8000/docs`.

### Вариант D — поиск без LLM/UI (быстрый смоук пайплайна)

```powershell
.venv\Scripts\python -m app.rag.pipeline "выпускаем промышленные чиллеры" 28.25.13
```

---

## Ежедневный быстрый запуск (после первичной настройки)

1. Запустить Docker Desktop (Qdrant поднимется сам) → `curl http://localhost:6333/healthz`.
2. `.venv\Scripts\python main.py` → `http://localhost:8000` (вход). Streamlit-форму — при
   необходимости, Вариант B.

---

## Если что-то не работает

| Симптом | Причина / решение |
|---|---|
| `Could not connect to ...:6333` | Не запущен Docker Desktop или контейнер Qdrant. Запустить Docker; `docker start qdrant`. |
| Ошибка про `DEEPSEEK_API_KEY` | Не заполнен `.env`. Вписать ключ с platform.deepseek.com. |
| `Коллекция ... точек = 0` в поиске | Не выполнена `load_kb.py`. Запустить индексацию. |
| Очень долгий первый запуск | Это эмбеддинг e5 на CPU (разово). Повторные — из кэша. |
| Скачивание модели e5 рвётся (`IncompleteRead`, `Timeout`) | Нестабильная сеть / РФ. `hf_xet` уже в зависимостях (чанковая докачка) — повторить запуск; либо `HF_ENDPOINT=https://hf-mirror.com` в `.env`; либо скачать модель вручную в `models/` и указать `EMBEDDING_MODEL` (см. [SETUP.md](../SETUP.md), Шаг 5). |
| Кракозябры в консоли | Кодировка cp1251. Данные корректны; для читаемого вывода: `set PYTHONUTF8=1`. |
| Streamlit не открылся | Проверить Qdrant и `.env`; ошибки видны в окне Streamlit и в терминале. |

---

> ⚠️ Ответы системы — **предварительный анализ**, не заключение. Окончательный вердикт —
> за уполномоченным экспертом Курской ТПП.
