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

## 3. Создать VM (Ubuntu 22.04, 4 vCPU / 8 ГБ)
```powershell
yc compute instance create `
  --name navigator-719 `
  --zone ru-central1-a `
  --cores 4 --memory 8GB --core-fraction 100 `
  --create-boot-disk image-folder-id=standard-images,image-family=ubuntu-2204-lts,size=30,type=network-ssd `
  --network-interface subnet-name=default-a,nat-ip-version=ipv4 `
  --ssh-key $HOME\.ssh\id_ed25519.pub
```
Записать **публичный IP** из вывода (поле `one_to_one_nat address`). Далее — `VM_IP`.

> Если CLI-сеть капризничает — VM проще создать в консоли `console.yandex.cloud`
> (Compute Cloud → Создать ВМ: Ubuntu 22.04, 4 vCPU/8 ГБ, публичный IP, вставить SSH-ключ).

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

## 6. Забрать код и создать .env
```bash
git clone -b dev https://github.com/pan3v1n/navigator-719.git
cd navigator-719
cp .env.example .env
nano .env
```
В `.env` задать (минимум):
```
DEEPSEEK_API_KEY=sk-...            # твой ключ
SESSION_SECRET=<длинная-случайная> # см. ниже
APP_VERSION=0.5.0
# QDRANT_URL/APP_DB_URL/HF_HOME НЕ трогать — их задаёт docker-compose
```
Сгенерировать секрет сессий:
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

## 9. После демо — погасить биллинг
```powershell
yc compute instance delete navigator-719
```
(Тома удаляются вместе с VM. Локальный продукт при этом полностью цел.)

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
