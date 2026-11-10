#!/usr/bin/env python3
"""Починка Vertex AI в Hermes без правки кода (опция A).

Что делает:
  1. Убирает из .env переменные VERTEX_CREDENTIALS_PATH / GOOGLE_APPLICATION_CREDENTIALS.
     Тогда agent/vertex_adapter.py уходит в ADC-ветку (google.auth.default(), строка 136),
     которая читает стандартный файл %APPDATA%\\gcloud\\application_default_credentials.json.
  2. Переставляет проект на тот, где реально включён биллинг (spry-starlight-500514-s7).

Почему это нужно (проверено 2026-10-05, см. artifacts/hermes-stashes-20261005/README.md):
  * VERTEX_CREDENTIALS_PATH указывал на ADC-файл (type=authorized_user), а текущий код
    зовёт service_account.Credentials.from_service_account_info() -> MalformedError.
  * VERTEX_PROJECT_ID=agent-harness-prod отдаёт HTTP 403 «requires billing to be enabled».
  * На spry-starlight-500514-s7 generateContent реально работает (pong от 2.5-flash,
    2.5-pro, 3-flash-preview).

Запуск:
    python hermes-vertex-adc-fix.py            # показать, что будет сделано (dry-run)
    python hermes-vertex-adc-fix.py --apply    # применить (создаёт .bak-<дата>-vertex-adc)
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

NEW_PROJECT = "spry-starlight-500514-s7"
DROP_VARS = ("VERTEX_CREDENTIALS_PATH", "GOOGLE_APPLICATION_CREDENTIALS")
SET_VARS = ("VERTEX_PROJECT_ID", "VERTEX_PROJECT", "GOOGLE_CLOUD_PROJECT")


def hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    return Path(os.environ["LOCALAPPDATA"]) / "hermes"


def target_env_files(home: Path) -> list[Path]:
    files = [home / ".env"]
    profiles = home / "profiles"
    if profiles.is_dir():
        for child in sorted(profiles.iterdir()):
            if child.is_dir():
                env = child / ".env"
                if env.is_file():
                    files.append(env)
    return [f for f in files if f.is_file()]


def plan_for(text: str) -> tuple[str, list[str]]:
    """Вернуть (новый текст, список изменений). Уже починенный файл -> без изменений."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    changes: list[str] = []
    seen_set: set[str] = set()

    for line in lines:
        stripped = line.strip()
        m = re.match(r"^([A-Z_]+)\s*=", stripped)

        if m and m.group(1) in DROP_VARS:
            key = m.group(1)
            if stripped.startswith("#"):
                out.append(line)
                continue
            changes.append(f"    - {key}=<убрано>")
            out.append(f"# [vertex-adc-fix] {line.rstrip()}\n")
            continue

        if m and m.group(1) in SET_VARS:
            key = m.group(1)
            old = stripped.split("=", 1)[1]
            if old.strip() == NEW_PROJECT:
                seen_set.add(key)
                out.append(line)
                continue
            changes.append(f"    ~ {key}: {old.strip() or '<пусто>'} -> {NEW_PROJECT}")
            out.append(f"{key}={NEW_PROJECT}\n")
            seen_set.add(key)
            continue

        out.append(line)

    for key in SET_VARS:
        if key not in seen_set:
            changes.append(f"    + {key}={NEW_PROJECT}  (добавлено)")
            out.append(f"\n{key}={NEW_PROJECT}\n")

    return "".join(out), changes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="реально записать изменения")
    args = ap.parse_args()

    home = hermes_home()
    files = target_env_files(home)
    if not files:
        print(f"НЕ НАЙДЕНО ни одного .env в {home}", file=sys.stderr)
        return 1

    stamp = datetime.now().strftime("%Y%m%d")
    touched = 0

    print(f"HERMES_HOME = {home}")
    print(f"Проект      = {NEW_PROJECT}")
    print(f"Режим       = {'APPLY' if args.apply else 'DRY-RUN (ничего не пишется)'}")
    print()

    for path in files:
        text = path.read_text(encoding="utf-8")
        new_text, changes = plan_for(text)
        rel = path.relative_to(home)
        if not changes:
            print(f"[=] {rel} — уже починено, пропускаю")
            continue

        print(f"[*] {rel}")
        for c in changes:
            print(c)

        if args.apply:
            backup = path.with_name(path.name + f".bak-{stamp}-vertex-adc")
            if not backup.exists():
                shutil.copy2(path, backup)
                print(f"    backup: {backup.name}")
            path.write_text(new_text, encoding="utf-8")
            print("    записано")
        touched += 1
        print()

    if not args.apply:
        print(f"Итого к изменению: {touched} файл(ов). Запусти с --apply, чтобы применить.")
        return 0

    print(f"Готово: изменено {touched} файл(ов).")
    print("Теперь перезапусти гейтвей:  hermes gateway restart")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
