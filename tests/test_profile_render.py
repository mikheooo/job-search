"""T8: ``candidate_profile.json`` is generated from the knowledge base.

The profile stopped being a source of truth. These tests hold the three
properties that make that safe:

* the render is complete -- no field of ``CandidateProfile`` is dropped silently;
* the render is fail-closed -- a field the KB cannot supply stops it rather than
  being invented;
* the render is faithful -- the live KB reproduces the live profile exactly,
  which is the acceptance criterion for T8.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_assistant.candidate import profile_render as pr
from ai_assistant.candidate.kb import CandidateKnowledgeBase, ClaimRecord

REPO_ROOT = Path(__file__).resolve().parent.parent
LIVE_KB = REPO_ROOT / "candidate_kb.json"
LIVE_PROFILE = REPO_ROOT / "candidate_profile.json"


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _preferences(**overrides: object) -> dict[str, object]:
    """A preference section that satisfies the render, with room to override.

    Built from ``PREFERENCE_FIELDS`` rather than hard-coded, so a new field is
    covered here the moment it is classified instead of turning every test red
    for the wrong reason.
    """
    prefs: dict[str, object] = {field: [] for field in pr.PREFERENCE_FIELDS}
    prefs.update(
        {
            "minimum_salary": 1000,
            "salary_currency": "USD",
            "years_experience": 3,
            "years_of_experience": 3,
            "remote_required": True,
            "provenance": {"name": "CONFIRMED"},
            "skill_confidence": {"python": "PROFESSIONAL"},
        }
    )
    prefs.update(overrides)
    return prefs


def _kb(**overrides: object) -> CandidateKnowledgeBase:
    claims = {
        "name-1": ClaimRecord(claim_id="name-1", kind="name", value="Test Candidate"),
        "email-1": ClaimRecord(
            claim_id="email-1", kind="contact_email", value="test@example.com"
        ),
    }
    return CandidateKnowledgeBase(claims=claims, preferences=_preferences(**overrides))


@pytest.fixture
def kb_file(tmp_path: Path) -> Path:
    path = tmp_path / "kb.json"
    _kb().save(path)
    return path


@pytest.fixture
def profile_file(tmp_path: Path) -> Path:
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(pr.render_profile(_kb()), ensure_ascii=False), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# Completeness: no field may be forgotten
# --------------------------------------------------------------------------


def test_every_profile_field_is_classified() -> None:
    """A field of ``CandidateProfile`` that no rule covers would be dropped.

    This is the test that catches the model growing a field: add one without
    deciding where it comes from and the render refuses to run.
    """
    assert pr.unclassified_fields() == ()


def test_the_classification_covers_the_model_exactly() -> None:
    classified = set(pr.CLAIM_FIELDS) | set(pr.PREFERENCE_FIELDS) | set(pr.FILE_EXCLUDED_FIELDS)
    assert classified == set(pr.profile_field_names())


def test_the_field_list_comes_from_the_model_not_an_empty_instance() -> None:
    """``to_dict()`` on an empty profile hides the optional contact fields.

    Measured: an empty instance serialises 32 keys, a populated one 39, because
    ``to_dict`` writes ``linkedin``, ``github`` and friends only when they are
    set. Reading the list from the dataclass is what keeps the completeness check
    honest.
    """
    from ai_assistant.candidate_profile import CandidateProfile

    empty_keys = set(CandidateProfile().to_dict())
    assert len(pr.profile_field_names()) > len(empty_keys)


def test_no_field_appears_in_two_places() -> None:
    """A field classified twice would let the two rules disagree silently."""
    claim = set(pr.CLAIM_FIELDS)
    preference = set(pr.PREFERENCE_FIELDS)
    excluded = set(pr.FILE_EXCLUDED_FIELDS)
    assert not claim & preference
    assert not claim & excluded
    assert not preference & excluded


# --------------------------------------------------------------------------
# Faithfulness: the render reproduces what the KB says
# --------------------------------------------------------------------------


def test_render_reads_contacts_from_claims() -> None:
    profile = pr.render_profile(_kb())
    assert profile["name"] == "Test Candidate"
    assert profile["email"] == "test@example.com"


def test_render_reads_preferences_verbatim() -> None:
    profile = pr.render_profile(_kb(excluded_roles=["php"], minimum_salary=2000))
    assert profile["excluded_roles"] == ["php"]
    assert profile["minimum_salary"] == 2000


def test_render_produces_every_field_the_file_carries() -> None:
    profile = pr.render_profile(_kb())
    assert set(profile) == set(pr.CLAIM_FIELDS) | set(pr.PREFERENCE_FIELDS)


def test_a_claim_that_left_its_source_is_not_used_for_contacts() -> None:
    """A resume no longer carrying the address must not put it on a letter."""
    kb = _kb()
    kb.claims["email-1"].in_source = False
    with pytest.raises(pr.ProfileRenderError) as excinfo:
        pr.render_profile(kb)
    assert "email" in str(excinfo.value)


def test_two_live_claims_for_one_contact_field_are_refused() -> None:
    """Silently picking one of two addresses is a coin flip, not a render."""
    kb = _kb()
    kb.claims["email-2"] = ClaimRecord(
        claim_id="email-2", kind="contact_email", value="other@example.com"
    )
    with pytest.raises(pr.ProfileRenderError) as excinfo:
        pr.render_profile(kb)
    assert "email-2" in str(excinfo.value) or "2 live" in str(excinfo.value)


def test_transferable_skills_survive_the_render() -> None:
    """``CandidateProfile.__post_init__`` aliases this field onto secondary_skills.

    The live file keeps a genuinely different list, so a render that went through
    the model would quietly overwrite it. This test pins that the render writes
    the KB's value through untouched.
    """
    kb = _kb(
        secondary_skills=["docker"],
        transferable_skills=["troubleshooting"],
    )
    profile = pr.render_profile(kb)
    assert profile["secondary_skills"] == ["docker"]
    assert profile["transferable_skills"] == ["troubleshooting"]


# --------------------------------------------------------------------------
# Fail-closed: never invent a field
# --------------------------------------------------------------------------


def test_a_missing_preference_stops_the_render() -> None:
    kb = _kb()
    del kb.preferences["excluded_roles"]
    with pytest.raises(pr.ProfileRenderError) as excinfo:
        pr.render_profile(kb)
    assert "excluded_roles" in str(excinfo.value)


def test_every_missing_field_is_named_at_once() -> None:
    """One run should list everything absent, not one field per attempt."""
    kb = _kb()
    del kb.preferences["excluded_roles"]
    del kb.preferences["minimum_salary"]
    with pytest.raises(pr.ProfileRenderError) as excinfo:
        pr.render_profile(kb)
    message = str(excinfo.value)
    assert "excluded_roles" in message
    assert "minimum_salary" in message


def test_a_missing_claim_stops_the_render() -> None:
    kb = CandidateKnowledgeBase(claims={}, preferences=_preferences())
    with pytest.raises(pr.ProfileRenderError) as excinfo:
        pr.render_profile(kb)
    assert "name" in str(excinfo.value)


def test_an_empty_preference_list_is_not_a_missing_field() -> None:
    """``allowed_timezones`` is legitimately ``[]``; absent is a different thing."""
    kb = _kb(allowed_timezones=[])
    assert pr.render_profile(kb)["allowed_timezones"] == []


def test_the_render_never_writes_a_partial_profile(tmp_path: Path) -> None:
    kb = _kb()
    del kb.preferences["excluded_roles"]
    kb_path = tmp_path / "kb.json"
    kb.save(kb_path)
    profile_path = tmp_path / "profile.json"
    profile_path.write_text('{"sentinel": true}', encoding="utf-8")

    code = pr.candidate_profile_render_cmd(
        kb_path=str(kb_path), profile_path=str(profile_path)
    )
    assert code == 1
    assert json.loads(profile_path.read_text(encoding="utf-8")) == {"sentinel": True}


# --------------------------------------------------------------------------
# Diffing
# --------------------------------------------------------------------------


def test_diff_is_empty_when_the_file_matches() -> None:
    kb = _kb()
    rendered = pr.render_profile(kb)
    assert pr.diff_profiles(rendered, rendered) == ()


def test_diff_names_each_disagreeing_field() -> None:
    kb = _kb(minimum_salary=2000)
    rendered = pr.render_profile(kb)
    current = dict(rendered)
    current["minimum_salary"] = 1000
    diff = pr.diff_profiles(current, rendered)
    assert [item.field for item in diff] == ["minimum_salary"]
    assert diff[0].current == 1000
    assert diff[0].rendered == 2000


def test_diff_reports_a_field_present_only_in_the_file() -> None:
    """A key the KB does not render is a difference, not something to ignore."""
    rendered = pr.render_profile(_kb())
    current = dict(rendered)
    current["something_extra"] = 1
    diff = pr.diff_profiles(current, rendered)
    assert [item.field for item in diff] == ["something_extra"]


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    kb_path = tmp_path / "kb.json"
    _kb(minimum_salary=2000).save(kb_path)
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(pr.render_profile(_kb()), ensure_ascii=False), encoding="utf-8")
    before = profile_path.read_bytes()

    code = pr.candidate_profile_render_cmd(
        kb_path=str(kb_path), profile_path=str(profile_path), dry_run=True
    )
    assert code == 0
    assert profile_path.read_bytes() == before


def test_a_real_run_writes_the_rendered_profile(tmp_path: Path) -> None:
    kb_path = tmp_path / "kb.json"
    _kb(minimum_salary=2000).save(kb_path)
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(pr.render_profile(_kb()), ensure_ascii=False), encoding="utf-8")

    code = pr.candidate_profile_render_cmd(
        kb_path=str(kb_path), profile_path=str(profile_path)
    )
    assert code == 0
    written = json.loads(profile_path.read_text(encoding="utf-8"))
    assert written["minimum_salary"] == 2000


def test_a_clean_render_leaves_the_file_alone(tmp_path: Path) -> None:
    """Nothing to do means nothing written, not a rewrite in another format."""
    kb_path = tmp_path / "kb.json"
    _kb().save(kb_path)
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(pr.render_profile(_kb()), ensure_ascii=False), encoding="utf-8")
    before = profile_path.read_bytes()

    code = pr.candidate_profile_render_cmd(
        kb_path=str(kb_path), profile_path=str(profile_path)
    )
    assert code == 0
    assert profile_path.read_bytes() == before


# --------------------------------------------------------------------------
# Import: the profile's decisions land in the KB
# --------------------------------------------------------------------------


def test_the_import_covers_every_preference_field() -> None:
    """A field the import skipped would block the very first render."""
    live = pr.load_profile_file(LIVE_PROFILE)
    imported = pr.preferences_from_profile(live)
    assert set(imported) == set(pr.PREFERENCE_FIELDS)


# --------------------------------------------------------------------------
# The duplicate profile copy stays gone
# --------------------------------------------------------------------------


def test_the_second_profile_copy_does_not_come_back() -> None:
    """T8 deleted it: byte-identical to the root file, read by nothing.

    kb_verify reported it as ``profile_copy_drift`` and DEFAULT_PROFILE_PATHS
    reached it through two different entries, so a stray copy would be a trap
    rather than dead weight.
    """
    assert not (REPO_ROOT / "ai_assistant" / "candidate_profile.json").exists()
    from ai_assistant.candidate_profile import DEFAULT_PROFILE_PATHS

    assert list(DEFAULT_PROFILE_PATHS) == [Path("candidate_profile.json")]


# --------------------------------------------------------------------------
# Acceptance: the live KB reproduces the live profile
# --------------------------------------------------------------------------


def test_the_live_kb_reproduces_the_live_profile() -> None:
    """The T8 acceptance criterion, measured rather than assumed.

    Read-only: it compares the render with the file on disk and writes nothing.
    """
    result = pr.render_from_paths(LIVE_KB, LIVE_PROFILE)
    assert result.is_clean, [item.describe() for item in result.diff]
    assert len(result.rendered) == len(pr.CLAIM_FIELDS) + len(pr.PREFERENCE_FIELDS)
