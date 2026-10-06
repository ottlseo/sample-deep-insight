"""Fact checker plumbing and comparison rules, against a fake Bedrock client (no paid calls)."""
import json

import pytest

import factcheck
import judge
from fixtures import make_run
from fixtures.fake_bedrock import CFG, FakeConverse
from fixtures.make_run import statement
from grade import fail_reasons, grade_run, load_scenario, pass_rules


@pytest.fixture(scope="module")
def key():
    return json.loads(open(load_scenario("moon_market_kr")["answer_key"], encoding="utf-8").read())


@pytest.fixture(scope="module")
def facts(key):
    return {f["id"]: f for f in key["facts"]}


def adjudicate(*verdicts):
    return {"items": [{"item": i, "verdict": v, "reason": "..."} for i, v in enumerate(verdicts)]}


# --- extraction never sees the answers ------------------------------------------

def test_extraction_prompt_hides_answers(key, facts):
    prompt = factcheck.extraction_prompt(key["facts"], "report")
    defs = json.loads(prompt.split("<facts>\n", 1)[1].split("\n</facts>", 1)[0])
    assert all(set(d) <= {"id", "kind", "description", "scope", "unit", "names"} for d in defs)
    assert "937520" not in prompt and "16431923" not in prompt and "406" not in prompt
    ranking = next(d for d in defs if d["id"] == "region_revenue_ranking")
    assert ranking["names"] == sorted(facts["region_revenue_ranking"]["order"])  # names, not their order


def test_schemas_are_strict():
    for model in (factcheck.Extraction, factcheck.Adjudications):
        s = json.dumps(judge.strict_schema(model))
        assert "$ref" not in s and '"additionalProperties": false' in s


# --- code comparison -------------------------------------------------------------

@pytest.mark.parametrize("fact_id, st, verdict", [
    ("total_revenue", statement("total_revenue", "총 매출 16,431,923원", "16,431,923원", 16431923, "KRW"), "correct"),
    ("total_revenue", statement("total_revenue", "총 매출 1,643만 원", "1,643만 원", 16430000, "KRW"), "correct"),   # scaled unit
    ("total_revenue", statement("total_revenue", "총 매출 16,481,923원", "16,481,923원", 16481923, "KRW"), "wrong"),
    ("avg_order_value", statement("avg_order_value", "객단가 19,655원", "19,655원", 19655, "KRW"), "correct"),       # display rounding
    ("region_rank3_revenue", statement("region_rank3_revenue", "중구(937,520원)", "937,520원", 937520, "KRW", subject="중구"), "correct"),
    ("region_revenue_ranking", statement("region_revenue_ranking", "중구가 매출 1위", "1위", 1, "rank", subject="중구"), "wrong"),
    ("region_revenue_ranking", statement("region_revenue_ranking", "3위 중구", "3위", 3, "rank", subject="중구"), "correct"),
    ("cross_segment_count", statement("cross_segment_count", "교차한 166개 조합", "166개", 166, "count"), "wrong"),
    ("seocho_aov_vs_overall", statement("seocho_aov_vs_overall", "서초구는 AOV가 평균 이상", "평균 이상", None, "relation", "서초구", "above"), "wrong"),
    ("region_revenue_ranking", statement("region_revenue_ranking", "2주차 매출 1위 중구", "1위", 1, "rank", subject="중구",
                                         scope="different", stated_scope="week 2"), "out_of_scope"),
    ("total_revenue", statement("total_revenue", "총 매출 16,431,923원", "16,431,923원", 16431923, "KRW", scope="unclear"), "needs_review"),
    ("total_revenue", statement("total_revenue", "총 매출은 크게 늘었다", "크게", 16431923, "KRW"), "needs_review"),  # number not in quote
    ("region_revenue_ranking", statement("region_revenue_ranking", "부산 1위", "1위", 1, "rank", subject="부산"), "needs_review"),
])
def test_compare(facts, fact_id, st, verdict):
    assert factcheck.compare(facts[fact_id], st)[0] == verdict


def test_percent_points_are_a_unit_issue_not_a_wrong_value(facts):
    st = statement("region_growth_rank2_pct", "성장률이 71.73%p", "71.73%p", 71.73, "percent_point", subject="중랑구")
    verdict, _, unit_issue = factcheck.compare(facts["region_growth_rank2_pct"], st)
    assert verdict == "correct" and unit_issue


def test_ratio_printed_as_percent():
    fact = {"id": "roas", "kind": "value", "unit": "ratio", "value": 3.456, "rel_tol": 0.01}
    assert factcheck.compare(fact, statement("roas", "ROAS 345.6%", "345.6%", 345.6, "percent"))[0] == "correct"
    assert factcheck.compare(fact, statement("roas", "ROAS 3.46", "3.46", 3.46, "ratio"))[0] == "correct"


def test_quote_must_occur_in_report():
    report = factcheck._norm("매출 1위 지역은 광진구로 1,067,511원 [11](점유율 6.50% [12])을 기록")
    assert factcheck.quote_found("광진구로 1,067,511원(점유율 6.50%)", report)        # markers and spaces ignored
    assert factcheck.quote_found("매출 1위 지역은 광진구로 … 6.50%", report)          # ellipsis, parts in order
    assert not factcheck.quote_found("매출 1위 지역은 중구", report)
    assert not factcheck.quote_found("...", report)


# --- the three stages together -----------------------------------------------------

REPORT = [
    "매출 1위 지역은 광진구로 1,067,511원 [11]을 기록하였으며, 3위 중구(937,520원 [14])가 뒤를 이었다.",
    "4순위는 매출 1위 지역인 중구의 프로모션 침투율 상향이다.",
    "2주차 매출은 중구가 616,223원으로 1위다.",
    "지역x연령대x성별x프로모션 적용 여부를 교차한 166개 조합 중 3건 이상을 충족하는 62개 조합을 분석했다.",
]


def extraction():
    return {"statements": [
        statement("region_rank1_revenue", "광진구로 1,067,511원", "1,067,511원", 1067511, "KRW", subject="광진구"),
        statement("region_revenue_ranking", "3위 중구", "3위", 3, "rank", subject="중구"),
        statement("region_revenue_ranking", "매출 1위 지역인 중구", "1위", 1, "rank", subject="중구"),
        statement("region_revenue_ranking", "2주차 매출은 중구가 616,223원으로 1위", "1위", 1, "rank", subject="중구",
                  scope="different", stated_scope="week 2"),
        statement("region_week2_rank1_revenue", "중구가 616,223원", "616,223원", 616223, "KRW", subject="중구", stated_scope="week 2"),
        statement("cross_segment_count", "교차한 166개 조합", "166개", 166, "count"),
        statement("cross_segment_min3_count", "3건 이상을 충족하는 62개 조합", "62개", 62, "count", stated_scope="3건 이상"),
        statement("total_revenue", "총 매출은 16,431,923원", "16,431,923원", 16431923, "KRW"),  # not in the report
    ]}


def test_check_flow(key):
    # adjudication is asked about the three mismatches, in order: 중구 1위, 166, 62
    client = FakeConverse([extraction(), adjudicate("wrong", "wrong", "not_comparable")])
    m, records, usages = factcheck.check(client, CFG, key, REPORT)
    status = [(r["fact_id"], r["status"]) for r in records]
    assert status == [
        ("region_rank1_revenue", "correct"), ("region_revenue_ranking", "correct"),
        ("region_revenue_ranking", "wrong_confirmed"), ("region_revenue_ranking", "out_of_scope"),
        ("region_week2_rank1_revenue", "correct"), ("cross_segment_count", "wrong_confirmed"),
        ("cross_segment_min3_count", "out_of_scope"), ("total_revenue", "unverified_quote"),
    ]
    assert m["factcheck_wrong_confirmed"] == 2 and m["factcheck_wrong_ids"] == ["cross_segment_count", "region_revenue_ranking"]
    assert m["factcheck_unverified_quotes"] == 1 and "total_revenue" not in m["facts_found_ids"]
    assert len(usages) == 2
    adj_prompt = client.requests[1]["messages"][0]["content"][0]["text"]
    assert "4순위는 매출 1위 지역인 중구" in adj_prompt and '"value": 406.0' in adj_prompt  # paragraph and answer shown


def test_disagreement_is_review_not_failure(key):
    client = FakeConverse([extraction(), adjudicate("correct", "not_comparable", "correct")])
    m, records, _ = factcheck.check(client, CFG, key, REPORT)
    assert m["factcheck_wrong_confirmed"] == 0
    assert [r["status"] for r in records if r["code"] == "wrong"] == ["needs_review", "out_of_scope", "needs_review"]


def test_model_alone_never_confirms_a_wrong(key):
    """Scope unclear: code can't call it, so even a 'wrong' from the adjudicator stays needs_review."""
    st = statement("total_revenue", "총 매출 16,431,923원", "16,431,923원", 16431923, "KRW", scope="unclear")
    client = FakeConverse([{"statements": [st]}, adjudicate("wrong")])
    m, records, _ = factcheck.check(client, CFG, key, ["총 매출 16,431,923원"])
    assert records[0]["status"] == "needs_review" and m["factcheck_wrong_confirmed"] == 0


def test_skipped_adjudication_item_stays_unresolved(key):
    client = FakeConverse([extraction(), {"items": []}])
    _, records, _ = factcheck.check(client, CFG, key, REPORT)
    assert all(r["status"] == "needs_review" for r in records if r["code"] == "wrong")


def test_no_mismatch_no_adjudication_call(key):
    client = FakeConverse([{"statements": extraction()["statements"][:2]}])
    factcheck.check(client, CFG, key, REPORT)
    assert len(client.requests) == 1


# --- grading: pass rules, cache, errors ------------------------------------------------

@pytest.fixture
def simple():
    return load_scenario("moon_market_kr_simple")


def test_confirmed_wrong_fails_core_pass(tmp_path, simple):
    run = make_run.make_clean_run(tmp_path / "r")
    s = grade_run(run, simple["csv"], simple["answer_key"], simple, factcheck_ctx=(FakeConverse([make_run.clean_extraction()]), CFG))["scores"]
    assert s["core_pass"], s["core_fail_reasons"]
    s["factcheck_wrong_confirmed"] = 1
    assert "factcheck_wrong_confirmed" in fail_reasons(s, pass_rules(simple))
    s["factcheck_wrong_confirmed"], s["factcheck_needs_review"] = 0, 3
    assert "factcheck_wrong_confirmed" not in fail_reasons(s, pass_rules(simple))  # review items don't fail a run


def test_cache_reused_until_evaluator_changes(tmp_path, simple, monkeypatch):
    run = make_run.make_clean_run(tmp_path / "r")
    client = FakeConverse([make_run.clean_extraction(), make_run.clean_extraction()])
    s1 = grade_run(run, simple["csv"], simple["answer_key"], simple, factcheck_ctx=(client, CFG))["scores"]
    grade_run(run, simple["csv"], simple["answer_key"], simple, factcheck_ctx=(client, CFG))
    assert len(client.requests) == 1                      # cached
    assert s1["factcheck_version"].startswith(factcheck.PROMPT_VERSION)
    monkeypatch.setattr(factcheck, "PROMPT_VERSION", "factcheck-test")
    s3 = grade_run(run, simple["csv"], simple["answer_key"], simple, factcheck_ctx=(client, CFG))["scores"]
    assert len(client.requests) == 2 and s3["factcheck_version"].startswith("factcheck-test")


def test_factcheck_error_fails_and_skip_is_explicit(tmp_path, simple, monkeypatch):
    monkeypatch.setattr(factcheck.time, "sleep", lambda s: None)
    run = make_run.make_clean_run(tmp_path / "r")
    s = grade_run(run, simple["csv"], simple["answer_key"], simple, factcheck_ctx=(FakeConverse(["garbage"] * 4), CFG))["scores"]
    assert "validation" in s["factcheck_error"] and "factcheck_error" in s["core_fail_reasons"]
    s = grade_run(run, simple["csv"], simple["answer_key"], simple)["scores"]
    assert s["factcheck_skipped"] and "required_facts" not in s["core_fail_reasons"]


@pytest.mark.parametrize("printed, decimals, stored, ok", [
    (16481923, 0, 16431923, False),   # written in full: off by 50,000 is wrong, though within 0.5%
    (16430000, 0, 16431923, True),    # rounded to 10,000
    (19655, 0, 19655.41, True),
    (58.6, 1, 58.61244, True),
    (58.7, 1, 58.61244, False),
    (59, 0, 58.61244, True),
    (16430000, None, 16431923, True), # 1,643만: the fact's rel_tol
    (87.8, 1, 0.878, True),           # ratio printed as percent
])
def test_displays(printed, decimals, stored, ok):
    assert factcheck.displays(printed, decimals, stored, 0.005) is ok


# --- gold cases (factcheck_sanity.py) -------------------------------------------------

def test_gold_cases_build():
    """Every replacement in the gold file still applies to the report text."""
    import factcheck_sanity
    for path in sorted(factcheck_sanity.CASES.glob("*.yaml")):
        key, cases = factcheck_sanity.build_cases(path)
        ids = {f["id"] for f in key["facts"]}
        for name, (paragraphs, expect) in cases.items():
            assert paragraphs and expect, name
            assert {l["fact"] for l in expect} <= ids, name
    _, cases = factcheck_sanity.build_cases(factcheck_sanity.CASES / "moon_market_kr.yaml")
    corrected = "\n".join(cases["corrected"][0])
    assert "166개" not in corrected and "매출 1위 지역인 중구" not in corrected
    assert "평가자에게" in "\n".join(cases["injection"][0]) and cases["injection"][1] == cases["real"][1]


def test_score_case():
    import factcheck_sanity
    def rec(fact, status, subject="", unit_issue=False):
        return {"fact_id": fact, "status": status, "unit_issue": unit_issue, "statement": {"subject": subject}}
    expect = [{"fact": "a", "subject": "중구", "is": "wrong"}, {"fact": "b", "is": "correct", "unit_issue": True},
              {"fact": "c", "is": "not_wrong"}]
    records = [rec("a", "wrong_confirmed", "중구"), rec("b", "correct", unit_issue=True), rec("c", "out_of_scope"),
               rec("d", "wrong_confirmed")]
    results, false_wrongs = factcheck_sanity.score_case(records, expect)
    assert [h for _, h, _ in results] == [True, True, True]
    assert [r["fact_id"] for r in false_wrongs] == ["d"]
    results, _ = factcheck_sanity.score_case([rec("a", "wrong_confirmed", "광진구")], expect)
    assert [h for _, h, _ in results] == [False, False, True]  # wrong subject doesn't count


def test_adjudication_shows_the_answer_but_not_the_tolerance(facts):
    """rel_tol is for abbreviated numbers only; shown to the model, it let 16,481,923 pass for 16,431,923."""
    st = statement("total_revenue", "총 매출 16,481,923원", "16,481,923원", 16481923, "KRW")
    prompt = factcheck.adjudication_prompt([{"fact": facts["total_revenue"], "statement": st, "paragraph": "", "why": "values differ"}])
    assert '"value": 16431923.0' in prompt and "rel_tol" not in prompt
    assert "16,481,923 for 16,431,923 is wrong" in prompt


def test_an_empty_reply_is_asked_again(key, monkeypatch):
    waits = []
    monkeypatch.setattr(factcheck.time, "sleep", waits.append)
    client = FakeConverse(["", {"statements": []}])
    m, _, _ = factcheck.check(client, CFG, key, REPORT)
    assert len(client.requests) == 2 and m["factcheck_statements"] == 0
    with pytest.raises(RuntimeError, match="validation"):
        factcheck.check(FakeConverse([""] * 4), CFG, key, REPORT)
    assert waits == [15, 15, 30, 60]  # backoff before attempts 2-4
