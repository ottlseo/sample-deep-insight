"""Graders must pass a clean run and catch each injected defect."""
import json

import pytest

from fixtures import make_run
from grade import grade_run, load_scenario
from graders.numbers import matches, parse_numbers


@pytest.fixture
def scenario():
    return load_scenario("moon_market_kr")


@pytest.fixture
def clean(tmp_path):
    return make_run.make_clean_run(tmp_path / "run")


def grade(run_dir, scenario):
    return grade_run(run_dir, scenario["csv"], scenario["answer_key"])["scores"]


def test_clean_run_passes(clean, scenario):
    s = grade(clean, scenario)
    assert s["core_pass"], s["core_fail_reasons"]
    assert s["citation_value_match_rate"] == 1.0
    assert s["cited_value_match_rate"] == 1.0
    assert s["recompute_supported"] == 4  # GROUP BY is not recomputed
    assert s["recompute_match_rate"] == 1.0
    assert s["core_fact_recall"] == 1.0
    assert s["audit_pass"] is True


@pytest.mark.parametrize("corrupt, metric, expected", [
    (make_run.corrupt_citation_value, "citations_ok", False),
    (make_run.corrupt_metadata_value, "recompute_match_rate", 0.75),
    (make_run.corrupt_report_number, "cited_value_match_rate", 0.8),
    (make_run.corrupt_broken_ref, "broken_citation_refs", 1),
    (make_run.corrupt_missing_file, "all_required_ok", False),
])
def test_each_defect_is_caught(clean, scenario, corrupt, metric, expected):
    corrupt(clean)
    s = grade(clean, scenario)
    assert s[metric] == pytest.approx(expected) if isinstance(expected, float) else s[metric] == expected
    assert not s["core_pass"]
    assert metric in s["core_fail_reasons"]


def test_missing_artifacts_still_produce_scores(tmp_path, scenario):
    (tmp_path / "artifacts").mkdir()
    s = grade(tmp_path, scenario)
    assert s["required_present"] == 0
    assert not s["core_pass"]


def test_failed_status_fails_core(clean, scenario):
    (clean / "run.json").write_text(json.dumps({"status": "error"}))
    s = grade(clean, scenario)
    assert s["core_fail_reasons"][0] == "status"


@pytest.mark.parametrize("text, value", [
    ("16,431,923원", 16431923), ("₩2,958,765", 2958765), ("19,655.41", 19655.41),
    ("87.8%", 87.8), ("1,643만", 16430000), ("3.5M", 3.5e6),
])
def test_parse_numbers(text, value):
    assert parse_numbers(text)[0][0] == pytest.approx(value)


@pytest.mark.parametrize("printed, decimals, stored, ok", [
    (19655, 0, 19655.41, True),      # display rounding
    (19655.4, 1, 19655.41, True),
    (87.8, 1, 0.878, True),          # ratio shown as percent
    (1643, None, 1643.2, True),
    (2686000, 0, 3048395, False),
    (15.0, 1, 15.006, True),
    (16.0, 1, 15.006, False),
])
def test_matches(printed, decimals, stored, ok):
    assert matches(printed, decimals, stored) is ok
