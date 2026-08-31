"""Regression test: mixed int/string vacancy IDs in save_vacancies().

Old code (Stage 72): max(v.get("id", 0) for v in current)
  -> TypeError: '>' not supported between instances of 'str' and 'int'
New code: int-only max with default=0.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["JOB_FETCHER_NO_REEXEC"] = "1"  # skip venv re-exec guard under pytest
sys.path.insert(0, r"C:\Users\Misha\AppData\Local\hermes\profiles\jobs\scripts")
import job_search_fetcher as jsf

MIXED = [
    {"id": 83, "url": "https://a/1", "title": "old-int"},
    {"id": "wwr_1787143139581_9381", "url": "https://a/2", "title": "old-wwr"},
    {"url": "https://a/3", "title": "no-id"},
]


def test_old_expression_reproduced():
    """Confirm the OLD expression really failed on mixed IDs."""
    try:
        max(v.get("id", 0) for v in MIXED)
    except TypeError as e:
        print(f"REPRODUCED old bug: TypeError: {e}")
        return
    raise AssertionError("Expected old expression to raise TypeError on mixed IDs")


def test_save_vacancies_mixed_ids():
    tmp = Path(tempfile.mkdtemp()) / "vacancies.json"
    tmp.write_text(json.dumps(MIXED), encoding="utf-8")
    original = jsf.VACANCIES_FILE
    jsf.VACANCIES_FILE = tmp
    try:
        jsf.save_vacancies([{"title": "New Job", "url": "https://new/1"}])
    finally:
        jsf.VACANCIES_FILE = original

    out = json.loads(tmp.read_text(encoding="utf-8"))

    assert len(out) == 4, f"expected 4 records, got {len(out)}"
    new = [v for v in out if v["url"] == "https://new/1"]
    assert new and new[0]["id"] == 84, f"new item must get id=84, got {new}"
    assert out[1]["id"] == "wwr_1787143139581_9381", "string wwr ID must be unchanged"
    assert out[0] == MIXED[0], "existing int-ID record must be unchanged"
    assert out[2] == MIXED[2], "record without id must be unchanged"
    new_rec = new[0]
    assert new_rec["applied"] is False and "found_at" in new_rec
    print("PASS: new id=84, wwr ID preserved, existing records unchanged, no TypeError")


if __name__ == "__main__":
    test_old_expression_reproduced()
    test_save_vacancies_mixed_ids()
    print("ALL TESTS PASS")
