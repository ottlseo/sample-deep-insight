"""Publish eval results to Langfuse, so they can be browsed, compared and labeled in its UI.

    python langfuse_sync.py eval_results/baseline [eval_results/my-change ...] [--queue] [--dry-run]

What goes where:

  scenario (request, requirements,     Dataset `deep-insight-eval`, one item per scenario
    answer key, pass thresholds)
  tag (baseline, my-change)            Dataset run of the same name (an experiment)
  one run of a scenario                Trace: input = request, output = the report text,
                                       metadata = git SHA, runtime version, model IDs
  per-agent tokens, cost, time span    one generation per agent inside the trace
  grading (scores.json, judge.json)    trace scores; judge scores carry the judge's
                                       justification as the comment
  pairwise win rate (pairwise.py)      a score on the candidate's dataset run
  --queue                              adds the traces to the annotation queue
                                       `deep-insight-calibration` for human labels that
                                       use the same score configs as the judge

IDs are derived from the run's path, so syncing again updates in place instead
of duplicating. Credentials: LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY,
LANGFUSE_SECRET_KEY from the environment or eval-harness/langfuse.env.
"""
import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from dotenv import dotenv_values

HERE = Path(__file__).resolve().parent
SCENARIOS_FILE = HERE / "scenarios.yaml"
sys.path.insert(0, str(HERE))

import cost  # noqa: E402
from grade import artifacts_dir, load_scenario  # noqa: E402

DATASET = "deep-insight-eval"
QUEUE = "deep-insight-calibration"
NS = uuid.UUID("5b6d1c1e-8f0a-4d39-9e0b-3c7a0e0d1a11")  # namespace for deterministic ids
CRITERIA = ["evidence_linkage", "strategy_specificity", "insight_depth", "reasoning_soundness"]
REQ_CATEGORIES = [{"label": "met", "value": 1}, {"label": "partial", "value": 0.5}, {"label": "missing", "value": 0}]

# scores.json key → score name; all numeric unless listed in BOOLEAN.
NUMERIC_SCORES = [
    "cited_value_match_rate", "cited_value_checked", "citation_value_match_rate", "recompute_match_rate",
    "recompute_supported", "recompute_lenient", "citation_coverage", "facts_found", "other_facts_found",
    "broken_citation_refs", "audit_block_findings", "cost_usd", "cache_hit_rate", "duration_s",
    "time_to_first_plan_s", "code_exec_failed", "code_executions", "judge_requirement_coverage",
    "judge_score_mean",
]
BOOLEAN_SCORES = ["core_pass", "all_required_ok", "citations_ok", "audit_pass"]


def sid(*parts):
    return str(uuid.uuid5(NS, "/".join(parts)))


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class Langfuse:
    """Thin client over the Langfuse public REST API (OpenAPI served at /generated/api/openapi.yml)."""

    def __init__(self, host, public_key, secret_key, session=None):
        import requests
        self.host = host.rstrip("/")
        self.http = session or requests.Session()
        self.http.auth = (public_key, secret_key)

    def _call(self, method, path, **kw):
        r = self.http.request(method, f"{self.host}{path}", timeout=60, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} → {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    def get(self, path, **params):
        return self._call("GET", path, params=params)

    def post(self, path, body):
        return self._call("POST", path, json=body)

    def pages(self, path, **params):
        page = 1
        while True:
            data = self.get(path, page=page, limit=100, **params)
            yield from data.get("data", [])
            if page >= (data.get("meta") or {}).get("totalPages", 1):
                return
            page += 1

    def ingest(self, events):
        for i in range(0, len(events), 50):
            res = self.post("/api/public/ingestion", {"batch": events[i:i + 50]})
            if res.get("errors"):
                raise RuntimeError(f"ingestion rejected {len(res['errors'])} event(s): {res['errors'][:3]}")


def _event(kind, body, event_id):
    return {"id": event_id, "type": kind, "timestamp": _iso(datetime.now(timezone.utc)), "body": body}


# --- setup: score configs, dataset, items --------------------------------------

def ensure_score_configs(lf, requirement_ids):
    have = {c["name"]: c["id"] for c in lf.pages("/api/public/score-configs") if not c.get("isArchived")}
    wanted = [{"name": f"req:{r}", "dataType": "CATEGORICAL", "categories": REQ_CATEGORIES,
               "description": f"Requirement '{r}' addressed by the report: met / partial / missing"} for r in requirement_ids]
    wanted += [{"name": c, "dataType": "NUMERIC", "minValue": 1, "maxValue": 5,
                "description": f"Rubric criterion {c} (1/3/5 anchors in eval-harness/judge.py)"} for c in CRITERIA]
    for cfg in wanted:
        if cfg["name"] not in have:
            have[cfg["name"]] = lf.post("/api/public/score-configs", cfg)["id"]
    return have


def ensure_dataset_items(lf, scenarios):
    try:
        lf.get(f"/api/public/v2/datasets/{DATASET}")
    except RuntimeError:
        lf.post("/api/public/v2/datasets", {"name": DATASET, "description": "Deep Insight eval scenarios (eval-harness/scenarios.yaml)"})
    for name, s in scenarios.items():
        key = json.loads(Path(s["answer_key"]).read_text(encoding="utf-8"))
        lf.post("/api/public/dataset-items", {
            "datasetName": DATASET, "id": sid("item", name),
            "input": {"scenario": name, "query": s["query"].strip(), "data_directory": s["data_directory"]},
            "expectedOutput": {"requirements": s.get("requirements", []), "pass": s.get("pass", {}),
                               "answer_key": [{"id": f["id"], "value": f["value"]} for f in key["facts"]]},
            "metadata": {"holdout": bool(s.get("holdout")), "hitl": s.get("hitl") or []},
        })


# --- one run → events -------------------------------------------------------------

def run_events(run_dir, tag, configs):
    """Ingestion events for one run folder, plus (trace_id, scenario, meta) for linking."""
    run_dir = Path(run_dir)
    meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    scores_doc = json.loads((run_dir / "scores.json").read_text(encoding="utf-8"))
    scores, details = scores_doc["scores"], scores_doc.get("details", {})
    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8")) if (run_dir / "config.json").is_file() else {}
    scenario = meta["scenario"]
    trace_id = sid("trace", tag, run_dir.name)
    started = datetime.fromisoformat(meta["started_at"]) if meta.get("started_at") else datetime.now(timezone.utc)

    report = ""
    docx = artifacts_dir(run_dir) / "final_report_with_citations.docx"
    if docx.is_file():
        from graders.report import read_docx
        report = "\n\n".join(read_docx(docx))

    judge = json.loads((run_dir / "judge.json").read_text(encoding="utf-8")) if (run_dir / "judge.json").is_file() else None
    status = "PASS" if scores.get("core_pass") else "FAIL"
    events = [_event("trace-create", {
        "id": trace_id, "name": f"eval/{scenario}", "timestamp": _iso(started),
        "input": {"query": load_scenario(scenario)["query"].strip()},
        "output": report or {"status": meta.get("status"), "note": "no report produced"},
        "sessionId": f"eval/{tag}", "release": str(config.get("git_sha", ""))[:12] or None,
        "version": f"runtime-v{config.get('runtime_version')}" if config.get("runtime_version") else None,
        "tags": [tag, scenario, status],
        "metadata": {"run_dir": f"{tag}/{run_dir.name}", "status": meta.get("status"), "core_fail_reasons": scores.get("core_fail_reasons"),
                     "models": config.get("models"), "runtime_version": config.get("runtime_version"), "git_sha": config.get("git_sha"),
                     "session_id": meta.get("session_id"), "agent_calls": meta.get("agent_calls"), "warning": meta.get("warning"),
                     "judge_summary": (judge or {}).get("verdict", {}).get("summary")},
    }, sid("ev", trace_id, "trace"))]

    # one generation per agent: tokens, cost, first-to-last event span
    by_agent = (details.get("cost_by_agent") or {})
    spans = meta.get("agent_span_s") or {}
    first = _agent_first_seen(run_dir)
    for agent, a in by_agent.items():
        start = started + timedelta(seconds=first.get(agent, 0))
        end = start + timedelta(seconds=spans.get(agent, 0))
        body = {"id": sid("gen", trace_id, agent), "traceId": trace_id, "name": agent, "model": a.get("model_id"),
                "startTime": _iso(start), "endTime": _iso(end),
                "usageDetails": {"input": a.get("input", 0), "output": a.get("output", 0),
                                 "cache_read_input_tokens": a.get("cache_read", 0), "cache_creation_input_tokens": a.get("cache_write", 0)},
                "metadata": {"calls": (meta.get("agent_calls") or {}).get(agent), "cache_hit_rate": a.get("cache_hit_rate")}}
        if a.get("cost_usd") is not None:
            body["costDetails"] = {"total": a["cost_usd"]}
        events.append(_event("generation-create", body, sid("ev", trace_id, "gen", agent)))

    def score(name, value, data_type, comment=None, config_id=None):
        body = {"id": sid("score", trace_id, name), "traceId": trace_id, "name": name, "value": value, "dataType": data_type}
        if comment:
            body["comment"] = comment[:3000]
        if config_id:
            body["configId"] = config_id
        events.append(_event("score-create", body, sid("ev", trace_id, "score", name)))

    reasons = ", ".join(scores.get("core_fail_reasons") or []) or None
    for k in BOOLEAN_SCORES:
        if isinstance(scores.get(k), bool):
            score(k, 1 if scores[k] else 0, "BOOLEAN", comment=reasons if k == "core_pass" else None)
    for k in NUMERIC_SCORES:
        v = scores.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            score(k, float(v), "NUMERIC")
    if judge:
        v = judge["verdict"]
        for c in v.get("criteria", []):
            score(c["criterion"], float(c["score"]), "NUMERIC", comment=c.get("justification"), config_id=configs.get(c["criterion"]))
        for r in v.get("requirements", []):
            name = f"req:{r['requirement_id']}"
            score(name, r["status"], "CATEGORICAL", comment=r.get("evidence"), config_id=configs.get(name))
    return events, trace_id, scenario, config


def _agent_first_seen(run_dir):
    first = {}
    path = Path(run_dir) / "events.jsonl"
    if not path.is_file():
        return first
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            a = ev.get("agent_name")
            if a and a not in first:
                first[a] = ev.get("client_ts", 0)
    return first


# --- dataset runs, pairwise, annotation queue ---------------------------------------

def link_run_items(lf, tag, linked, description):
    for trace_id, scenario in linked:
        lf.post("/api/public/dataset-run-items", {"runName": tag, "runDescription": description,
                                                 "datasetItemId": sid("item", scenario), "traceId": trace_id})


def pairwise_scores(lf, tag_dir):
    events = []
    for f in Path(tag_dir).glob("pairwise_vs_*.json"):
        summary = json.loads(f.read_text(encoding="utf-8"))
        run = lf.get(f"/api/public/datasets/{DATASET}/runs/{summary['candidate']}")
        for scenario, s in summary["scenarios"].items():
            if not s.get("pairs"):
                continue
            name = f"pairwise_win_rate_vs_{summary['baseline']}:{scenario}"
            comment = (f"{s['pairs']} pairs · W/T/L/both_bad {s['wins']}/{s['ties']}/{s['losses']}/{s['both_bad']} · "
                       f"position-consistent {s['position_consistency']:.0%} · judge {summary['judge_model']}")
            events.append(_event("score-create", {"id": sid("score", run["id"], name), "datasetRunId": run["id"], "name": name,
                                                  "value": s["win_rate"], "dataType": "NUMERIC", "comment": comment},
                                 sid("ev", run["id"], name)))
    if events:
        lf.ingest(events)
    return len(events)


def queue_traces(lf, trace_ids, configs):
    queue = next((q for q in lf.pages("/api/public/annotation-queues") if q["name"] == QUEUE), None)
    if queue is None:
        ids = [configs[c] for c in CRITERIA] + [v for k, v in configs.items() if k.startswith("req:")]
        queue = lf.post("/api/public/annotation-queues", {"name": QUEUE, "scoreConfigIds": ids,
                        "description": "Label reports blind to the judge (hide API scores), then run calibrate.py langfuse"})
    queued = {i["objectId"] for i in lf.pages(f"/api/public/annotation-queues/{queue['id']}/items")}
    added = 0
    for t in trace_ids:
        if t not in queued:
            lf.post(f"/api/public/annotation-queues/{queue['id']}/items", {"objectId": t, "objectType": "TRACE"})
            added += 1
    return queue, added


def credentials():
    env = {**dotenv_values(HERE / "langfuse.env"), **os.environ}
    keys = ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")
    missing = [k for k in keys if not env.get(k)]
    if missing:
        raise SystemExit(f"missing {', '.join(missing)} (set them in the environment or eval-harness/langfuse.env)")
    return [env[k] for k in keys]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tags", nargs="+", help="tag folders under eval_results/")
    ap.add_argument("--queue", action="store_true", help=f"add the traces to the annotation queue {QUEUE}")
    ap.add_argument("--dry-run", action="store_true", help="build the events and print counts; send nothing")
    args = ap.parse_args()

    scenarios_all = yaml.safe_load(SCENARIOS_FILE.read_text(encoding="utf-8"))["scenarios"]
    scenarios = {n: load_scenario(n) for n in scenarios_all}
    req_ids = sorted({r["id"] for s in scenarios.values() for r in s.get("requirements", [])})

    if args.dry_run:
        for tag in args.tags:
            runs = sorted(p.parent for p in Path(tag).glob("*/scores.json") if (p.parent / "run.json").is_file())
            n = sum(len(run_events(r, Path(tag).name, {})[0]) for r in runs)
            print(f"{Path(tag).name}: {len(runs)} runs → {n} events")
        return 0

    lf = Langfuse(*credentials())
    configs = ensure_score_configs(lf, req_ids)
    ensure_dataset_items(lf, scenarios)
    manifest_path = HERE / "eval_results" / "langfuse_sync.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    all_traces = []
    for tag_dir in args.tags:
        tag = Path(tag_dir).name
        runs = sorted(p.parent for p in Path(tag_dir).glob("*/scores.json") if (p.parent / "run.json").is_file())
        events, linked, configs_seen = [], [], set()
        for r in runs:
            ev, trace_id, scenario, config = run_events(r, tag, configs)
            events += ev
            linked.append((trace_id, scenario))
            configs_seen.add((str(config.get("git_sha", ""))[:7], str(config.get("runtime_version"))))
            manifest[trace_id] = {"run_dir": str(r.relative_to(HERE)), "tag": tag, "scenario": scenario}
        lf.ingest(events)
        desc = "; ".join(f"git {g} runtime v{v}" for g, v in sorted(configs_seen))
        link_run_items(lf, tag, linked, desc)
        n_pw = pairwise_scores(lf, tag_dir)
        all_traces += [t for t, _ in linked]
        print(f"{tag}: {len(runs)} runs, {len(events)} events, {n_pw} pairwise scores → dataset run '{tag}'")
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    if args.queue:
        queue, added = queue_traces(lf, all_traces, configs)
        print(f"annotation queue '{QUEUE}': {added} trace(s) added")
    print(f"→ {lf.host}  (Datasets → {DATASET})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
