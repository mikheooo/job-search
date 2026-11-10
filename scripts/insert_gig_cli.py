# -*- coding: utf-8 -*-
"""Append the gig dispatch handler module + insert dispatch branch into cli.py."""
import io

path = r"C:\Users\Misha\Documents\job-search\ai_assistant\cli.py"
src = open(path, "r", encoding="utf-8", newline="").read()

anchor = '    elif args.command == "ui":\r\r\n        return ui_cmd(args.host, args.port)\r\r\n'
idx = src.find(anchor)
assert idx != -1, "ui dispatch anchor not found"

NL = "\r\r\n"
lines = [
    '    elif args.command == "gig":',
    "        from ai_assistant.gig.cli import run_gig_command",
    "        return run_gig_command(args)",
    "",
]
block = NL.join(lines)
src = src[: idx + len(anchor)] + block + src[idx + len(anchor) :]
open(path, "w", encoding="utf-8", newline="").write(src)
print("dispatch inserted")
