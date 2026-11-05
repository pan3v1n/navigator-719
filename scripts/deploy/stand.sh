#!/usr/bin/env bash
# Стенд dev.719-навигатор.рф. Исполняется НА СЕРВЕРЕ, порядок — docs/DEPLOY.md §4б.
#
#   stand.sh check              только чтение: хватает ли памяти и диска, есть ли всё нужное у боя
#   stand.sh init               настройки стенда, пароль входа, блок caddy; бой не перезапускается
#   stand.sh up <архив> <метка> собрать и поднять стенд из архива `git archive`
#   stand.sh status             что крутится
#   stand.sh down               остановить стенд, база на томе остаётся
#
# ⚠ Блок caddy с хешем пароля лежит только на сервере (`caddy/stand.d/`): репозиторий публичный.
# ⚠ Пароли не печатаются: они в ~/navigator-stand/ACCESS.txt, доступ только у владельца файла.
set -euo pipefail

PROD="${PROD:-$HOME/navigator-719}"
STAND="${STAND:-$HOME/navigator-stand}"
SITE="$PROD/caddy/stand.d/dev.caddy"
CONTAINER=navigator-stand-app
DOCKER="${DOCKER:-sudo -n docker}"
# Как в deploy.sh: BuildKit ходит в реестр за метаданными базового образа, а `env` обязан стоять после sudo.
case "$DOCKER" in
  sudo*) DOCKER_NOBUILDKIT="${DOCKER% *} env DOCKER_BUILDKIT=0 ${DOCKER##* }" ;;
  *) DOCKER_NOBUILDKIT="env DOCKER_BUILDKIT=0 $DOCKER" ;;
esac
MIN_MEM_MB=3000
MIN_DISK_GB=15
# Настройки движка берутся у боя, если там заданы: иначе стенд покажет не то, что будет после выкатки.
ENGINE_SETTINGS="DEEPSEEK_MODEL DEEPSEEK_BASE_URL EMBEDDING_MODEL EMBEDDING_DIM RERANK_ENABLED CASE_RELEVANCE_MIN
PROCEDURAL_DEFLECT_ENABLED PROCEDURAL_ANSWER_FROM_RULES QDRANT_RULES_COLLECTION QDRANT_CASES_COLLECTION"

say() { printf '%s\n' "$*"; }
die() { printf 'СТОП: %s\n' "$*" >&2; exit 1; }

env_value() { sed -n "s/^$1=//p" "$PROD/.env" | tail -1 | tr -d '\r"'; }
prod_container() { $DOCKER ps --filter "label=com.docker.compose.service=$1" --filter "label=com.docker.compose.project.working_dir=$PROD" --format '{{.Names}}' | head -1; }
prod_network() { $DOCKER inspect "$(prod_container qdrant)" --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}'; }
prod_models() { $DOCKER inspect "$(prod_container app)" --format '{{range .Mounts}}{{if eq .Destination "/models"}}{{.Name}}{{end}}{{end}}'; }

domain() {
  local d
  d="$(env_value PUBLIC_DOMAIN)"
  [ -n "$d" ] || die "в $PROD/.env нет PUBLIC_DOMAIN"
  printf '%s' "$d"
}

# Ответ сервиса через caddy на этой машине, мимо внешней сети. Код 000 — соединения нет.
http_code() {
  curl -sk -o /dev/null -w '%{http_code}' --max-time 10 --resolve "$1:443:127.0.0.1" "https://$1$2" || true
}

cmd_check() {
  local bad=0 mem disk
  mem="$(free -m | awk '/^Mem:/ {print $7}')"
  disk="$(df -BG --output=avail / | tail -1 | tr -dc '0-9')"
  say "память свободно: $mem МБ (нужно $MIN_MEM_MB), диск свободно: $disk ГБ (нужно $MIN_DISK_GB)"
  [ "$mem" -ge "$MIN_MEM_MB" ] || { say "мало памяти"; bad=1; }
  [ "$disk" -ge "$MIN_DISK_GB" ] || { say "мало диска"; bad=1; }
  for svc in caddy qdrant app; do
    [ -n "$(prod_container "$svc")" ] && say "у боя работает: $svc" || { say "у боя нет контейнера: $svc"; bad=1; }
  done
  if [ "$bad" -eq 0 ]; then
    say "сеть боя: $(prod_network), том модели: $(prod_models)"
    [ -n "$(prod_models)" ] || { say "том модели боя не найден"; bad=1; }
  fi
  grep -q 'import stand.d/\*\.caddy' "$PROD/caddy/Caddyfile" \
    && say "импорт стенда в Caddyfile есть" || { say "в Caddyfile нет импорта стенда: нужен test45"; bad=1; }
  [ "$bad" -eq 0 ] && say "можно настраивать" || die "настраивать рано"
}

caddy_cmd() { $DOCKER exec "$(prod_container caddy)" caddy "$@" --config /etc/caddy/Caddyfile --adapter caddyfile; }

# Откат блока стенда: бой важнее стенда, поэтому любая ошибка возвращает прежний конфиг.
drop_site() {
  rm -f "$SITE"
  caddy_cmd reload >/dev/null 2>&1 || say "⚠ caddy не перечитал конфиг после отката, проверьте бой"
}

cmd_init() {
  cmd_check
  local dom pw hash key svc_host dev_host code name value
  dom="$(domain)"
  svc_host="$(env_value APP_SUBDOMAIN).$dom"
  dev_host="dev.$dom"
  mkdir -p "$STAND"
  chmod 700 "$STAND"
  umask 077
  if [ ! -f "$STAND/.env.stand" ]; then
    key="$(env_value DEEPSEEK_API_KEY)"
    [ -n "$key" ] || die "в $PROD/.env нет DEEPSEEK_API_KEY"
    {
      printf 'SESSION_SECRET=%s\n' "$(openssl rand -hex 32)"
      printf 'DEEPSEEK_API_KEY=%s\n' "$key"
      printf 'COOKIE_SECURE=true\nGUEST_TRIAL_ENABLED=true\nPUBLIC_DOMAIN=%s\n' "$dom"
      for name in $ENGINE_SETTINGS; do
        value="$(env_value "$name")"
        [ -z "$value" ] || printf '%s=%s\n' "$name" "$value"
      done
    } > "$STAND/.env.stand"
    say "настройки стенда записаны, секрет сессии свой"
  fi
  if [ -f "$SITE" ]; then
    say "блок caddy уже есть, пароль не меняю"
    return 0
  fi
  pw="$(openssl rand -base64 18 | tr -d '/+=')"
  hash="$($DOCKER exec "$(prod_container caddy)" caddy hash-password --plaintext "$pw" | tr -d '\r')"
  [ -n "$hash" ] || die "хеш пароля не получен"
  printf 'адрес: https://dev.719-навигатор.рф\nвход на стенд: stand / %s\n' "$pw" > "$STAND/ACCESS.txt"
  mkdir -p "$(dirname "$SITE")"
  chmod 755 "$(dirname "$SITE")"   # umask выше закрыл бы каталог от контейнера caddy
  cat > "$SITE" <<EOF
dev.{\$PUBLIC_DOMAIN} {
	encode gzip
	basic_auth {
		stand $hash
	}
	reverse_proxy app-stand:8000 {
		flush_interval -1
	}
	header {
		X-Robots-Tag "noindex, nofollow"
		X-Content-Type-Options nosniff
		Referrer-Policy strict-origin-when-cross-origin
	}
}
EOF
  chmod 644 "$SITE"
  if ! caddy_cmd validate >/dev/null 2>&1; then
    rm -f "$SITE"
    die "конфиг caddy не прошёл проверку, блок убран, бой не тронут"
  fi
  caddy_cmd reload >/dev/null 2>&1 || { drop_site; die "caddy не перечитал конфиг, блок убран"; }
  code="$(http_code "$svc_host" /ping)"
  [ "$code" = "200" ] || { drop_site; die "бой после перечитывания ответил $code, блок убран"; }
  say "бой отвечает 200"
  # Сертификат стенда выпускается сразу после перечитывания, это до пары минут.
  for _ in $(seq 1 24); do
    code="$(http_code "$dev_host" /ping)"
    [ "$code" = "401" ] && break
    sleep 5
  done
  [ "$code" = "401" ] && say "стенд закрыт паролем: без него 401" \
    || say "⚠ стенд ответил $code вместо 401: проверьте сертификат и блок caddy"
}

cmd_up() {
  local archive="${1:-}" label="${2:-}"
  [ -f "$archive" ] || die "нет архива: $archive"
  [ -n "$label" ] || die "нужна метка версии"
  [ -f "$STAND/.env.stand" ] || die "сначала stand.sh init"
  local src="$STAND/src" net models pw
  net="$(prod_network)"
  models="$(prod_models)"
  rm -rf "$src.new"
  mkdir -p "$src.new"
  tar -xzf "$archive" -C "$src.new"
  [ -f "$src.new/docker-compose.stand.yml" ] || { rm -rf "$src.new"; die "в архиве нет docker-compose.stand.yml"; }
  cp "$STAND/.env.stand" "$src.new/.env.stand"
  printf 'PROD_NETWORK=%s\nPROD_MODELS=%s\n' "$net" "$models" > "$src.new/.env"
  printf '%s\n' "$label" > "$src.new/RELEASE"
  rm -rf "$src"
  mv "$src.new" "$src"
  cd "$src"
  $DOCKER_NOBUILDKIT compose -f docker-compose.stand.yml build
  $DOCKER compose -f docker-compose.stand.yml up -d
  for _ in $(seq 1 60); do
    $DOCKER exec "$CONTAINER" curl -fsS http://127.0.0.1:8000/ping >/dev/null 2>&1 && break
    sleep 5
  done
  $DOCKER exec "$CONTAINER" curl -fsS http://127.0.0.1:8000/ping >/dev/null || die "стенд не отвечает, журнал: docker logs $CONTAINER"
  if ! grep -q '^учётка admin:' "$STAND/ACCESS.txt"; then
    pw="$(openssl rand -base64 12 | tr -d '/+=')"
    $DOCKER exec "$CONTAINER" python scripts/seed_users.py --experts 0 --admin-password "$pw" >/dev/null
    (umask 077; printf 'учётка admin: admin / %s\n' "$pw" >> "$STAND/ACCESS.txt")
    say "учётка admin создана, пароль в $STAND/ACCESS.txt"
  fi
  say "стенд поднят: $label"
}

cmd_status() {
  $DOCKER ps --filter "name=$CONTAINER" --format '{{.Names}} {{.Status}}'
  $DOCKER stats --no-stream --format '{{.Name}} {{.MemUsage}} {{.CPUPerc}}'
  $DOCKER exec "$CONTAINER" curl -fsS http://127.0.0.1:8000/ping || say "стенд не отвечает"
}

cmd_down() {
  cd "$STAND/src"
  $DOCKER compose -f docker-compose.stand.yml down
  say "стенд остановлен, база на томе сохранена"
}

case "${1:-}" in
  check) cmd_check ;;
  init) cmd_init ;;
  up) shift; cmd_up "$@" ;;
  status) cmd_status ;;
  down) cmd_down ;;
  *) say "использование: stand.sh check | init | up <архив> <метка> | status | down"; exit 2 ;;
esac
