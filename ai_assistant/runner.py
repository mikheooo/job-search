"""Production execution runner, single-instance lock, log management, and failure tracking (Stage 83)."""
import datetime
import json
import os
import subprocess
import sys
import time
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import config


ADAPTER_FAILURE_RE = re.compile(r"Adapter\s+([^\r\n:]+)\s+fetch error:\s*([^\r\n]+)", re.IGNORECASE)
PRODUCTION_CIRCUIT_OPEN_EXIT_CODE = 5


def _tracker_defaults() -> Dict[str, Any]:
    """Return the backward-compatible persistent production tracker schema."""
    return {
        "consecutive_failures": 0,
        "last_success_at": None,
        "last_failure_at": None,
        "last_error": None,
        "circuit_opened_at": None,
        "last_operator_resume_at": None,
    }


def extract_adapter_failures(output: str) -> list[str]:
    """Extract per-adapter failures emitted by the canonical watcher."""
    failures = []
    for adapter, reason in ADAPTER_FAILURE_RE.findall(output or ""):
        failures.append(f"{adapter.strip()}: {reason.strip()}")
    return failures


def is_pid_running(pid: int) -> bool:
    """Check whether a given PID is currently active on the host OS."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            exit_code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return exit_code.value == STILL_ACTIVE
        except Exception:
            try:
                out = subprocess.check_output(f"tasklist /FI \"PID eq {pid}\"", shell=True, text=True, errors="replace")
                return str(pid) in out
            except Exception:
                return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def mask_secrets(text: str) -> str:
    """Sanitize secrets, tokens, and credentials from log text."""
    if not text:
        return ""
    import re
    # Mask bot tokens e.g. bot123456:ABC-DEF...
    masked = re.sub(r'bot\d+:[A-Za-z0-9_-]{25,}', 'bot<MASKED_TELEGRAM_TOKEN>', text)
    masked = re.sub(r'TELEGRAM_BOT_TOKEN=[^\s\r\n]+', 'TELEGRAM_BOT_TOKEN=<MASKED>', masked)
    masked = re.sub(r'Bearer\s+[A-Za-z0-9_\-\.]{20,}', 'Bearer <MASKED_TOKEN>', masked)
    return masked


class SingleInstanceLock:
    """Guarantees at most one production job-search process runs concurrently."""

    def __init__(self, lock_dir: Optional[str] = None, lock_name: str = "job_search.lock", stale_seconds: int = 1800):
        self.lock_dir = lock_dir or config.LOGS_DIR
        self.lock_file = os.path.join(self.lock_dir, lock_name)
        self.stale_seconds = stale_seconds
        self._acquired = False

    def acquire(self) -> Tuple[bool, str]:
        """Try to acquire process lock.
        
        Returns:
            (True, "ACQUIRED") if lock obtained.
            (False, "SKIPPED_ALREADY_RUNNING") if another active instance owns the lock.
        """
        os.makedirs(self.lock_dir, exist_ok=True)
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        
        if os.path.exists(self.lock_file):
            try:
                with open(self.lock_file, "r", encoding="utf-8") as f:
                    lock_data = json.load(f)
                locked_pid = lock_data.get("pid", 0)
                locked_at_str = lock_data.get("timestamp", "")
                
                # Check if process is actively running
                if locked_pid and is_pid_running(locked_pid):
                    return False, f"SKIPPED_ALREADY_RUNNING: Active PID {locked_pid} owns lock (started at {locked_at_str})."
                
                # If PID is dead -> stale lock recovery
                print(f"[STAGE 83] Stale lock detected (dead PID {locked_pid}). Recovering lock file.", file=sys.stderr)
            except Exception as e:
                print(f"[STAGE 83] Corrupted lock file encountered ({e}). Overwriting.", file=sys.stderr)

        # Write current PID and timestamp
        lock_info = {
            "pid": os.getpid(),
            "timestamp": now_dt.isoformat(),
            "host": os.getenv("COMPUTERNAME", "localhost"),
        }
        try:
            with open(self.lock_file, "w", encoding="utf-8") as f:
                json.dump(lock_info, f, indent=2)
            self._acquired = True
            return True, "ACQUIRED"
        except Exception as write_err:
            return False, f"FAILED_LOCK_WRITE: {write_err}"

    def release(self) -> bool:
        """Release lock file if owned by this process."""
        if os.path.exists(self.lock_file):
            try:
                with open(self.lock_file, "r", encoding="utf-8") as f:
                    lock_data = json.load(f)
                if lock_data.get("pid") == os.getpid() or not self._acquired:
                    os.remove(self.lock_file)
                    self._acquired = False
                    return True
            except Exception:
                try:
                    os.remove(self.lock_file)
                    self._acquired = False
                    return True
                except Exception:
                    pass
        return False


class ConsecutiveFailureTracker:
    """Track production failures and persist the fail-closed circuit state."""

    def __init__(self, storage_dir: Optional[str] = None):
        self.storage_dir = storage_dir or config.LOGS_DIR
        self.file_path = os.path.join(self.storage_dir, "failure_tracker.json")

    def _read_data(self) -> Dict[str, Any]:
        data = _tracker_defaults()
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    stored = json.load(f)
                if isinstance(stored, dict):
                    data.update(stored)
            except Exception:
                pass
        return data

    def _write_data(self, data: Dict[str, Any]) -> None:
        os.makedirs(self.storage_dir, exist_ok=True)
        try:
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def get_consecutive_failures(self) -> int:
        return self._read_data().get("consecutive_failures", 0)

    def get_status(self) -> Dict[str, Any]:
        data = self._read_data()
        data["circuit_open"] = self.is_circuit_open()
        data["circuit_threshold"] = config.PRODUCTION_FAILURE_ALERT_THRESHOLD
        return data

    def is_circuit_open(self, threshold: Optional[int] = None) -> bool:
        """Return True once tripped; only an explicit operator resume unlatches it."""
        limit = threshold or config.PRODUCTION_FAILURE_ALERT_THRESHOLD
        data = self._read_data()
        return bool(data.get("circuit_opened_at")) or data.get("consecutive_failures", 0) >= max(1, int(limit))

    def record_success(self) -> None:
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        data = self._read_data()
        data["consecutive_failures"] = 0
        data["last_success_at"] = now_iso
        data["last_error"] = None
        data["circuit_opened_at"] = None
        self._write_data(data)

    def record_failure(self, error: str = "", threshold: Optional[int] = None) -> int:
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        data = self._read_data()
        data["consecutive_failures"] = data.get("consecutive_failures", 0) + 1
        data["last_failure_at"] = now_iso
        data["last_error"] = error
        limit = threshold or config.PRODUCTION_FAILURE_ALERT_THRESHOLD
        if data["consecutive_failures"] >= max(1, int(limit)) and not data.get("circuit_opened_at"):
            data["circuit_opened_at"] = now_iso
        self._write_data(data)
        return data["consecutive_failures"]

    def resume_after_operator_review(self) -> Dict[str, Any]:
        """Close the circuit explicitly while preserving the last failure evidence."""
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        data = self._read_data()
        data["consecutive_failures"] = 0
        data["circuit_opened_at"] = None
        data["last_operator_resume_at"] = now_iso
        self._write_data(data)
        return self.get_status()


def rotate_log_if_needed(log_path: str, max_size_bytes: int = 5 * 1024 * 1024, max_backups: int = 5) -> None:
    """Rotate log file if it exceeds max size."""
    if not os.path.exists(log_path):
        return
    try:
        if os.path.getsize(log_path) < max_size_bytes:
            return
        for i in range(max_backups - 1, 0, -1):
            s_fn = f"{log_path}.{i}"
            d_fn = f"{log_path}.{i + 1}"
            if os.path.exists(s_fn):
                os.replace(s_fn, d_fn)
        os.replace(log_path, f"{log_path}.1")
    except Exception:
        pass


def run_production_pipeline(
    fetcher_script: Optional[str] = None,
    dry_run: bool = False,
    logs_dir: Optional[str] = None,
) -> int:
    """Canonical production execution wrapper (Stage 83)."""
    target_logs_dir = logs_dir or config.LOGS_DIR
    os.makedirs(target_logs_dir, exist_ok=True)
    log_file = os.path.join(target_logs_dir, "job_search_production.log")
    
    rotate_log_if_needed(log_file)
    
    lock = SingleInstanceLock(lock_dir=target_logs_dir)
    acquired, msg = lock.acquire()
    if not acquired:
        print(f"[INFO] {msg}")
        return 0  # SKIPPED_ALREADY_RUNNING is a safe no-op for scheduler

    script_path = fetcher_script or os.path.expanduser(r"~\AppData\Local\hermes\profiles\jobs\scripts\job_search_fetcher.py")
    python_exe = sys.executable

    start_time = time.time()
    start_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    pid = os.getpid()

    tracker = ConsecutiveFailureTracker(storage_dir=target_logs_dir)

    header = (
        f"\n==================== RUN START ====================\n"
        f"timestamp: {start_iso}\n"
        f"pid: {pid}\n"
        f"command: {python_exe} {script_path}\n"
        f"working_dir: {config.PROJECT_ROOT}\n"
        f"---------------------------------------------------\n"
    )
    
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(header)

    if not dry_run and tracker.is_circuit_open():
        threshold = config.PRODUCTION_FAILURE_ALERT_THRESHOLD
        failures = tracker.get_consecutive_failures()
        message = (
            "[SAFETY] PRODUCTION_CIRCUIT_OPEN: "
            f"circuit remains latched after {failures} consecutive failures "
            f"(configured threshold: {threshold}). "
            "Live production execution is blocked. Review production-health, "
            "run an offline --dry-run probe, then explicitly run "
            "'production-control resume'.\n"
        )
        end_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(
                message
                + "---------------------------------------------------\n"
                + f"RUN END: {end_iso} | exit_code: {PRODUCTION_CIRCUIT_OPEN_EXIT_CODE} | duration: 0.0s\n"
                + "===================================================\n"
            )
        lock.release()
        print(message, end="")
        return PRODUCTION_CIRCUIT_OPEN_EXIT_CODE

    env = os.environ.copy()
    if dry_run:
        env["JOB_SEARCH_DRY_RUN"] = "1"

    exit_code = 0
    captured_output = ""
    try:
        proc = subprocess.run(
            [python_exe, script_path],
            cwd=config.PROJECT_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        exit_code = proc.returncode
        captured_output = proc.stdout or ""
        adapter_failures = extract_adapter_failures(captured_output)
        if adapter_failures and exit_code == 0:
            exit_code = 4
            captured_output += (
                "\n[PRODUCTION FAILURE] One or more vacancy adapters failed; "
                "overriding dispatcher SUCCESS.\n"
                + "\n".join(f"  - {failure}" for failure in adapter_failures)
                + "\n"
            )
    except Exception as e:
        exit_code = 99
        captured_output = f"[CRITICAL RUNNER ERROR] Failed to spawn process: {e}\n"

    duration = round(time.time() - start_time, 2)
    end_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    sanitized_output = mask_secrets(captured_output)

    footer = (
        f"{sanitized_output}"
        f"---------------------------------------------------\n"
        f"RUN END: {end_iso} | exit_code: {exit_code} | duration: {duration}s\n"
        f"===================================================\n"
    )

    with open(log_file, "a", encoding="utf-8") as f:
        f.write(footer)

    # A dry-run is an offline probe, not evidence that live production recovered.
    if not dry_run:
        if exit_code == 0:
            tracker.record_success()
        else:
            failures = extract_adapter_failures(captured_output)
            detail = "; ".join(failures) if failures else f"Exit code {exit_code}"
            tracker.record_failure(error=detail)

    lock.release()
    print(sanitized_output, end="")
    print(f"[STAGE 83] Production run completed (exit_code: {exit_code}, duration: {duration}s).")
    return exit_code
