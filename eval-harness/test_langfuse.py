"""langfuse_sync.py and calibrate.py's Langfuse path against an in-memory fake of the REST API."""
import json
import shutil

import pytest

import calibrate
import langfuse_sync as ls
from fixtures import make_run
from grade import grade_run, load_scenario


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.content = b"x"
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeLangfuse:
    """Just enough of the public API: configs, datasets, items, run items, runs, ingestion, queues, scores."""
    def __init__(self, ingestion_errors=None):
        self.auth = None
        self.configs, self.datasets, self.items, self.run_items, self.events = {}, set(), {}, [], []
        self.queues, self.queue_items, self.annotations = {}, [], []
        self.ingestion_errors = ingestion_errors or []

    def request(self, method, url, params=None, json=None, timeout=None):
        path = url.split("://", 1)[1].split("/", 1)[1]
        path = "/" + path
        if method == "GET":
            return self._get(path, params or {})
        return self._post(path, json)

    def _page(self, rows):
        return Resp(200, {"data": rows, "meta": {"totalPages": 1}})

    def _get(self, path, params):
        if path == "/api/public/score-configs":
            return self._page([{"id": i, "name": n} for n, i in self.configs.items()])
        if path.startswith("/api/public/v2/datasets/"):
            return Resp(200, {"name": path.rsplit("/", 1)[1]}) if path.rsplit("/", 1)[1] in self.datasets else Resp(404, {"message": "not found"})
        if path.startswith(f"/api/public/datasets/{ls.DATASET}/runs/"):
            return Resp(200, {"id": "run-" + path.rsplit("/", 1)[1]})
        if path == "/api/public/annotation-queues":
            return self._page(list(self.queues.values()))
        if path.endswith("/items") and "annotation-queues" in path:
            return self._page([{"objectId": o} for o in self.queue_items])
        if path == "/api/public/v2/scores":
            return self._page([a for a in self.annotations if a["traceId"] == params.get("traceId")])
        raise AssertionError(f"unexpected GET {path}")

    def _post(self, path, body):
        if path == "/api/public/score-configs":
            self.configs[body["name"]] = f"cfg-{body['name']}"
            return Resp(200, {"id": self.configs[body["name"]]})
        if path == "/api/public/v2/datasets":
            self.datasets.add(body["name"])
            return Resp(200, body)
        if path == "/api/public/dataset-items":
            self.items[body["id"]] = body
            return Resp(200, body)
        if path == "/api/public/dataset-run-items":
            self.run_items.append(body)
            return Resp(200, body)
        if path == "/api/public/ingestion":
            self.events += body["batch"]
            return Resp(207, {"successes": [], "errors": self.ingestion_errors})
        if path == "/api/public/annotation-queues":
            q = {"id": "q1", **body}
            self.queues["q1"] = q
            return Resp(200, q)
        if path.endswith("/items") and "annotation-queues" in path:
            self.queue_items.append(body["objectId"])
            return Resp(200, body)
        raise AssertionError(f"unexpected POST {path}")


@pytest.fixture
def tag_dir(tmp_path, monkeypatch):
    """A tag folder with one graded run (clean fixture + judge.json), inside a fake eval-harness root."""
    monkeypatch.setattr(ls, "HERE", tmp_path)
    monkeypatch.setattr(calibrate, "HERE", tmp_path)
    tag = tmp_path / "eval_results" / "baseline"
    run = make_run.make_clean_run(tag / "moon_market_kr_simple-20261002-000000-1")
    (run / "run.json").write_text(json.dumps({"scenario": "moon_market_kr_simple", "status": "completed",
                                              "started_at": "2026-10-02T06:00:00+00:00", "agent_span_s": {"coder": 12.0},
                                              "agent_calls": {"coder": 1}}))
    (run / "config.json").write_text(json.dumps({"git_sha": "abcdef1234567", "runtime_version": "5", "models": {"CODER_MODEL_ID": "global.anthropic.claude-sonnet-5"}}))
    (run / "usage.json").write_text(json.dumps({"by_agent": {"coder": {"model_id": "global.anthropic.claude-sonnet-5", "input": 1000, "output": 100, "cache_read": 0, "cache_write": 0}}}))
    sc = load_scenario("moon_market_kr_simple")
    verdict = {"requirements": [{"requirement_id": r["id"], "status": "met", "evidence": "quoted"} for r in sc["requirements"]],
               "criteria": [{"criterion": c, "score": 4, "justification": f"why {c}"} for c in ls.CRITERIA], "summary": "ok"}
    (run / "judge.json").write_text(json.dumps({"verdict": verdict, "metrics": {}}))
    result = grade_run(run, sc["csv"], sc["answer_key"], sc)
    (run / "scores.json").write_text(json.dumps(result))
    (tag / "pairwise_vs_baseline.json").write_text(json.dumps({
        "baseline": "baseline", "candidate": "baseline", "judge_model": "m",
        "scenarios": {"moon_market_kr_simple": {"pairs": 3, "win_rate": 0.5, "wins": 1, "ties": 1, "losses": 1, "both_bad": 0, "position_consistency": 2 / 3}}}))
    return tag, run


def sync(tag, fake, queue=False):
    lf = ls.Langfuse("http://lf", "pk", "sk", session=fake)
    configs = ls.ensure_score_configs(lf, ["total_revenue", "segments"])
    ls.ensure_dataset_items(lf, {"moon_market_kr_simple": load_scenario("moon_market_kr_simple")})
    events, trace_id, scenario, _ = ls.run_events(next(tag.glob("*/run.json")).parent, "baseline", configs)
    lf.ingest(events)
    ls.link_run_items(lf, "baseline", [(trace_id, scenario)], "git abcdef1")
    n_pw = ls.pairwise_scores(lf, tag)
    if queue:
        ls.queue_traces(lf, [trace_id], configs)
    return lf, trace_id, n_pw


def test_trace_has_report_scores_and_agents(tag_dir):
    tag, run = tag_dir
    fake = FakeLangfuse()
    _, trace_id, _ = sync(tag, fake)
    trace = next(e["body"] for e in fake.events if e["type"] == "trace-create")
    assert trace["id"] == trace_id and "16,431,923원" in trace["output"]       # the report is readable in the UI
    assert trace["tags"] == ["baseline", "moon_market_kr_simple", "PASS"] and trace["release"] == "abcdef123456"
    gen = next(e["body"] for e in fake.events if e["type"] == "generation-create")
    assert gen["name"] == "coder" and gen["usageDetails"]["input"] == 1000 and gen["costDetails"]["total"] > 0
    scores = {e["body"]["name"]: e["body"] for e in fake.events if e["type"] == "score-create"}
    assert scores["core_pass"]["value"] == 1 and scores["core_pass"]["dataType"] == "BOOLEAN"
    assert scores["recompute_supported"]["value"] == 5.0
    assert scores["insight_depth"]["comment"] == "why insight_depth" and scores["insight_depth"]["configId"] == "cfg-insight_depth"
    assert scores["req:total_revenue"]["value"] == "met" and scores["req:total_revenue"]["dataType"] == "CATEGORICAL"


def test_ids_are_stable_so_resync_updates_in_place(tag_dir):
    tag, _ = tag_dir
    a, b = FakeLangfuse(), FakeLangfuse()
    sync(tag, a)
    sync(tag, b)
    assert [e["body"].get("id") for e in a.events] == [e["body"].get("id") for e in b.events]
    assert len({e["id"] for e in a.events}) == len(a.events)                    # no duplicate events within a sync


def test_dataset_item_run_item_and_pairwise(tag_dir):
    tag, _ = tag_dir
    fake = FakeLangfuse()
    _, trace_id, n_pw = sync(tag, fake)
    item = fake.items[ls.sid("item", "moon_market_kr_simple")]
    assert item["expectedOutput"]["pass"]["required_facts"] == ["total_revenue", "order_count", "avg_order_value"]
    assert fake.run_items == [{"runName": "baseline", "runDescription": "git abcdef1", "datasetItemId": item["id"], "traceId": trace_id}]
    pw = [e["body"] for e in fake.events if e["body"].get("datasetRunId")]
    assert n_pw == 1 and pw[0]["datasetRunId"] == "run-baseline" and pw[0]["value"] == 0.5


def test_configs_created_once(tag_dir):
    tag, _ = tag_dir
    fake = FakeLangfuse()
    sync(tag, fake)
    n = len(fake.configs)
    sync(tag, fake)
    assert len(fake.configs) == n and "req:segments" in fake.configs and "reasoning_soundness" in fake.configs


def test_ingestion_errors_raise(tag_dir):
    tag, _ = tag_dir
    with pytest.raises(RuntimeError, match="ingestion rejected"):
        sync(tag, FakeLangfuse(ingestion_errors=[{"id": "x", "status": 400, "message": "bad"}]))


def test_queue_adds_each_trace_once(tag_dir):
    tag, _ = tag_dir
    fake = FakeLangfuse()
    lf, trace_id, _ = sync(tag, fake, queue=True)
    ls.queue_traces(lf, [trace_id], {c: f"cfg-{c}" for c in fake.configs})
    assert fake.queue_items == [trace_id]
    assert "cfg-insight_depth" in fake.queues["q1"]["scoreConfigIds"]


def test_calibrate_reads_langfuse_annotations(tag_dir, capsys):
    tag, run = tag_dir
    fake = FakeLangfuse()
    lf, trace_id, _ = sync(tag, fake)
    (tag.parent / "langfuse_sync.json").write_text(json.dumps({trace_id: {"run_dir": str(run.relative_to(tag.parent.parent))}}))
    fake.annotations = [
        {"traceId": trace_id, "name": "insight_depth", "value": 3},
        {"traceId": trace_id, "name": "req:total_revenue", "value": 1, "stringValue": "met"},
    ]
    calibrate.score_langfuse(lf)
    out = capsys.readouterr().out
    assert "requirement status agreement: 1/1" in out
    assert "| insight_depth | 1 | 0% | 100% |" in out                          # judge 4 vs human 3


def test_dry_run_needs_no_credentials(tag_dir, monkeypatch, capsys):
    tag, _ = tag_dir
    for k in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("sys.argv", ["langfuse_sync.py", str(tag), "--dry-run"])
    assert ls.main() == 0
    assert "1 runs" in capsys.readouterr().out
