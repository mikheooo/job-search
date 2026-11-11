"""Cross-platform entrypoint for running `python -m ai_assistant`."""

from __future__ import annotations

import sys

from ai_assistant.cli import main

if __name__ == "__main__":
    sys.exit(main())
