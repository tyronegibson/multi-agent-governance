"""Each test simulates one mistake an AI agent could make in a shared file."""
import copy
import json
from pathlib import Path

import pytest

import validate

EXAMPLE = json.loads((Path(__file__).parent.parent / "examples" / "pipeline.example.json").read_text())


@pytest.fixture
def base():
    return copy.deepcopy(EXAMPLE)


def rules(findings):
    return {f.rule for f in findings}


def high(findings):
    return {f.rule for f in findings if f.severity == "high"}


def session(data, by="Grok Bot"):
    """Return a copy with a properly logged session, ready for an edit."""
    new = copy.deepcopy(data)
    new["meta"]["last_updated"] = "2026-11-04"
    new["meta"]["last_updated_by"] = by
    new["changelog"].append({"date": "2026-11-04", "by": by, "changes": ["test edit"]})
    return new


# ---------------------------------------------------------------- clean cases

def test_example_is_clean(base):
    assert validate.check_file(base) == []


def test_well_behaved_session_is_clean(base):
    new = session(base)
    new["grants"][0]["last_verified"] = "2026-11-04"
    assert validate.check_change(base, new) == []


# ---------------------------------------------------------------- invented facts

def test_date_without_confidence_is_high(base):
    base["grants"][1]["next_cycle"]["confidence"] = None
    assert "dates-need-confidence" in high(validate.check_file(base))


def test_date_without_sources_is_high(base):
    base["grants"][0]["sources"] = []
    assert "dates-need-sources" in high(validate.check_file(base))


def test_unknown_confidence_word_is_high(base):
    base["grants"][0]["current_cycle"]["confidence"] = "pretty sure"
    assert "dates-need-confidence" in high(validate.check_file(base))


def test_null_dates_need_nothing(base):
    # Unknown dates stay null; that's the honest answer, not a violation.
    assert base["grants"][0]["next_cycle"]["deadline"] is None
    assert validate.check_file(base) == []


# ---------------------------------------------------------------- deleting history

def test_deleting_a_grant_is_high(base):
    new = session(base)
    new["grants"].pop()
    assert "never-delete" in high(validate.check_change(base, new))


def test_archiving_with_reason_is_fine(base):
    new = session(base)
    new["grants"][1]["priority"] = "archived"
    new["grants"][1]["notes"] = "Funder confirmed nonprofits only."
    assert validate.check_change(base, new) == []
    assert validate.check_file(new) == []


def test_archiving_without_reason_is_flagged(base):
    base["grants"][1]["priority"] = "archived"
    base["grants"][1]["notes"] = ""
    assert "archive-with-reason" in rules(validate.check_file(base))


def test_rewriting_another_agents_review_is_high(base):
    new = session(base)
    new["reviews"][0]["findings"][0]["severity"] = "low"
    assert "append-only" in high(validate.check_change(base, new))


def test_editing_old_changelog_is_high(base):
    new = session(base)
    new["changelog"][1]["changes"] = ["something else"]
    assert "append-only" in high(validate.check_change(base, new))


# ---------------------------------------------------------------- human-only actions

def test_agent_marking_submitted_is_high(base):
    new = session(base, by="Grant Bot")
    new["grants"][0]["application_status"] = "submitted"
    assert "human-only-status" in high(validate.check_change(base, new))


def test_owner_marking_submitted_is_fine(base):
    new = session(base, by="Alex Rivera")
    new["grants"][0]["application_status"] = "submitted"
    assert "human-only-status" not in rules(validate.check_change(base, new))


# ---------------------------------------------------------------- logging

def test_unlogged_edit_is_high(base):
    new = copy.deepcopy(base)
    new["grants"][0]["fit_score"] = 9
    found = validate.check_change(base, new)
    assert "log-every-session" in high(found)
    assert "bump-last-updated" in rules(found)


# ---------------------------------------------------------------- secrets

@pytest.mark.parametrize("value, label", [
    ("SSN 123-45-6789", "SSN-like"),
    ("EIN 12-3456789", "EIN-like"),
    ("key sk-abcdefghijklmnop1234", "API key"),
])
def test_secrets_are_high(base, value, label):
    base["grants"][0]["notes"] = value
    found = validate.check_file(base)
    assert any(f.rule == "no-secrets" and label in f.message for f in found)


def test_phone_numbers_and_dates_are_not_secrets(base):
    base["grants"][0]["notes"] = "Call 202-555-0143 before 2027-01-15."
    assert validate.check_file(base) == []


# ---------------------------------------------------------------- CLI

def test_cli_exit_codes(base, tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(base))
    assert validate.main([str(good)]) == 0

    bad = session(base)
    bad["grants"].pop()
    bad_path = tmp_path / "bad.json"
    bad_path.write_text(json.dumps(bad))
    assert validate.main([str(bad_path), "--previous", str(good)]) == 1


def test_changelog_entry_without_author_is_flagged(base):
    base["changelog"].append({"date": "2026-11-05", "changes": ["anonymous edit"]})
    assert "log-author" in rules(validate.check_file(base))
