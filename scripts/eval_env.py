"""Локальная среда замера: поднять Qdrant из снапшотов и прогнать по нему серию.

ЗАЧЕМ. Прогон 16.08.2026 выполнялся на боевой VM и положил сервис: полный свип уровня 1 —
это ~5500 прогонов e5-large на двух ядрах, `/ping` уходил в таймаут на 25–30 с, SSH рвался на
banner exchange. Замер шёл 40 минут, и всё это время пользователи получили бы недоступность.
Сошло с рук только потому, что рассылка волны ещё не ушла и обращений в окне было ноль.

Правильное место замера — отдельный инстанс из СНАПШОТОВ той же коллекции. Рецепт уже был
добыт в `DEPLOY_MANIFEST.md` (⚠ снапшот нельзя снимать с локального Qdrant на bind-mount NTFS:
`wal/first-index` попадает в него шестнадцатью нулевыми байтами, и восстановление падает с
`Can't init WAL`, при этом checksum сходится). Здесь он оформлен скриптом, чтобы не
восстанавливать по памяти.

Что делает:
  * поднимает контейнер Qdrant на своём порту со storage в DOCKER VOLUME (не bind-mount);
  * восстанавливает в него коллекции из .snapshot / .snapshot.gz;
  * печатает готовую строку окружения для запуска замеров.

Запуск:
  python scripts/eval_env.py up   --snapshots D:/navigator-deploy/2026-08-16 --port 6534
  python scripts/eval_env.py down --port 6534
"""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

CONTAINER = "qdrant-eval"
VOLUME = "qdrant_eval_vol"
# Версия фиксируется намеренно: снапшоты между мажорными версиями Qdrant не переносимы, а
# compose на VM тянет `latest` — расхождение обнаружилось бы уже при восстановлении.
IMAGE = "qdrant/qdrant:v1.18.2"


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    print("  $", " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.stdout.strip():
        print("   ", r.stdout.strip()[:400])
    if check and r.returncode != 0:
        sys.exit(f"команда не удалась: {r.stderr.strip()[:400]}")
    return r


def _wait_ready(port: int, timeout: float = 90) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://localhost:{port}/collections", timeout=3).read()
            print(f"  Qdrant на :{port} отвечает")
            return
        except Exception:  # noqa: BLE001 — ждём подъёма
            time.sleep(2)
    sys.exit(f"Qdrant на :{port} не поднялся за {timeout:.0f} с")


def up(snapshots: Path, port: int) -> None:
    _run(["docker", "rm", "-f", CONTAINER], check=False)
    _run(["docker", "volume", "create", VOLUME])
    _run(["docker", "run", "-d", "--name", CONTAINER, "-p", f"{port}:6333",
          "-v", f"{VOLUME}:/qdrant/storage", IMAGE])
    _wait_ready(port)

    files = sorted(list(snapshots.glob("*.snapshot")) + list(snapshots.glob("*.snapshot.gz")))
    if not files:
        sys.exit(f"в {snapshots} нет файлов *.snapshot(.gz) — нечего восстанавливать")

    for f in files:
        raw = f
        if f.suffix == ".gz":  # распаковываем рядом, оригинал не трогаем
            raw = f.with_suffix("")
            if not raw.exists():
                print(f"  распаковываю {f.name}")
                with gzip.open(f, "rb") as src, open(raw, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        collection = raw.name.split("-")[0]
        print(f"  восстанавливаю «{collection}» из {raw.name}")
        _run(["docker", "exec", CONTAINER, "mkdir", "-p", f"/qdrant/snapshots/{collection}"])
        _run(["docker", "cp", str(raw), f"{CONTAINER}:/qdrant/snapshots/{collection}/{raw.name}"])
        req = urllib.request.Request(
            f"http://localhost:{port}/collections/{collection}/snapshots/recover",
            data=json.dumps({"location": f"file:///qdrant/snapshots/{collection}/{raw.name}"}).encode(),
            headers={"Content-Type": "application/json"}, method="PUT")
        print("   ", urllib.request.urlopen(req, timeout=1800).read().decode()[:160])

    print("\nКоллекции в среде замера:")
    data = json.load(urllib.request.urlopen(f"http://localhost:{port}/collections", timeout=30))
    for c in sorted(x["name"] for x in data["result"]["collections"]):
        info = json.load(urllib.request.urlopen(
            f"http://localhost:{port}/collections/{c}", timeout=30))["result"]
        print(f"   {c}: {info['points_count']} точек, {info['status']}")

    print("\nГотово. Запускать замеры так (Windows PowerShell):")
    print(f'  $env:QDRANT_URL="http://localhost:{port}"; '
          f'$env:QDRANT_CASES_COLLECTION="__eval_disabled__"; '
          f'.venv\\Scripts\\python.exe scripts\\eval_coverage.py --repeat 3')
    print("  (кейсы выключены — по гайду замер не должен идти по собственным подсказкам)")


def down(port: int) -> None:
    _run(["docker", "rm", "-f", CONTAINER], check=False)
    print(f"среда замера остановлена (том {VOLUME} оставлен — удалить: docker volume rm {VOLUME})")


def main() -> None:
    ap = argparse.ArgumentParser(description="Среда замера из снапшотов (не трогая бой)")
    ap.add_argument("action", choices=["up", "down"])
    ap.add_argument("--snapshots", type=Path, default=Path("."),
                    help="папка со снапшотами коллекций (*.snapshot или *.snapshot.gz)")
    ap.add_argument("--port", type=int, default=6534)
    args = ap.parse_args()
    if args.action == "up":
        up(args.snapshots, args.port)
    else:
        down(args.port)


if __name__ == "__main__":
    main()
