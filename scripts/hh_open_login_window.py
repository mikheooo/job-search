"""Открыть ВИДИМОЕ окно нашего headless-профиля (JobSearchHeadless),
проверить есть ли авторизация в HH и при необходимости подождать ручного входа.

Режимы:
- По умолчанию (или двойным кликом по ярлыку):
  1. Освобождает профиль JobSearchHeadless от фоновых headless-процессов
     и ставит лок logs/autopilot.lock, чтобы демоны не мешали.
  2. Открывает видимое окно Chrome на https://hh.ru/applicant/resumes.
  3. Проверяет авторизацию в HH:
     - Если УЖЕ авторизован — пишет "✅ АВТОРИЗАЦИЯ ЕСТЬ", оставляет окно
       открытым пока ты сам его не закроешь (или нажмёшь Enter в консоли).
     - Если НЕ авторизован — пишет "❌ АВТОРИЗАЦИИ НЕТ", переводит вкладку
       на https://hh.ru/account/login и ждёт, пока ты войдёшь. Как только
       вход выполнен — сообщает "✅ ВХОД ВЫПОЛНЕН!" и закрывает окно (или
       ждёт закрытия).
- --check-only:
  Проверяет авторизацию без открытия видимого окна (в фоне) и выводит статус.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

if sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
if sys.stderr is not None:
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from ai_assistant import hh_browser_provider as provider  # noqa: E402

CHECK_URL = "https://hh.ru/applicant/resumes"
LOGIN_URL = "https://hh.ru/account/login"
POLL_EVERY = 3.0
MAX_WAIT = 600.0  # 10 минут на вход
AUTOPILOT_LOCK = REPO_ROOT / "logs" / "autopilot.lock"
AUTH_ALERT_MARKER = REPO_ROOT / "logs" / "hh_auth_alert.sent"


def _current_tab(cdp_url: str):
    """Живая вкладка hh.ru, если она есть."""
    try:
        tabs = provider.list_tabs(cdp_url)
    except Exception as exc:  # noqa: BLE001
        return None, f"list_tabs упал: {type(exc).__name__}: {exc}"
    for tab in tabs:
        if tab.get("type") == "page" and "hh.ru" in (tab.get("url") or ""):
            return tab, None
    return None, "вкладки hh.ru нет"


def _verdict(cdp_url: str):
    tab, err = _current_tab(cdp_url)
    if tab is None:
        return None, err
    try:
        ev = provider.make_target_evaluate(cdp_url, tab["id"])
        return provider.check_hh_authentication(ev), None
    except Exception as exc:  # noqa: BLE001
        return None, f"проверка авторизации упала: {type(exc).__name__}: {exc}"


def _acquire_autopilot_lock() -> bool:
    """Поставить лок автопилота, чтобы hh_realtime_daemon и hh_autopilot уступили профиль."""
    try:
        AUTOPILOT_LOCK.parent.mkdir(parents=True, exist_ok=True)
        AUTOPILOT_LOCK.write_text(
            f"{os.getpid()}\n{datetime.datetime.now().isoformat()}",
            encoding="utf-8",
        )
        return True
    except OSError:
        return False


def _release_autopilot_lock() -> None:
    try:
        if AUTOPILOT_LOCK.exists():
            lines = AUTOPILOT_LOCK.read_text(encoding="utf-8").splitlines()
            if lines and lines[0].strip() == str(os.getpid()):
                AUTOPILOT_LOCK.unlink(missing_ok=True)
    except OSError:
        pass


def _free_profile_and_port(cdp_url: str, port: int, profile: Path) -> None:
    """Завершить все процессы, удерживающие профиль JobSearchHeadless."""
    if provider.port_is_listening(port):
        provider.quit_browser(cdp_url)
        provider.wait_port_free(port, timeout=4.0)

    holders = provider.dedicated_profile_processes(profile)
    for h in holders:
        provider.terminate_pid(int(h["pid"]), timeout=3.0)

    provider.wait_port_free(port, timeout=5.0)


def _check_only(cdp_url: str, port: int, profile: Path) -> int:
    print("Проверка авторизации HH в профиле JobSearchHeadless (без окна)...")
    _acquire_autopilot_lock()
    try:
        _free_profile_and_port(cdp_url, port, profile)
        _acquire_autopilot_lock()
        launch = provider.launch_headless(
            url=CHECK_URL,
            port=port,
            profile_dir=profile,
            headless=True,
        )
        if not launch.get("ok"):
            print(f"❌ Не удалось запустить фоновую проверку: {launch.get('error')}")
            return 2
        time.sleep(4.0)
        verdict, err = _verdict(cdp_url)
        if err:
            print(f"⚠️ Ошибка проверки: {err}")
            return 2
        if verdict and verdict.get("authenticated"):
            print("=" * 66)
            print("  ✅ АВТОРИЗАЦИЯ ЕСТЬ (профиль JobSearchHeadless залогинен в HH)")
            print(f"  Детали: {verdict.get('reason')}")
            print(f"  URL:    {(verdict.get('evidence') or {}).get('url')}")
            print("=" * 66)
            return 0
        print("=" * 66)
        print("  ❌ АВТОРИЗАЦИИ НЕТ (нужен вход в HH)")
        print(f"  Причина: {(verdict or {}).get('reason')}")
        print("=" * 66)
        return 1
    finally:
        if provider.port_is_listening(port):
            provider.quit_browser(cdp_url)
            provider.wait_port_free(port, timeout=4.0)
        _release_autopilot_lock()


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверка и вход в HH для профиля JobSearchHeadless")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Только проверить авторизацию в фоне без открытия окна",
    )
    parser.add_argument(
        "--keep-open",
        action="store_true",
        help="Не закрывать браузер автоматически после успешного входа (ждать закрытия окна руками)",
    )
    args = parser.parse_args()

    cdp_url = provider.headless_cdp_url()
    port = urlparse(cdp_url).port or provider.HEADLESS_CDP_PORT
    profile = provider.headless_profile_dir()

    if args.check_only:
        return _check_only(cdp_url, port, profile)

    chrome_exe = provider.find_headless_chrome()
    if not chrome_exe:
        print("Не найден Google Chrome в стандартных путях.")
        return 2

    _acquire_autopilot_lock()
    proc: subprocess.Popen | None = None
    try:
        print("1/3 Освобождаю профиль JobSearchHeadless от фоновых процессов...")
        _free_profile_and_port(cdp_url, port, profile)
        _acquire_autopilot_lock()

        argv = [
            str(chrome_exe),
            "--start-maximized",
            "--new-window",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            CHECK_URL,
        ]

        print(f"2/3 Открываю видимое окно Chrome (профиль: {profile})...")
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        deadline_cdp = time.time() + 15.0
        cdp_up = False
        while time.time() < deadline_cdp:
            if provider.cdp_version(cdp_url, timeout=1.0):
                cdp_up = True
                break
            time.sleep(0.5)

        if not cdp_up:
            print(f"❌ Не удалось дождаться ответа CDP на {cdp_url} (pid={proc.pid}).")
            return 3

        print("3/3 Проверяю статус авторизации на hh.ru...")
        time.sleep(3.5)
        verdict, err = _verdict(cdp_url)

        if verdict and verdict.get("authenticated"):
            try:
                AUTH_ALERT_MARKER.unlink(missing_ok=True)
            except OSError:
                pass
            print()
            print("=" * 66)
            print("  ✅ АВТОРИЗАЦИЯ ЕСТЬ! Профиль JobSearchHeadless активен.")
            print(f"  Статус: {verdict.get('reason')}")
            print(f"  URL:    {(verdict.get('evidence') or {}).get('url')}")
            print("=" * 66)
            print("  Можешь посмотреть открытое окно Chrome и закрыть его, когда закончишь.")
            print("  (Пока окно открыто, фоновые демоны ждут и не трогают профиль.)")
            while proc.poll() is None and provider.port_is_listening(port):
                _acquire_autopilot_lock()
                time.sleep(2.0)
            print("Окно закрыто, профиль передан обратно фоновым демонам.")
            return 0

        print()
        print("=" * 66)
        print("  ❌ АВТОРИЗАЦИИ НЕТ — ТРЕБУЕТСЯ ВХОД В HH")
        print(f"  Детали: {(verdict or {}).get('reason') or err}")
        print("=" * 66)
        print("  Войди в аккаунт HH в открывшемся окне Chrome (телефон/почта + код).")
        print("  Скрипт проверяет статус каждые 3 секунды...")
        print("=" * 66)

        deadline = time.time() + MAX_WAIT
        last_reason = ""
        while time.time() < deadline:
            _acquire_autopilot_lock()
            if proc.poll() is not None and not provider.port_is_listening(port):
                print("\nОкно Chrome было закрыто до завершения входа.")
                return 1
            time.sleep(POLL_EVERY)
            verdict, err = _verdict(cdp_url)
            if err:
                if err != last_reason:
                    print(f"  … {err}")
                    last_reason = err
                continue
            if verdict and verdict.get("authenticated"):
                print()
                print("=" * 66)
                print(f"  ✅ АВТОРИЗАЦИЯ ЕСТЬ! Вход успешно выполнен.")
                print(f"  Статус: {verdict.get('reason')}")
                print(f"  URL:    {(verdict.get('evidence') or {}).get('url')}")
                print("=" * 66)
                try:
                    AUTH_ALERT_MARKER.unlink(missing_ok=True)
                except OSError:
                    pass
                if args.keep_open:
                    print("  Окно оставлено открытым (--keep-open). Закрой его, когда будешь готов.")
                    while proc.poll() is None and provider.port_is_listening(port):
                        _acquire_autopilot_lock()
                        time.sleep(2.0)
                else:
                    try:
                        provider.quit_browser(cdp_url)
                        provider.wait_port_free(port, timeout=6.0)
                        print("  Окно Chrome автоматически закрыто, сессия сохранена для демонов.")
                    except Exception as exc:  # noqa: BLE001
                        print(f"  (закрой окно Chrome вручную: {exc})")
                return 0
            reason = (verdict or {}).get("reason") or "ожидание входа..."
            if reason != last_reason:
                print(f"  … пока не вошёл: {reason}")
                last_reason = reason
        print()
        print("⏱ Время ожидания (10 минут) вышло.")
        return 1
    finally:
        if provider.port_is_listening(port):
            try:
                provider.quit_browser(cdp_url)
                provider.wait_port_free(port, timeout=5.0)
            except Exception:
                pass
        _release_autopilot_lock()


if __name__ == "__main__":
    raise SystemExit(main())
