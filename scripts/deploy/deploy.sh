#!/usr/bin/env bash
# ЭТАЛОННЫЙ СКРИПТ ВЫКАТКИ — `O2` (#100). Один файл на все релизы, различия живут в профиле.
#
# ЗАЧЕМ ОН В РЕПОЗИТОРИИ. До 21.08.2026 скрипты выкатки лежали вне git (`D:/navigator-deploy/
# <дата>/deploy_<тег>.sh`) и писались заново копированием предыдущего. Предохранители, найденные
# кровью на живых выкатках, переживали сессию только если их вручную переносили в чек-лист. Три
# релиза подряд источником дефекта был сам скрипт:
#   * 14.08 — обрыв ssh плодил ВТОРОЙ nohup, четыре параллельные выкатки положили SSH на полчаса;
#   * 18.08 — `backup_db.py --verify` без ИМЕНИ ФАЙЛА печатал usage, и копия НЕ создавалась:
#             шаг «бэкап до всего» был фикцией во всех прошлых выкатках;
#   * 20.08 — два дефекта, оба класса «предохранитель отчитался, не сработав» (см. C и D ниже).
# Теперь правки скрипта накапливаются, проходят ревью и закреплены тестами
# (`tests/test_deploy_script.py`).
#
# ⚠⚠ ЧЕТЫРЕ ПРЕДОХРАНИТЕЛЯ, КОТОРЫЕ ЗДЕСЬ НЕЛЬЗЯ ТРОГАТЬ:
#
#  A. `flock` — канал до VM рвётся (DPI), и ретрай ssh после обрыва запускает НОВУЮ выкатку
#     поверх идущей. Обрыв сессии ≠ «не выполнилось».
#
#  B. Бэкап БД зовётся как `backup_db.py --dir <кат> --keep <N>` и ПРЕРЫВАЕТ выкатку, если копия
#     не снялась. У `--verify` обязателен позиционный аргумент — без него argparse печатает usage
#     и выходит, то есть шаг считается выполненным, не будучи им.
#
#  C. Внутри контейнера `qdrant` НЕТ ни `curl`, ни `wget` (образ минимальный). Любой HTTP к
#     Qdrant делается из контейнера ПРИЛОЖЕНИЯ по адресу `http://qdrant:6333`. 20.08 первая
#     редакция звала `curl` в контейнере qdrant: в лог падало «curl: not found», запрос
#     `snapshots/recover` НЕ уходил, коллекция осталась прежней, правка не доехала — а рапорт был
#     успешный.
#
#  D. УПАВШАЯ ПРОВЕРКА ОСТАНАВЛИВАЕТ ВЫКАТКУ. Под одним `set -u` AssertionError печатался в лог,
#     а скрипт доходил до строки «выкатка завершена» и возвращал 0. Оператор читает последнюю
#     строку. Каждая проверка ведёт в `fail()`, итог зависит от `FAILED`, код возврата — 1.
#
# ⚠ Проверки-маркеры ищут ОПРЕДЕЛЕНИЕ или присвоение, а не упоминание: 18.08 `grep` по имени
#   удалённой константы поймал её в комментарии, объясняющем удаление, и остановил ВЕРНУЮ
#   выкатку, а `\d` нашёл «цифру» в слове «ОКПД2». Предохранитель, бьющий по верному коду,
#   дороже отсутствующего — он заставляет усомниться в правке.
#
# ⚠ Секретов и адресов здесь нет и быть не должно: всё, что зависит от машины, приходит
#   переменными окружения или профилем релиза.
#
# ЗАПУСК (на VM, из каталога пакета):
#   ./deploy.sh --release releases/v0.5.0-test12.env
#   ./deploy.sh --release releases/v0.5.0-test12.env --dry-run   # проверить профиль, не трогая бой
#
# Переменные окружения: PKG (каталог пакета, по умолчанию текущий), APP (рабочая копия на VM,
# по умолчанию ~/navigator-719), DOCKER (по умолчанию «sudo -n docker»).

set -u

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELEASE=""
DRY_RUN=0

usage() {
  echo "Использование: $0 --release <профиль> [--dry-run]"
  echo "  --release <файл>  профиль релиза (см. scripts/deploy/releases/*.env)"
  echo "  --dry-run         проверить профиль и наличие файлов, ничего не выполняя"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --release) RELEASE="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "неизвестный аргумент: $1"; usage; exit 2 ;;
  esac
done

[ -n "$RELEASE" ] || { echo "не указан --release"; usage; exit 2; }
[ -f "$RELEASE" ] || { echo "профиль не найден: $RELEASE"; exit 2; }

PKG="${PKG:-$(pwd)}"
APP="${APP:-$HOME/navigator-719}"
DOCKER="${DOCKER:-sudo -n docker}"

# --- профиль релиза -----------------------------------------------------------------------
# Обязательные: TAG и EXPECT (счётчики коллекций). Необязательные: SNAPSHOT,
# SNAPSHOT_COLLECTION, MARKERS, ANTI_MARKERS, CHECKS_DIR.
TAG=""; SNAPSHOT=""; SNAPSHOT_COLLECTION="pp719"; CHECKS_DIR=""
declare -A EXPECT=()
MARKERS=(); ANTI_MARKERS=()
# shellcheck disable=SC1090
. "$RELEASE"

[ -n "$TAG" ] || { echo "в профиле не задан TAG"; exit 2; }
[ "${#EXPECT[@]}" -gt 0 ] || { echo "в профиле не заданы ожидаемые счётчики EXPECT"; exit 2; }
[ -n "$CHECKS_DIR" ] || CHECKS_DIR="$SELF_DIR/checks/$TAG"

say()  { echo "[$(date '+%H:%M:%S')] $*"; }
FAILED=0
fail() { FAILED=1; say "❌ ПРОВАЛ ПРОВЕРКИ: $*"; }

# Проверка МАРКЕРА: строка вида «файл|шаблон|описание».
check_marker() {
  local spec="$1" want_present="$2"
  local file="${spec%%|*}"; local rest="${spec#*|}"
  local pattern="${rest%%|*}"; local desc="${rest#*|}"
  if grep -qE "$pattern" "$file" 2>/dev/null; then
    [ "$want_present" = "1" ] && return 0
    say "СТАРЫЙ КОД: $desc (в $file всё ещё есть «$pattern»)"; return 1
  else
    [ "$want_present" = "1" ] || return 0
    say "НЕ ДОЕХАЛО: $desc (в $file нет «$pattern»)"; return 1
  fi
}

# ⚠⚠ ГЕЙТ CI НА КОММИТЕ, КОТОРЫЙ ЕДЕТ. Куплен инцидентом 21.08.2026: батарея простояла красной
# ШЕСТЬ прогонов подряд, и в этой красноте PR #103 уехал в `main`, а `v0.5.0-test13` — на бой.
# Ломался один тест, переставший быть офлайновым; CI поймал его ВЕРНО и ровно так, как обещает
# комментарий в tests.yml, — сигнал просто некому было прочитать. Ровно тот класс, что уже
# записан в уроках: предохранитель, который сообщает о провале и не останавливает, равен
# отсутствующему. Здесь он останавливает.
#
# ПОЧЕМУ В СУХОМ ПРОГОНЕ. Он обязателен перед каждой выкаткой и идёт на машине разработчика, где
# есть `gh` и сеть; на VM нет ни того, ни другого. Там же рядом лежит код, который поедет, — тот
# самый `$SRC`, на котором сверяются маркеры.
#
# ⚠ «Проверить не смог» НИКОГДА не печатается как «зелено»: нет gh, нет сети, нет прогона — это
# предупреждение и просьба посмотреть руками, но не провал. Предохранитель, бьющий по верному
# коду, дороже отсутствующего (урок 18.08), а отсутствие интернета кодом не является.
ci_gate() {
  local src="$1" sha concl
  if [ "${SKIP_CI_GATE:-0}" = "1" ]; then
    say "  ⚠ ГЕЙТ CI ОТКЛЮЧЁН вручную (SKIP_CI_GATE=1) — осознанный риск, а не «зелено»"
    return 0
  fi
  command -v gh >/dev/null 2>&1 || {
    say "  ⚠ CI НЕ ПРОВЕРЕН: gh не установлен. Это НЕ «зелено» — смотрите прогон руками"; return 0; }
  sha=$(cd "$src" && git rev-parse HEAD 2>/dev/null) || sha=""
  [ -n "$sha" ] || { say "  ⚠ CI НЕ ПРОВЕРЕН: в $src нет git-репозитория"; return 0; }
  if [ -n "$(cd "$src" && git status --porcelain 2>/dev/null)" ]; then
    say "  ⚠ рабочее дерево ГРЯЗНОЕ: CI отвечает за $sha, а поедет не он"
  fi
  concl=$(cd "$src" && gh run list --commit "$sha" --limit 20 --json conclusion \
            --jq '[.[].conclusion] | join(" ")' 2>/dev/null) || concl=""
  case " $concl " in
    *" failure "*|*" timed_out "*|*" startup_failure "*)
      say "  ❌ CI НА $sha КРАСНЫЙ ($concl) — выкатывать нельзя, чинить батарею"
      say "     осознанный обход, если провал заведомо не про этот код: SKIP_CI_GATE=1"
      return 1 ;;
    *" success "*) say "  CI на $sha: зелёный" ;;
    *) say "  ⚠ CI НЕ ПРОВЕРЕН: прогона на $sha не нашлось (нет сети либо он ещё идёт)" ;;
  esac
  return 0
}

# --- сухой прогон -------------------------------------------------------------------------
if [ "$DRY_RUN" = "1" ]; then
  say "СУХОЙ ПРОГОН профиля $RELEASE"
  say "  тег: $TAG"
  say "  пакет: $PKG   рабочая копия: $APP"
  for c in "${!EXPECT[@]}"; do say "  ждём коллекцию $c = ${EXPECT[$c]}"; done
  if [ -n "$SNAPSHOT" ]; then
    say "  снапшот: $SNAPSHOT → коллекция $SNAPSHOT_COLLECTION"
  else
    say "  снапшот НЕ задан — выкатка рантаймовая, корпус не трогаем"
  fi
  # ⚠ Маркеры сверяем С КОДОМ, а не только считаем. Шаблон, не совпадающий с тем, что едет в
  # пакете, останавливает ВЕРНУЮ выкатку — это уже случалось (18.08). Дешевле поймать здесь.
  SRC="${SRC:-}"
  if [ -z "$SRC" ]; then
    for cand in "$PWD" "$APP" "$SELF_DIR/../.."; do
      [ -f "$cand/app/core/config.py" ] && { SRC="$cand"; break; }
    done
  fi
  if [ -n "$SRC" ]; then
    say "  сверяю маркеры с кодом в $SRC"
    dry_bad=0
    for m in ${MARKERS[@]+"${MARKERS[@]}"}; do
      (cd "$SRC" && check_marker "$m" 1) || dry_bad=$((dry_bad + 1))
    done
    for m in ${ANTI_MARKERS[@]+"${ANTI_MARKERS[@]}"}; do
      (cd "$SRC" && check_marker "$m" 0) || dry_bad=$((dry_bad + 1))
    done
    if [ "$dry_bad" != "0" ]; then
      say "  ❌ маркеров не совпало: $dry_bad — профиль остановил бы верную выкатку, чинить ЗДЕСЬ"
      exit 2
    fi
    say "  маркеры сошлись: ${#MARKERS[@]} + ${#ANTI_MARKERS[@]} анти"
  else
    say "  ⚠ исходников рядом нет — маркеры ${#MARKERS[@]}+${#ANTI_MARKERS[@]} проверятся на выкатке"
  fi
  common_n=$(ls "$SELF_DIR/checks/common"/*.py 2>/dev/null | wc -l)
  release_n=$(ls "$CHECKS_DIR"/*.py 2>/dev/null | wc -l)
  say "  проверки рантайма: общих $common_n, релизных $release_n"
  [ "$common_n" -gt 0 ] || say "  ⚠ общих проверок НЕТ — выкатка пойдёт вслепую"
  if [ -n "$SRC" ]; then
    ci_gate "$SRC" || { say "  ❌ сухой прогон остановлен красным CI"; exit 2; }
  fi
  say "профиль читается, обязательные поля на месте"
  exit 0
fi

# --- A. один процесс ----------------------------------------------------------------------
LOCK="${LOCK:-$HOME/deploy-$TAG.lock}"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "выкатка уже выполняется другим процессом — выхожу"
  exit 0
fi

say "=== старт выкатки $TAG ==="
cd "$PKG" || { say "нет каталога пакета: $PKG"; exit 1; }
sha256sum -c SHA256SUMS.txt || { say "контрольная сумма НЕ сошлась — выхожу"; exit 1; }

# --- B. бэкап боевой БД ДО всего ----------------------------------------------------------
say "--- бэкап боевой БД ДО всего ---"
if ! $DOCKER compose -f "$APP/docker-compose.yml" exec -T app \
     python scripts/backup_db.py --dir /data/backups --keep 14 2>&1 | tail -3; then
  say "БЭКАП НЕ СНЯЛСЯ — выкатку не начинаю"; exit 1
fi

# --- код ----------------------------------------------------------------------------------
say "--- код поверх рабочей копии (.env и /data не трогаем) ---"
tar -xzf "navigator-719-$TAG.tar.gz" -C "$APP" || { say "распаковка не удалась"; exit 1; }
cd "$APP"
grep -m1 APP_VERSION app/core/config.py
say "файлы данных (без них R6/R29/D9 молча выключаются):"
ls -la knowledge_base/pp719/inherited_requirements.json \
       knowledge_base/pp719/incomplete_thresholds.json \
       knowledge_base/pp719/fragmented_requirements.json 2>/dev/null | awk '{print "    ", $5, $9}'
say "кейсов: $(ls knowledge_base/cases/*.json 2>/dev/null | wc -l)"

say "--- маркеры ЭТОГО пакета (иначе распаковался старый код) ---"
for m in ${MARKERS[@]+"${MARKERS[@]}"}; do
  check_marker "$m" 1 || { say "выхожу: распакован не тот код"; exit 1; }
done
for m in ${ANTI_MARKERS[@]+"${ANTI_MARKERS[@]}"}; do
  check_marker "$m" 0 || { say "выхожу: распакован не тот код"; exit 1; }
done
say "    маркеров проверено: ${#MARKERS[@]} + ${#ANTI_MARKERS[@]} анти"

# --- C. восстановление коллекции снапшотом (только если корпус менялся) --------------------
if [ -n "$SNAPSHOT" ]; then
  say "--- ВОССТАНОВЛЕНИЕ «$SNAPSHOT_COLLECTION» ИЗ СНАПШОТА (корпус изменился) ---"
  QC=$($DOCKER ps --format '{{.Names}}' | grep -m1 qdrant) || true
  [ -n "$QC" ] || { say "не нашёл контейнер qdrant — выхожу"; exit 1; }
  say "контейнер qdrant: $QC"
  # Снапшот едет сжатым: канал рвётся, каждый лишний мегабайт — риск.
  if [ ! -f "$PKG/$SNAPSHOT" ]; then
    say "распаковываю снапшот"
    gunzip -kf "$PKG/$SNAPSHOT.gz" || { say "не удалось распаковать снапшот — выхожу"; exit 1; }
  fi
  ls -la "$PKG/$SNAPSHOT" | awk '{print "    ", $5, $9}'
  # Снапшот обязан лежать ВНУТРИ /qdrant/snapshots, иначе Qdrant отвечает
  # «Forbidden: … must be inside the snapshots directory».
  $DOCKER exec "$QC" mkdir -p "/qdrant/snapshots/$SNAPSHOT_COLLECTION" \
    || { say "не создать каталог снапшотов — выхожу"; exit 1; }
  $DOCKER cp "$PKG/$SNAPSHOT" "$QC:/qdrant/snapshots/$SNAPSHOT_COLLECTION/$SNAPSHOT" \
    || { say "снапшот не скопировался в контейнер — выхожу"; exit 1; }
  # ⚠ Предохранитель C: HTTP к Qdrant — ТОЛЬКО из контейнера ПРИЛОЖЕНИЯ.
  $DOCKER compose exec -T -e SNAP="$SNAPSHOT" -e COLL="$SNAPSHOT_COLLECTION" app \
    python -c 'import json, os, urllib.request
snap, coll = os.environ["SNAP"], os.environ["COLL"]
body = json.dumps({"location": "file:///qdrant/snapshots/%s/%s" % (coll, snap),
                   "priority": "snapshot"}).encode()
req = urllib.request.Request("http://qdrant:6333/collections/%s/snapshots/recover" % coll,
                             data=body, method="PUT",
                             headers={"Content-Type": "application/json"})
print("   ", urllib.request.urlopen(req, timeout=900).read().decode()[:200])' \
    || fail "recover снапшота не выполнен"
fi

# --- сборка -------------------------------------------------------------------------------
say "--- сборка образа ---"
BUILT=0
# DOCKER_BUILDKIT=0: BuildKit идёт в реестр за метаданными базового образа даже когда он есть
# локально, и падает на TLS до auth.docker.io.
if DOCKER_BUILDKIT=0 $DOCKER compose build app 2>&1 | tail -5; then
  BUILT=1; say "собрано классическим сборщиком"
else
  say "классический сборщик не справился — тонкий слой поверх текущего образа"
  IMG=$($DOCKER inspect --format '{{.Config.Image}}' navigator-719-app-1 2>/dev/null)
  [ -z "$IMG" ] && IMG="navigator-719-app:latest"
  say "базовый образ: $IMG"
  printf 'FROM %s\nWORKDIR /app\nCOPY . .\n' "$IMG" > /tmp/Dockerfile.thin
  if DOCKER_BUILDKIT=0 $DOCKER build -f /tmp/Dockerfile.thin -t "$IMG" . 2>&1 | tail -5; then
    BUILT=1; say "собрано тонким слоем"
  fi
fi
[ "$BUILT" = "1" ] || { say "СБОРКА НЕ УДАЛАСЬ — код на диске обновлён, контейнер прежний"; exit 1; }

say "--- рестарт приложения ---"
$DOCKER compose up -d app
sleep 12
$DOCKER compose ps --format "  {{.Name}} {{.Status}}"

# --- коллекции ----------------------------------------------------------------------------
say "--- КОЛЛЕКЦИИ: счётчики обязаны совпасть с профилем релиза ---"
WANT_JSON="{"
for c in "${!EXPECT[@]}"; do WANT_JSON="$WANT_JSON\"$c\": ${EXPECT[$c]},"; done
WANT_JSON="${WANT_JSON%,}}"
$DOCKER compose exec -T -e WANT="$WANT_JSON" app \
  python -c 'import json, os, urllib.request
want = json.loads(os.environ["WANT"])
d = json.load(urllib.request.urlopen("http://qdrant:6333/collections", timeout=30))
bad = []
for c in sorted(x["name"] for x in d["result"]["collections"]):
    info = json.load(urllib.request.urlopen(
        "http://qdrant:6333/collections/" + c, timeout=30))["result"]
    n, exp = info["points_count"], want.get(c)
    flag = "" if exp in (None, n) else "  ! ЖДАЛИ %s" % exp
    if flag:
        bad.append(c)
    print("  ", c, "точек:", n, info["status"], flag)
assert not bad, "коллекции разошлись: %s" % bad
print("   OK: корпус на сервере совпадает с профилем релиза")' \
  || fail "коллекции разошлись с профилем релиза"

# --- проверки рантайма --------------------------------------------------------------------
# ⚠ ДВА КАТАЛОГА, И ЭТО НЕ УДОБСТВО. `checks/common` — проверки, которые обязаны проходить на
# КАЖДОМ релизе: однажды найденный дефект больше не должен вернуться незамеченным. Раньше они
# жили в скрипте очередного релиза и умирали вместе с ним: `D12` проверялась на test11 и
# исчезла в test12. `checks/<тег>` — только то, что специфично для этого релиза.
run_checks_dir() {
  local dir="$1" kind="$2"
  if [ ! -d "$dir" ]; then
    say "⚠ каталога проверок ($kind) нет: $dir"
    return 0
  fi
  local n=0
  for chk in "$dir"/*.py; do
    [ -e "$chk" ] || continue
    n=$((n + 1))
    say "--- проверка рантайма [$kind]: $(basename "$chk") ---"
    $DOCKER compose exec -T app python - < "$chk" || fail "$(basename "$chk")"
  done
  [ "$n" -gt 0 ] || say "⚠ в $dir нет ни одной проверки"
}

run_checks_dir "$SELF_DIR/checks/common" "общие"
run_checks_dir "$CHECKS_DIR" "релизные"

# --- доступность --------------------------------------------------------------------------
say "--- /ping и страницы ---"
curl -fsS http://127.0.0.1/ping || fail "/ping не ответил"
echo
# ⚠ ПРОВЕРЯЕМ, ЧТО СТРАНИЦА ОТВЕТИЛА, А НЕ ЧТО ОНА ОТДАЛА РОВНО 200. Первая редакция требовала
# 200 у всех четырёх — и на выкатке v0.5.0-test13 объявила ПРОВАЛ на верном коде: `/` законно
# отдаёт 302 на `/login` неаутентифицированному пользователю (проверено там же: `/login` → 200).
# Ровно тот класс, о котором предупреждает шапка этого файла: предохранитель, бьющий по верному
# коду, дороже отсутствующего — он заставляет усомниться в правке. Ошибкой считаем 4xx/5xx и
# пустой ответ, то есть случаи, когда страницы действительно нет.
for p in / /help /terms /privacy; do
  code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1$p")
  echo "   $p → $code"
  case "$code" in
    2??|3??) ;;
    *) fail "страница $p отдала $code" ;;
  esac
done
df -h / | tail -1

# --- D. итог зависит от проверок, а не от того, что скрипт дошёл до конца ------------------
if [ "$FAILED" = "1" ]; then
  say "=== ❌ ВЫКАТКА С ПРОВАЛЕННЫМИ ПРОВЕРКАМИ — разбирать выше по логу, НЕ считать закрытой ==="
  exit 1
fi
say "=== ✅ выкатка $TAG завершена, все проверки прошли ==="
