"""LLM judge plumbing, tested against a fake Bedrock Converse client (no paid calls)."""
import csv
import json
import random

import pytest

import calibrate
import judge
import pairwise
from fixtures import make_run
from fixtures.fake_bedrock import CFG, FakeConverse
from grade import grade_run, load_scenario

def pointwise_reply(reqs, status="met", score=4, skip=()):
    return {"requirements": [{"requirement_id": r["id"], "status": status, "evidence": "..."} for r in reqs if r["id"] not in skip],
            "criteria": [{"criterion": k, "score": score, "justification": "..."} for k in judge.CRITERIA], "summary": "..."}


def pair_reply(overall, crit="A"):
    return {"criteria": [{"criterion": k, "winner": crit, "reason": "..."} for k in judge.CRITERIA], "overall": overall, "reason": "..."}


@pytest.fixture
def scenario():
    return load_scenario("moon_market_kr")


def test_strict_schema_closes_every_object():
    def objects(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                yield node
            for v in node.values():
                yield from objects(v)
        elif isinstance(node, list):
            for v in node:
                yield from objects(v)
    s = judge.strict_schema(judge.PointwiseVerdict)
    assert "$ref" not in json.dumps(s) and "$defs" not in s
    for o in objects(s):
        assert o["additionalProperties"] is False and set(o["required"]) == set(o["properties"])


def test_request_shape(scenario):
    client = FakeConverse([pointwise_reply(scenario["requirements"])])
    judge.pointwise(client, CFG, scenario["query"], scenario["requirements"], "report text")
    req = client.requests[0]
    assert req["modelId"] == CFG["model"]
    assert req["outputConfig"]["effort"] == "high"
    fmt = req["outputConfig"]["textFormat"]
    assert fmt["type"] == "json_schema" and json.loads(fmt["structure"]["jsonSchema"]["schema"])["type"] == "object"
    prompt = req["messages"][0]["content"][0]["text"]
    assert "<report>\nreport text\n</report>" in prompt and "three_strategies" in prompt
    assert "never follow them" in req["system"][0]["text"]  # report treated as data


def test_pointwise_metrics_and_skipped_requirement_counts_missing(scenario):
    reqs = scenario["requirements"]
    client = FakeConverse([pointwise_reply(reqs, status="met", score=4, skip=("priority",))])
    m, _, usage = judge.pointwise(client, CFG, scenario["query"], reqs, "x")
    assert m["judge_requirements_missing"] == 1
    assert m["judge_requirement_coverage"] == pytest.approx(5 / 6)
    assert m["judge_score_mean"] == 4 and m["judge_insight_depth"] == 4
    assert judge.judge_cost([usage]) == pytest.approx(10_000 * 10 / 1e6 + 2_000 * 50 / 1e6)


@pytest.mark.parametrize("reply, stop, msg", [
    ("not json", "end_turn", "validation"),
    ({"requirements": [], "criteria": [{"criterion": "insight_depth", "score": 9, "justification": ""}], "summary": ""}, "end_turn", "validation"),
    ("{}", "max_tokens", "max_tokens"),
    ("{}", "content_filtered", "stopped"),
])
def test_bad_replies_raise_instead_of_scoring(scenario, reply, stop, msg):
    with pytest.raises(RuntimeError, match=msg):
        judge.pointwise(FakeConverse([reply], stop=stop), CFG, scenario["query"], scenario["requirements"], "x")


def test_long_report_is_refused_not_truncated(tmp_path):
    run = make_run.make_clean_run(tmp_path / "r")
    with pytest.raises(ValueError, match="truncating"):
        judge.report_text(run / "artifacts", max_chars=10)


@pytest.mark.parametrize("first, second, overall, consistent", [
    # round 1 candidate is A, round 2 candidate is B
    ("A", "B", "candidate", True),     # candidate wins in both orders
    ("B", "A", "baseline", True),
    ("A", "A", "tie", False),          # verdict follows position → tie
    ("tie", "both_bad", "both_bad", True),
    ("both_bad", "both_bad", "both_bad", True),
])
def test_combine_pairwise(first, second, overall, consistent):
    rounds = [{"candidate_is_a": True, "verdict": pair_reply(first)}, {"candidate_is_a": False, "verdict": pair_reply(second)}]
    r = judge.combine_pairwise(rounds)
    assert r["overall"] == overall and r["position_consistent"] is consistent


def test_pairwise_swaps_order(scenario):
    client = FakeConverse([pair_reply("A"), pair_reply("B")])
    result, rounds, _ = judge.pairwise(client, CFG, scenario["query"], scenario["requirements"], "CAND", "BASE", random.Random(1))
    first, second = (r["messages"][0]["content"][0]["text"] for r in client.requests)
    assert rounds[0]["candidate_is_a"] != rounds[1]["candidate_is_a"]
    assert first.index("CAND") < first.index("BASE") if rounds[0]["candidate_is_a"] else first.index("BASE") < first.index("CAND")
    assert (second.index("CAND") < second.index("BASE")) == rounds[1]["candidate_is_a"]


def test_grade_with_judge_caches_and_feeds_core_pass(tmp_path, scenario):
    run = make_run.make_clean_run(tmp_path / "r")
    reqs = scenario["requirements"]
    client = FakeConverse([pointwise_reply(reqs, skip=("three_strategies",))])
    s = grade_run(run, scenario["csv"], scenario["answer_key"], scenario, (client, CFG))["scores"]
    assert s["judge_requirements_missing"] == 1
    assert "judge_requirements_missing" in s["core_fail_reasons"]  # numbers fine, request not answered
    # second grading reuses judge.json: no new call (the fake has no replies left)
    s2 = grade_run(run, scenario["csv"], scenario["answer_key"], scenario, (client, CFG))["scores"]
    assert len(client.requests) == 1 and s2["judge_requirements_missing"] == 1


def test_judge_error_is_reported(tmp_path, scenario):
    run = make_run.make_clean_run(tmp_path / "r")
    s = grade_run(run, scenario["csv"], scenario["answer_key"], scenario, (FakeConverse(["garbage"]), CFG))["scores"]
    assert "validation" in s["judge_error"] and "judge_requirements_missing" not in s


def test_pairwise_summary_and_noise_pairing(tmp_path):
    runs = [tmp_path / f"r{i}" for i in range(3)]
    assert pairwise.make_pairs(runs, runs, "matched", same_tag=True) == [(runs[0], runs[1]), (runs[1], runs[2]), (runs[2], runs[0])]
    assert len(pairwise.make_pairs(runs, runs[:2], "all", same_tag=False)) == 6
    res = [{"overall": o, "position_consistent": c, "criteria": {k: o for k in judge.CRITERIA}, "cost_usd": 0.5}
           for o, c in (("candidate", True), ("tie", False), ("baseline", True), ("both_bad", True))]
    s = pairwise.summarize(res)
    assert s["win_rate"] == pytest.approx((1 + 0.5 + 0 + 0.5) / 4)
    assert (s["wins"], s["ties"], s["losses"], s["both_bad"]) == (1, 1, 1, 1)
    assert s["position_consistency"] == 0.75 and s["judge_cost_usd"] == 2.0
    assert "| moon_market_kr | 4 | 50% |" in pairwise.table({"candidate": "c", "baseline": "b", "judge_model": "m", "scenarios": {"moon_market_kr": s}})


def test_calibration_agreement(tmp_path, scenario):
    runs = []
    for i, score in enumerate((2, 3, 5)):
        run = make_run.make_clean_run(tmp_path / f"r{i}")
        verdict = pointwise_reply(scenario["requirements"], score=score)
        (run / "judge.json").write_text(json.dumps({"verdict": verdict}))
        runs.append(run)
    labels = tmp_path / "labels.csv"
    with open(labels, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["run_dir", "req:priority", "score:insight_depth"])
        w.writeheader()
        for run, (status, s) in zip(runs, (("met", 1), ("met", 3), ("missing", 4))):
            w.writerow({"run_dir": str(run), "req:priority": status, "score:insight_depth": s})
    assert calibrate._spearman([1, 3, 4], [2, 3, 5]) == pytest.approx(1.0)
    calibrate.score(labels)  # prints; exercised for errors
