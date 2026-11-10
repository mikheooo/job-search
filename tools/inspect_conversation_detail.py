"""Read-only dump of one HH conversation's DOM, for triage.

T7.25 §9. This module used to launch Chrome at **import** time - a bare
``ensure_hh_browser()`` sat at module level, so merely importing it (a test
collecting the directory, an editor indexing it, a REPL tab completing it)
started a browser on the dedicated profile and left it running with nobody
owning it. That is one of the ways the T7.24 orphan could have appeared.

Two things changed:

* the launch moved into ``main()`` under ``if __name__ == "__main__"``, so an
  import is inert - no Chrome, no 9111, no lease, no state written;
* the launch is now an *owned* operation (``hh_operation``), so the browser is
  released on every exit, including an exception mid-dump.

    python tools/inspect_conversation_detail.py [conversation_id]
"""

from __future__ import annotations

import json
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_CONVERSATION_ID = "5585426482"

_INSPECT_JS = """(() => {
    const messages = Array.from(document.querySelectorAll('[class*="message"], [data-qa*="message"], [class*="bubble"]')).map(el => ({
        tag: el.tagName,
        qa: el.getAttribute('data-qa'),
        className: el.className,
        text: (el.innerText || '').trim()
    })).filter(m => m.text && m.text.length < 500);

    const allTextBlocks = Array.from(document.querySelectorAll('div, p, span')).filter(el => {
        const t = (el.innerText || '').trim();
        return t.includes('Михаил Кириллович') || t.includes('Здравствуйте') || t.includes('резюме');
    }).map(el => ({
        tag: el.tagName,
        className: el.className,
        qa: el.getAttribute('data-qa'),
        text: el.innerText.trim()
    }));

    const composer = document.querySelector('textarea, [contenteditable="true"], [data-qa*="composer"], [data-qa*="input"]');

    return JSON.stringify({
        url: location.href,
        title: document.title,
        bodySnippet: (document.body.innerText || '').slice(0, 600),
        messagesSample: messages.slice(0, 15),
        textBlocks: allTextBlocks.slice(0, 5),
        composer: composer ? {tag: composer.tagName, qa: composer.getAttribute('data-qa')} : null
    });
})()"""


def inspect_conversation_detail(conversation_id: str = DEFAULT_CONVERSATION_ID) -> dict:
    """Read one conversation's DOM. Read-only: never sends, never submits.

    The browser exists for exactly the length of this call. ``evaluate_on`` is
    addressed by target id rather than by URL substring, because with several
    hh.ru tabs open "the hh.ru tab" is not a well-defined object.
    """
    from ai_assistant import hh_browser_lease as lease

    url = f"https://hh.ru/chat/{conversation_id}"
    with lease.hh_operation(
        lease.OWNER_TOOL, purpose="messages",
        session_kwargs={"require_auth": False},
    ) as op:
        session = op["session"]
        start = session.start()
        if not start.get("ok"):
            return {"ok": False, "conversation_id": conversation_id,
                    "error": start.get("message") or "the headless browser is unavailable"}

        target = session.ensure_work_tab()
        if target is None:
            return {"ok": False, "conversation_id": conversation_id,
                    "error": "could not open a working tab"}

        session.navigate(target, url)
        raw = session.evaluate_on(target, _INSPECT_JS)
        return {
            "ok": True,
            "conversation_id": conversation_id,
            "data": json.loads(raw) if isinstance(raw, str) else raw,
            "release": op["release_report"],
        }


def main(argv: list[str]) -> int:
    conversation_id = argv[1] if len(argv) > 1 else DEFAULT_CONVERSATION_ID
    res = inspect_conversation_detail(conversation_id)
    if not res.get("ok"):
        print(f"failed: {res.get('error')}", file=sys.stderr)
        return 1
    print(f"=== CONVERSATION {conversation_id} DETAIL ===")
    print(json.dumps(res["data"], indent=2, ensure_ascii=False))
    release = res.get("release") or {}
    print(
        "\nrelease: "
        f"stopped={release.get('stopped')} port_freed={release.get('port_freed')} "
        f"profile_unlocked={release.get('profile_unlocked')} "
        f"owned_processes={release.get('owned_processes')}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
