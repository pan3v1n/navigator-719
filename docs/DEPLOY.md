# Деплой Навигатора ПП №719 на Yandex Cloud (демо)

Одна VM, `docker-compose`: контейнер приложения (FastAPI + e5) + контейнер Qdrant + тома для
модели и данных. Рассчитано на короткое демо на гранте Yandex (почасовой биллинг → после демо
VM удаляем).

> **Безопасность.** Эти шаги НЕ трогают локально работающий продукт: облако — отдельная машина
> со своим Qdrant/БД/`.env`. Если что-то не взлетит — демо идёт на `localhost` (см. docs/LAUNCH.md,
> Вариант A) или через туннель. VM одноразовая: `yc compute instance delete` — и всё исчезло.

Артефакты: [`Dockerfile`](../Dockerfile), [`docker-compose.yml`](../docker-compose.yml),
[`.dockerignore`](../.dockerignore).

---

## 0. Что понадобится
- Аккаунт Yandex Cloud с активным грантом (есть).
- SSH-ключ. Если нет: `ssh-keygen -t ed25519` (Enter на все вопросы) → публичный в `~/.ssh/id_ed25519.pub`.
- Ключ DeepSeek (тот же, что локально).

---

## 1. Установить и авторизовать yc CLI (делаешь ты — вход в браузере)
Windows PowerShell:
```powershell
iex (New-Object System.Net.WebClient).DownloadString('https://storage.yandexcloud.net/yandexcloud-yc/install.ps1')
# перезапусти терминал, затем:
yc init      # откроет браузер → выбрать аккаунт, облако (cloud), каталог (folder), зону ru-central1-a
```
Проверка: `yc config list` показывает `token`, `cloud-id`, `folder-id`.

---

## 2. Сеть (если в каталоге ещё нет сети/подсети)
```powershell
yc vpc network list        # если пусто — создать:
yc vpc network create --name default
yc vpc subnet create --name default-a --zone ru-central1-a --network-name default --range 10.0.0.0/24
```

## 3. Создать VM (Ubuntu 24.04, 2 vCPU / 8 ГБ)
```powershell
yc compute instance create `
  --name navigator-719 `
  --zone ru-central1-a `
  --cores 2 --memory 8GB --core-fraction 100 `
  --create-boot-disk image-folder-id=standard-images,image-family=ubuntu-2204-lts,size=30,type=network-ssd `
  --network-interface subnet-name=default-a,nat-ip-version=ipv4 `
  --ssh-key $HOME\.ssh\id_ed25519.pub
```
Записать **публичный IP** из вывода (поле `one_to_one_nat address`). Далее — `VM_IP`.

> Если CLI-сеть капризничает — VM проще создать в консоли `console.yandex.cloud`
> (Compute Cloud → Создать ВМ: Ubuntu 24.04, 2 vCPU/8 ГБ, публичный IP, вставить SSH-ключ).
> Для 5-дневного теста сразу **зарезервируй статический публичный IP** (иначе при stop/start
> адрес сменится и ссылка у экспертов протухнет).

## 4. Открыть порты 22 и 80
В security group подсети/VM (консоль → Virtual Private Cloud → Security groups, или дефолтная SG)
разрешить входящие: TCP **22** (SSH) и TCP **80** (демо). Без правила на 80 сайт не откроется.

---

## 5. Поставить Docker на VM
```powershell
ssh yc-user@VM_IP
```
На VM:
```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker      # чтобы docker без sudo
docker version && docker compose version            # проверка
```

## 6. Доставить код и создать .env
Репозиторий **приватный** → код везём снимком ветки с ЛОКАЛЬНОЙ машины (`git clone` без токена не
сработает). С локального ПК из корня репо:
```bash
git archive dev | ssh yc-user@VM_IP "mkdir -p navigator-719 && tar -x -C navigator-719"
```
Затем на VM:
```bash
cd navigator-719
cp .env.example .env
nano .env
```
В `.env` задать (минимум):
```
DEEPSEEK_API_KEY=sk-...             # твой ключ
SESSION_SECRET=<длинная-случайная>  # ОБЯЗАТЕЛЬНО (см. ниже)
APP_VERSION=0.5.0
# QDRANT_URL/APP_DB_URL/HF_HOME/APP_ENV НЕ трогать — их задаёт docker-compose
```
> ⚠️ **SESSION_SECRET обязателен.** compose ставит `APP_ENV=production`, а стартап-гард приложения
> **не даст запуститься** с публичным дефолт-секретом (иначе можно подделать admin-cookie). Сгенерировать:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

## 7. Первый запуск (индексация — разово)
```bash
docker compose build                                   # соберёт образ (torch+e5 deps, ~5–10 мин)
docker compose up -d qdrant                            # поднять Qdrant
docker compose run --rm app python scripts/load_kb.py  # скачает e5 (~2 ГБ) + проиндексирует 1379 точек
docker compose run --rm app python scripts/seed_users.py --admin-password ВАШ_ПАРОЛЬ
docker compose up -d app                               # поднять приложение
docker compose ps                                      # app и qdrant в статусе Up/healthy
```

## 8. Проверить
- В браузере: `http://VM_IP/` → страница входа → `admin` / `ВАШ_ПАРОЛЬ`.
- `curl http://VM_IP/ping` → `{"status":"ok",...,"version":"0.5.0"}`.
- Прогнать демо-запросы из [LAUNCH.md](LAUNCH.md) / раннбука.

---

## 9. Бэкап данных теста + пауза биллинга

**Бэкап БД (ежедневно).** Фидбек/оценки/исправления/логи диалогов = результат теста. Тома
переживают редеплой (`up --build`/`down`), но `instance delete` их стирает — снимайте копию off-VM:
```bash
# на VM: консистентный дамп SQLite из тома → вынести на хост
docker compose exec -T app python -c "import sqlite3; c=sqlite3.connect('/data/navigator_app.db'); b=sqlite3.connect('/tmp/backup.db'); c.backup(b); b.close()"
docker compose cp app:/tmp/backup.db ./navigator_backup.db
# с локального ПК: забрать к себе
scp yc-user@VM_IP:navigator-719/navigator_backup.db ./navigator_backup-$(date +%F).db
```

**Автоматизация (cron на VM).** Скрипт `scripts/backup_db.py` делает консистентную копию с
ротацией в том `/data/backups` (переживает редеплой; `.backup` без остановки сервиса). Поставить
ежедневный дамп в 03:00, хранить 30 копий:
```bash
( sudo crontab -l 2>/dev/null; \
  echo '0 3 * * * cd /home/yc-user/navigator-719 && docker compose exec -T app python scripts/backup_db.py --dir /data/backups --keep 30' \
) | sudo crontab -
```
Off-VM (том стирается при `instance delete`) — периодически с ЛОКАЛЬНОГО ПК забирать папку бэкапов:
```bash
ssh yc-user@VM_IP 'cd navigator-719 && sudo docker compose cp app:/data/backups ./backups-vm'
scp -r yc-user@VM_IP:navigator-719/backups-vm ./navigator-backups
```
Перед финальным `instance delete` — обязательно сделать этот вынос.

**Пауза биллинга (на ночь / между днями теста):** останавливать VM **в консоли**
`console.yandex.cloud` («Остановить»). Гостевой `sudo poweroff` биллинг НЕ гасит. Данные (тома)
переживают stop/start; при отсутствии статического IP адрес сменится (см. шаг 3).

**Финальный teardown (после теста — СНАЧАЛА бэкап!):** удалить инстанс в консоли (реальное имя,
не `navigator-719`). Тома удаляются вместе с VM; локальный продукт полностью цел.

---

## Если что-то не так
| Симптом | Решение |
|---|---|
| `load_kb` рвётся на скачивании e5 (`IncompleteRead`/`Timeout`) | В `.env` добавить `HF_ENDPOINT=https://hf-mirror.com`, повторить `docker compose run --rm app python scripts/load_kb.py`. `hf_xet` (чанковая докачка) уже в образе. |
| Сайт не открывается по `http://VM_IP` | Не открыт порт 80 в security group; либо `docker compose ps` показывает app не Up — смотреть `docker compose logs app`. |
| app падает с ошибкой Qdrant | Qdrant не поднят/не проиндексирован: `docker compose up -d qdrant`, затем `load_kb`. |
| «Сервис временно недоступен» в чате | Нет/неверный `DEEPSEEK_API_KEY` в `.env` или нет исходящей сети к api.deepseek.com. `docker compose logs app`. |
| Долгий первый ответ | Первый запрос грузит e5 в память (разово). Дальше быстро. |

> ⚠️ Коллекция Qdrant в git не хранится — на VM она наполняется `load_kb.py` (шаг 7). При
> пересоздании VM индексацию повторить.
