"""Паритет ruff до/после: подмена файлов версией из ревизии, замер, восстановление.

Зачем инструмент, а не разовая команда
--------------------------------------
Цена правки в этом проекте измеряется ПОДМЕНОЙ файла на версию из `HEAD` **внутри
репозитория**. Во временном каталоге мерить нельзя: `per-file-ignores` из
`ruff.toml` там не совпадают с репозиторием, и файл, у которого правило снято
конфигом, даёт десятки лишних находок (измерено: `cli.py`, 68 находок).

Ручная подмена опасна ровно одним: восстановлением. Скрипт, который в `finally`
перечитывает файл и пишет прочитанное обратно, **не восстанавливает ничего** - он
записывает ту же подменённую версию. Здесь байты сохраняются в память ДО подмены и
восстанавливаются ИЗ ПАМЯТИ, а хеши печатаются до и после: без совпадения хешей
вердикта нет.

Запуск:
    .venv/Scripts/python.exe tools/ble001_ruff_parity.py ai_assistant/db.py
    .venv/Scripts/python.exe tools/ble001_ruff_parity.py --rev HEAD~1 ai_assistant/db.py ai_assistant/hh_questionnaire.py
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUFF = ["uvx", "ruff@0.16.6"]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ruff_count(target: str) -> int:
    out = subprocess.run(
        [*RUFF, "check", target], cwd=REPO, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    ).stdout
    for line in out.splitlines():
        if line.startswith("Found "):
            return int(line.split()[1])
    return 0


def ruff_ble001(target: str) -> int:
    out = subprocess.run(
        [*RUFF, "check", target, "--select", "BLE001", "--output-format", "concise"],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False,
    ).stdout
    return sum(1 for line in out.splitlines() if "BLE001" in line)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("files", nargs="+", help="пути относительно корня репозитория")
    ap.add_argument("--rev", default="HEAD", help="ревизия для версии «до» (по умолчанию HEAD)")
    args = ap.parse_args(argv)

    paths = [REPO / f for f in args.files]
    for path in paths:
        if not path.is_file():
            print(f"нет файла: {path}")
            return 2

    # Байты «после» - в память. Это и есть восстановление; перечитывание файла
    # восстановлением не является.
    after_bytes = {path: path.read_bytes() for path in paths}
    before_bytes: dict[Path, bytes] = {}
    for path, rel in zip(paths, args.files):
        proc = subprocess.run(
            ["git", "show", f"{args.rev}:{rel}"], cwd=REPO,
            capture_output=True, check=False,
        )
        if proc.returncode != 0:
            print(f"нет {rel} в {args.rev}: {proc.stderr.decode(errors='replace').strip()}")
            return 2
        before_bytes[path] = proc.stdout

    after_hash = {path: hashlib.sha256(data).hexdigest() for path, data in after_bytes.items()}
    print(f"ревизия «до»: {args.rev}")
    for path in paths:
        print(f"  sha256 после  {path.name:32} {after_hash[path]}")

    after_total = ruff_count(".")
    after_ble = {path: ruff_ble001(str(path.relative_to(REPO))) for path in paths}
    print(f"\n  ruff по репозиторию, после: {after_total}")

    before_total = None
    before_ble: dict[Path, int] = {}
    try:
        for path, data in before_bytes.items():
            path.write_bytes(data)
        before_total = ruff_count(".")
        before_ble = {path: ruff_ble001(str(path.relative_to(REPO))) for path in paths}
    finally:
        for path, data in after_bytes.items():
            path.write_bytes(data)

    print(f"  ruff по репозиторию, до   : {before_total}")
    print(f"  дельта                    : {after_total - (before_total or 0):+d}")
    print()
    print(f"  {'файл':32} {'BLE001 до':>10} {'BLE001 после':>13}")
    for path in paths:
        print(f"  {path.name:32} {before_ble.get(path, -1):>10} {after_ble[path]:>13}")

    print()
    ok = True
    for path in paths:
        now = sha(path)
        same = now == after_hash[path]
        ok = ok and same
        print(f"  sha256 восстановления {path.name:32} {now}")
        print(f"    байт-в-байт: {same}")
    if not ok:
        print("\nВЕРДИКТ: дерево не восстановлено, замеру верить нельзя.")
        return 2
    print("\nВЕРДИКТ: дерево восстановлено байт-в-байт.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
