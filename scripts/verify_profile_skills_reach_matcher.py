"""Проверка: доехали ли новые навыки профиля до МАТЧЕРА (а не только до JSON).

Читает профиль тем же путём, что и продакшен (load_candidate_profile),
затем прогоняет его через matcher._coerce_profile и смотрит, что лежит в
списках, которые матчер реально использует:

    core_skills_lc, secondary_skills_lc, transferable_skills_lc, skills_lc

Отдельно печатает, какие навыки из JSON-списков НЕ видны матчеру
(классическая ловушка: transferable_skills перезаписывается secondary).
"""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

from ai_assistant.candidate_profile import load_candidate_profile  # noqa: E402
from ai_assistant.matcher import _coerce_profile  # noqa: E402

# Навыки, которые раньше не были видны, — по ним и проверяем.
PROBE = [
    "kubernetes", "vmware", "excel", "gcp", "zapier", "grafana", "prometheus",
    "gitlab", "github actions", "ansible", "onboarding", "playwright", "django",
    "mongodb", "clickhouse", "jenkins", "windows server", "exchange", "outlook",
    "ms office", "hyper-v", "monitoring", "backups", "bitrix24", "technical support",
    "it infrastructure", "mssql", "etl", "service desk", "hardware", "imap", "gpo",
    "rag", "openai", "gemini", "anthropic", "summarization", "transcription",
]


def main() -> int:
    raw = json.loads((ROOT / "candidate_profile.json").read_text(encoding="utf-8"))
    prof = load_candidate_profile()
    p = _coerce_profile(prof)

    core = p.get("core_skills_lc", [])
    sec = p.get("secondary_skills_lc", [])
    trans = p.get("transferable_skills_lc", [])
    flat = p.get("skills_lc", [])
    visible = set(core) | set(sec) | set(trans) | set(flat)

    print(f"матчер видит: core={len(core)} secondary={len(sec)} "
          f"transferable={len(trans)} skills={len(flat)}")
    print()

    missing = [s for s in PROBE if s not in visible]
    for s in PROBE:
        mark = "OK " if s in visible else "НЕТ"
        where = []
        if s in core:
            where.append("core")
        if s in sec:
            where.append("secondary")
        if s in trans:
            where.append("transferable")
        if s in flat and s not in core:
            where.append("skills")
        print(f"  {mark} {s:<20} {','.join(where)}")
    print()

    if missing:
        print(f"НЕ ДОЕХАЛИ ({len(missing)}): {missing}")
        return 1
    print("Все проверяемые навыки видны матчеру.")

    # Ловушка с алиасом: если в JSON лежит что-то, чего нет в visible.
    json_lists = (
        list(raw.get("core_skills") or [])
        + list(raw.get("secondary_skills") or [])
        + list(raw.get("transferable_skills") or [])
    )
    dropped = sorted({s for s in json_lists if s not in visible})
    print(f"\nВ JSON, но невидимо матчеру: {dropped or 'нет'}")

    # secondary_lc и transferable_lc должны быть РАЗНЫМИ объектами/списками?
    # Нет: по дизайну transferable — алиас secondary. Проверяем, что sec не пуст.
    if not sec:
        print("ПРОБЛЕМА: secondary_skills_lc пуст — расширение не доехало")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
