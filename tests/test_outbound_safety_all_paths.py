"""Comprehensive Outbound Safety Verification Suite (Stage 1 / Phase 0 Verification).

Tests every known outbound execution path strictly in mock / simulated mode:
1. HH Autopilot & Controlled Submit (hh_submission.py & hh_cdp_submit.py)
2. HH Realtime Daemon Autonomous Reply (hh_autonomous_agent.py)
3. CLI & Browser Executor (browser_executor.py)
4. Message Reply Gateway (hh_message_reply.py)
5. Dynamic kill-switch reaction test for running processes without restart
"""

from __future__ import annotations

import os
import pathlib
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from ai_assistant import config, db
from ai_assistant.outbound_governor import (
    OutboundAction,
    check_outbound_allowed,
    is_disk_kill_switch_active,
    is_db_pause_active,
    get_outbound_halt_reason,
)
from ai_assistant.hh_submission import (
    submission_halt_reason,
    preflight_submission,
    SubmissionStatus,
)


class TestOutboundSafetyAllPaths(unittest.TestCase):
    """Exhaustive safety checks for all outbound paths."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        os.environ["STOP_OUTBOUNDS_DIR"] = self.temp_dir.name
        self.db_path = pathlib.Path(self.temp_dir.name) / "test_safety.db"
        self._orig_db_path = config.DB_FILE
        config.DB_FILE = str(self.db_path)
        db.init_db()

        # Ensure stop files are clear initially
        self._clean_stop_files()

    def tearDown(self) -> None:
        self._clean_stop_files()
        os.environ.pop("STOP_OUTBOUNDS_DIR", None)
        config.DB_FILE = self._orig_db_path
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def _clean_stop_files(self) -> None:
        if hasattr(self, "temp_dir"):
            for fname in ["STOP_ALL_OUTBOUNDS", "STOP_SUBMITS", "data/STOP_ALL_OUTBOUNDS", "data/STOP_SUBMITS"]:
                p = pathlib.Path(self.temp_dir.name) / fname
                if p.exists():
                    try:
                        p.unlink()
                    except Exception:
                        pass
        for fname in ["STOP_ALL_OUTBOUNDS", "STOP_SUBMITS", "data/STOP_ALL_OUTBOUNDS", "data/STOP_SUBMITS"]:
            p = pathlib.Path(fname)
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass

    def _make_stop_file(self, fname: str = "STOP_ALL_OUTBOUNDS") -> pathlib.Path:
        p = pathlib.Path(self.temp_dir.name) / fname
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
        return p

    # =========================================================================
    # PATH 1: HH Submission Authority (submission_halt_reason & preflight)
    # =========================================================================
    def test_path1_submission_halt_reason_stop_all_outbounds(self) -> None:
        """Creating STOP_ALL_OUTBOUNDS must halt submission_halt_reason immediately."""
        p = self._make_stop_file("STOP_ALL_OUTBOUNDS")
        try:
            reason = submission_halt_reason(include_submit_allowed=True)
            self.assertIsNotNone(reason)
            self.assertIn("STOP_SUBMITS file present", reason)

            # Preflight must refuse
            report = preflight_submission(
                review_store={},
                review_id="rev-1",
                fingerprint="fp-1",
                package=MagicMock(),
                plan=MagicMock(),
                orchestration=MagicMock(),
                evaluate_fn=lambda x: "{}",
            )
            self.assertEqual(report.status, SubmissionStatus.BLOCKED)
            self.assertIn("STOP_SUBMITS file present", report.reason)
        finally:
            if p.exists():
                p.unlink()

    def test_path1_submission_halt_reason_data_stop_submits(self) -> None:
        """Creating data/STOP_SUBMITS must halt submission."""
        stop_file = self._make_stop_file("data/STOP_SUBMITS")
        try:
            reason = submission_halt_reason(include_submit_allowed=True)
            self.assertIsNotNone(reason)
            self.assertIn("STOP_SUBMITS file present", reason)
        finally:
            if stop_file.exists():
                stop_file.unlink()

    def test_path1_submission_halt_reason_db_pause(self) -> None:
        """Database system_settings submit_paused=1 must halt submission."""
        db.set_system_setting("submit_paused", "1")
        reason = submission_halt_reason(include_submit_allowed=True)
        self.assertIsNotNone(reason)
        self.assertIn("submit_paused=1", reason)

    def test_path1_submission_halt_reason_submit_allowed_false(self) -> None:
        """SUBMIT_ALLOWED=false must halt submission in non-dry-run mode."""
        with patch("ai_assistant.config.submit_allowed", return_value=False):
            reason = submission_halt_reason(include_submit_allowed=True)
            self.assertIsNotNone(reason)
            self.assertIn("SUBMIT_ALLOWED configuration", reason)

    def test_path1_submission_halt_reason_fail_closed_on_db_error(self) -> None:
        """Any exception accessing DB must fail closed (halt, not allow)."""
        with patch("ai_assistant.db.is_submit_paused", side_effect=RuntimeError("DB locked")):
            reason = submission_halt_reason(include_submit_allowed=True)
            self.assertIsNotNone(reason)
            self.assertIn("Cannot read the submission kill switch", reason)

    # =========================================================================
    # PATH 2: HH CDP Submit (run_submit) refuses before touching browser
    # =========================================================================
    def test_path2_cdp_submit_refuses_on_kill_switch_without_browser(self) -> None:
        """hh_cdp_submit.run_submit must return REFUSED with 0 clicks when kill-switch is active."""
        from ai_assistant import hh_cdp_submit
        import argparse

        stop_file = self._make_stop_file("STOP_ALL_OUTBOUNDS")

        # Temporary letter file
        letter_file = pathlib.Path(self.temp_dir.name) / "test_letter.txt"
        letter_file.write_text("Test letter", encoding="utf-8")

        args = argparse.Namespace(
            vid="12345678",
            letter=str(letter_file),
            expect_resume="Python Developer",
        )

        try:
            # We mock HeadlessBrowserSession so that if it EVER gets instantiated, test fails
            with patch("ai_assistant.hh_cdp_submit.HeadlessBrowserSession") as mock_session:
                res = hh_cdp_submit.run_submit(args)
                self.assertEqual(res.get("verdict"), "REFUSED")
                self.assertEqual(res.get("clicked"), 0)
                self.assertTrue(any("STOP_SUBMITS file present" in b for b in res.get("blockers", [])))
                mock_session.assert_not_called()
        finally:
            if stop_file.exists():
                stop_file.unlink()

    # =========================================================================
    # PATH 3: HH Realtime Daemon Autonomous Reply Guard
    # =========================================================================
    def test_path3_realtime_daemon_autonomous_reply_governor_guard(self) -> None:
        """AutonomousJobAgent._process_messages must suppress send when kill-switch is active."""
        from ai_assistant.hh_autonomous_agent import AutonomousJobAgent, AutonomousConfig
        from ai_assistant.hh_message_reply import HHDialog, HHMessage

        agent = AutonomousJobAgent(AutonomousConfig(mode="FULL"))

        # Case A: STOP_ALL_OUTBOUNDS active -> BLOCKED_KILL_SWITCH
        stop_file = self._make_stop_file("STOP_ALL_OUTBOUNDS")
        try:
            d = HHDialog(
                conversation_id="conv_safety_123",
                employer="Test Safety Corp",
                vacancy_title="Senior Python Engineer",
                messages=[
                    HHMessage(
                        message_id="m1",
                        text="Здравствуйте! Когда можете пообщаться по вакансии?",
                        sender="employer",
                    )
                ],
            )
            mock_eval_fn = MagicMock(return_value="{}")

            with patch("ai_assistant.hh_autonomous_agent._autonomous_reply_enabled", return_value=True):
                res = agent._process_messages(convs=[d], _eval_fn=mock_eval_fn)
                self.assertEqual(res.get("messages_check_status"), "CHECKED")
                self.assertEqual(res.get("auto_replies_count"), 0)

                conn = db.get_connection()
                cur = conn.cursor()
                cur.execute("SELECT status, error FROM autonomous_conversation_audits WHERE conversation_id = ?", ("conv_safety_123",))
                row = cur.fetchone()
                conn.close()

                self.assertIsNotNone(row)
                self.assertEqual(row[0], "BLOCKED_KILL_SWITCH")
                self.assertIn("STOP_ALL_OUTBOUNDS", str(row[1]))

                # Crucial: mock_eval_fn must NEVER be called to type or submit message in chat DOM
                for call_args in mock_eval_fn.call_args_list:
                    arg_str = str(call_args)
                    self.assertNotIn("targetConvId", arg_str)
                    self.assertNotIn("dispatchEvent", arg_str)
                    self.assertNotIn("sendBtn", arg_str)
        finally:
            if stop_file.exists():
                stop_file.unlink()

    def test_path3_realtime_daemon_autonomous_reply_disabled_flag(self) -> None:
        """When HH_AUTONOMOUS_REPLY_ENABLED is off, autonomous reply must be blocked without DOM mutations."""
        from ai_assistant.hh_autonomous_agent import AutonomousJobAgent, AutonomousConfig
        from ai_assistant.hh_message_reply import HHDialog, HHMessage

        agent = AutonomousJobAgent(AutonomousConfig(mode="FULL"))
        d = HHDialog(
            conversation_id="conv_safety_456",
            employer="Test Corp 2",
            vacancy_title="Lead Architect",
            messages=[
                HHMessage(
                    message_id="m2",
                    text="Добрый день! Вы работаете удаленно?",
                    sender="employer",
                )
            ],
        )
        mock_eval_fn = MagicMock(return_value="{}")

        with patch("ai_assistant.hh_autonomous_agent._autonomous_reply_enabled", return_value=False):
            res = agent._process_messages(convs=[d], _eval_fn=mock_eval_fn)
            self.assertEqual(res.get("messages_check_status"), "CHECKED")
            self.assertEqual(res.get("auto_replies_count"), 0)

            conn = db.get_connection()
            cur = conn.cursor()
            cur.execute("SELECT status, error FROM autonomous_conversation_audits WHERE conversation_id = ?", ("conv_safety_456",))
            row = cur.fetchone()
            conn.close()

            self.assertIsNotNone(row)
            self.assertEqual(row[0], "BLOCKED_AUTONOMOUS_REPLY_DISABLED")

            for call_args in mock_eval_fn.call_args_list:
                arg_str = str(call_args)
                self.assertNotIn("targetConvId", arg_str)
                self.assertNotIn("dispatchEvent", arg_str)
                self.assertNotIn("sendBtn", arg_str)

    # =========================================================================
    # PATH 4: CLI / Browser Executor Submission Guard
    # =========================================================================
    def test_path4_browser_executor_submission_guard(self) -> None:
        """submit_application_in_browser must block when kill switch is on."""
        from ai_assistant.browser_executor import submit_application_in_browser

        stop_file = self._make_stop_file("STOP_ALL_OUTBOUNDS")
        try:
            res = submit_application_in_browser(
                vacancy_stable_id="hh:99999999",
                confirm_submit=True,
                dry_run=False,
            )
            self.assertEqual(res.status, "BLOCKED")
            self.assertIn("STOP_SUBMITS file present", str(res.error))
        finally:
            if stop_file.exists():
                stop_file.unlink()

    # =========================================================================
    # PATH 5: Manual Message Reply Gate (Review Only)
    # =========================================================================
    def test_path5_message_reply_gate_always_blocks(self) -> None:
        """hh_message_reply.SendGate must always block sends in Stage 22 review mode."""
        from ai_assistant.hh_message_reply import SendGate, HHDialog

        gate = SendGate()
        dialog = HHDialog(
            conversation_id="conv_review_1",
            vacancy_title="Developer",
            employer="Corp",
            messages=[],
        )
        res = gate.send_reply(dialog, "Reply draft")
        self.assertFalse(res.get("ok"))
        self.assertTrue(res.get("blocked"))
        self.assertIn("forbidden in Stage 22", res.get("reason", ""))

    # =========================================================================
    # PATH 6: Dynamic Reaction Test (Live Process Simulation Without Restart)
    # =========================================================================
    def test_path6_dynamic_kill_switch_reaction_in_running_process(self) -> None:
        """A long-running process must dynamically adapt to stop files and DB pause flags without restart."""
        # 1. Initially allowed under dry-run
        allowed, reason = check_outbound_allowed(action=OutboundAction.SUBMISSION, dry_run=True)
        self.assertTrue(allowed)
        self.assertIsNone(reason)

        # 2. Touch STOP_ALL_OUTBOUNDS on disk -> immediately blocked even for dry-run
        stop_file = self._make_stop_file("STOP_ALL_OUTBOUNDS")
        try:
            allowed, reason = check_outbound_allowed(action=OutboundAction.SUBMISSION, dry_run=True)
            self.assertFalse(allowed)
            self.assertIn("Emergency stop file present", reason)

            # Chat reply also immediately blocked
            chat_allowed, chat_reason = check_outbound_allowed(action=OutboundAction.CHAT_REPLY)
            self.assertFalse(chat_allowed)
            self.assertIn("Emergency stop file present", chat_reason)
        finally:
            stop_file.unlink()

        # 3. File removed -> immediately unblocked for dry-run
        allowed_after, reason_after = check_outbound_allowed(action=OutboundAction.SUBMISSION, dry_run=True)
        self.assertTrue(allowed_after)
        self.assertIsNone(reason_after)

        # 4. Set DB pause in SQLite -> immediately blocked dynamically
        db.set_system_setting("submit_paused", "1")
        allowed_db, reason_db = check_outbound_allowed(action=OutboundAction.SUBMISSION, dry_run=True)
        self.assertFalse(allowed_db)
        self.assertIn("submit_paused = 1", reason_db)

        # 5. Clear DB pause in SQLite -> immediately unblocked dynamically
        db.set_system_setting("submit_paused", "0")
        allowed_db_cleared, _ = check_outbound_allowed(action=OutboundAction.SUBMISSION, dry_run=True)
        self.assertTrue(allowed_db_cleared)


if __name__ == "__main__":
    unittest.main()
