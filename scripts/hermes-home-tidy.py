"""Move one-off debris out of HERMES_HOME into a quarantine folder.

Conservative by design: an explicit allowlist of filename globs, nothing else is
touched. Live state (state.db and friends, .env, locks, pid files, caches,
JSON state, logs) is never matched. Moving, not deleting -- fully reversible.

Run:  python hermes-home-tidy.py [--apply]
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

HH = Path.home() / "AppData" / "Local" / "hermes"
TRASH = HH / "_trash-20261005"

# --- allowlist -------------------------------------------------------------
PATTERNS: dict[str, list[str]] = {
    "config-backups": [
        "config.yaml.backup*",
        "config.yaml.bak",
        "config.yaml.bak_*",
        "config.yaml.bak-20260917",
        "config.yaml.bak_loore",
    ],
    "one-off-scripts": [
        "check1636.ps1",
        "check_1636. ps1",
        "check_exec.js",
        "check_task.cs",
        "com_update_task.ps1",
        "elevated_update.ps1",
        "export_task.ps1",
        "fix_gateway_xml.py",
        "fix_task.ps1",
        "fix_task_clean.ps1",
        "fix_task_com.ps1",
        "update_task.ps1",
        "update_task_vbs.ps1",
        "update_schtasks.bat",
        "apply_vbs_task.py",
        "UPDATE_Hermes_Gateway.ps1",
        "test_base_pythonw.ps1",
        "test_admin.py",
        "tmp_probe.py",
        "gw_find.py",
        "hermes_check. py",
        "hermes_gateway_check.py",
        "vertex_auth.py",
        "vertex_auth2.py",
        "vertex_auth3.py",
        "publish_test.py",
    ],
    "task-artifacts": [
        "Hermes_Gateway_BACKUP_*.xml",
        "Hermes_Gateway_FINAL.xml",
        "fixed_task.xml",
        "current_task.xml",
    ],
    "debris": [
        "spawn-ledger.json.corrupt",
        "provider_models_cache.json.bak.*",
        ".gateway_state_*.tmp",
        ".provider_models_cache_*.tmp",
        "task_update_result.txt",
        "fix_task.log",
    ],
}

# --- explicit never-touch guard -------------------------------------------
# Exact names first: "config.yaml" must not match "config.yaml.backup.*" via substring.
PROTECTED_EXACT = {
    "config.yaml", "SOUL.md", "auth.json", ".env", "gateway_state.json",
    "gateway.pid", "gateway.lock", ".restart_pending.json", "profile.yaml",
    "install_id", "processes.json", "spawn-ledger.json",
}
PROTECTED_SUBSTRINGS = ("state.db", ".lock", ".pid", "credentials", "token")


def main() -> int:
    apply = "--apply" in sys.argv
    if not HH.is_dir():
        print(f"HERMES_HOME not found: {HH}")
        return 1

    planned: list[tuple[str, Path, Path]] = []
    seen: set[Path] = set()
    for bucket, patterns in PATTERNS.items():
        for pattern in patterns:
            for src in sorted(HH.glob(pattern)):
                if not src.is_file() or src in seen:
                    continue
                seen.add(src)
                if src.name.startswith("config.yaml.bak-20261005-autoprune"):
                    continue  # today's rollback copy -- keep
                if src.name in PROTECTED_EXACT or any(
                    bad in src.name for bad in PROTECTED_SUBSTRINGS
                ):
                    print(f"  SKIP (guard)  {src.name}")
                    continue
                planned.append((bucket, src, TRASH / bucket / src.name))

    if not planned:
        print("Nothing to move.")
        return 0

    total = 0
    for bucket, src, dst in planned:
        total += src.stat().st_size
        print(f"  {bucket:16s} {src.stat().st_size:>8} {src.name}")
        if apply:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))

    print()
    print(f"{len(planned)} file(s), {total / 1024:.0f} KB -> {TRASH}")
    print("APPLIED" if apply else "DRY RUN -- re-run with --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
