"""Phase 3 regression tests: Telegram control-bot single-instance + heartbeat.

All network is mocked; the global conftest blocks real urlopen. Lock and
heartbeat paths are redirected to tmp_path — production logs/DB untouched.
"""
from __future__ import annotations

import io
import json
import os
import threading
import time
import urllib.request

import pytest

import ai_assistant.telegram_control_bot as bot
from ai_assistant.runner import SingleInstanceLock


class _StopTest(BaseException):
    pass


def _resp(payload: dict) -> io.BytesIO:
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


class _FakeHTTP:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, req, *args, **kwargs):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        self.calls.append(url)
        if "sendMessage" in url:
            return _FakeCtx({"ok": True, "result": {"message_id": 1}})
        assert "getUpdates" in url, url
        action = self.script.pop(0) if self.script else "empty"
        if action == "stop":
            raise _StopTest()
        return _FakeCtx({"ok": True, "result": []})


class _FakeCtx:
    def __init__(self, payload):
        self._buf = _resp(payload)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._buf.read()


def _patch_bot_paths(monkeypatch, tmp_path):
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    monkeypatch.setattr(bot, "BOT_LOCK_DIR", str(lock_dir))
    monkeypatch.setattr(bot, "BOT_HEARTBEAT_FILE", str(tmp_path / "bot.heartbeat"))
    return lock_dir


def test_single_polling_loop_writes_heartbeat_and_sends_one_startup(monkeypatch, tmp_path):
    _patch_bot_paths(monkeypatch, tmp_path)
    fake = _FakeHTTP(["empty", "empty", "stop"])
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    with pytest.raises(_StopTest):
        bot.run_bot_polling()

    sends = [u for u in fake.calls if "sendMessage" in u]
    polls = [u for u in fake.calls if "getUpdates" in u]
    assert len(sends) == 1, f"startup message must be sent exactly once, got {len(sends)}"
    assert len(polls) == 3, f"expected 3 polls, got {len(polls)}"

    hb = json.loads((tmp_path / "bot.heartbeat").read_text(encoding="utf-8"))
    assert hb["pid"] == os.getpid()
    assert hb["timestamp"], "heartbeat must carry a timestamp"


def test_second_concurrent_poller_exits_3(monkeypatch, tmp_path):
    _patch_bot_paths(monkeypatch, tmp_path)
    stop = threading.Event()

    def spinning_urlopen(req, *args, **kwargs):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if stop.is_set():
            raise _StopTest()
        if "sendMessage" in url:
            return _FakeCtx({"ok": True, "result": {"message_id": 1}})
        return _FakeCtx({"ok": True, "result": []})

    monkeypatch.setattr(urllib.request, "urlopen", spinning_urlopen)
    _real_sleep = time.sleep
    monkeypatch.setattr(time, "sleep", lambda s: _real_sleep(0.01))

    def _worker():
        try:
            bot.run_bot_polling()
        except _StopTest:
            pass  # test-only stop signal, not a production path

    t = threading.Thread(target=_worker)
    t.start()
    try:
        # Wait until the first instance holds the lock.
        deadline = time.time() + 15
        lock_file = tmp_path / "locks" / bot.BOT_LOCK_NAME
        while time.time() < deadline and not lock_file.exists():
            time.sleep(0.05)
        assert lock_file.exists(), "first instance never acquired the lock"

        with pytest.raises(SystemExit) as exc:
            bot.run_bot_polling()
        assert exc.value.code == 3, f"second instance must exit(3), got {exc.value.code}"
    finally:
        stop.set()
        t.join(timeout=30)
        assert not t.is_alive(), "first instance did not stop"


def test_atomic_race_allows_exactly_one_poller(monkeypatch, tmp_path):
    """Phase 4A: N same-second starters -> 1 runs, N-1 exit(3). No dupes."""
    _patch_bot_paths(monkeypatch, tmp_path)
    stop = threading.Event()
    startups = []
    exits = []
    gate = threading.Barrier(3)

    def racing_urlopen(req, *args, **kwargs):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if stop.is_set():
            raise _StopTest()
        if "sendMessage" in url:
            startups.append(url)
            return _FakeCtx({"ok": True, "result": {"message_id": 1}})
        return _FakeCtx({"ok": True, "result": []})

    monkeypatch.setattr(urllib.request, "urlopen", racing_urlopen)
    _real_sleep = time.sleep
    monkeypatch.setattr(time, "sleep", lambda s: _real_sleep(0.01))

    def _racer():
        gate.wait(timeout=15)  # release all racers in the same instant
        try:
            bot.run_bot_polling()
        except _StopTest:
            pass
        except SystemExit as e:
            exits.append(e.code)

    threads = [threading.Thread(target=_racer) for _ in range(3)]
    for t in threads:
        t.start()
    try:
        deadline = time.time() + 20
        while time.time() < deadline and len(startups) + len(exits) < 3:
            _real_sleep(0.05)
        assert len(startups) == 1, f"exactly one poller must start, got {len(startups)}"
        assert sorted(exits) == [3, 3], f"losers must exit(3), got {exits}"
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=30)
        assert not any(t.is_alive() for t in threads)


def test_empty_win_file_waits_for_winner(tmp_path, monkeypatch):
    """Phase 4C.4: empty guard file (winner mid-write) must not be mistaken
    for stale. A live winner -> loser exits 3; the file is never unlinked
    under a live owner."""
    import os as _os

    import ai_assistant.telegram_control_bot as bot

    win = tmp_path / "telegram_control_bot.lock.win"
    win.write_text("", encoding="utf-8")  # winner created, hasn't written yet
    monkeypatch.setattr(bot, "BOT_LOCK_DIR", str(tmp_path))
    monkeypatch.setattr(bot, "BOT_HEARTBEAT_FILE", str(tmp_path / "hb"))

    import urllib.request as _url

    monkeypatch.setattr(_url, "urlopen", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must exit before any network")))
    # Occupy the win file as the live winner would (our own PID = live).
    win.write_text(f"{_os.getpid()}\n2026-01-01T00:00:00+00:00", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        bot.run_bot_polling()
    assert exc.value.code == 3
    assert win.exists(), "live owner's guard must survive"


def test_poll_requests_message_updates(tmp_path, monkeypatch):
    """Phase 4C.5: getUpdates must explicitly allow message updates.

    The account carries a legacy server-side allow-list of
    ["callback_query"] which silently drops every text command.
    """
    import urllib.request as _url

    import ai_assistant.telegram_control_bot as bot

    seen: list[str] = []

    class _StopTest(BaseException):
        pass

    def _capture(req, *args, **kwargs):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "getUpdates" in url:
            seen.append(url)
            raise _StopTest()
        return _FakeCtx({"ok": True, "result": {"message_id": 1}})

    monkeypatch.setattr(bot, "BOT_LOCK_DIR", str(tmp_path))
    monkeypatch.setattr(bot, "BOT_HEARTBEAT_FILE", str(tmp_path / "hb"))
    monkeypatch.setattr(_url, "urlopen", _capture)
    with pytest.raises(_StopTest):
        bot.run_bot_polling()
    assert seen, "poll must hit getUpdates"
    assert "allowed_updates" in seen[0]
    assert "message" in seen[0] and "callback_query" in seen[0]


def test_stale_lock_is_recoverable(tmp_path):
    lock = SingleInstanceLock(lock_dir=str(tmp_path), lock_name="probe.lock")
    # Simulate a crashed predecessor: lock file with a dead PID.
    (tmp_path / "probe.lock").write_text(
        json.dumps({"pid": 999999999, "timestamp": "2000-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    ok, reason = lock.acquire()
    assert ok, f"stale lock must be recoverable, got: {reason}"
    assert lock.release()
    assert not (tmp_path / "probe.lock").exists()
