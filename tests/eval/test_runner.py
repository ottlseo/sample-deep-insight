"""run_eval.py and compare.py against a fake runtime stream and a fake S3."""
import json
import shutil
import types
from datetime import datetime, timezone
from pathlib import Path

import compare
import run_eval
from fixtures import make_run
from grade import load_scenario


def sse(ev):
    return f"data: {json.dumps(ev, ensure_ascii=False)}".encode()


class FakeAgentCore:
    def __init__(self, events):
        self.events = events
        self.payload = None

    def invoke_agent_runtime(self, **kw):
        self.payload = json.loads(kw["payload"])
        lines = [sse(e) for e in self.events] + [b""]
        return {"response": types.SimpleNamespace(iter_lines=lambda chunk_size=1: iter(lines))}


class FakeS3:
    """Serves one session folder built by make_run, records feedback uploads."""
    def __init__(self, session_dir, session_id):
        self.session_dir, self.session_id = Path(session_dir), session_id
        self.feedback = []

    def put_object(self, Bucket, Key, Body, **_):
        self.feedback.append((Bucket, Key, json.loads(Body)))

    def get_paginator(self, _):
        files = [p for p in self.session_dir.rglob("*") if p.is_file()]
        prefix = f"{run_eval.SESSIONS_PREFIX}{self.session_id}/"
        contents = [{"Key": prefix + str(p.relative_to(self.session_dir)), "LastModified": datetime.now(timezone.utc)} for p in files]
        return types.SimpleNamespace(paginate=lambda **kw: [{"Contents": [c for c in contents if c["Key"].startswith(kw["Prefix"])]}])

    def download_file(self, bucket, key, dest):
        rel = key.split(f"{self.session_id}/", 1)[1]
        shutil.copy(self.session_dir / rel, dest)


def events(n_reviews):
    out = []
    for i in range(n_reviews):
        out.append({"type": "plan_review_request", "request_id": "req-1", "revision_count": i,
                    "feedback_s3_path": "s3://bucket-x/deep-insight/feedback/req-1.json"})
    out += [
        {"event_type": "usage_metadata", "agent_name": "planner", "model_id": "global.anthropic.claude-opus-5-5",
         "input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 3000, "cache_write_input_tokens": 0},
        {"event_type": "tool_use", "agent_name": "supervisor", "tool_name": "coder_agent_custom_interpreter_tool", "tool_id": "t1"},
        {"event_type": "tool_use", "agent_name": "supervisor", "tool_name": "coder_agent_custom_interpreter_tool", "tool_id": "t1"},
        {"event_type": "tool_result", "agent_name": "supervisor", "tool_id": "t1", "output": "Error: boom"},
        {"event_type": "usage_metadata", "agent_name": "planner", "model_id": "global.anthropic.claude-opus-5-5",
         "input_tokens": 500, "output_tokens": 100},
        {"type": "workflow_complete", "session_id": "sess-1"},
        {"type": "should_not_be_read"},
    ]
    return out


def run(tmp_path, scenario_name, n_reviews):
    session = make_run.make_clean_run(tmp_path / "session")
    s3 = FakeS3(session, "sess-1")
    agentcore = FakeAgentCore(events(n_reviews))
    args = types.SimpleNamespace(tag="t", runtime_arn="arn:x", bucket="bucket-x", timeout=60)
    run_dir = tmp_path / "eval_results" / "t" / "r1"
    meta, scores = run_eval.run_once(args, scenario_name, load_scenario(scenario_name), run_dir, (agentcore, s3))
    return meta, scores, s3, agentcore, run_dir


def test_auto_approve_and_scoring(tmp_path):
    meta, scores, s3, agentcore, run_dir = run(tmp_path, "moon_market_kr", n_reviews=1)
    assert agentcore.payload["data_directory"] == "./data/moon_market/kr/"
    assert s3.feedback == [("bucket-x", "deep-insight/feedback/req-1.json", s3.feedback[0][2])]
    assert s3.feedback[0][2]["approved"] is True
    assert meta["status"] == "completed" and meta["session_id"] == "sess-1"
    assert meta["agent_calls"] == {"coder_agent_custom_interpreter_tool": 1}  # streamed twice, counted once
    assert meta["tool_errors"] == 1
    usage = json.loads((run_dir / "usage.json").read_text())["by_agent"]["planner"]
    assert usage["input"] == 1500 and usage["cache_read"] == 3000
    assert scores["core_pass"], scores["core_fail_reasons"]
    assert scores["cache_hit_rate"] == 3000 / 4500
    assert "should_not_be_read" not in (run_dir / "events.jsonl").read_text()


def test_scripted_revision_then_approve(tmp_path):
    meta, _, s3, _, _ = run(tmp_path, "moon_market_kr_revision", n_reviews=2)
    first, second = (f[2] for f in s3.feedback)
    assert first["approved"] is False and "연령대" in first["feedback"]
    assert second["approved"] is True
    assert meta["plan_revisions"] == 1


def test_compare_flags_regression(tmp_path):
    for tag, recall in (("base", [1.0, 1.0, 1.0]), ("cand", [0.0, 0.0, 0.0])):
        for i, r in enumerate(recall):
            d = tmp_path / tag / f"moon_market_kr-x-{i}"
            d.mkdir(parents=True)
            (d / "run.json").write_text(json.dumps({"scenario": "moon_market_kr"}))
            (d / "scores.json").write_text(json.dumps({"scores": {"core_fact_recall": r, "cost_usd": 2.0 + i, "core_pass": r == 1.0}}))
    text = compare.table("moon_market_kr", [str(tmp_path / "base"), str(tmp_path / "cand")],
                         {str(tmp_path / t): [s for _, s in compare.load_runs(tmp_path / t)] for t in ("base", "cand")})
    row = next(l for l in text.splitlines() if l.startswith("| core facts correct"))
    assert "-100.0%p ▼ worse" in row
    cost_row = next(l for l in text.splitlines() if l.startswith("| cost"))
    assert "▼" not in cost_row and "▲" not in cost_row  # same cost, no flag
