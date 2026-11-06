import argparse
import hashlib
import json
import logging
import os
import sys

from . import config, core, db, prefilter

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Legacy entry. The production path is `python -m ai_assistant.cli`. This module
# still imports, but it will not scan, notify, or call the LLM unless
# LEGACY_PIPELINE_OVERRIDE is explicitly set.
def _legacy_pipeline_enabled() -> bool:
    return os.environ.get("LEGACY_PIPELINE_OVERRIDE", "").strip().lower() in {"1", "true", "yes"}


def _refuse_legacy_pipeline() -> None:
    print(
        "[SAFETY LOCKOUT] 'ai_assistant.pipeline' is a legacy entry and is disabled.\n"
        "It is not a production runner. Use:\n"
        "  python -m ai_assistant.cli production-run\n"
        "Set LEGACY_PIPELINE_OVERRIDE=1 only to inspect this old path deliberately.",
        file=sys.stderr,
    )
    raise SystemExit(1)

def get_content_hash(title: str, description: str, salary: str) -> str:
    content = f"{title}|{description}|{salary}".encode('utf-8')
    return hashlib.sha256(content).hexdigest()

def run_pipeline(dry_run: bool = False):
    if not _legacy_pipeline_enabled():
        _refuse_legacy_pipeline()
    logging.info(f"Starting pipeline. Dry run: {dry_run}")
    db.init_db()
    
    try:
        with open(config.VACANCIES_FILE, 'r', encoding='utf-8') as f:
            vacancies = json.load(f)
    except FileNotFoundError:
        logging.error(f"File not found: {config.VACANCIES_FILE}")
        return
        
    stats = {"scanned": 0, "passed": 0, "rejected": 0, "errors": 0, "notified": 0}
    
    # Резюме пока захардкодим или прочитаем из файла (в MVP захардкодим заглушку)
    # В реальности нужно читать из файла, например resume.md
    my_resume = "AI Automation Engineer. n8n, Python, Telegram Bots. Умею автоматизировать бизнес-процессы."

    for vac in vacancies[:config.BATCH_LIMIT]:  # В реальности нужно сортировать/выбирать новые
        stats["scanned"] += 1
        vac_id = str(vac.get("id"))
        title = vac.get("title", "")
        desc = vac.get("description", "")
        salary_str = vac.get("salary", "")
        salary_val = vac.get("salary_val", 0) # если парсер отдает число
        url = vac.get("url", f"https://hh.ru/vacancy/{vac_id}")
        company = vac.get("company", "Unknown")

        current_hash = get_content_hash(title, desc, salary_str)
        
        if db.is_processed(vac_id, current_hash):
            continue
            
        logging.info(f"[FOUND] {vac_id}: {title}")
        
        # 1. Prefilter
        passed, reason = prefilter.check_vacancy(title, desc, salary_val)
        if not passed:
            logging.info(f"[FILTERED] {vac_id} - {reason}")
            stats["rejected"] += 1
            if not dry_run:
                db.save_status(vac_id, "auto_rejected", current_hash)
            continue
            
        stats["passed"] += 1
        
        if dry_run:
            logging.info(f"[DRY RUN] Would process {vac_id} via LLM")
            continue
            
        # 2. AI Analysis
        try:
            logging.info(f"[TO_LLM] {vac_id}")
            analysis = core.analyze_vacancy(title, desc, my_resume)
            
            # 3. Notification
            core.send_to_telegram(vac_id, title, company, salary_str, analysis, url)
            logging.info(f"[TO_TG] {vac_id}")
            
            # 4. Save Status
            db.save_status(vac_id, "notified", current_hash)
            stats["notified"] += 1
            
        except Exception as e:
            logging.error(f"[ERROR] {vac_id} - {e}")
            stats["errors"] += 1
            db.save_status(vac_id, "error", current_hash)
            
    logging.info("=== Job Assistant Run Summary ===")
    logging.info(f"Total Scanned: {stats['scanned']}")
    logging.info(f"Passed Filter: {stats['passed']}")
    logging.info(f"Auto-Rejected: {stats['rejected']}")
    logging.info(f"Errors: {stats['errors']}")
    logging.info(f"Notified in TG: {stats['notified']}")
    logging.info("=================================")

if __name__ == "__main__":
    if not _legacy_pipeline_enabled():
        _refuse_legacy_pipeline()
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    run_pipeline(dry_run=args.dry_run)
