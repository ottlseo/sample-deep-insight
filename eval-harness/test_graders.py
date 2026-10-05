"""Graders must pass a clean run and catch each injected defect."""
import json

import pytest

from fixtures import make_run
from grade import fail_reasons, grade_run, load_scenario, pass_rules
from graders import recompute
from graders.numbers import matches, parse_numbers


@pytest.fixture
def scenario():
    # The fixture run is small (5 calculations), so grade it under the simple
    # scenario's thresholds; moon_market_kr needs 50 recomputed calculations.
    return load_scenario("moon_market_kr_simple")


@pytest.fixture
def clean(tmp_path):
    return make_run.make_clean_run(tmp_path / "run")


def grade(run_dir, scenario):
    return grade_run(run_dir, scenario["csv"], scenario["answer_key"], scenario)["scores"]


def test_clean_run_passes(clean, scenario):
    s = grade(clean, scenario)
    assert s["core_pass"], s["core_fail_reasons"]
    assert s["citation_value_match_rate"] == 1.0
    assert s["cited_value_match_rate"] == 1.0 and s["cited_value_checked"] == 5
    assert s["recompute_supported"] == 5 and s["recompute_lenient"] == 0  # GROUP BY checked against the named group
    assert s["recompute_match_rate"] == 1.0
    assert s["required_facts_missing"] == []
    assert s["audit_pass"] is True


@pytest.mark.parametrize("corrupt, metric, expected", [
    (make_run.corrupt_citation_value, "citations_ok", False),
    (make_run.corrupt_metadata_value, "recompute_match_rate", 0.8),
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


# --- a check that produced nothing must fail, not pass ------------------------

def test_grader_crash_fails(clean, scenario, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(recompute, "grade", boom)
    s = grade(clean, scenario)
    assert s["recompute_grader_error"] is True
    assert "recompute_grader_error" in s["core_fail_reasons"]


def test_unrecognised_formulas_fail(clean, scenario):
    """Agent writes formulas the grader can't parse → nothing recomputed → FAIL (used to pass)."""
    path = clean / "artifacts" / "calculation_metadata.json"
    data = json.loads(path.read_text())
    for c in data["calculations"]:
        c["formula"] = "합계(매출)"
    path.write_text(json.dumps(data, ensure_ascii=False))
    s = grade(clean, scenario)
    assert s["recompute_supported"] == 0 and s["recompute_match_rate"] is None
    assert "recompute_supported" in s["core_fail_reasons"]


def test_no_numbers_before_markers_fails(clean, scenario):
    """Citations the grader can't pair with a number → 0 compared → FAIL (used to pass)."""
    make_run.write_docx(clean / "artifacts" / "final_report_with_citations.docx",
                        ["총 매출은 16,431,923원이며 주문은 836건, 평균 주문 금액은 19,655원입니다.", "출처는 [1] [2] [3] [4] [5]입니다."], [])
    s = grade(clean, scenario)
    assert s["cited_value_checked"] == 0
    assert "cited_value_checked" in s["core_fail_reasons"]


def test_required_fact_missing_fails(clean, scenario):
    """The simple request names three numbers; dropping AOV fails it (2 of 3 used to pass)."""
    calcs, top = make_run._calcs()
    body = [l for l in make_run._body(calcs, top) if "객단가" not in l]
    make_run.write_docx(clean / "artifacts" / "final_report_with_citations.docx", body, [])
    s = grade(clean, scenario)
    assert s["required_facts_missing"] == ["avg_order_value"]
    assert "required_facts" in s["core_fail_reasons"]


def test_min_other_facts(clean):
    growth = load_scenario("moon_market_kr")
    rules = pass_rules(growth)
    s = grade_run(clean, growth["csv"], growth["answer_key"], growth)["scores"]
    assert rules["min_other_facts"] == 4
    assert ("other_facts_found" in s["core_fail_reasons"]) == (s["other_facts_found"] < 4)


def test_judge_not_run_is_skipped_but_judge_error_fails(clean, scenario):
    s = grade(clean, scenario)
    assert "judge_requirements_missing" not in s["core_fail_reasons"]
    s["judge_error"] = "RuntimeError: x"
    assert "judge_error" in fail_reasons(s, pass_rules(scenario))


def test_missing_artifacts_still_produce_scores(tmp_path, scenario):
    (tmp_path / "artifacts").mkdir()
    s = grade(tmp_path, scenario)
    assert s["required_present"] == 0
    assert not s["core_pass"]


def test_failed_status_fails_core(clean, scenario):
    (clean / "run.json").write_text(json.dumps({"status": "session_unresolved"}))
    s = grade(clean, scenario)
    assert s["core_fail_reasons"][0] == "status"


# --- recompute: grouped formulas ---------------------------------------------

@pytest.fixture(scope="module")
def df():
    return recompute.load_csv(load_scenario("moon_market_kr")["csv"])


@pytest.mark.parametrize("formula, description, expected", [
    ("SUM(Amount) GROUP BY Category", "간편식/밀키트/샐러드 매출", 3048395),
    ("SUM(Amount) grouped by Category", "헤어/바디/구강 카테고리 매출", 2180136),
    ("SUM(Amount)/TOTAL(Amount)*100 GROUP BY Category", "간편식/밀키트/샐러드 카테고리 매출 비중", 18.551663),
    ("MAX(SUM(Amount) GROUP BY Date)", "일별 매출 최고점", 1440065),
    ("SUM(Amount) GROUP BY Date", "2025-05-02 일별 매출", 1440065),
    ("SUM(Amount) GROUP BY Gender", "여성 고객 매출", 8577786),
    ("SUM(Amount) WHERE Gender=F", "", 8577786),
])
def test_grouped_formulas(df, formula, description, expected):
    r = recompute.recompute(formula, df, description)
    assert r["lenient"] is False
    assert r["value"] == pytest.approx(expected, rel=1e-6)


def test_group_not_named_is_lenient(df):
    r = recompute.recompute("SUM(Amount) GROUP BY Category", df, "어떤 카테고리 매출")
    assert r["lenient"] is True and len(r["candidates"]) == 14


def test_weekday_group(df):
    r = recompute.recompute("SUM(Amount) GROUP BY weekday", df, "월요일 총매출")
    assert r["lenient"] is False and r["value"] == pytest.approx(2398597)


def test_wrong_group_value_is_caught(clean, scenario):
    """A segment number that doesn't match its group fails (segments weren't checked before)."""
    path = clean / "artifacts" / "calculation_metadata.json"
    data = json.loads(path.read_text())
    data["calculations"][3]["value"] = 2686000.0  # the real past mis-statement of the top category
    path.write_text(json.dumps(data, ensure_ascii=False))
    s = grade(clean, scenario)
    assert s["recompute_match_rate"] < 1.0


@pytest.mark.parametrize("stored, expected, ok", [
    (19655.0, 19655.41, True),           # stored as a whole number
    (18.6, 18.551663, True),             # stored to one decimal
    (18.55166312549055, 18.55166312549055, True),
    (16432923.0, 16431923.0, False),     # off by 1,000
    (18.7, 18.551663, False),
])
def test_same_value_respects_stored_precision(stored, expected, ok):
    assert recompute.same_value(stored, expected) is ok


# --- number parsing and matching ---------------------------------------------

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
    (1500, 0, 15.0, False),          # ×100 only for ratios between 0 and 1
])
def test_matches(printed, decimals, stored, ok):
    assert matches(printed, decimals, stored) is ok


def test_keywords_match_word_starts():
    from graders.report import _keyword_in
    assert _keyword_in("men", "revenue from men was 47.8%")
    assert not _keyword_in("men", "revenue from women was 52.2%")
    assert _keyword_in("남성", "남성 고객 매출 비중")
