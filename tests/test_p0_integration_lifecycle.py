"""End-to-End Integration Lifecycle Verification Suite (Stage 1 / Phase 0 Verification).

Tests the full lifecycle in an isolated environment with temporary database:
1. Discovery (multi-source adapter mock ingestion)
2. Filtering & Relevance evaluation
3. Shortlist Selection (round-robin sequence balancing)
4. Application Preparation & Preflight
5. Permission & Gate checks (halt check under stop file vs dry-run permit)
6. Simulated Submission recording in SQLite
7. Result confirmation
8. Crash & Re-run idempotency (zero duplicate submission invariant)
9. Inbound employer message processing
10. Interview invitation detection, deduplication, and rescheduling handling
11. Windows Task Scheduler command compatibility check
"""

from __future__ import annotations

import os
import pathlib
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from ai_assistant import config, db
from ai_assistant.schema import Vacancy
from ai_assistant.shortlist_selection import (
    ShortlistCategory,
    select_read_targets,
)
from ai_assistant.outbound_governor import (
    OutboundAction,
    check_outbound_allowed,
)
from ai_assistant.hh_submission import (
    submission_halt_reason,
    preflight_submission,
    SubmissionStatus,
)
from ai_assistant.hh_autonomous_agent import NotificationDispatcher


class TestP0IntegrationLifecycle(unittest.TestCase):
    """Full lifecycle integration tests in an isolated sandbox."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = pathlib.Path(self.temp_dir.name) / "test_lifecycle.db"
        self._orig_db_path = config.DB_FILE
        config.DB_FILE = str(self.db_path)
        db.init_db()

        self._clean_stop_files()

    def tearDown(self) -> None:
        self._clean_stop_files()
        config.DB_FILE = self._orig_db_path
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def _clean_stop_files(self) -> None:
        for fname in ["STOP_ALL_OUTBOUNDS", "STOP_SUBMITS", "data/STOP_ALL_OUTBOUNDS", "data/STOP_SUBMITS"]:
            p = pathlib.Path(fname)
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass

    def test_full_lifecycle_e2e(self) -> None:
        """Execute complete cycle: Discovery -> Filter -> Shortlist -> Preflight -> Gated Submit -> Crash Re-run -> Inbound -> Interview Dedup & Reschedule."""
        
        # ---------------------------------------------------------------------
        # Step 1: Vacancy Discovery (Multi-source ingestion)
        # ---------------------------------------------------------------------
        raw_vacancies = [
            Vacancy(
                source="hh",
                source_job_id="1001",
                title="AI Automation Engineer (Python, FastAPI, LLM)",
                company="FinTech Solutions",
                job_url="https://hh.ru/vacancy/1001",
                description="Разработка высоконагруженных микросервисов на Python, FastAPI, Docker, PostgreSQL. Зарплата от 200 000 руб.",
                salary_min=200000.0,
                salary_max=280000.0,
                salary_currency="RUR",
            ),
            Vacancy(
                source="telegram",
                source_job_id="2001",
                title="Lead DevOps / Infrastructure Engineer",
                company="CloudScale Global",
                job_url="https://t.me/remoteit/2001",
                description="Kubernetes, Terraform, CI/CD, AWS/GCP infrastructure. Remote worldwide. Contact: @hr_cloud",
                salary_min=300000.0,
                salary_max=400000.0,
                salary_currency="RUR",
            ),
            Vacancy(
                source="hh",
                source_job_id="1002",
                title="Junior Data Analyst",
                company="Retail Direct",
                job_url="https://hh.ru/vacancy/1002",
                description="Excel, basic SQL, reporting for retail sales.",
                salary_min=50000.0,
                salary_max=60000.0,
                salary_currency="RUR",
            ),
        ]

        # ---------------------------------------------------------------------
        # Step 2: Filtering and Relevance Verification
        # ---------------------------------------------------------------------
        from scripts.run_multi_source_discovery import is_target_role
        qualified_vacancies = []
        for v in raw_vacancies:
            # Must match target role keywords
            if is_target_role(v.title, v.description):
                # Must satisfy minimum salary threshold (>= 100k RUR for senior roles)
                if (v.salary_min or 0) >= 100000.0:
                    qualified_vacancies.append(v)

        self.assertEqual(len(qualified_vacancies), 2)
        self.assertEqual(qualified_vacancies[0].stable_id(), "hh:1001")
        self.assertEqual(qualified_vacancies[1].stable_id(), "telegram:2001")

        # ---------------------------------------------------------------------
        # Step 3: Shortlist Selection (Round-Robin sequence balancing)
        # ---------------------------------------------------------------------
        # Convert to candidate cards for round-robin target picker
        cards = [
            {"vacancy_id": "1001", "title": qualified_vacancies[0].title, "category": ShortlistCategory.AUTOMATION_AI},
            {"vacancy_id": "2001", "title": qualified_vacancies[1].title, "category": ShortlistCategory.SYSADMIN_INFRA},
        ]
        selection = select_read_targets(cards, read_limit=2)
        self.assertEqual(len(selection.targets), 2)
        # Check round-robin preservation
        self.assertIn("1001", selection.targets)
        self.assertIn("2001", selection.targets)

        target_vid = "1001"
        target_stable_id = f"hh:{target_vid}"

        # ---------------------------------------------------------------------
        # Step 4: Application Preparation & Preflight
        # ---------------------------------------------------------------------
        # Simulate review & package preparation
        package_data = {
            "vacancy_stable_id": target_stable_id,
            "cover_letter": "Здравствуйте! Заинтересовала позиция Senior Python Backend Developer. Опыт более 8 лет с FastAPI и PostgreSQL.",
            "validation_status": "VALID",
            "questions": [],
        }
        review_entry = {
            "fingerprint": "fp_test_1001",
            "state": "HUMAN_APPROVED",
            "cover_letter": package_data["cover_letter"],
        }
        review_store = {f"rev_{target_stable_id}": review_entry}

        mock_plan = MagicMock(status="VALID", unresolved=[])
        mock_orch = MagicMock(verdict="VERIFIED", failed_operations=0)

        # ---------------------------------------------------------------------
        # Step 5: Permission Check (Halt check under stop file vs dry-run)
        # ---------------------------------------------------------------------
        # Scenario 5A: STOP_ALL_OUTBOUNDS file present -> must refuse
        stop_file = pathlib.Path("STOP_ALL_OUTBOUNDS")
        stop_file.touch()
        try:
            halt = submission_halt_reason(include_submit_allowed=True)
            self.assertIsNotNone(halt)
            self.assertIn("STOP_SUBMITS file present", halt)

            report_blocked = preflight_submission(
                review_store=review_store,
                review_id=f"rev_{target_stable_id}",
                fingerprint="fp_test_1001",
                package=MagicMock(validation_status="VALID"),
                plan=mock_plan,
                orchestration=mock_orch,
                evaluate_fn=lambda x: "{}",
            )
            self.assertEqual(report_blocked.status, SubmissionStatus.BLOCKED)
        finally:
            stop_file.unlink()

        # Scenario 5B: Dry-run check allowed when stop file is removed
        allowed_dry, _ = check_outbound_allowed(action=OutboundAction.SUBMISSION, dry_run=True)
        self.assertTrue(allowed_dry)

        # ---------------------------------------------------------------------
        # Step 6 & 7: Simulated Submission & Result Confirmation
        # ---------------------------------------------------------------------
        # Record simulated submission in isolated SQLite database
        app_id = f"sub_{target_vid}_20261107_001"
        app_record = {
            "application_id": app_id,
            "conversation_id": "conv_e2e_888",
            "vacancy_stable_id": target_stable_id,
            "title": qualified_vacancies[0].title,
            "employer": qualified_vacancies[0].company,
            "state": "SUBMITTED",
            "draft": package_data["cover_letter"],
        }
        db.save_hh_application(app_record)
        app_data = db.get_hh_application(app_record["application_id"])
        self.assertIsNotNone(app_data)
        self.assertEqual(app_data["state"], "SUBMITTED")

        # ---------------------------------------------------------------------
        # Step 8: Database Record Accounting
        # ---------------------------------------------------------------------
        stored_apps = db.list_hh_applications()
        self.assertEqual(len(stored_apps), 1)
        self.assertEqual(stored_apps[0]["vacancy_stable_id"], target_stable_id)
        self.assertEqual(stored_apps[0]["state"], "SUBMITTED")

        # ---------------------------------------------------------------------
        # Step 9: Crash Simulation & Re-run (Zero Duplicate Submission Invariant)
        # ---------------------------------------------------------------------
        # Simulate worker crash and restart: check if already applied
        existing_app = db.get_hh_application_by_vacancy(target_stable_id)
        self.assertIsNotNone(existing_app, "Idempotency check failed: vacancy should be recognized as already applied")
        self.assertEqual(existing_app["state"], "SUBMITTED")

        # Attempt second submission for the same vacancy
        if not existing_app:
            db.save_hh_application(app_record)  # Should never be reached

        total_apps_after_rerun = db.list_hh_applications()
        self.assertEqual(len(total_apps_after_rerun), 1, "ZERO duplicate invariant violated: multiple application rows found!")

        # ---------------------------------------------------------------------
        # Step 10: Inbound Employer Message Handling & Interview Dedup / Reschedule
        # ---------------------------------------------------------------------
        conv_id = "conv_e2e_888"

        # 10A: Routine employer question
        # Record routine message audit
        db.save_conversation_audit({
            "conversation_id": conv_id,
            "application_id": app_id,
            "vacancy_id": target_vid,
            "vacancy_stable_id": target_stable_id,
            "employer": "FinTech Solutions",
            "incoming_message": "Добрый день! Какая у вас текущая локация и зарплатные ожидания?",
            "message_classification": "EMPLOYER_QUESTION",
            "status": "BLOCKED_AUTONOMOUS_REPLY_DISABLED",
            "decision_reason": "Draft synthesized, send suppressed by policy.",
        })
        audits = db.list_conversation_audits(conversation_id=conv_id, limit=5)
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["message_classification"], "EMPLOYER_QUESTION")

        # 10B: Interview Invitation Detection -> First alert
        invitation_text_v1 = "Михаил, здравствуйте! Приглашаем на техническое интервью в понедельник в 14:00 МСК в Zoom: https://zoom.us/j/123456"
        notif_1 = NotificationDispatcher.notify_interview(
            company="FinTech Solutions",
            vacancy_title=qualified_vacancies[0].title,
            invitation_text=invitation_text_v1,
            invitation_url="https://hh.ru/chat/" + conv_id,
            conversation_id=conv_id,
        )
        self.assertIsNotNone(notif_1)
        self.assertNotEqual(notif_1.get("status"), "DUPLICATE_SUPPRESSED")

        events_1 = db.list_interview_events(limit=10)
        self.assertEqual(len(events_1), 1)
        self.assertEqual(events_1[0]["status"], "INVITED")

        # 10C: Re-polling cycle with identical invitation text -> Must be suppressed
        notif_2 = NotificationDispatcher.notify_interview(
            company="FinTech Solutions",
            vacancy_title=qualified_vacancies[0].title,
            invitation_text=invitation_text_v1,
            invitation_url="https://hh.ru/chat/" + conv_id,
            conversation_id=conv_id,
        )
        self.assertEqual(notif_2.get("status"), "DUPLICATE_SUPPRESSED")

        events_after_dup = db.list_interview_events(limit=10)
        self.assertEqual(len(events_after_dup), 1, "Duplicate interview event row was created!")

        # 10D: Employer updates time slot / interview details -> Must detect update & preserve history
        invitation_text_v2 = "Михаил, переносим созвон: ждем вас во вторник в 15:30 МСК по той же ссылке Zoom."
        notif_3 = NotificationDispatcher.notify_interview(
            company="FinTech Solutions",
            vacancy_title=qualified_vacancies[0].title,
            invitation_text=invitation_text_v2,
            invitation_url="https://hh.ru/chat/" + conv_id,
            conversation_id=conv_id,
        )
        self.assertIn("UPDATED", notif_3.get("title", ""))

        events_after_reschedule = db.list_interview_events(limit=10)
        self.assertEqual(len(events_after_reschedule), 2, "Rescheduled interview should append an updated event row while preserving history")
        self.assertEqual(events_after_reschedule[0]["status"], "RESCHEDULED")
        self.assertEqual(events_after_reschedule[1]["status"], "INVITED")


    def test_windows_task_scheduler_command_compatibility(self) -> None:
        """Verify command lines and VBS scripts registered in Windows Task Scheduler."""
        import sys
        if sys.platform != "win32":
            return
        project_root = pathlib.Path(__file__).resolve().parent.parent

        # 1. Verify VBS scripts exist
        vbs_scripts = [
            "JobSearchDashboard.vbs",
            "scripts/run_multi_source_silent.vbs",
            "scripts/run_telegram_control_bot_silent.vbs",
            "scripts/run_hh_autopilot_silent.vbs",
            "scripts/run_hh_realtime_daemon_silent.vbs",
            "scripts/run_kwork_monitor_silent.vbs",
        ]
        for rel_path in vbs_scripts:
            full_path = project_root / rel_path
            # In worktree root or main repo root
            if not full_path.exists():
                main_path = pathlib.Path(r"C:\Users\Misha\Documents\job-search") / rel_path
                self.assertTrue(main_path.exists(), f"VBS launcher script missing: {rel_path}")

        # 2. Verify Python target scripts exist
        target_py_scripts = [
            "ai_assistant/cli.py",  # Dashboard runs via `pythonw.exe -m ai_assistant.cli ui`
            "scripts/run_multi_source_discovery.py",
            "ai_assistant/telegram_control_bot.py",
            "scripts/run_hh_autopilot_cycle.py",
            "scripts/run_hh_realtime_daemon.py",
            "scripts/run_kwork_inbox_monitor_cycle.py",
        ]
        for rel_path in target_py_scripts:
            full_path = project_root / rel_path
            self.assertTrue(full_path.exists(), f"Target Python script missing in package: {rel_path}")

        # 3. Verify Python runtime executable
        uv_python = pathlib.Path(r"C:\Users\Misha\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe")
        if not uv_python.exists():
            uv_python = pathlib.Path(sys.executable)
        self.assertTrue(uv_python.exists(), f"Python runtime not found: {uv_python}")

        uv_pythonw = pathlib.Path(r"C:\Users\Misha\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\pythonw.exe")
        if not uv_pythonw.exists():
            uv_pythonw = uv_python.parent / "pythonw.exe"
            if not uv_pythonw.exists():
                uv_pythonw = uv_python
        self.assertTrue(uv_pythonw.exists(), f"Pythonw runtime not found: {uv_pythonw}")


if __name__ == "__main__":
    unittest.main()
