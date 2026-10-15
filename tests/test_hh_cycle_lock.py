"""Phase 3.2 regression tests: HH autopilot cycle lock (competing cycles).

The 20-minute mtime-only takeover could evict a LIVE cycle, doubling submit
limits (max_submits=3 per cycle -> up to 6 parallel). The fixed acquire_lock
checks recorded-PID liveness first. No network, no browser, no prod files:
LOCK_FILE is redirected to tmp_path.
"""
from __future__ import annotations

import importlib.util
import os
import time

import pytest


def _load_runner():
    import pathlib
    path = (
        pathlib.Path(__file__).resolve().parent.parent
        / "scripts"
        / "run_hh_autopilot_cycle.py"
    )
    spec = importlib.util.spec_from_file_location("hh_cycle_runner_under_test", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def runner(tmp_path, monkeypatch):
    mod = _load_runner()
    monkeypatch.setattr(mod, "LOCK_FILE", tmp_path / "autopilot.lock")
    return mod


def test_second_competing_cycle_is_rejected(runner):
    assert runner.acquire_lock() is True
    assert runner.acquire_lock() is False, "parallel cycle must not start"
    runner.release_lock()
    assert runner.acquire_lock() is True, "after release a new cycle may start"
    runner.release_lock()


def test_live_owner_survives_stale_mtime(runner, tmp_path):
    assert runner.acquire_lock() is True
    lock = tmp_path / "autopilot.lock"
    old = time.time() - 3600  # 1 hour old, but owner (this pytest) is alive
    os.utime(lock, (old, old))
    assert runner.acquire_lock() is False, "live process must never lose the lock to age"
    assert lock.exists(), "live owner's lock file must not be unlinked"
    runner.release_lock()


def test_crashed_owner_lock_is_recoverable(runner, tmp_path):
    lock = tmp_path / "autopilot.lock"
    lock.write_text("999999999\n2000-01-01T00:00:00+00:00", encoding="utf-8")
    old = time.time() - 3600
    os.utime(lock, (old, old))
    assert runner.acquire_lock() is True, "dead-PID lock must be recoverable"
    runner.release_lock()
    assert not lock.exists()


def test_corrupt_lock_does_not_start_parallel_cycle(runner, tmp_path):
    (tmp_path / "autopilot.lock").write_text("not-a-pid\n", encoding="utf-8")
    # Corrupt content: safest is to recover (nothing alive can own it),
    # and a second acquire right after must still refuse.
    assert runner.acquire_lock() is True
    assert runner.acquire_lock() is False
    runner.release_lock()
