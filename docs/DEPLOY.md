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

## 4. Открыть порты 22, 80 и 443
В security group подсети/VM (консоль → Virtual Private Cloud → Security groups, или дефолтная SG)
разрешить входящие: TCP **22** (SSH), TCP **80** (http → редирект на https и проверка ACME) и
TCP **443** (HTTPS). Без 80 Let's Encrypt не выпустит сертификат, без 443 сайт не откроется.

## 4а. Домен и HTTPS (с 22.09.2026)
Публичный адрес VM **статический** (сделан в консоли: VPC → IP-адреса → «Сделать статическим»),
домен **`719-навигатор.рф`** (Timeweb). Снаружи слушает только контейнер **caddy**:

| Имя | Что отдаёт |
|---|---|
| `719-навигатор.рф` | лендинг `landing/index.html`, кнопка «Открыть сервис» → `сервис.` |
| `сервис.719-навигатор.рф` | приложение (reverse proxy на контейнер `app:8000`); punycode `xn--b1afk4ade.xn--719--83dani8b8bqyy.xn--p1ai` |

1. В DNS-панели регистратора две записи типа **A** на IP VM: хост пустой (корень) и хост `сервис`.
   ⚠ У свежекупленного домена корень указывает на **парковку Timeweb** (`92.53.96.169`) — эту
   запись заменить на IP VM, иначе лендинг не откроется.
2. В `.env` на VM: `PUBLIC_DOMAIN=xn--719--83dani8b8bqyy.xn--p1ai` (⚠ **punycode**, не кириллица;
   посчитать: `python3 -c "print('719-навигатор.рф'.encode('idna').decode())"`) и
   `COOKIE_SECURE=true` (куки только по https). `APP_SUBDOMAIN=xn--b1afk4ade` (punycode метки «сервис») — дефолт, менять не нужно.
3. `docker compose up -d app caddy`. Caddy сам выпустит сертификаты Let's Encrypt для обоих имён
   (запасной ЦС — ZeroSSL) и будет продлевать их; ключи — в томе `caddy_data`. **SSL у
   регистратора не покупать.** Первый выпуск занимает до минуты после того, как DNS разъехался
   (проверить: `dig +short xn--b1afk4ade.xn--719--83dani8b8bqyy.xn--p1ai`).
4. Проверка: `curl -sI https://xn--b1afk4ade.xn--719--83dani8b8bqyy.xn--p1ai/login | head -1` → `200`;
   `https://719-навигатор.рф` открывает лендинг. Голый `http://IP` теперь не обслуживается —
   ссылки экспертам давать только доменные.

Приложение публикует порт **только на петле VM** (`127.0.0.1:8000`) — для скрипта выкатки и
ручных проверок с самой машины (`curl http://127.0.0.1:8000/ping`). uvicorn запущен с
`--proxy-headers`, поэтому троттлинг входа видит реальный IP клиента, а не адрес caddy.

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
**Копия проверяется сразу при снятии (R13).** `backup_db.py` после `.backup` прогоняет
`PRAGMA integrity_check` и считает строки в `users/messages/feedback`. Битая копия **удаляется**, а
скрипт выходит с кодом 1 — cron об этом сообщит. Проверяется и «успешно снятая» пустая база
(типичный результат копирования не того пути): целостность у неё `ok`, поэтому одной PRAGMA мало.

Проверить любой готовый файл (в том числе увезённый off-VM):
```bash
python scripts/backup_db.py --verify /path/to/navigator_app-2026-08-10_0300.db
# [OK] …: целостность ok, users 28, messages 1080, feedback 274      → код 0
# [ОШИБКА] …: не читается как база SQLite: file is not a database    → код 1
```

### Off-VM: вынос за пределы виртуалки — ОБЯЗАТЕЛЬНО

⚠️ Том `/data` **стирается вместе с `instance delete`**. Данные теста (оценки экспертов,
исправления, диалоги 17 региональных ТПП) — главный актив проекта, второй попытки их собрать не
будет. Бэкап, лежащий только внутри VM, от потери инстанса не защищает.

**Вариант А (рекомендуется) — по расписанию с ЛОКАЛЬНОГО ПК**, тогда VM ничего не знает о внешнем
хранилище и её компрометация не даёт доступа к архиву:
```bash
# забрать свежие копии и сразу проверить каждую
ssh yc-user@VM_IP 'cd navigator-719 && sudo docker compose cp app:/data/backups ./backups-vm'
scp -r yc-user@VM_IP:navigator-719/backups-vm ./navigator-backups
for f in ./navigator-backups/*.db; do python scripts/backup_db.py --verify "$f" || echo "БИТЫЙ: $f"; done
```
⚠️ Выгруженные копии содержат **ПДн** — хранить вне рабочего дерева репозитория (см. `CLAUDE.md`,
раздел про 152-ФЗ); `*_backup*.db` и `backups/` уже в `.gitignore`.

**Вариант Б — в Yandex Object Storage с самой VM** (если нужен автомат без включённого ПК):
завести сервисный аккаунт с правами только на запись в бакет, положить ключ на VM и добавить в тот
же cron после снятия копии `aws s3 cp --endpoint-url=https://storage.yandexcloud.net …`.
Минус: ключ лежит на VM, и её компрометация открывает архив — поэтому вариант А предпочтительнее.

**Ретенция:** ≥30 копий на VM (`--keep 30`) и не меньше 30 дней off-VM.

**Проверка восстановления — раз в месяц и обязательно перед `instance delete`:** поднять копию
локально (`APP_DB_URL=sqlite:///<путь к копии>`), открыть `/admin` и убедиться, что скоркард и
логи диалогов на месте. Непроверенное восстановление — не восстановление.

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
| Сайт не открывается по домену | Не открыты 80/443 в security group; DNS ещё не разъехался (`dig`); `PUBLIC_DOMAIN` в `.env` не в punycode; сертификат не выпустился — `docker compose logs caddy`. Голый `http://VM_IP` с 22.09 не обслуживается. |
| Приложение не отвечает на `127.0.0.1:8000` | `docker compose ps` показывает app не Up — смотреть `docker compose logs app`. |
| После входа сразу выбрасывает на /login | `COOKIE_SECURE=true`, а сайт открыт по http — кука не отправляется. По домену ходить только https; для проверки по петле временно `COOKIE_SECURE=false`. |
| app падает с ошибкой Qdrant | Qdrant не поднят/не проиндексирован: `docker compose up -d qdrant`, затем `load_kb`. |
| «Сервис временно недоступен» в чате | Нет/неверный `DEEPSEEK_API_KEY` в `.env` или нет исходящей сети к api.deepseek.com. `docker compose logs app`. |
| Долгий первый ответ | Первый запрос грузит e5 в память (разово). Дальше быстро. |

> ⚠️ Коллекция Qdrant в git не хранится — на VM она наполняется `load_kb.py` (шаг 7). При
> пересоздании VM индексацию повторить.
