import os
import sys

# ==============================================================================
# STAGE 75 SAFETY GATE: LEGACY HH_REPLY IS DEPRECATED & LOCKED
# ==============================================================================
# All HeadHunter recruiter dialogs MUST be handled through canonical Stage 22-53
# modules (hh_message_watcher.py, hh_message_reply.py, hh_autonomous_agent.py)
# to guarantee truth-only facts, candidate profile validation, and state.db recording.
# Raw automated injection is strictly forbidden to prevent dual-reply races.
# ==============================================================================

if not os.environ.get("LEGACY_HH_REPLY_OVERRIDE"):
    print(
        "[SAFETY LOCKOUT] 'hh_reply.py' is deprecated and locked to prevent dual-reply conflicts.\n"
        "To triage or send recruiter replies with truth-only validation, use:\n"
        "  - Triage/Preview: python -m ai_assistant.cli hh-message triage\n"
        "  - Preview draft:  python -m ai_assistant.cli hh-message preview [conversation_id]\n"
        "  - Send confirmed: python -m ai_assistant.cli hh-message send [conversation_id] --confirm\n",
        file=sys.stderr,
    )
    sys.exit(1)


# User text is a Playwright argument, never interpolated into the script.
# The old body built the script with an f-string, so a message could break out
# of the JavaScript string. This constant is the only permitted shape.
CHAT_INPUT_JS = """(text) => {
    const nodes = document.querySelectorAll('[contenteditable="true"], textarea');
    if (!nodes.length) return false;
    const t = nodes[nodes.length - 1];
    if (t.tagName === "TEXTAREA") t.value = text;
    else t.textContent = text;
    t.dispatchEvent(new Event("input", { bubbles: true }));
    return true;
}"""


def send_reply(url, text):
    """Quarantined. Does not connect to Chrome and does not send.

    LEGACY_HH_REPLY_OVERRIDE unlocks import only. The message is not written
    into a page and the send button is not clicked. CHAT_INPUT_JS is the safe
    argument-passing form if a future audited path needs it.
    """
    if not isinstance(text, str):
        print("[SAFETY] hh_reply refused a non-string message.", file=sys.stderr)
        return False
    # Reference the safe script so it stays the injection path of record.
    _ = CHAT_INPUT_JS
    print(
        "[SAFETY] hh_reply.send_reply is quarantined and will not connect to CDP "
        f"or send a message to {url!r}.\n"
        "Use: python -m ai_assistant.cli hh-message send [conversation_id] --confirm",
        file=sys.stderr,
    )
    return False


if __name__ == "__main__":
    if len(sys.argv) > 2:
        ok = send_reply(sys.argv[1], sys.argv[2])
        sys.exit(0 if ok else 2)
    print("Usage: python hh_reply.py <URL> <TEXT>", file=sys.stderr)
    sys.exit(2)
