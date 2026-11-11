"""Launch the Job-Search Web Dashboard server reliably.

Handles background daemonization, redirecting stdout/stderr if None (pythonw),
preventing port collisions, and logging to logs/dashboard.log.
"""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

# Add DLL directories for Windows CPython distributions (e.g. Astral uv)
if sys.platform == "win32":
    for p in [
        Path(sys.executable).parent / "DLLs",
        Path(r"C:\Users\Misha\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\DLLs"),
    ]:
        if p.exists():
            try:
                os.add_dll_directory(str(p))
            except Exception:
                pass

# Ensure root directory is on sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Ensure site-packages from .venv is on sys.path
VENV_PACKAGES = ROOT_DIR / ".venv" / "Lib" / "site-packages"
if VENV_PACKAGES.exists() and str(VENV_PACKAGES) not in sys.path:
    sys.path.insert(0, str(VENV_PACKAGES))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT_DIR / ".env")
except Exception:
    pass

LOGS_DIR = ROOT_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOGS_DIR / "dashboard.log"

# Fix for pythonw.exe where stdout / stderr are None
if sys.stdout is None or sys.stderr is None:
    try:
        log_fp = open(LOG_FILE, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        if sys.stdout is None:
            sys.stdout = log_fp
        if sys.stderr is None:
            sys.stderr = log_fp
    except Exception:
        pass


def is_port_in_use(port: int = 8001, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect((host, port))
            return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            return False


def is_job_search_dashboard(port: int = 8001) -> bool:
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/stats")
        with opener.open(req, timeout=1.5) as resp:
            return resp.status == 200
    except Exception:
        return False


def main() -> int:
    port = int(os.getenv("DASHBOARD_PORT", "8001"))
    host = os.getenv("DASHBOARD_HOST", "0.0.0.0")

    if is_port_in_use(port, "127.0.0.1"):
        if is_job_search_dashboard(port):
            print(f"Job Search Dashboard is already listening and responsive on port {port}.")
            return 0
        else:
            print(f"Port {port} is occupied. Searching for an available port...")
            found_port = None
            for candidate in range(8002, 8025):
                if is_port_in_use(candidate, "127.0.0.1"):
                    if is_job_search_dashboard(candidate):
                        print(f"Job Search Dashboard is already active on port {candidate}.")
                        return 0
                else:
                    found_port = candidate
                    break
            if found_port is not None:
                port = found_port
            else:
                print("Could not find an available port in range 8002-8024.")
                return 1

    print(f"Starting Job Search Dashboard on http://{host}:{port}...")
    try:
        import uvicorn
        uvicorn.run(
            "ai_assistant.ui.app:app",
            host=host,
            port=port,
            reload=False,
            log_level="info",
            use_colors=False,
        )
        return 0
    except Exception as e:
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
