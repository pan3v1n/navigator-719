#!/usr/bin/env bash
# Перенос рабочего места navigator-719 на другой ПК — всего, чего нет в git.
#
# ЗАЧЕМ. Код целиком на GitHub, но рядом с ним живёт то, без чего работа на новом ПК начинается
# с потерь: `.env` (секреты), `models/` (e5, 2.2 ГБ — из РФ с HuggingFace качается плохо),
# `graphify-out/` (слой документации собирали LLM-агенты — пересборка стоит денег), git-хуки
# (автопуш и graphify), первоисточники в корне репо (RTF Контура, PDF — в git их нет), память
# и настройки Claude Code. Скрипт собирает их в одну папку и раскладывает на новом ПК, правя
# абсолютные пути: Python в хуках и в `.graphify_python`, корень в `.graphify_root`, модель в
# `.env`, имя папки памяти Claude Code (оно выводится из пути репозитория).
#
# ЧЕГО НЕ ПЕРЕНОСИТ — намеренно:
#   * Qdrant. Коллекции пересобираются штатными загрузчиками (WORK_ON_OTHER_PC.md): снапшот с
#     локального Qdrant на NTFS-монтировании ломается (см. `scripts/eval_env.py`), а сырая папка
#     `qdrant_storage/` не обязательно та, с которой работает контейнер.
#   * Приватный SSH-ключ. На новом ПК заводится свой, его .pub добавляется на сервер.
#   * `.venv`, `hf-cache/`, `scratchpad/`, `__pycache__`, `navigator_app.db` — пересоздаются.
#   * ПДн (выгрузки с боя) — только по явному `--with-pii DIR` (152-ФЗ, см. CLAUDE.md).
#   * Историю сессий Claude Code — только по `--with-transcripts`: в ней бывают фрагменты данных
#     боя, прочитанных в сессиях. Память переносится всегда.
#
# ЗАПУСК (Git Bash, из корня репозитория):
#   старый ПК:  bash scripts/pc_transfer.sh pack   E:/navigator-719-transfer [--dry-run]
#                    [--no-models] [--with-transcripts] [--with-pii D:/navigator-export] [--force]
#   новый ПК:   bash scripts/pc_transfer.sh unpack E:/navigator-719-transfer
#               (копия скрипта лежит и в самой папке переноса — если в клоне его ещё нет:
#                cd D:/navigator-719 && bash E:/navigator-719-transfer/pc_transfer.sh unpack E:/navigator-719-transfer)
#   поправить пути ещё раз (например, после `pip install graphifyy`):
#               bash scripts/pc_transfer.sh fix-paths
#
# Папка переноса содержит секреты (`.env`): носитель шифровать (BitLocker To Go — только Windows
# Pro/Enterprise; в Home — архив 7-Zip с паролем) или не выпускать из рук, после переноса
# папку удалить. Целостность проверяется по SHA256SUMS ДО того, как что-то копируется на новый ПК.

set -euo pipefail

STAMP=$(date +%Y%m%d-%H%M%S)
CLAUDE_HOME="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
CCSTATUS_DIR="$HOME/.config/ccstatusline"

die()  { printf '✗ %s\n' "$*" >&2; exit 1; }
note() { printf '  %s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
warn() { printf '⚠ %s\n' "$*" >&2; }

usage() { sed -n '22,29p' "$0" | sed 's/^#//'; exit 2; }

command -v cygpath >/dev/null 2>&1 || die "нужен Git Bash (нет cygpath) — скрипт рассчитан на Windows"

repo_root() {
    local r
    r=$(git rev-parse --show-toplevel 2>/dev/null) || die "запускать из папки репозитория navigator-719"
    [ -f "$r/knowledge_base/manifest.yaml" ] && [ -f "$r/CLAUDE.md" ] \
        || die "$r — не репозиторий navigator-719 (нет knowledge_base/manifest.yaml)"
    printf '%s' "$r"
}

# Имя папки проекта в ~/.claude/projects: Claude Code заменяет каждый не-[A-Za-z0-9] символ
# Windows-пути на «-» (D:\navigator-719 → D--navigator-719). Для кириллицы в пути байтовая замена
# sed разойдётся с посимвольной — такой путь только предупреждаем.
project_key() {
    local win; win=$(cygpath -w "$1")
    if printf '%s' "$win" | LC_ALL=C grep -q '[^ -~]'; then
        warn "в пути репозитория не-ASCII символы — проверьте имя папки памяти в $CLAUDE_HOME/projects вручную"
    fi
    printf '%s' "$win" | LC_ALL=C sed 's/[^A-Za-z0-9]/-/g'
}

hooks_dir() { (cd "$1" && git rev-parse --path-format=absolute --git-path hooks); }

# Первоисточники в корне репозитория: git их игнорирует, на GitHub их нет.
root_docs() {
    git -C "$1" ls-files -z --others --ignored --exclude-standard --directory |
    while IFS= read -r -d '' p; do
        case "$p" in */*) continue ;; esac
        case "${p,,}" in *.rtf|*.pdf|*.doc|*.docx|*.xls|*.xlsx) printf '%s\0' "$p" ;; esac
    done
}

size_of() { du -sh "$1" 2>/dev/null | cut -f1; }

# Не затираем молча. Цель в репозитории уезжает в соседнюю папку <репо>.bak-<время> ВНЕ его:
# `.env.bak` рядом с `.env` git не игнорирует, и секрет уехал бы в первый же `git add -A`.
# Цель вне репозитория (~/.claude, хуки в .git) — в <путь>.bak-<время> рядом.
REPO_BACKUP=""
backup_existing() {  # $1 — цель; $2 — корень репозитория, если цель в нём
    [ -e "$1" ] || return 0
    if [ -n "${2:-}" ]; then
        local rel="${1#"$2"/}"
        mkdir -p "$(dirname "$REPO_BACKUP/$rel")"
        mv "$1" "$REPO_BACKUP/$rel"
        note "прежний $rel → $REPO_BACKUP/$rel"
    else
        mv "$1" "$1.bak-$STAMP"
        note "прежний $(basename "$1") → $(basename "$1").bak-$STAMP"
    fi
}

put() {  # $1 — источник, $2 — куда (полный путь цели), $3 — корень репозитория, если цель в нём
    mkdir -p "$(dirname "$2")"
    backup_existing "$2" "${3:-}"
    cp -r "$1" "$2"
}

# ── проверка, что на старом ПК ничего не теряется ─────────────────────────────────────────────
git_problems() {
    local repo="$1" out wt line
    out=$(git -C "$repo" status --porcelain)
    [ -z "$out" ] || printf -- '- незакоммиченные изменения в %s:\n%s\n' "$repo" "$out"
    out=$(git -C "$repo" log --branches --not --remotes --oneline)
    [ -z "$out" ] || printf -- '- коммиты, которых нет ни в одной удалённой ветке:\n%s\n' "$out"
    out=$(git -C "$repo" stash list)
    [ -z "$out" ] || printf -- '- непустой git stash:\n%s\n' "$out"
    while IFS= read -r line; do
        case "$line" in worktree\ *) ;; *) continue ;; esac
        wt=${line#worktree }
        [ "$wt" = "$repo" ] || [ ! -d "$wt" ] && continue
        out=$(git -C "$wt" status --porcelain 2>/dev/null || true)
        [ -z "$out" ] || printf -- '- незакоммиченные изменения в worktree %s:\n%s\n' "$wt" "$out"
    done < <(git -C "$repo" worktree list --porcelain)
}

# ── pack ──────────────────────────────────────────────────────────────────────────────────────
cmd_pack() {
    local dest="" dry=0 models=1 transcripts=0 pii="" force=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --dry-run) dry=1 ;;
            --no-models) models=0 ;;
            --with-transcripts) transcripts=1 ;;
            --with-pii) shift; pii="${1:-}"; [ -n "$pii" ] || die "--with-pii требует путь к папке" ;;
            --force) force=1 ;;
            -*) die "неизвестный флаг: $1" ;;
            *) [ -z "$dest" ] || die "лишний аргумент: $1"; dest="$1" ;;
        esac
        shift
    done
    [ -n "$dest" ] || usage

    local repo key; repo=$(repo_root); key=$(project_key "$repo")
    dest=$(cygpath -m "$dest")
    # регистр не различаем: на Windows d:/navigator-719 и D:/navigator-719 — одна папка
    case "${dest,,}/" in "${repo,,}"/*) die "папка переноса внутри репозитория — туда уедут секреты; выберите другой путь" ;; esac
    if [ "$dry" = 0 ] && [ -e "$dest" ] && [ -n "$(ls -A "$dest" 2>/dev/null)" ]; then
        die "$dest уже существует и не пуста — укажите новую папку"
    fi

    step "Проверка git: всё ли уже на GitHub"
    local problems; problems=$(git_problems "$repo")
    if [ -n "$problems" ]; then
        printf '%s\n' "$problems"
        if [ "$dry" = 1 ]; then warn "при настоящем запуске это остановит перенос"
        elif [ "$force" = 1 ]; then warn "--force: продолжаю, хотя часть работы есть только на этом ПК"
        else die "сначала закоммитьте и запушьте (или --force, если это осознанно)"; fi
    else
        note "чисто: незакоммиченного нет, неотправленных коммитов нет, stash пуст"
    fi

    # Что везём: «откуда» (абсолютный путь) → «куда» (относительно папки переноса).
    local -a src=() dst=()
    add() { if [ -e "$1" ]; then src+=("$1"); dst+=("$2"); else note "нет $1 — пропускаю"; fi; }

    add "$repo/.env"                        "repo/.env"
    [ "$models" = 1 ] && add "$repo/models" "repo/models"
    add "$repo/graphify-out"                "repo/graphify-out"
    add "$repo/.emb_cache"                  "repo/.emb_cache"
    add "$repo/.claude/settings.local.json" "repo/.claude/settings.local.json"
    local doc
    while IFS= read -r -d '' doc; do add "$repo/$doc" "repo/$doc"; done < <(root_docs "$repo")

    local hd h; hd=$(hooks_dir "$repo")
    for h in "$hd"/*; do
        [ -f "$h" ] || continue
        case "$h" in *.sample) continue ;; esac
        add "$h" "git-hooks/$(basename "$h")"
    done

    if [ "$transcripts" = 1 ]; then
        add "$CLAUDE_HOME/projects/$key"        "claude/project"
    else
        add "$CLAUDE_HOME/projects/$key/memory" "claude/project/memory"
    fi
    add "$CLAUDE_HOME/settings.json"        "claude/settings.json"
    add "$CLAUDE_HOME/skills/graphify"      "claude/skills/graphify"
    add "$CCSTATUS_DIR/settings.json"       "claude/ccstatusline/settings.json"

    if [ -n "$pii" ]; then
        pii=$(cygpath -m "$pii")
        case "${pii,,}/" in "${repo,,}"/*) die "выгрузка с ПДн внутри репозитория — так быть не должно (CLAUDE.md, 152-ФЗ)" ;; esac
        [ -d "$pii" ] || die "нет папки $pii"
        add "$pii" "pii/$(basename "$pii")"
    fi

    step "Состав переноса → $dest"
    local i
    for i in "${!src[@]}"; do printf '  %-8s %s\n' "$(size_of "${src[$i]}")" "${dst[$i]}"; done
    [ "$transcripts" = 1 ] || note "(история сессий Claude Code не входит — --with-transcripts)"
    [ -n "$pii" ] && warn "в переносе ПДн реальных пользователей: только шифрованный носитель, вне репозитория на новом ПК"
    if [ "$dry" = 1 ]; then note "сухой прогон — ничего не скопировано"; return 0; fi

    step "Копирование"
    mkdir -p "$dest"
    for i in "${!src[@]}"; do
        note "${dst[$i]}"
        mkdir -p "$dest/$(dirname "${dst[$i]}")"
        cp -r "${src[$i]}" "$dest/${dst[$i]}"
    done
    cp "$0" "$dest/pc_transfer.sh"

    {
        printf 'Перенос рабочего места navigator-719 (scripts/pc_transfer.sh)\n\n'
        printf 'Собрано:      %s\n' "$(date '+%Y-%m-%d %H:%M:%S %z')"
        printf 'Репозиторий:  %s\n' "$repo"
        printf 'Ветка, HEAD:  %s %s\n' "$(git -C "$repo" rev-parse --abbrev-ref HEAD)" "$(git -C "$repo" rev-parse HEAD)"
        printf 'Состав:\n'; for i in "${!dst[@]}"; do printf '  %s\n' "${dst[$i]}"; done
        printf '\n⚠ Внутри секреты (.env)%s. Носитель — шифрованный; после переноса папку удалить.\n' \
            "$([ -n "$pii" ] && printf ' и ПДн (pii/)')"
        printf 'На новом ПК: склонировать репозиторий, затем из его корня:\n'
        printf '  bash <эта папка>/pc_transfer.sh unpack <эта папка>\n'
        printf 'Подробно — WORK_ON_OTHER_PC.md в репозитории.\n'
    } > "$dest/README.txt"
    printf '%s\n' "$(git -C "$repo" rev-parse HEAD)" > "$dest/.source_head"

    step "Контрольные суммы"
    ( cd "$dest" && find . -type f ! -name SHA256SUMS -print0 | LC_ALL=C sort -z | xargs -0 sha256sum ) > "$dest/SHA256SUMS"
    note "$(wc -l < "$dest/SHA256SUMS") файлов, итого $(size_of "$dest")"

    step "Готово"
    note "папка: $dest"
    note "дальше — WORK_ON_OTHER_PC.md, раздел «На новом ПК»"
}

# ── fix-paths ─────────────────────────────────────────────────────────────────────────────────
# Python для хуков graphify: тот, в котором стоит graphify (как и при `graphify hook install`).
find_python() {
    local cand exe first=""
    for cand in "py -3" python python3; do
        exe=$($cand -c 'import sys; print(sys.executable)' 2>/dev/null | tr -d '\r') || continue
        [ -n "$exe" ] || continue
        [ -n "$first" ] || first="$exe"
        if "$exe" -c 'import graphify' 2>/dev/null; then printf '%s' "$exe"; return 0; fi
    done
    [ -n "$first" ] || return 1
    printf '%s' "$first"; return 3
}

cmd_fix_paths() {
    local repo; repo=$(repo_root)
    local repo_m; repo_m=$(cygpath -m "$repo")

    step "Пути под этот ПК ($repo_m)"
    if [ -d "$repo/graphify-out" ]; then
        printf '%s' "$repo_m" > "$repo/graphify-out/.graphify_root"
        note ".graphify_root → $repo_m"
    fi

    local py rc=0
    py=$(find_python) || rc=$?
    if [ "$rc" = 1 ] || [ -z "$py" ]; then
        warn "Python не найден — хуки graphify не перенастроены (поставьте Python и запустите fix-paths ещё раз)"
    else
        [ "$rc" = 3 ] && warn "graphify не установлен ($py) — 'pip install graphifyy', затем fix-paths ещё раз"
        [ -d "$repo/graphify-out" ] && printf '%s' "$py" > "$repo/graphify-out/.graphify_python"
        local hd h esc; hd=$(hooks_dir "$repo")
        esc=$(printf '%s' "$py" | sed 's/[\\&|]/\\&/g')
        for h in "$hd/post-commit" "$hd/post-checkout"; do
            [ -f "$h" ] && grep -q "^_PINNED=" "$h" || continue
            sed -b -i "s|^_PINNED=.*|_PINNED='$esc'|" "$h"
            note "$(basename "$h"): Python → $py"
        done
    fi

    if [ -f "$repo/.env" ] && [ -d "$repo/models/multilingual-e5-large" ]; then
        local model="$repo_m/models/multilingual-e5-large"
        if grep -q '^EMBEDDING_MODEL=' "$repo/.env"; then
            # -b: без него sed Git Bash переписывает ВЕСЬ файл из CRLF в LF (проверено 21.10);
            # [^\r]* — заменённая строка сохраняет свой \r
            sed -b -i "s|^EMBEDDING_MODEL=[^\r]*|EMBEDDING_MODEL=$model|" "$repo/.env"
        else
            printf '\nEMBEDDING_MODEL=%s\n' "$model" >> "$repo/.env"
        fi
        note ".env: EMBEDDING_MODEL → $model"
    fi
}

# ── unpack ────────────────────────────────────────────────────────────────────────────────────
cmd_unpack() {
    [ $# -eq 1 ] || usage
    local bundle; bundle=$(cygpath -m "$1")
    [ -f "$bundle/SHA256SUMS" ] && [ -f "$bundle/README.txt" ] || die "$bundle — не папка переноса (нет SHA256SUMS)"
    local repo key; repo=$(repo_root); key=$(project_key "$repo")
    REPO_BACKUP="$(dirname "$repo")/$(basename "$repo").bak-$STAMP"

    step "Проверка целостности (SHA256SUMS)"
    ( cd "$bundle" && sha256sum -c --quiet SHA256SUMS ) || die "суммы не сошлись — папка повреждена при копировании, перенесите заново"
    note "все $(wc -l < "$bundle/SHA256SUMS") файлов целы"

    local head; head=$(cat "$bundle/.source_head" 2>/dev/null || true)
    if [ -n "$head" ] && ! git -C "$repo" cat-file -e "$head^{commit}" 2>/dev/null; then
        warn "в этом клоне нет коммита $head, с которого собран перенос — сделайте git fetch / git pull"
    fi

    step "Файлы репозитория → $repo"
    local e name
    if [ -d "$bundle/repo" ]; then
        for e in "$bundle/repo"/* "$bundle/repo"/.[!.]*; do
            [ -e "$e" ] || continue
            name=$(basename "$e")
            if [ "$name" = ".claude" ]; then   # только свои файлы, не весь .claude/ (там worktree)
                local f
                for f in "$e"/*; do [ -e "$f" ] && put "$f" "$repo/.claude/$(basename "$f")" "$repo" && note ".claude/$(basename "$f")"; done
            else
                put "$e" "$repo/$name" "$repo"; note "$name"
            fi
        done
    fi

    if [ -d "$bundle/git-hooks" ]; then
        local hd h; hd=$(hooks_dir "$repo")
        for h in "$bundle/git-hooks"/*; do
            put "$h" "$hd/$(basename "$h")"; chmod +x "$hd/$(basename "$h")"
            note "git-хук $(basename "$h")"
        done
    fi

    step "Claude Code → $CLAUDE_HOME"
    if [ -d "$bundle/claude/project" ]; then
        local pdir="$CLAUDE_HOME/projects/$key"
        mkdir -p "$pdir"
        for e in "$bundle/claude/project"/*; do
            [ -e "$e" ] || continue
            put "$e" "$pdir/$(basename "$e")"
        done
        note "память (и сессии, если были) → projects/$key"
    fi
    [ -f "$bundle/claude/settings.json" ]   && put "$bundle/claude/settings.json" "$CLAUDE_HOME/settings.json" && note "settings.json"
    [ -d "$bundle/claude/skills/graphify" ] && put "$bundle/claude/skills/graphify" "$CLAUDE_HOME/skills/graphify" && note "skills/graphify"
    if [ -f "$bundle/claude/ccstatusline/settings.json" ]; then
        put "$bundle/claude/ccstatusline/settings.json" "$CCSTATUS_DIR/settings.json"
        note "ccstatusline (сам пакет: npm install -g ccstatusline)"
    fi

    cmd_fix_paths

    if [ -d "$bundle/pii" ]; then
        step "ПДн"
        warn "в переносе есть $bundle/pii — НЕ в репозиторий: переложите вручную в папку вне него (например D:\\navigator-export)"
    fi

    step "Готово. Дальше — WORK_ON_OTHER_PC.md, «На новом ПК», с шага venv:"
    note "py -3 -m venv .venv && .venv/Scripts/pip install -r requirements.txt"
    note "Qdrant + пересборка коллекций, SSH-ключ, gh auth login, claude → /login"
    note "проверка: .venv/Scripts/python -m unittest discover -s tests"
}

case "${1:-}" in
    pack)      shift; cmd_pack "$@" ;;
    unpack)    shift; cmd_unpack "$@" ;;
    fix-paths) shift; cmd_fix_paths ;;
    *)         usage ;;
esac
