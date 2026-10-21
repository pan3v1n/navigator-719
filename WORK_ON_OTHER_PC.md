# Работа над проектом на другом ПК

Два случая:
- **Переезд** — работа целиком переходит на другой ПК, прежний уходит из работы или становится
  запасным. Рецепт — ниже, со скриптом `scripts/pc_transfer.sh`.
- **Просто сесть за другой ПК** на день — хватит клона, `.env` и `git pull`, см.
  [«Короткий вариант»](#короткий-вариант-без-переезда).

Полная настройка окружения с нуля — [SETUP.md](SETUP.md).

**Проще всего — поручить переезд Claude Code на новом ПК:** инструкция для него —
[docs/NEW_PC_SETUP_CLAUDE.md](docs/NEW_PC_SETUP_CLAUDE.md). Её копия лежит в папке переноса
как `START_HERE_CLAUDE.md`, команда запуска — в её шапке. Ниже — те же шаги для человека.

---

## Что едет через git, а что нет

Код, документация, база знаний (`knowledge_base/`), тесты и профили выкатки — на GitHub.
Переезжают сами, через `git clone`. Всё остальное живёт только на диске:

| Что | Где | Как переносится |
|---|---|---|
| Секреты (`DEEPSEEK_API_KEY` и др.) | `.env` | скриптом |
| Модель эмбеддингов e5, 2.2 ГБ | `models/` | скриптом — из РФ HuggingFace рвётся |
| Граф знаний graphify | `graphify-out/` | скриптом — слой документации собирали LLM-агенты |
| Кэш эмбеддингов замеров | `.emb_cache/` | скриптом |
| Разрешения Claude Code для проекта | `.claude/settings.local.json` | скриптом |
| **Первоисточники** — RTF Контура (все редакции ПП 719), PDF Методрекомендаций и FAQ ГИСП, Приказ №52, Соглашение СНГ | корень репо | скриптом. Нужны для актуализации корпуса (например, редакция, вступающая 31.10.2026) |
| Git-хуки: автопуш и автообновление графа | `.git/hooks/` | скриптом, путь к Python правится |
| Память Claude Code о проекте | `~/.claude/projects/D--navigator-719/memory/` | скриптом |
| Настройки Claude Code, скилл graphify, статус-строка | `~/.claude/settings.json`, `~/.claude/skills/graphify/`, `~/.config/ccstatusline/` | скриптом |
| Коллекции Qdrant | Docker | **пересобираются** загрузчиками (шаг 6 ниже) |
| SSH-ключ к боевому серверу | `~/.ssh/` | **новый ключ** на новом ПК (шаг 7) |
| Выгрузки с боя с ПДн (152-ФЗ) | `D:\navigator-export\` | только если нужны: `--with-pii`, шифрованный носитель |
| История сессий Claude Code | `~/.claude/projects/…/*.jsonl` | только по `--with-transcripts`: в ней бывают фрагменты данных боя |
| `.venv`, `hf-cache/`, `scratchpad/`, `navigator_app.db` | репо | не нужны — пересоздаются |

Почему Qdrant не копируется: снапшот с локального Qdrant на NTFS-монтировании ломается
(WAL уходит в снапшот нулями, см. `scripts/eval_env.py`), а сырая папка `qdrant_storage/` в
репо — не обязательно та, с которой работает контейнер. Пересборка из репо надёжнее: коллекции
гарантированно соответствуют текущему корпусу.

---

## Переезд: на прежнем ПК

**1. Всё должно быть на GitHub.** Скрипт проверяет это сам и не соберёт перенос, если есть
незакоммиченные правки (в том числе в worktree), неотправленные коммиты или непустой stash.

**2. Сухой прогон** (Git Bash, из корня репо) — показывает состав и объём, ничего не копирует:
```bash
bash scripts/pc_transfer.sh pack E:/navigator-719-transfer --dry-run
```

**3. Сборка на носитель** (~2.6 ГБ, из них модель 2.2 ГБ):
```bash
bash scripts/pc_transfer.sh pack E:/navigator-719-transfer
# флаги: --no-models · --with-transcripts · --with-pii D:/navigator-export · --force
```
⚠ В папке переноса лежит `.env` с ключом DeepSeek, а с `--with-pii` — ещё и ПДн. Облако и
мессенджеры не годятся. Носитель лучше шифровать, но **BitLocker To Go шифрует флешку только в
Windows Pro/Enterprise**: в Home пункта «Включить BitLocker» нет (прежний ПК — Home, 21.10.2026).
Тогда — шифрованный архив 7-Zip с паролем, либо флешка, которая не выходит из рук, а после
переноса папку с неё удалить (и старые копии репо на ней — в них тот же `.env`). Флешка
потерялась — ключ DeepSeek перевыпустить на platform.deepseek.com. Без `--with-pii` других
секретов в переносе нет (проверено 21.10.2026). Целостность проверяется по `SHA256SUMS`.

Прежний ПК пока **не выводить из работы**: на шаге 7 с него добавляется SSH-ключ нового.

---

## Переезд: на новом ПК

**1. Установить:**

| Программа | Зачем | Проверка |
|---|---|---|
| Git for Windows (с Git Bash) | код, скрипт переноса | `git --version` |
| Python **3.12** (как в CI), с галочкой `py launcher` | сервис, тесты, graphify | `py -3 --version` |
| Docker Desktop | локальный Qdrant | `docker version` |
| GitHub CLI | PR, CI, доска задач | `gh --version` |
| Claude Code | работа над проектом | `claude --version` |
| Node.js — по желанию | статус-строка `ccstatusline` | `node --version` |

**2. Клонировать в тот же путь — `D:\navigator-719`:**
```bash
git clone https://github.com/pan3v1n/navigator-719.git D:/navigator-719
```
От пути зависит имя папки памяти Claude Code (`D--navigator-719`), путь к модели в `.env` и
корень графа. Если диск или путь другой, скрипт поправит пути сам, но история сессий
(`/resume`) привязана к прежнему пути.

**3. Поставить graphify в системный Python** — до распаковки, чтобы скрипт сразу прописал
хукам этот Python:
```bash
py -3 -m pip install graphifyy
```

**4. Распаковать перенос** (Git Bash, из корня клона):
```bash
cd D:/navigator-719
bash scripts/pc_transfer.sh unpack E:/navigator-719-transfer
```
Скрипт сверяет контрольные суммы до копирования. Затем раскладывает файлы и правит пути:
`.graphify_root`, `.graphify_python`, Python в хуках, `EMBEDDING_MODEL` в `.env`.
То, что уже лежало на месте (например, `.env`, сделанный из `.env.example`), он не затирает:
файлы из репо уходят в соседнюю папку `D:\navigator-719.bak-<время>` (вне репо — чтобы
секрет не попал в коммит), а файлы из `~/.claude` — в `*.bak-<время>` рядом с ними.
Если в клоне скрипта ещё нет: `bash E:/navigator-719-transfer/pc_transfer.sh unpack E:/navigator-719-transfer`.

**5. Окружение Python:**
```powershell
py -3 -m venv .venv
.venv\Scripts\pip install --only-binary :all: -r requirements.txt
```

**6. Qdrant и пересборка коллекций.** Хранилище — именованный том Docker, не папка на NTFS.
Версия закреплена, как в `scripts/eval_env.py`:
```powershell
docker run -d --name qdrant --restart unless-stopped -p 6333:6333 -v qdrant_data:/qdrant/storage qdrant/qdrant:v1.18.2
.venv\Scripts\python scripts\load_kb.py         # товарный корпус  → pp719
.venv\Scripts\python scripts\load_rules_kb.py   # процедурный      → pp719_rules
.venv\Scripts\python scripts\seed_cases.py      # кейсы экспертов  → verified_cases
.venv\Scripts\python scripts\seed_users.py      # локальные учётки; пароли печатает в консоль
```
Каждая загрузка идёт десятки минут на CPU. Проверка — число точек: на 15.10.2026 было
`pp719` **1534**, `pp719_rules` **761**, `verified_cases` **12**. Если корпус с тех пор менялся,
ориентир — профиль последнего релиза в `scripts/deploy/releases/`.
```bash
for c in pp719 pp719_rules verified_cases; do curl -s localhost:6333/collections/$c | grep -o '"points_count":[0-9]*'; done
```

**7. SSH к боевому серверу — новый ключ.** На сервер пускают только по ключу.
```bash
ssh-keygen -t ed25519 -C "main-pc"          # на НОВОМ ПК, ~/.ssh/id_ed25519
```
Файл `~/.ssh/id_ed25519.pub` (публичный, не секрет) перенести на прежний ПК и оттуда добавить:
```bash
cat main-pc.pub | ssh dan_tpp_srv@77.241.30.132 "cat >> ~/.ssh/authorized_keys"
```
Первый вход с нового ПК: ssh спросит отпечаток сервера. Он должен быть
`SHA256:SWnSbRBwVtpZST18NLvOaXU+gAObHnYja4E1MMT+cjM` — иначе не соглашаться.
```bash
ssh dan_tpp_srv@77.241.30.132 'echo ok'
```
⚠ Канал до сервера режет SSH по частоте подключений (DPI): подключения объединять, не
переподключаться в цикле. Подробности — `CLAUDE.md`, раздел «Бой».

**8. Входы:**
```bash
gh auth login                    # GitHub: PR, CI, доска
npm install -g ccstatusline      # если переносили статус-строку
claude                           # затем /login
```

**9. Проверить:**
- батарея — **без** `PYTHONUTF8=1`, иначе она зеленеет там, где документированная среда
  падает (#107):
  ```powershell
  .venv\Scripts\python -m unittest discover -s tests
  ```
  Ожидается `OK` (число тестов — в `CLAUDE.md`, дерево репозитория).
- граф: в `graphify-out/.graphify_root` — Windows-путь (`D:/navigator-719`), не `/d/navigator-719`.
  После первого коммита `tail -3 ~/.cache/graphify-rebuild.log` пишет `Rebuilt: N nodes`.
- бой отвечает: `curl -s https://xn--b1afk4ade.xn--719--83dani8b8bqyy.xn--p1ai/ping` → поле `release`.
- Claude Code видит память: спросить в новой сессии «что ты помнишь о проекте».
- ПДн, если переносили: из `pii/` переложить в `D:\navigator-export\` — **вне** репозитория.

**10. Первая сессия Claude Code на новом ПК** — попросить перепроверить заметку памяти об
окружении (`navigator-719-environment`). Она записана на прежнем ПК: пути Python, `gh` вне
PATH, сломанный PowerShell-инструмент, перехват сети zapret — на новом ПК всё это может быть
иначе.

---

## Вывести прежний ПК из работы

Только после того, как новый ПК прошёл шаг 9 и вошёл на сервер своим ключом:
1. Убрать ключ прежнего ПК из `~/.ssh/authorized_keys` на сервере. Делать с нового ПК и не
   закрывать при этом уже открытую сессию: ошибка в файле иначе запрёт снаружи.
2. На прежнем ПК: удалить `.env`, папку переноса, `D:\navigator-export\`; `gh auth logout`;
   в Claude Code — `/logout`.
3. Если прежний ПК остаётся запасным — перед любой работой на нём `git fetch` и сверка с
   `origin/dev`. Коммиты с двух машин уже приводили к тому, что документация отставала от боя.

---

## Каждый раз, когда садитесь за работу

```bash
git fetch && git status -sb       # локальный dev совпадает с origin/dev?
curl -s https://xn--b1afk4ade.xn--719--83dani8b8bqyy.xn--p1ai/ping   # какой релиз на бою
```
Коммит сам пушится хуком автопуша (ветки `exp/*` — нет). Перед `git add -A` — всегда
`git status`: выгрузки с ПДн в репозиторий попадать не должны ни при каких условиях.

---

## Короткий вариант (без переезда)

```powershell
git clone https://github.com/pan3v1n/navigator-719.git D:\navigator-719
cd D:\navigator-719
py -3 -m venv .venv
.venv\Scripts\pip install --only-binary :all: -r requirements.txt
copy .env.example .env            # вписать DEEPSEEK_API_KEY
claude
```
Этого хватает, чтобы править код и гонять батарею (она офлайновая). Чтобы запустить сам
сервис, нужны Qdrant и модель — шаги 6 и SETUP.md (Шаг 5). Автопуш-хук на таком ПК ставится
вручную: текст — в [docs/BRANCHING.md](docs/BRANCHING.md).

---

## Если что-то пошло не так

- **`unpack`: «суммы не сошлись»** — папка повредилась при копировании, ничего не скопировано.
  Перенести заново.
- **Хук graphify пишет «No code files found»** — в `.graphify_root` POSIX-путь. Запустить
  `bash scripts/pc_transfer.sh fix-paths` (он же нужен после поздней установки `graphifyy`).
- **Индексация полезла качать модель с HuggingFace** — в `.env` не тот `EMBEDDING_MODEL`:
  `fix-paths` или вручную `EMBEDDING_MODEL=D:/navigator-719/models/multilingual-e5-large`.
- **Claude Code не видит контекст** — запускать `claude` из корня репо (где `CLAUDE.md`).
- **`git pull` ругается на конфликты** — значит, работа шла на двух ПК сразу. Попросить Claude
  Code помочь разрешить конфликт; на будущее — правило «Каждый раз…» выше.
