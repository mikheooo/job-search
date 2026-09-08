"""Zone policy for broad exception handling (BLE001 triage, step 3).

203 of the 369 broad `except Exception` handlers in this repo discard the error
entirely. Fixing all of them is not worth it; what matters is that the
*submission path* cannot grow new silent failures.

So instead of a lint rule that either fires everywhere or nowhere, the policy
is enforced here:

  RED ZONE   (submission path) - broad catches stay VISIBLE in ruff, and the
             number of handlers that swallow an error without a trace is
             frozen at a baseline. New silent handlers fail this test.

  GREEN ZONE (CLI, pollers)    - a broad catch is the correct shape, so
             ruff ignores BLE001 there via [lint.per-file-ignores].
             This test asserts that config still says so.

S110 (try-except-pass) is never ignored in any zone: surviving a bad iteration
is fine, swallowing it silently is not.

See docs/ble001_triage.md.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "ai_assistant"
RUFF_TOML = REPO_ROOT / "ruff.toml"

# Anything that can put an application in front of a real employer.
RED_ZONE = {
    "hh_submission.py",
    "hh_application_queue.py",
    "hh_application_runner.py",
    "hh_application_orchestrator.py",
    "submission_recovery.py",
    "submission_state.py",
    "submission_verifier.py",
    "prefill_execute.py",
    "application_integrity.py",
    "application_qa.py",
}

# CLI entry points and long-running pollers: one bad iteration must not kill
# the process. A broad catch is the right shape here.
GREEN_ZONE = {
    "cli.py",
    "runner.py",
    "watcher.py",
    "hh_message_watcher.py",
    "hh_browser_launcher.py",
}

# Frozen 2026-09-09. Count = broad handlers in that file that leave NO trace of
# the exception (no raise, no logger call, exception name never used).
# Lowering a number is a win -- update this baseline when you fix one.
# Raising it is what this test is here to stop.
RED_BASELINE: dict[str, int] = {
    "hh_submission.py": 9,
    "submission_recovery.py": 8,
    "hh_application_runner.py": 2,
    "submission_verifier.py": 3,
    "submission_state.py": 0,
    "application_integrity.py": 2,
    "prefill_execute.py": 0,
    "application_qa.py": 1,
    "hh_application_queue.py": 0,
    "hh_application_orchestrator.py": 0,
}

_LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical", "log"}


def _is_broad(handler: ast.ExceptHandler) -> bool:
    """True for `except Exception`, `except BaseException` and bare `except`."""
    if handler.type is None:
        return True
    if isinstance(handler.type, ast.Name):
        return handler.type.id in {"Exception", "BaseException"}
    if isinstance(handler.type, ast.Tuple):
        return any(
            isinstance(e, ast.Name) and e.id in {"Exception", "BaseException"}
            for e in handler.type.elts
        )
    return False


def _leaves_no_trace(handler: ast.ExceptHandler, alias: str | None) -> bool:
    """True if the handler discards the exception without any record of it."""
    for node in ast.walk(handler):
        if isinstance(node, ast.Raise):
            return False
        if alias and isinstance(node, ast.Name) and node.id == alias:
            return False
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in _LOG_METHODS:
                # logger.warning(...) / logging.error(...) / self._log.exception(...)
                return False
    return True


def _silent_handlers(path: Path) -> list[int]:
    """Line numbers of broad handlers that swallow the error without a trace."""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
    found: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            if _is_broad(handler) and _leaves_no_trace(handler, handler.name):
                found.append(handler.lineno)
    return found


def _load_ruff_config() -> dict:
    return tomllib.loads(RUFF_TOML.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# RED ZONE
# ---------------------------------------------------------------------------


def test_red_zone_files_all_exist() -> None:
    """The zone list is a contract; a renamed file must be updated here too."""
    missing = sorted(name for name in RED_ZONE if not (SRC / name).exists())
    assert not missing, f"RED_ZONE refers to files that no longer exist: {missing}"


@pytest.mark.parametrize("name", sorted(RED_ZONE))
def test_red_zone_has_no_new_silent_handlers(name: str) -> None:
    """A new broad-and-silent handler on the submission path fails the build.

    If you are fixing one of these: this test starts passing as soon as you
    make the handler log, re-raise, or use the exception. Then lower the
    number in RED_BASELINE so the improvement is locked in.
    """
    silent = _silent_handlers(SRC / name)
    baseline = RED_BASELINE[name]
    assert len(silent) <= baseline, (
        f"{name}: {len(silent)} broad handlers swallow the error with no trace, "
        f"baseline was {baseline}. New silent failure on the submission path. "
        f"Handlers at lines {silent}. "
        f"Either make them log / re-raise / use the exception, or justify it in "
        f"docs/ble001_triage.md and update RED_BASELINE."
    )


def test_red_zone_baseline_is_not_stale() -> None:
    """Baseline must name exactly the files in RED_ZONE -- no more, no less."""
    assert set(RED_BASELINE) == RED_ZONE, (
        f"RED_BASELINE and RED_ZONE disagree: "
        f"{set(RED_BASELINE) ^ RED_ZONE}"
    )


# ---------------------------------------------------------------------------
# GREEN ZONE / config
# ---------------------------------------------------------------------------


def test_green_zone_ble001_is_ignored_in_ruff_config() -> None:
    """The per-file-ignores in ruff.toml must still cover the green zone."""
    per_file = _load_ruff_config()["lint"]["per-file-ignores"]
    covered = set()
    for pattern, rules in per_file.items():
        if "BLE001" not in rules:
            continue
        if pattern.endswith("*_watcher.py"):
            covered.update(p.name for p in SRC.glob("*_watcher.py"))
        else:
            covered.add(Path(pattern).name)

    missing = sorted(GREEN_ZONE - covered)
    assert not missing, (
        f"ruff.toml no longer ignores BLE001 for green-zone files: {missing}. "
        f"Either add them to [lint.per-file-ignores] or, if they moved onto the "
        f"submission path, move them to RED_ZONE."
    )


def test_no_zone_ignores_s110() -> None:
    """try-except-pass stays visible everywhere: silence is never the policy."""
    per_file = _load_ruff_config()["lint"]["per-file-ignores"]
    offenders = {pattern: rules for pattern, rules in per_file.items() if "S110" in rules}
    assert not offenders, (
        f"S110 (try-except-pass) must never be ignored, but is ignored for: "
        f"{sorted(offenders)}"
    )


def test_red_zone_is_not_accidentally_ignored() -> None:
    """A red-zone file must never end up in per-file-ignores for BLE001."""
    per_file = _load_ruff_config()["lint"]["per-file-ignores"]
    leaked = []
    for pattern, rules in per_file.items():
        if "BLE001" not in rules:
            continue
        if Path(pattern).name in RED_ZONE:
            leaked.append(pattern)
    assert not leaked, (
        f"These submission-path files ignore BLE001, which hides exactly the "
        f"fail-open handlers we care about: {leaked}"
    )
