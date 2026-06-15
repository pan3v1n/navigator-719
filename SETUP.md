# Инструкция по развёртыванию на новом устройстве

Этот файл описывает полный процесс настройки проекта на любом ПК с нуля.

---

## Требования

- Python 3.11 или новее (`py -3 --version`)
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

## Шаг 5 — Восстановить исходный RTF-файл ПП №719

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

## Шаг 6 — Проверить что сервис запускается

```powershell
.venv\Scripts\python main.py
```

Открыть в браузере: [http://localhost:8000/ping](http://localhost:8000/ping)  
Ожидаемый ответ: `{"status":"ok","version":"0.1.0"}`

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

**Windows PowerShell:**
```powershell
Set-Content .git\hooks\post-commit "#!/bin/sh`ngit push origin main"
```

**Linux / macOS:**
```bash
echo '#!/bin/sh
git push origin main' > .git/hooks/post-commit
chmod +x .git/hooks/post-commit
```

После этого `git push` будет выполняться автоматически при каждом `git commit`.

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
