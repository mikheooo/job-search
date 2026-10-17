import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
LEGACY_SCRIPTS = [
    "artifacts/p6/finalize_review.py",
    "artifacts/p6/refresh_manual_qa.py",
    "artifacts/p7/build_report.py",
    "artifacts/p7/collect.py",
    "artifacts/p7/refresh_final.py",
    "artifacts/p7/verify_backup_options.py",
    "artifacts/p7/verify_backups.py",
    "artifacts/p7/verify_candidates.py",
    "artifacts/p7/verify_more.py",
    "artifacts/p7/verify_sales.py",
]


@pytest.mark.parametrize("relative_path", LEGACY_SCRIPTS)
def test_legacy_plaintext_scripts_exit_before_io(relative_path, tmp_path):
    script = ROOT / relative_path
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "disabled" in result.stderr.lower()
    assert not (tmp_path / "artifacts").exists()
