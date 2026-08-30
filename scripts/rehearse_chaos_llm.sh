#!/usr/bin/env bash
# Репетиция механизма сценария «LLM недоступна» (#134) в ОДНОРАЗОВОМ контейнере.
#
# ⚠⚠ ЗАЧЕМ ИМЕННО ТАК. 28.08.2026 сценарий уронил бой и не смог поднять, а репетиция этого не
# поймала: локально гонялся только сценарий Qdrant, потому что сценарий LLM требует КОНТЕЙНЕРА
# приложения. Единственный непроверенный путь и оказался сломанным. Здесь проверяется ровно тот
# механизм — правка `/etc/hosts`, который в Docker ВСЕГДА bind-mount отдельного файла, — но на
# контейнере-однодневке. Боевых контейнеров скрипт не касается.
#
# ⚠⚠ ПОЛОЖИТЕЛЬНЫЙ КОНТРОЛЬ НА САМУ РЕПЕТИЦИЮ. Если СТАРАЯ команда (`sed -i`) здесь СРАБОТАЕТ,
# значит среда НЕ воспроизводит боевое условие, и «новая команда прошла» не доказывает ничего.
# Такой исход — провал репетиции, а не успех.
set -u
OUT=$HOME/rehearse
mkdir -p "$OUT"
LOG="$OUT/log.txt"
: > "$LOG"
say() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }
fail=0
C=chaos-rehearsal-719
LINE="127.0.0.1 api.deepseek.com"
UNDO="grep -v '^$LINE\$' /etc/hosts > /tmp/hosts.new && cat /tmp/hosts.new > /etc/hosts && rm -f /tmp/hosts.new"
OLD="sed -i '\\|^$LINE\$|d' /etc/hosts"

IMG=$(sudo docker images --format '{{.Repository}}:{{.Tag}}' | grep -v '<none>' | head -1)
say "образ для репетиции: $IMG"
[ -n "$IMG" ] || { say "ОСТАНОВ: нет ни одного образа"; exit 2; }

sudo docker rm -f "$C" >/dev/null 2>&1
sudo docker run -d --rm --name "$C" --entrypoint sh "$IMG" -c 'sleep 600' >/dev/null 2>&1 \
  || { say "ОСТАНОВ: контейнер не поднялся"; exit 2; }
say "контейнер-однодневка поднят"

inc() { sudo docker exec "$C" sh -c "$1" < /dev/null 2>&1; }
line_count() { sudo docker exec "$C" sh -c "grep -c '^$LINE\$' /etc/hosts || true" < /dev/null 2>&1 | tr -d '\r'; }
inode() { sudo docker exec "$C" sh -c "stat -c %i /etc/hosts" < /dev/null 2>&1 | tr -d '\r'; }

say "--- 0. это точно bind-mount? ---"
MNT=$(inc "grep ' /etc/hosts ' /proc/mounts | head -1")
say "    $MNT"
case "$MNT" in *"/etc/hosts"*) say "    ✅ /etc/hosts смонтирован отдельно — условие боя воспроизведено";;
  *) say "    ⚠⚠ ПРОВАЛ РЕПЕТИЦИИ: /etc/hosts НЕ bind-mount, среда не та"; fail=1;; esac

I0=$(inode); say "--- inode до всего: $I0 ---"

say "--- 1. ПОЛОЖИТЕЛЬНЫЙ КОНТРОЛЬ: старая команда обязана УПАСТЬ ---"
inc "grep -q '$LINE' /etc/hosts || echo '$LINE' >> /etc/hosts" >/dev/null
say "    строка поставлена, вхождений: $(line_count)"
OLD_OUT=$(inc "$OLD"); OLD_RC=$?
say "    старая команда: код $OLD_RC, вывод: ${OLD_OUT:-<пусто>}"
if [ $OLD_RC -eq 0 ]; then
  say "    ⚠⚠ ПРОВАЛ КОНТРОЛЯ: sed -i ЗДЕСЬ СРАБОТАЛ — среда НЕ воспроизводит боевое условие,"
  say "       значит успех новой команды ниже НИЧЕГО НЕ ДОКАЗЫВАЕТ."
  fail=1
else
  say "    ✅ старая команда упала — ровно как на бою (repro подтверждён)"
fi
say "    вхождений после старой команды: $(line_count) (ожидание: 1, она не сработала)"

say "--- 2. НОВАЯ команда снимает РЕАЛЬНО ПОСТАВЛЕННУЮ запись ---"
NEW_OUT=$(inc "$UNDO"); NEW_RC=$?
AFTER=$(line_count)
I1=$(inode)
say "    код $NEW_RC, вывод: ${NEW_OUT:-<пусто>}"
say "    вхождений после: $AFTER (ожидание 0)"
say "    inode после: $I1 (был $I0)"
[ $NEW_RC -eq 0 ] || { say "    ⚠⚠ новая команда вернула не 0"; fail=1; }
[ "$AFTER" = "0" ]  || { say "    ⚠⚠ ЗАПИСЬ НЕ СНЯТА — это и есть инцидент 28.08"; fail=1; }
[ "$I0" = "$I1" ]   || { say "    ⚠⚠ inode СМЕНИЛСЯ — значит был rename, на bind-mount он и падал"; fail=1; }
[ $NEW_RC -eq 0 ] && [ "$AFTER" = "0" ] && [ "$I0" = "$I1" ] && \
  say "    ✅ запись снята, inode тот же — переписан на месте, rename не понадобился"

say "--- 3. идемпотентность: снятие на файле, где строки НЕТ ---"
IDEM_OUT=$(inc "$UNDO"); IDEM_RC=$?
say "    код $IDEM_RC, вывод: ${IDEM_OUT:-<пусто>}"
if [ $IDEM_RC -eq 0 ]; then
  say "    ✅ идемпотентно — холостая проверка ПЕРЕД поломкой законна"
else
  say "    ⚠⚠ НЕ идемпотентно: холостая проверка перед поломкой будет ложно отказывать"; fail=1
fi

say "--- 4. чужие записи не тронуты ---"
inc "echo '10.0.0.7 api.deepseek.com чужая-запись' >> /etc/hosts" >/dev/null
inc "$UNDO" >/dev/null
FOREIGN=$(inc "grep -c 'чужая-запись' /etc/hosts || true" | tr -d '\r')
say "    чужих записей осталось: $FOREIGN (ожидание 1 — сносим ТОЧНУЮ строку, не по хосту)"
[ "$FOREIGN" = "1" ] || { say "    ⚠⚠ снесли чужую запись — прав на это нет"; fail=1; }

sudo docker rm -f "$C" >/dev/null 2>&1
say "контейнер убран"
if [ $fail -eq 0 ]; then echo "РЕПЕТИЦИЯ ПРОЙДЕНА" > "$OUT/VERDICT"; else echo "РЕПЕТИЦИЯ ПРОВАЛЕНА" > "$OUT/VERDICT"; fi
say "=== $(cat "$OUT/VERDICT") ==="
exit $fail
