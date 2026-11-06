import os
import sys

# ==============================================================================
# STAGE 75 SAFETY GATE: LEGACY AUTO_APPLY IS DEPRECATED & LOCKED
# ==============================================================================
# All HeadHunter applications MUST flow through canonical Stage 17-51 architecture:
# matcher.py -> candidate_profile.json -> truth-only Q&A -> prefill_orchestrate ->
# application_review_gate.py -> hh_controlled_submit.py -> hh_post_submit_verifier.py.
# Direct un-gated application scripts are strictly forbidden in production.
# ==============================================================================

if not os.environ.get("LEGACY_AUTO_APPLY_OVERRIDE"):
    print(
        "[SAFETY LOCKOUT] 'auto_apply.py' is deprecated and blocked from direct execution.\n"
        "To run applications with defense-in-depth safety gates and post-verification, use:\n"
        "  - Single application: python -m ai_assistant.cli submit <vacancy_id> --confirm-submit\n"
        "  - Batch runner:       python -m ai_assistant.cli application runner next --confirm-submit\n"
        "  - Autonomous agent:   python -m ai_assistant.cli autonomous once\n",
        file=sys.stderr,
    )
    sys.exit(1)


def process_vacancy(page, url, lang):
    """Quarantined. Does not open a browser or click submit."""
    print(
        "[SAFETY] auto_apply.process_vacancy is quarantined and will not submit.\n"
        f"Refusing {url!r} (lang={lang!r}).",
        file=sys.stderr,
    )
    return False


def main() -> int:
    """Quarantined body. No mid-run package install and no application click.

    LEGACY_AUTO_APPLY_OVERRIDE only unlocks import so the lock can be inspected.
    It does not restore the old Playwright submit loop.
    """
    print(
        "[SAFETY] auto_apply.main is quarantined. It will not pip-install Playwright "
        "and it will not click Откликнуться / Submit.\n"
        "Use: python -m ai_assistant.cli submit <vacancy_id> --confirm-submit",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
