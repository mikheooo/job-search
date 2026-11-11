#!/usr/bin/env python
"""Stage 75 / 89.2: Unified Hermes Job Search Fetcher & Gateway Dispatcher.

Canonical repository copy of the Hermes fetcher script.
Connects Hermes Cron to the canonical job-search engine:
1. Runs canonical discovery & matching (Watcher / Adapters -> normalizer -> vacancy_identity -> state.db).
2. Generates validated digest from state.db matching Mikhail's CandidateProfile.
3. Attaches inline feedback buttons for Stage 89 feedback actions (fb:INT, fb:NOT, fb:APP, fb:SKP).
4. Delivers validated Markdown post + inline keyboard to Telegram channel @remotejobd.
5. Single Source of Truth: state.db (no ungrounded LLM hallucination).
"""
import os
import sys
import json
import re
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone

JOB_SEARCH_ROOT = Path(r"C:\Users\Misha\Documents\job-search")
if str(JOB_SEARCH_ROOT) not in sys.path:
    sys.path.insert(0, str(JOB_SEARCH_ROOT))

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

# Canonical project venv interpreter (Python 3.11). Hermes Cron launches this
# script under Hermes's own Python (3.14), which cannot load the project venv's
# compiled binary deps (pydantic_core etc.). Hand over to the project's venv
# python so every dependency resolves under a matching interpreter.
#
# Deliberately NOT os.execv: on Windows execv spawns a detached child and exits
# the current process immediately, so the caller (Hermes Cron) loses the child's
# stdout and its exit status and records a false "completed" run. subprocess.run
# waits for the child and lets its output and exit code through unchanged.
_PROJECT_PY = JOB_SEARCH_ROOT / ".venv" / "Scripts" / "python.exe"


def _canonical_path(path) -> str:
    """Canonicalize a path for identity comparison.

    ``os.path.abspath`` only normalizes the *string* form, so the same file
    reached through a different alias compares as different. On this machine
    ``C:\\Users\\Misha\\Documents\\job-search`` and
    ``C:\\Users\\Misha\\Мои документы\\job-search`` are the same folder — the
    second is the localized display alias of the first. Running the project
    venv's python through the alias made ``sys.executable`` differ from the
    hardcoded ``_PROJECT_PY`` as strings, which tripped this handover and ran a
    real production dispatch on a mere import. ``realpath`` resolves symlinks
    and junctions, and ``normcase`` folds case and separators, so both aliases
    collapse to one identity.
    """
    return os.path.normcase(os.path.realpath(path))


def _maybe_reexec():
    if os.environ.get("JOB_FETCHER_NO_REEXEC") != "1" and _PROJECT_PY.is_file() and _canonical_path(sys.executable) != _canonical_path(str(_PROJECT_PY)):
        import subprocess

        # Hermes Cron runs this script under Hermes's own Python (3.14) with the
        # Hermes venv's site-packages exported in PYTHONPATH. A normal venv puts
        # its own site-packages first, but an inherited PYTHONPATH env var wins
        # over it, so the project venv (3.11) would import the 3.14-compiled
        # pydantic_core and crash with ModuleNotFoundError. Neutralize the leak
        # for the child so only the project venv's deps are used.
        # Adopted from the deployed runtime copy on 2026-10-03 and folded back into
        # the canonical source on 2026-10-05 so that `hermes sync` cannot regress it.
        _child_env = dict(os.environ)
        for _leaked in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
            _child_env.pop(_leaked, None)

        _handover = subprocess.run(
            [str(_PROJECT_PY), os.path.abspath(__file__), *sys.argv[1:]],
            check=False,  # the child's status is propagated by hand on the next line
            env=_child_env,
        )
        sys.exit(_handover.returncode)


try:
    from ai_assistant import config
except ImportError as exc:
    config = None
    print(f"[WARN] ai_assistant.config unavailable: {exc}", file=sys.stderr)


def target_channel() -> str:
    """Public digest channel from the environment. Empty when unset.

    Never falls back to a hardcoded chat id. Send paths must treat empty as
    fail-closed (see require_telegram_channel_id).
    """
    env = (os.getenv("TELEGRAM_CHANNEL_ID") or "").strip()
    if env:
        return env
    if config is not None:
        return (getattr(config, "TELEGRAM_CHANNEL_ID", "") or "").strip()
    return ""


# Import-time snapshot for readers that expect the name. Live sends call
# target_channel() so a later environment change is honored.
TARGET_CHANNEL = target_channel()
VACANCIES_FILE = JOB_SEARCH_ROOT / "vacancies.json"


def load_vacancies() -> list:
    if VACANCIES_FILE.exists():
        try:
            return json.loads(VACANCIES_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save_vacancies(new_items: list):
    """Legacy helper maintained for backward-compatibility with tests."""
    current = load_vacancies()
    max_id = max((v["id"] for v in current if isinstance(v.get("id"), int)), default=0)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for v in new_items:
        max_id += 1
        v["id"] = max_id
        v["found_at"] = today_str
        v["applied"] = False
        current.append(v)

    VACANCIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    VACANCIES_FILE.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_http_error_body(exc: BaseException) -> str:
    """Best-effort read of an HTTPError response body for error classification."""
    try:
        import urllib.error as _uelib

        if isinstance(exc, _uelib.HTTPError):
            try:
                raw = exc.read()
                if isinstance(raw, bytes):
                    return raw.decode("utf-8", errors="replace")
                return str(raw)
            except Exception:
                return getattr(exc, "msg", "") or str(exc)
    except Exception:
        pass
    return str(exc)


def _is_markdown_format_error(exc: BaseException) -> bool:
    """True only for a confirmed Telegram Markdown formatting rejection.

    Telegram answers malformed Markdown with HTTP 400 and a description such as
    "can't parse entities" / "can't parse message". Only this case permits the
    single plain-text retry. Every other error (network, timeout, auth) must not
    trigger a second send, otherwise a timeout after a successful server-side
    write would deliver the digest twice.
    """
    try:
        import urllib.error as _uelib

        if not isinstance(exc, _uelib.HTTPError):
            return False
        if getattr(exc, "code", None) != 400:
            return False
        body = _read_http_error_body(exc).lower()
        markers = (
            "can't parse",
            "cannot parse",
            "parse entities",
            "parse message",
            "bad request: can't",
            "wrong type of the web page content",
        )
        return any(m in body for m in markers)
    except Exception:
        return False


def _is_auth_error(exc: BaseException) -> bool:
    """True for Telegram authorization failures (invalid/revoked token, blocked bot)."""
    try:
        import urllib.error as _uelib

        if isinstance(exc, _uelib.HTTPError) and getattr(exc, "code", None) in (401, 403):
            return True
    except Exception:
        pass
    text = str(exc).lower()
    return "unauthorized" in text or "invalid token" in text or "bot was blocked" in text


def _is_ambiguous_transport_error(exc: BaseException) -> bool:
    """True when the request may have reached Telegram despite the exception.

    Timeout, reset, or truncated read after the POST leaves delivery uncertain:
    the server could have stored the message while the client never saw the
    acknowledgement. Such outcomes must be recorded as AMBIGUOUS with no
    automatic retry — a retry would risk a duplicate digest.
    """
    import socket as _socket

    try:
        import urllib.error as _uerr

        if isinstance(exc, _uerr.URLError) and not isinstance(exc, _uerr.HTTPError):
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (_socket.timeout, TimeoutError)):
                return True
            rs = str(reason).lower()
            if "timed out" in rs or "timeout" in rs:
                return True
            # DNS / refused / unreachable before any bytes left: confirmed failure.
            return False
    except Exception:
        pass
    if isinstance(exc, (_socket.timeout, TimeoutError)):
        return True
    try:
        import http.client as _http

        if isinstance(
            exc,
            (
                _http.RemoteDisconnected,
                _http.IncompleteRead,
                _http.BadStatusLine,
                ConnectionResetError,
                BrokenPipeError,
            ),
        ):
            return True
    except Exception:
        pass
    es = str(exc).lower()
    if "timed out" in es or "timeout" in es:
        return True
    return False


def _post_to_telegram(url: str, payload: dict, headers: dict, timeout: int = 20) -> bool:
    """Single POST attempt. Returns True on HTTP 200, False on confirmed failure.

    Raises the original exception for ambiguous transport outcomes (timeout /
    reset / truncated read) so the caller records AMBIGUOUS instead of retrying.
    """
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None)
            if status == 200:
                try:
                    res_body = json.loads(resp.read().decode("utf-8"))
                    msg_id = res_body.get("result", {}).get("message_id")
                    if msg_id:
                        print(f"[SUCCESS] Telegram API message_id: {msg_id}")
                except Exception:
                    # 200 received: delivery is confirmed even if the ack body
                    # cannot be parsed. Do not turn success into ambiguity.
                    pass
                return True
            if status is None:
                # No status and no exception: undefined result -> ambiguous.
                raise RuntimeError("Telegram response has no status and no error")
            return False
    except Exception as exc:
        if _is_ambiguous_transport_error(exc):
            raise
        raise


def send_to_telegram(text: str, reply_markup: dict = None) -> bool:
    """Send a digest post to Telegram. Fail-closed against duplicate delivery.

    Contract (stabilization 2026-10-13):
    - Returns True on confirmed delivery (HTTP 200).
    - Returns False on confirmed failure (missing token, auth rejection,
      DNS/refused, non-format 400, etc.). Caller records FAILED. No retry.
    - Raises on timeout / undefined result. Caller must record AMBIGUOUS and
      must NOT retry automatically — the message may already be stored
      server-side.
    - Exactly one plain-text retry (no parse_mode) is permitted, and only after
      a confirmed Markdown formatting error (HTTP 400 with parse markers).
    - Authorization errors are never retried.
    """
    token = None
    channel = None
    try:
        from ai_assistant import telegram_routing

        route = telegram_routing.resolve_job_route()
        token = route.token
        channel = route.chat_id
    except telegram_routing.TelegramRoutingError as exc:
        from ai_assistant import telegram_routing

        channel = target_channel()
        cand = (os.getenv("TELEGRAM_BOT_TOKEN") or (getattr(config, "TELEGRAM_BOT_TOKEN", None) if config else None) or "").strip()
        blocked = telegram_routing.main_hermes_tokens()
        if channel and cand and cand not in blocked and "main Hermes" not in str(exc):
            token = cand
        else:
            print(f"[ERROR] Telegram routing error: {exc}", file=sys.stderr)
            return False
    except Exception:
        pass

    if not channel:
        channel = target_channel()
    if not channel:
        print("[ERROR] TELEGRAM_CHANNEL_ID is not set; refusing to send the digest", file=sys.stderr)
        return False
    if not token:
        print("[ERROR] TELEGRAM_BOT_TOKEN is not configured; refusing to send", file=sys.stderr)
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    headers = {"Content-Type": "application/json"}

    # 1. Try Markdown once.
    payload = {
        "chat_id": channel,
        "text": text,
        "parse_mode": "Markdown"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        return _post_to_telegram(url, payload, headers, timeout=20)
    except Exception as first_err:
        # Authorization failures are confirmed failures: never retry.
        if _is_auth_error(first_err):
            print(f"[ERROR] Telegram API auth failure, no retry: {first_err}", file=sys.stderr)
            return False
        # Ambiguous transport (timeout/reset/truncated): do NOT retry here.
        # Propagate so the caller records AMBIGUOUS instead of risking a duplicate.
        if _is_ambiguous_transport_error(first_err):
            print(f"[ERROR] Telegram API ambiguous result, no retry: {first_err}", file=sys.stderr)
            raise
        # Exactly one plain-text retry, only after a confirmed Markdown
        # formatting rejection (HTTP 400 with parse markers).
        if not _is_markdown_format_error(first_err):
            print(f"[ERROR] Telegram API: {first_err}", file=sys.stderr)
            return False
        print(f"[WARNING] Markdown rejected ({first_err}); retrying once without parse_mode.", file=sys.stderr)

    # 2. Single plain-text retry after confirmed formatting error.
    plain_payload = {
        "chat_id": channel,
        "text": text[:4000]
    }
    if reply_markup:
        plain_payload["reply_markup"] = reply_markup
    try:
        return _post_to_telegram(url, plain_payload, headers, timeout=20)
    except Exception as second_err:
        # Even the plain-text retry must not loop: ambiguous -> raise for
        # AMBIGUOUS, confirmed failure -> False for FAILED.
        if _is_ambiguous_transport_error(second_err):
            print(f"[ERROR] Telegram API ambiguous on plain-text retry, no further retry: {second_err}", file=sys.stderr)
            raise
        print(f"[ERROR] Telegram API: {second_err}", file=sys.stderr)
        return False


def score_unscored_vacancies() -> int:
    """Проставить match_score вакансиям, у которых он пуст. Ничего не отправляет.

    Зачем это здесь. Раньше оценки в базу не писал никто, кроме ручного
    `python -m ai_assistant.cli analyze --persist`, который в ежедневном прогоне
    не вызывался. В итоге между 29 августа и 22 сентября match_score оставался
    NULL у 1516 вакансий. Дайджест и очередь отклика считают оценку заново на
    лету, поэтому пустой колонки они не замечают, — но два места её читают:
    агрегаты дашборда и резервный путь дайджеста, когда профиль не загрузился
    (там NULL превращается в 70.0 и решение APPLY, то есть канал забивается
    мусором). Чтобы колонка не протухала, заполняем её каждый прогон.

    Оцениваем ТОЛЬКО пустые строки: повторный счёт всех 2107 вакансий занимает
    около четырёх минут (коммит на каждую), а нужен только для новых.

    Возвращает число оценённых вакансий. Любая ошибка не должна ломать доставку,
    поэтому вызывающий обязан обернуть вызов в try/except.
    """
    import sqlite3

    from ai_assistant import config
    from ai_assistant.candidate_profile import load_candidate_profile
    from ai_assistant.db import _row_to_vacancy, init_db
    from ai_assistant.matcher import JobMatcher

    init_db()
    conn = sqlite3.connect(config.DB_FILE)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows = cur.execute("SELECT * FROM vacancies WHERE match_score IS NULL").fetchall()
    if not rows:
        conn.close()
        return 0

    profile = load_candidate_profile()
    matcher = JobMatcher(profile)

    scored = 0
    for row in rows:
        vacancy = _row_to_vacancy(row)
        if not vacancy:
            continue
        try:
            res = matcher.match(vacancy)
        except Exception as e:  # noqa: BLE001 - one bad vacancy must not cost the scores of all the others
            print(f"[WARNING] Match failed for {row['stable_id']}: {e}", file=sys.stderr)
            continue
        cur.execute(
            "UPDATE vacancies SET match_score=?, match_decision=?, match_reasons=?,"
            " match_strengths=?, match_gaps=? WHERE stable_id=?",
            (
                res.score,
                res.decision,
                json.dumps(res.reasons, ensure_ascii=False),
                json.dumps(res.strengths, ensure_ascii=False),
                json.dumps(res.gaps, ensure_ascii=False),
                vacancy.stable_id(),
            ),
        )
        scored += 1

    conn.commit()
    conn.close()
    return scored


def run_canonical_discovery_and_export(limit: int = 5, min_score: float = 60.0, dry_run: bool = False) -> tuple[str, list] | None:
    """Execute canonical discovery cycle via job-search and export validated digest from state.db.

    Returns ``(post_text, vacancies_data)`` on success — including a legitimate
    "no matching vacancies" result, where ``vacancies_data`` is empty.

    Returns ``None`` when the export itself failed: the exporter returned a
    non-zero code, produced no output, or produced output that is not JSON. The
    caller must treat ``None`` as a failure. An export that could not be read is
    not the same as a day with no matching vacancies; conflating the two is what
    let the 2026-10-08 run report success while delivering nothing
    (PROJECT_STATE.md §11.5).
    """
    from ai_assistant.watcher import Watcher, WatcherConfig
    from ai_assistant.cli import export_digest_cmd
    from ai_assistant.db import init_db
    import io
    import contextlib

    if not dry_run:
        init_db()

    # 1. Run a single poll cycle across canonical adapters (Himalayas, RemoteOK, WWR, Habr Career)
    try:
        cfg = WatcherConfig(
            sources=["himalayas", "remoteok", "weworkremotely", "habrcareer"],
            poll_interval_seconds=60,
            max_iterations=1,
            candidate_country="TH",
            batch_limit=20,
        )
        watcher = Watcher(cfg)
        poll_res = watcher.poll_once(iteration=1, dry_run=dry_run)
        print(f"[SUCCESS] Canonical discovery completed: fetched {poll_res.fetched_count}, new {poll_res.new_vacancies_count}, matched {poll_res.matched_count}")
    except Exception as e:
        print(f"[WARNING] Canonical poll warning (will still export from state.db): {e}", file=sys.stderr)

    # 1b. Score only the vacancies that have no match_score yet, so the column
    # never goes stale again. Failure here must not break delivery.
    try:
        scored = score_unscored_vacancies()
        if scored:
            print(f"[SUCCESS] Scored {scored} previously unscored vacancies in state.db.")
        else:
            print("[SUCCESS] All vacancies already scored.")
    except Exception as e:  # noqa: BLE001 - scoring is a side errand; it must never break the daily delivery
        print(f"[WARNING] Scoring step failed (delivery continues): {e}", file=sys.stderr)

    # 2. Export validated digest from state.db
    stdout_buf = io.StringIO()
    with contextlib.redirect_stdout(stdout_buf):
        export_rc = export_digest_cmd(format_type="json", limit=limit, min_score=min_score, output_json=True)
    
    raw_json = stdout_buf.getvalue().strip()
    if export_rc != 0:
        print(f"[ERROR] Digest export failed (exit code {export_rc}); see the export error above.", file=sys.stderr)
        return None
    if not raw_json:
        print("[ERROR] Digest export produced no output.", file=sys.stderr)
        return None
    try:
        data = json.loads(raw_json)
    except Exception as e:
        print(f"[ERROR] Failed to parse exported digest: {e}", file=sys.stderr)
        return None
    post_text = data.get("telegram_post", "")
    vacancies_data = data.get("new_vacancies_data", [])
    return post_text, vacancies_data


def main():
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}] Starting Unified Hermes Job Search Dispatcher (Stage 75/81)...")
    dry_run = bool(os.environ.get("JOB_SEARCH_DRY_RUN"))

    # Operational audit: surface any unresolved / stale digest attempts
    try:
        from ai_assistant.db import list_digest_attempts
        for att in list_digest_attempts(limit=5):
            if att.get("stale") or att.get("status") in ("AMBIGUOUS", "ATTEMPTING"):
                print(
                    f"[WARNING] Unresolved digest attempt detected:\n"
                    f"  batch={att['batch_key']}\n"
                    f"  persisted_state={att['status']}\n"
                    f"  effective_state={att['effective_status']}\n"
                    f"  age_minutes={att.get('age_minutes')}\n"
                    f"  automatic_retry=false\n"
                    f"  action_required=python -m ai_assistant.cli digest-attempts recover --batch {att['batch_key']} --status [DELIVERED|FAILED]"
                )
    except Exception:
        pass

    # Mutual exclusion for the discovery/export phase.
    #
    # Two independent schedulers reach this script at ~10:00: the Windows Task
    # "JobSearch_Daily_Digest" (via ai_assistant.runner, which spawns this file as
    # its child) and the Hermes cron job "Job Search Monitor" (which runs this file
    # directly). The runner's own job_search.lock covers only its own path, so on
    # 2026-10-08 both entry points ran at once and the second hit
    # "database is locked" mid-export. This lock is deliberately a *different*
    # file from the runner's, otherwise the runner's child would deadlock against
    # its parent. A second arrival is a safe no-op: the first run is already doing
    # the work (PROJECT_STATE.md §11.5).
    from ai_assistant import config
    from ai_assistant.runner import SingleInstanceLock

    lock = SingleInstanceLock(lock_dir=config.LOGS_DIR, lock_name="job_search_fetcher.lock")
    acquired, lock_msg = lock.acquire()
    if not acquired:
        print(f"[INFO] {lock_msg}")
        return

    try:
        _run_discovery_and_deliver(dry_run)
    finally:
        lock.release()


def _run_discovery_and_deliver(dry_run: bool) -> None:
    result = run_canonical_discovery_and_export(limit=5, min_score=60.0, dry_run=dry_run)

    # An export that could not be read is not the same as a day with no matching
    # vacancies. Fail closed so the failure reaches failure_tracker and
    # production-health instead of being reported as an empty, successful day
    # (2026-10-08: exit 0 while nothing was delivered — PROJECT_STATE.md §11.5).
    if result is None:
        print(
            "[ERROR] Digest export failed; nothing was delivered. Exiting non-zero so "
            "the failure is recorded instead of looking like an empty day.",
            file=sys.stderr,
        )
        sys.exit(1)

    post_text, vacancies_data = result
    if not post_text or not vacancies_data:
        print("Сегодня новых подходящих вакансий не найдено — все уже в базе state.db.")
        return

    print(f"[SUCCESS] Validated {len(vacancies_data)} matching vacancies from state.db.")

    from ai_assistant.telegram_notifier import TelegramNotifier
    reply_markup = TelegramNotifier.build_digest_inline_keyboard(vacancies_data) if vacancies_data else None

    # In dry run mode, skip actual Telegram delivery
    if dry_run:
        print("[DRY RUN] Skipped actual Telegram delivery to @remotejobd.")
        print(post_text)
        return

    from ai_assistant.db import (
        record_digest_attempt,
        mark_digest_delivered,
        record_digest_failed,
        record_digest_ambiguous,
    )

    vacancy_ids = [v["id"] for v in vacancies_data if v.get("id")]
    if not vacancy_ids:
        print("Нет валидных vacancy IDs для отправки.")
        return

    channel = target_channel()
    if not channel:
        print(
            "[ERROR] TELEGRAM_CHANNEL_ID is not set; refusing to send the digest",
            file=sys.stderr,
        )
        sys.exit(1)

    # 1. Durably record delivery attempt BEFORE external Telegram API side-effect (with atomic concurrency lock)
    batch_key = record_digest_attempt(vacancy_ids, chat_id=channel)
    if not batch_key:
        print("[STAGE 81] Concurrency lock: Batch is already being attempted or completed by another worker. Exiting safely.")
        return
    print(f"[STAGE 80/81] Pre-send attempt acquired: batch_key='{batch_key}', status='ATTEMPTING'.")

    # 2. Call external Telegram side effect.
    # Contract: True = confirmed delivery -> DELIVERED; False = confirmed
    # failure -> FAILED; raise = timeout/undefined -> AMBIGUOUS with no retry.
    # No blanket retry here and no TypeError fallback: a TypeError is a real
    # programming error and must surface as AMBIGUOUS for operator review,
    # not silently resend without reply_markup.
    send_success = False
    try:
        send_success = send_to_telegram(post_text, reply_markup=reply_markup)
    except Exception as send_err:
        print(f"[ERROR] Telegram transmission exception (no retry): {send_err}", file=sys.stderr)
        try:
            record_digest_ambiguous(vacancy_ids, batch_key=batch_key, chat_id=channel, reason=str(send_err))
        except Exception:
            pass
        sys.exit(1)

    # 3. Handle confirmed result
    if send_success:
        print("[SUCCESS] Вакансии успешно отправлены в Telegram @remotejobd.")
        try:
            delivered_count = mark_digest_delivered(vacancy_ids, batch_key=batch_key, chat_id=channel)
            print(f"[SUCCESS] Atomically marked {delivered_count} vacancies as DELIVERED in state.db.")
        except Exception as mark_err:
            print(f"[CRITICAL] Telegram send succeeded but DB marking failed: {mark_err}", file=sys.stderr)
            try:
                record_digest_ambiguous(vacancy_ids, batch_key=batch_key, chat_id=channel, reason=f"DB mark failed after send: {mark_err}")
            except Exception:
                pass
            sys.exit(2)
    else:
        print("[ERROR] Confirmed failure during Telegram transmission.", file=sys.stderr)
        try:
            record_digest_failed(vacancy_ids, batch_key=batch_key, chat_id=channel, error="Telegram send_to_telegram returned False")
        except Exception as fail_err:
            print(f"[WARNING] Failed to record failure state in DB: {fail_err}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    _maybe_reexec()
    main()
