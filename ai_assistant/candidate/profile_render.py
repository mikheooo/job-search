"""Render ``candidate_profile.json`` from the knowledge base (T8).

The profile stops being a source of truth. It becomes a derived artifact: the KB
holds the facts (``claims``, read out of the resume, the interview and the
candidate's own repositories) and the search decisions (``preferences``), and
this module turns both into the JSON the rest of the pipeline reads.

Two design rules, both deliberate:

**Fail-closed, never fail-soft.** A field the KB cannot supply is not filled with
a guess, an empty list or a default. The render stops and names every missing
field at once. A profile that quietly lost ``excluded_roles`` would keep sending
applications to companies the candidate ruled out, and nothing downstream would
notice until an employer did.

**No field may be forgotten.** ``CandidateProfile`` has 39 fields. The render
walks that model rather than a hand-kept list, so adding a field to the profile
without deciding where it comes from fails the render instead of silently
dropping it. This is what makes "no existing field is lost" checkable rather than
hopeful.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .kb import CandidateKnowledgeBase

# --------------------------------------------------------------------------
# Where each profile field comes from
# --------------------------------------------------------------------------

#: Fields read out of claims, so they cannot drift from the resume. The value is
#: the claim kind that carries them.
CLAIM_FIELDS: dict[str, str] = {
    "name": "name",
    "email": "contact_email",
}

#: Everything else lives in the KB's ``preferences`` section. These are not
#: facts about the candidate -- they are decisions about the search (which roles
#: to look for, what to exclude, the salary floor) plus contact details the
#: resume does not carry. No source contains them, which is exactly why they
#: cannot be claims.
PREFERENCE_FIELDS: tuple[str, ...] = (
    "target_roles",
    "desired_roles",
    "alternative_roles",
    "role_families",
    "role_priorities",
    "role_family_seniority",
    "domain_years",
    "core_skills",
    "secondary_skills",
    "transferable_skills",
    "skills",
    "provenance",
    "skill_confidence",
    "role_specific_skills",
    "seniority_range",
    "preferred_seniority",
    "years_experience",
    "years_of_experience",
    "remote_required",
    "allowed_locations",
    "allowed_timezones",
    "languages",
    "employment_types",
    "minimum_salary",
    "salary_currency",
    "phone_ru",
    "phone_th",
    "linkedin",
    "github",
    "portfolio",
    "excluded_roles",
    "excluded_companies",
    "excluded_countries",
    "excluded_industries",
    "must_avoid_conditions",
)

#: Fields ``CandidateProfile`` knows about but the profile *file* has never
#: carried, so the render does not write them. Each one is here for a measured
#: reason, not for convenience:
#:
#: * ``location_constraints`` is a mirror of ``allowed_locations`` --
#:   ``CandidateProfile.__post_init__`` assigns one from the other -- so writing
#:   both would duplicate a single decision in two places.
#: * ``industries`` is empty and read by nobody outside the model itself.
#: * ``phone`` is the generic slot; ``browser_executor`` reads ``phone_ru`` and
#:   ``phone_th``, which are real fields below.
#:
#: Kept as an explicit tuple on purpose: a field landing here by accident would
#: be a silent loss, and the completeness check below would not catch it.
FILE_EXCLUDED_FIELDS: tuple[str, ...] = (
    "location_constraints",
    "industries",
    "phone",
)

#: Dataclass fields that are not profile-file fields at all -- internal
#: alternates the model carries for other callers.
NOT_PROFILE_FIELDS: tuple[str, ...] = ("employment_type", "salary_preferences")


class ProfileRenderError(RuntimeError):
    """The KB cannot supply the profile. Raised instead of guessing."""


@dataclass(frozen=True)
class FieldDiff:
    """One field where the KB's rendering disagrees with the file on disk."""

    field: str
    current: Any
    rendered: Any

    def describe(self) -> str:
        return f"{self.field}: file={self.current!r} kb={self.rendered!r}"


@dataclass(frozen=True)
class RenderResult:
    """A rendered profile plus how it compares with what is already on disk."""

    rendered: dict[str, Any]
    diff: tuple[FieldDiff, ...]

    @property
    def is_clean(self) -> bool:
        return not self.diff


def profile_field_names() -> tuple[str, ...]:
    """Every field the profile model carries, read from the model itself.

    Taken from the dataclass rather than from ``to_dict()`` of an empty
    instance: ``to_dict`` writes the optional contact fields only when they are
    set, so an empty profile hides eight of them and the completeness check
    below would wave a lost field through.
    """
    import dataclasses

    from ..candidate_profile import CandidateProfile

    return tuple(
        f.name
        for f in dataclasses.fields(CandidateProfile)
        if not f.name.startswith("_") and f.name not in NOT_PROFILE_FIELDS
    )


def unclassified_fields() -> tuple[str, ...]:
    """Profile fields that neither a claim, a preference nor an exclusion covers.

    Non-empty means the model grew a field and nobody decided where it comes
    from. That is a bug in this module, not in the KB, and it must be loud.
    """
    known = set(CLAIM_FIELDS) | set(PREFERENCE_FIELDS) | set(FILE_EXCLUDED_FIELDS)
    return tuple(sorted(set(profile_field_names()) - known))


def _claim_value(kb: CandidateKnowledgeBase, kind: str) -> Any:
    """The value of the single claim of ``kind``, or None if there is none.

    Only claims still present in their source count. A claim the resume no
    longer carries is history, and rendering contact details out of history
    would put a stale address on an outgoing letter.
    """
    values = [
        record.value
        for record in kb.claims.values()
        if record.kind == kind and record.in_source
    ]
    if not values:
        return None
    if len(values) > 1:
        raise ProfileRenderError(
            f"the KB holds {len(values)} live '{kind}' claims ({values!r}); "
            "the profile can carry only one. Resolve the duplicate first."
        )
    return values[0]


def missing_fields(kb: CandidateKnowledgeBase) -> tuple[str, ...]:
    """Profile fields the KB cannot currently supply, in one list.

    Both halves are checked: a claim kind with no live claim, and a preference
    key that was never recorded. Reported together so one run names everything
    that is missing instead of one field per attempt.
    """
    absent: list[str] = []
    for field, kind in CLAIM_FIELDS.items():
        if _claim_value(kb, kind) is None:
            absent.append(f"{field} (no live '{kind}' claim)")
    for field in PREFERENCE_FIELDS:
        if field not in kb.preferences:
            absent.append(f"{field} (not in kb.preferences)")
    return tuple(absent)


def render_profile(kb: CandidateKnowledgeBase) -> dict[str, Any]:
    """Build the profile dict from the KB.

    Raises ``ProfileRenderError`` listing every missing field when the KB is
    incomplete, and when the profile model has a field nobody classified.
    """
    unclassified = unclassified_fields()
    if unclassified:
        raise ProfileRenderError(
            "these CandidateProfile fields are neither claim- nor "
            f"preference-supplied, so the render would drop them: {list(unclassified)}. "
            "Add them to CLAIM_FIELDS or PREFERENCE_FIELDS in profile_render.py."
        )

    absent = missing_fields(kb)
    if absent:
        raise ProfileRenderError(
            "the KB cannot supply these profile fields, and this render will not "
            "invent them:\n  - " + "\n  - ".join(absent)
        )

    profile: dict[str, Any] = {}
    for field, kind in CLAIM_FIELDS.items():
        profile[field] = _claim_value(kb, kind)
    for field in PREFERENCE_FIELDS:
        profile[field] = kb.preferences[field]
    return profile


def diff_profiles(
    current: dict[str, Any], rendered: dict[str, Any]
) -> tuple[FieldDiff, ...]:
    """Field-by-field disagreement between the file and the KB's rendering."""
    fields = sorted(set(current) | set(rendered))
    return tuple(
        FieldDiff(field, current.get(field), rendered.get(field))
        for field in fields
        if current.get(field) != rendered.get(field)
    )


def preferences_from_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """The ``preferences`` section for a profile dict.

    Used by the one-off import: it lifts the decisions that already live in the
    profile file into the KB, so the KB describes the profile it is about to
    start generating. Without this step the first render would have nothing to
    render from.
    """
    return {field: profile[field] for field in PREFERENCE_FIELDS if field in profile}


def load_profile_file(path: str | Path) -> dict[str, Any]:
    """Read a profile JSON file as a plain dict."""
    import json

    return json.loads(Path(path).read_text(encoding="utf-8"))


def render_from_paths(
    kb_path: str | Path, profile_path: str | Path
) -> RenderResult:
    """Load the KB, render, and compare with the profile already on disk."""
    kb = CandidateKnowledgeBase.load(kb_path)
    current = load_profile_file(profile_path) if Path(profile_path).is_file() else {}
    rendered = render_profile(kb)
    return RenderResult(rendered=rendered, diff=diff_profiles(current, rendered))


def write_profile(path: str | Path, profile: dict[str, Any]) -> None:
    """Write the rendered profile atomically.

    A half-written profile is worse than a stale one: the submit path reads
    ``excluded_roles`` and ``minimum_salary`` from it, and a truncated file
    would fail open rather than closed. Same temp-then-rename dance the KB uses.
    """
    import json
    import os

    target = Path(path)
    text = json.dumps(profile, indent=2, ensure_ascii=False) + "\n"
    temp = target.with_suffix(target.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, target)


def candidate_profile_render_cmd(
    *,
    kb_path: str | None = None,
    profile_path: str | None = None,
    dry_run: bool = False,
    as_json: bool = False,
) -> int:
    """``candidate-profile render`` -- generate candidate_profile.json from the KB.

    Returns 0 when the profile already matches the KB or was written, and 1 when
    the KB cannot supply the profile. It never writes a partial or guessed
    profile: a missing field stops the render and is named.
    """
    import json

    from .kb import resolve_kb_path

    resolved_kb = resolve_kb_path(kb_path)
    resolved_profile = Path(profile_path) if profile_path else Path("candidate_profile.json")

    try:
        result = render_from_paths(resolved_kb, resolved_profile)
    except ProfileRenderError as exc:
        if as_json:
            print(json.dumps({"status": "BLOCKED", "errors": [str(exc)]}, ensure_ascii=False, indent=2))
        else:
            print("BLOCKED: the knowledge base cannot supply the profile.")
            print(str(exc))
        return 1

    if result.is_clean:
        status = "UNCHANGED"
    elif dry_run:
        status = "DRY-RUN"
    else:
        status = "WRITTEN"

    if as_json:
        print(
            json.dumps(
                {
                    "status": status,
                    "kb_path": str(resolved_kb),
                    "profile_path": str(resolved_profile),
                    "fields": len(result.rendered),
                    "diff": [
                        {"field": d.field, "file": d.current, "kb": d.rendered}
                        for d in result.diff
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if result.is_clean:
        print(f"profile matches the knowledge base: {len(result.rendered)} fields, no differences.")
        print(f"  kb:      {resolved_kb}")
        print(f"  profile: {resolved_profile}")
        return 0

    print(f"{len(result.diff)} field(s) differ between the KB and {resolved_profile}:")
    for item in result.diff:
        print(f"  {item.field}:")
        print(f"    file: {json.dumps(item.current, ensure_ascii=False)}")
        print(f"    kb:   {json.dumps(item.rendered, ensure_ascii=False)}")

    if dry_run:
        print("\ndry run: nothing written.")
        return 0

    write_profile(resolved_profile, result.rendered)
    print(f"\nwritten: {resolved_profile} ({len(result.rendered)} fields)")
    return 0
