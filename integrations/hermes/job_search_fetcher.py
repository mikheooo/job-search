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
from datetime import datetime

JOB_SEARCH_ROOT = Path(r"C:\Users\Misha\Documents\job-search")
if str(JOB_SEARCH_ROOT) not in sys.path:
    sys.path.insert(0, str(JOB_SEARCH_ROOT))

TARGET_CHANNEL = "-1004399255305"  # @remotejobd
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
    today_str = datetime.now().strftime("%Y-%m-%d")

    for v in new_items:
        max_id += 1
        v["id"] = max_id
        v["found_at"] = today_str
        v["applied"] = False
        current.append(v)

    VACANCIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    VACANCIES_FILE.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")


def send_to_telegram(text: str, reply_markup: dict = None) -> bool:
    token = None
    jobs_env_file = Path(r"C:\Users\Misha\AppData\Local\hermes\profiles\jobs\.env")
    if jobs_env_file.exists():
        m = re.search(r"^TELEGRAM_BOT_TOKEN\s*=\s*(.+)$", jobs_env_file.read_text(encoding="utf-8"), re.M)
        if m:
            token = m.group(1).strip()

    if not token:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")

    if not token:
        for p in [Path(r"C:\Users\Misha\AppData\Local\hermes\.env"), Path(r"C:\Users\Misha\tools\.env")]:
            if p.exists():
                m = re.search(r"^TELEGRAM_BOT_TOKEN\s*=\s*(.+)$", p.read_text(encoding="utf-8"), re.M)
                if m:
                    token = m.group(1).strip()
                    break

    if not token:
        print("[ERROR] TELEGRAM_BOT_TOKEN не найден", file=sys.stderr)
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    headers = {"Content-Type": "application/json"}
    
    # 1. Try Markdown
    payload = {
        "chat_id": TARGET_CHANNEL,
        "text": text,
        "parse_mode": "Markdown"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status == 200:
                try:
                    res_body = json.loads(resp.read().decode("utf-8"))
                    msg_id = res_body.get("result", {}).get("message_id")
                    if msg_id:
                        print(f"[SUCCESS] Telegram API message_id: {msg_id}")
                except Exception:
                    pass
                return True
    except Exception:
        # 2. Fallback without parse_mode if Markdown formatting failed
        try:
            plain_payload = {
                "chat_id": TARGET_CHANNEL,
                "text": text[:4000]
            }
            if reply_markup:
                plain_payload["reply_markup"] = reply_markup
            req_plain = urllib.request.Request(url, data=json.dumps(plain_payload).encode("utf-8"), headers=headers)
            with urllib.request.urlopen(req_plain, timeout=20) as resp:
                if resp.status == 200:
                    try:
                        res_body = json.loads(resp.read().decode("utf-8"))
                        msg_id = res_body.get("result", {}).get("message_id")
                        if msg_id:
                            print(f"[SUCCESS] Telegram API message_id: {msg_id}")
                    except Exception:
                        pass
                    return True
        except Exception as e2:
            print(f"[ERROR] Telegram API: {e2}", file=sys.stderr)
            return False
    return False


def run_canonical_discovery_and_export(limit: int = 5, min_score: float = 60.0, dry_run: bool = False) -> tuple[str, list]:
    """Execute canonical discovery cycle via job-search and export validated digest from state.db."""
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

    # 2. Export validated digest from state.db
    stdout_buf = io.StringIO()
    with contextlib.redirect_stdout(stdout_buf):
        export_digest_cmd(format_type="json", limit=limit, min_score=min_score, output_json=True)
    
    raw_json = stdout_buf.getvalue().strip()
    try:
        data = json.loads(raw_json)
        post_text = data.get("telegram_post", "")
        vacancies_data = data.get("new_vacancies_data", [])
        return post_text, vacancies_data
    except Exception as e:
        print(f"[ERROR] Failed to parse exported digest: {e}", file=sys.stderr)
        return "", []


def main():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting Unified Hermes Job Search Dispatcher (Stage 75/81)...")
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

    post_text, vacancies_data = run_canonical_discovery_and_export(limit=5, min_score=60.0, dry_run=dry_run)

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

    # 1. Durably record delivery attempt BEFORE external Telegram API side-effect (with atomic concurrency lock)
    batch_key = record_digest_attempt(vacancy_ids, chat_id=TARGET_CHANNEL)
    if not batch_key:
        print("[STAGE 81] Concurrency lock: Batch is already being attempted or completed by another worker. Exiting safely.")
        return
    print(f"[STAGE 80/81] Pre-send attempt acquired: batch_key='{batch_key}', status='ATTEMPTING'.")

    # 2. Call external Telegram side effect
    send_success = False
    try:
        try:
            send_success = send_to_telegram(post_text, reply_markup=reply_markup)
        except TypeError:
            # Fallback for older test fixtures mocking send_to_telegram(text: str) without reply_markup
            send_success = send_to_telegram(post_text)
    except Exception as send_err:
        print(f"[ERROR] Telegram transmission exception: {send_err}", file=sys.stderr)
        try:
            record_digest_ambiguous(vacancy_ids, batch_key=batch_key, chat_id=TARGET_CHANNEL, reason=str(send_err))
        except Exception:
            pass
        sys.exit(1)

    # 3. Handle confirmed result
    if send_success:
        print("[SUCCESS] Вакансии успешно отправлены в Telegram @remotejobd.")
        try:
            delivered_count = mark_digest_delivered(vacancy_ids, batch_key=batch_key, chat_id=TARGET_CHANNEL)
            print(f"[SUCCESS] Atomically marked {delivered_count} vacancies as DELIVERED in state.db.")
        except Exception as mark_err:
            print(f"[CRITICAL] Telegram send succeeded but DB marking failed: {mark_err}", file=sys.stderr)
            try:
                record_digest_ambiguous(vacancy_ids, batch_key=batch_key, chat_id=TARGET_CHANNEL, reason=f"DB mark failed after send: {mark_err}")
            except Exception:
                pass
            sys.exit(2)
    else:
        print("[ERROR] Confirmed failure during Telegram transmission.", file=sys.stderr)
        try:
            record_digest_failed(vacancy_ids, batch_key=batch_key, chat_id=TARGET_CHANNEL, error="Telegram send_to_telegram returned False")
        except Exception as fail_err:
            print(f"[WARNING] Failed to record failure state in DB: {fail_err}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
