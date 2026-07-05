# Инструкция по развёртыванию на новом устройстве

Этот файл описывает полный процесс настройки проекта на любом ПК с нуля.

---

## Требования

- Python 3.12 (рекомендуется; 3.11+ тоже работает) (`py -3 --version`)
- Git (`git --version`)
- Доступ к интернету (для установки пакетов и DeepSeek API)
- Docker — для Qdrant (опционально, можно заменить локальным бинарником)

---

## Шаг 1 — Клонировать репозиторий

```powershell
git clone https://github.com/pan3v1n/navigator-719.git
cd navigator-719
```

---

## Шаг 2 — Создать виртуальное окружение и установить зависимости

**Windows:**
```powershell
py -3 -m venv .venv
.venv\Scripts\pip install --only-binary :all: -r requirements.txt
```

**Linux / macOS:**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> На Windows используйте флаг `--only-binary :all:` — Python 3.11+ может не иметь C++ Build Tools,
> и часть пакетов не соберётся из исходников без него.

---

## Шаг 3 — Заполнить секреты

```powershell
copy .env.example .env      # Windows
# cp .env.example .env      # Linux/macOS
```

Откройте `.env` и заполните:

```env
DEEPSEEK_API_KEY=sk-...       # platform.deepseek.com → API Keys
QDRANT_URL=http://localhost:6333
```

Остальные поля можно оставить по умолчанию для локальной разработки.
Модель эмбеддингов (`EMBEDDING_MODEL`) — см. **Шаг 5** (важно при нестабильной сети / из РФ).

---

## Шаг 4 — Запустить Qdrant (векторная база)

**Через Docker (рекомендуется):**
```powershell
docker run -d --name qdrant -p 6333:6333 qdrant/qdrant
```

**Без Docker — скачать бинарник:**  
[github.com/qdrant/qdrant/releases](https://github.com/qdrant/qdrant/releases) → скачать `qdrant-x86_64-pc-windows-msvc.zip`, распаковать, запустить `qdrant.exe`.

Проверить что Qdrant работает: открыть [http://localhost:6333/dashboard](http://localhost:6333/dashboard)

---

## Шаг 5 — Модель эмбеддингов e5 и индексация базы знаний

Гибридный поиск использует локальную модель **multilingual-e5-large** (~2 ГБ). По умолчанию
она скачивается с HuggingFace при первом запуске индексации.

```powershell
.venv\Scripts\python scripts\load_kb.py        # JSON → Qdrant, ~1379 точек (разово, ~15–25 мин на CPU)
.venv\Scripts\python scripts\seed_cases.py     # кейсы эксперта → verified_cases (на старте пусто)
```

**Если скачивание модели рвётся (нестабильная сеть / из РФ)** — три способа:

1. **hf_xet** (уже в `requirements.txt`) — чанковая докачка, устойчива к обрывам. Обычно
   достаточно просто повторить запуск.
2. **Зеркало** — добавить в `.env`: `HF_ENDPOINT=https://hf-mirror.com`.
3. **Скачать вручную** в папку `models/` (gitignored) и указать абсолютный путь в `.env`:
   ```env
   EMBEDDING_MODEL=D:/navigator-719/models/multilingual-e5-large
   ```
   Нужны файлы: `model.safetensors`, `tokenizer.json`, `sentencepiece.bpe.model`,
   `config.json`, `tokenizer_config.json`, `special_tokens_map.json`, `modules.json`,
   `sentence_bert_config.json` и `1_Pooling/config.json`
   (источник: `huggingface.co/intfloat/multilingual-e5-large` или зеркало `hf-mirror.com`).

Коллекции Qdrant хранятся постоянно — индексацию повторять только при смене данных/схемы.

---

## Шаг 6 — Восстановить исходный RTF-файл ПП №719

RTF-файл не хранится в репозитории (слишком большой, исключён в `.gitignore`).  
Текст уже распарсен и лежит в `knowledge_base/pp719/` — **этот шаг нужен только если хотите перепарсить**.

Если нужно: положите RTF-файл постановления в корень проекта с именем:
```
Постановление Правительства РФ от 17.07.2015 N 719 О подтверждении производства Российской.rtf
```
Затем запустите:
```powershell
.venv\Scripts\python scripts\parse_rtf.py
```

---

## Шаг 7 — Проверить что сервис запускается

```powershell
.venv\Scripts\python main.py
```

Открыть в браузере: [http://localhost:8000/ping](http://localhost:8000/ping)  
Ожидаемый ответ: `{"status":"ok","app":"Навигатор ПП РФ №719","version":"0.5.0"}`

---

## Рабочий процесс (ежедневно)

### Начало сессии — получить актуальный код с GitHub:
```powershell
git pull
```

### Конец сессии — отправить изменения на GitHub:
```powershell
git add .
git commit -m "описание что сделано"
git push
```

### Автопуш после каждого коммита (настраивается один раз на каждом ПК):

Хук `post-commit` пушит **текущую ветку** (`git push origin HEAD`) и пропускает
экспериментальные `exp/*`. Канонический текст хука, модель веток и важное предупреждение
«не затирать блок graphify при переустановке» — в **[docs/BRANCHING.md](docs/BRANCHING.md)**.

> ⚠️ Старый вариант `git push origin main` устарел: на ветке `dev` он бы не пушил твои
> изменения. Используй версию из BRANCHING.md (push `HEAD`).

---

## Работа с Claude Code

Если на устройстве установлен Claude Code — он автоматически прочитает `CLAUDE.md`
и получит полный контекст проекта. Можно продолжать работу с любой задачи из `ROADMAP.md`.

```powershell
# Запустить Claude Code в папке проекта
claude
```

---

## Частые проблемы

**`pip install` падает с ошибкой компиляции:**  
Добавьте флаг `--only-binary :all:` к команде установки.

**Qdrant недоступен (`Connection refused`):**  
Убедитесь что контейнер запущен: `docker ps | grep qdrant`  
Или запустите снова: `docker start qdrant`

**`DEEPSEEK_API_KEY` не найден:**  
Проверьте что файл `.env` существует и заполнен (не `.env.example`).

**Кириллица отображается кракозябрами в терминале Windows:**  
Это визуальная проблема терминала, на работу кода не влияет.  
Если мешает: `$env:PYTHONIOENCODING="utf-8"` перед запуском.
