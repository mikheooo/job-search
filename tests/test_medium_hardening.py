"""Medium hardening: config parse isolation, channel fail-closed, CDP hop, legacy locks."""

from __future__ import annotations

import ast
import importlib
import logging
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _reload_config(monkeypatch):
    from ai_assistant import config

    importlib.reload(config)
    return config


@pytest.fixture
def restored_config(monkeypatch):
    yield
    monkeypatch.undo()
    from ai_assistant import config

    importlib.reload(config)


def test_min_salary_parse_failure_does_not_wipe_word_lists(monkeypatch, caplog, restored_config):
    monkeypatch.setenv("STOP_WORDS", "PHP, Java")
    monkeypatch.setenv("REQUIRED_WORDS", "Python, n8n")
    monkeypatch.setenv("MIN_SALARY", "not-a-number")
    monkeypatch.setenv("BATCH_LIMIT", "7")
    monkeypatch.setenv("PREFERENCE_MAX_ADJUSTMENT", "3.5")

    with caplog.at_level(logging.ERROR, logger="ai_assistant.config"):
        config = _reload_config(monkeypatch)

    assert config.STOP_WORDS == ["php", "java"]
    assert config.REQUIRED_WORDS == ["python", "n8n"]
    assert config.MIN_SALARY == 0
    assert config.BATCH_LIMIT == 7
    assert config.PREFERENCE_MAX_ADJUSTMENT == 3.5
    assert any("MIN_SALARY" in rec.getMessage() for rec in caplog.records)
    assert not any(
        "STOP_WORDS" in rec.getMessage() and "Failed to parse" in rec.getMessage()
        for rec in caplog.records
    )


def test_float_parse_failure_does_not_reset_min_salary(monkeypatch, caplog, restored_config):
    monkeypatch.setenv("MIN_SALARY", "1500")
    monkeypatch.setenv("PREFERENCE_MAX_ADJUSTMENT", "nope")
    monkeypatch.setenv("STOP_WORDS", "intern")

    with caplog.at_level(logging.ERROR, logger="ai_assistant.config"):
        config = _reload_config(monkeypatch)

    assert config.MIN_SALARY == 1500
    assert config.STOP_WORDS == ["intern"]
    assert config.PREFERENCE_MAX_ADJUSTMENT == 8.0
    assert any("PREFERENCE_MAX_ADJUSTMENT" in rec.getMessage() for rec in caplog.records)


def test_telegram_channel_empty_fails_closed(monkeypatch, caplog, restored_config):
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("TELEGRAM_CHANNEL_ID", raising=False)
    config = _reload_config(monkeypatch)
    assert config.TELEGRAM_CHANNEL_ID == ""
    with caplog.at_level(logging.ERROR, logger="ai_assistant.config"), pytest.raises(config.TelegramChannelRequired):
        config.require_telegram_channel_id()
    assert any("TELEGRAM_CHANNEL_ID" in rec.getMessage() for rec in caplog.records)


def test_telegram_channel_reads_env(monkeypatch, restored_config):
    monkeypatch.setenv("TELEGRAM_CHANNEL_ID", "-100123")
    config = _reload_config(monkeypatch)
    assert config.require_telegram_channel_id() == "-100123"


def test_production_sources_do_not_hardcode_channel_default():
    needles = [
        ROOT / "ai_assistant" / "config.py",
        ROOT / "ai_assistant" / "db.py",
        ROOT / "integrations" / "hermes" / "job_search_fetcher.py",
        ROOT / "scripts" / "run_multi_source_discovery.py",
    ]
    for path in needles:
        text = path.read_text(encoding="utf-8")
        assert "-1004399255305" not in text, path.name


def test_resolve_cdp_url_requires_explicit_endpoint(monkeypatch):
    from ai_assistant import hh_browser_launcher as hl

    monkeypatch.delenv("CDP_URL", raising=False)
    monkeypatch.delenv("HH_CDP_URL", raising=False)
    monkeypatch.delenv("HH_CDP_ALLOW_FALLBACK", raising=False)
    probed = []
    monkeypatch.setattr(hl, "probe_cdp", lambda *a, **k: probed.append(a) or hl.CDP_ALIVE)
    with pytest.raises(hl.CdpUrlRequired):
        hl.resolve_cdp_url(None, allow_browseros=True)
    assert probed == []


def test_resolve_cdp_url_fallback_stays_off_in_production(monkeypatch):
    from ai_assistant import hh_browser_launcher as hl

    monkeypatch.delenv("CDP_URL", raising=False)
    monkeypatch.delenv("HH_CDP_URL", raising=False)
    monkeypatch.setenv("HH_CDP_ALLOW_FALLBACK", "1")
    monkeypatch.setattr(hl, "_clearly_nonprod", lambda: False)
    monkeypatch.setattr(hl, "probe_cdp", lambda *a, **k: hl.CDP_DEAD)
    with pytest.raises(hl.CdpUrlRequired):
        hl.resolve_cdp_url()


def test_resolve_cdp_url_opt_in_nonprod_can_hop(monkeypatch):
    from ai_assistant import hh_browser_launcher as hl

    monkeypatch.delenv("CDP_URL", raising=False)
    monkeypatch.delenv("HH_CDP_URL", raising=False)
    monkeypatch.setenv("HH_CDP_ALLOW_FALLBACK", "1")
    monkeypatch.setattr(hl, "_clearly_nonprod", lambda: True)
    monkeypatch.setattr(
        hl,
        "probe_cdp",
        lambda url, timeout=1.5: hl.CDP_ALIVE if url == hl.BROWSEROS_CDP_URL else hl.CDP_DEAD,
    )
    assert hl.resolve_cdp_url(None, allow_browseros=True) == hl.BROWSEROS_CDP_URL


def test_explicit_default_port_does_not_hop(monkeypatch):
    from ai_assistant import hh_browser_launcher as hl

    monkeypatch.delenv("CDP_URL", raising=False)
    monkeypatch.delenv("HH_CDP_URL", raising=False)
    monkeypatch.setattr(hl, "probe_cdp", lambda *a, **k: (_ for _ in ()).throw(AssertionError("probed")))
    assert hl.resolve_cdp_url(hl.DEFAULT_HH_CDP_URL) == hl.DEFAULT_HH_CDP_URL


def test_playwright_close_and_fill_log(caplog):
    from ai_assistant.browser_executor import PlaywrightBrowserAdapter

    adapter = PlaywrightBrowserAdapter(cdp_url="http://127.0.0.1:9222")

    class _Boom:
        def fill(self, *args, **kwargs):
            raise RuntimeError("fill broke")

        def close(self):
            raise RuntimeError("close broke")

    adapter.page = _Boom()
    adapter._is_cdp = True
    adapter.play = None
    with caplog.at_level(logging.DEBUG, logger="ai_assistant.browser_executor"):
        assert adapter.fill_field("input[name='email']", "a@b.c") is False
        adapter.close()
    text = caplog.text
    assert "fill_field" in text
    assert "page close" in text


def test_submit_close_handlers_leave_a_trace():
    """close/submit methods must not swallow Exception with a bare pass."""
    path = ROOT / "ai_assistant" / "browser_executor.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    silent = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in {"close", "submit_application"}:
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.ExceptHandler):
                continue
            body = sub.body
            if len(body) == 1 and isinstance(body[0], ast.Pass):
                silent.append((node.name, sub.lineno))
    assert silent == []


def test_pipeline_and_bot_are_locked():
    for module in ("ai_assistant.pipeline", "ai_assistant.bot"):
        proc = subprocess.run(
            [sys.executable, "-m", module, "--dry-run"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 1, proc.stderr
        assert "SAFETY LOCKOUT" in proc.stderr
        assert "LEGACY_" in proc.stderr


def test_pipeline_run_refuses_without_override(monkeypatch):
    from ai_assistant import pipeline

    monkeypatch.delenv("LEGACY_PIPELINE_OVERRIDE", raising=False)
    with pytest.raises(SystemExit) as exc:
        pipeline.run_pipeline(dry_run=True)
    assert exc.value.code == 1


def test_pipeline_override_reaches_the_body(monkeypatch, tmp_path):
    from ai_assistant import config, pipeline

    monkeypatch.setenv("LEGACY_PIPELINE_OVERRIDE", "1")
    missing = tmp_path / "no-vacancies.json"
    monkeypatch.setattr(config, "VACANCIES_FILE", str(missing))
    pipeline.run_pipeline(dry_run=True)


def test_legacy_scripts_do_not_pip_install_or_interpolate_js():
    auto = (ROOT / "auto_apply.py").read_text(encoding="utf-8")
    reply = (ROOT / "hh_reply.py").read_text(encoding="utf-8")
    assert "pip install" not in auto
    assert "subprocess" not in auto
    assert "f'''" not in reply
    assert 'f"""' not in reply
    assert "innerText = `" not in reply
    assert ".format(text" not in reply


def test_hh_reply_override_does_not_send(monkeypatch):
    monkeypatch.setenv("LEGACY_HH_REPLY_OVERRIDE", "1")
    sys.modules.pop("hh_reply", None)
    import hh_reply

    nasty = "`; alert(1); //"
    assert nasty not in hh_reply.CHAT_INPUT_JS
    assert "(text)" in hh_reply.CHAT_INPUT_JS
    assert hh_reply.send_reply("https://hh.ru/chat/1", nasty) is False


def test_auto_apply_override_does_not_submit(monkeypatch):
    monkeypatch.setenv("LEGACY_AUTO_APPLY_OVERRIDE", "1")
    sys.modules.pop("auto_apply", None)
    import auto_apply

    assert auto_apply.main() == 2
    assert auto_apply.process_vacancy(None, "https://hh.ru/vacancy/1", "ru") is False


def test_audit_commands_are_reexported():
    from ai_assistant import cli, cli_audit

    assert cli.audit is cli_audit.audit
    assert cli.audit_json is cli_audit.audit_json
    assert cli.audit_errors is cli_audit.audit_errors
    assert cli.audit_warnings is cli_audit.audit_warnings
    assert cli.audit_show is cli_audit.audit_show
    assert cli.audit_canonical is cli_audit.audit_canonical


def test_audit_command_still_runs(capsys):
    from ai_assistant.cli import audit

    code = audit(scope="full")
    assert code in (0, 1, 2)
    assert "APPLICATION INTEGRITY AUDIT" in capsys.readouterr().out


def test_root_hygiene_notes_job_search_db():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "job_search.db" in gitignore
    assert "artifacts/tmp/" in gitignore
    note = (ROOT / "artifacts" / "tmp" / "README.md").read_text(encoding="utf-8")
    assert "state.db" in note
    assert "zero-byte" in note
