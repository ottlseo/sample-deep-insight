"""Grade one run folder and write scores.json next to it.

A run folder is what run_eval.py writes (or a manual download from S3):

    <run_dir>/
      artifacts/          final_report_with_citations.docx, citations.json, ...
      usage.json          optional, token usage by agent
      run.json            optional, runner metadata (status, timings)

Usage:
    python grade.py <run_dir> --scenario moon_market_kr
    python grade.py <run_dir> --csv path/to.csv --answer-key answer_keys/x.json
"""
import argparse
import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from graders import artifacts, audit, citations, recompute, report  # noqa: E402
import cost  # noqa: E402

# The single definition of "core functionality works" for a run. Kept strict
# on integrity (wrong numbers must fail) and lenient on style.
PASS_RULES = {
    "all_required_ok": lambda v: v is True,
    "citations_ok": lambda v: v is True,
    "broken_citation_refs": lambda v: v == 0,
    "cited_value_match_rate": lambda v: v is None or v >= 0.95,
    "recompute_match_rate": lambda v: v is None or v == 1.0,
    "core_fact_recall": lambda v: v is None or v >= 2 / 3,
}


def load_scenario(name):
    data = yaml.safe_load((HERE / "scenarios.yaml").read_text(encoding="utf-8"))["scenarios"]
    if name not in data:
        raise SystemExit(f"unknown scenario {name!r}; known: {', '.join(data)}")
    s = dict(data[name])
    s["csv"] = str((HERE / s["csv"]).resolve())
    s["answer_key"] = str((HERE / s["answer_key"]).resolve())
    return s


def artifacts_dir(run_dir):
    run_dir = Path(run_dir)
    return run_dir / "artifacts" if (run_dir / "artifacts").is_dir() else run_dir


def grade_run(run_dir, csv_path=None, answer_key_path=None):
    run_dir = Path(run_dir)
    adir = artifacts_dir(run_dir)
    key = json.loads(Path(answer_key_path).read_text(encoding="utf-8")) if answer_key_path else None

    scores, details = {}, {}
    sections = [
        ("artifacts", lambda: artifacts.grade(adir)),
        ("citations", lambda: citations.grade(adir)),
        ("report", lambda: report.grade(adir, key)),
        ("audit", lambda: audit.grade(adir)),
    ]
    if csv_path:
        sections.append(("recompute", lambda: recompute.grade(adir, csv_path)))
    for name, fn in sections:
        try:
            result = fn()
        except Exception as e:  # a grader bug must not hide the other scores
            result = {"details": [f"grader error: {type(e).__name__}: {e}"], f"{name}_grader_error": True}
        details[name] = result.pop("details", [])
        scores.update(result)

    usage_path = run_dir / "usage.json"
    if usage_path.is_file():
        c = cost.compute(json.loads(usage_path.read_text(encoding="utf-8")))
        scores.update({k: v for k, v in c.items() if k != "by_agent"})
        details["cost_by_agent"] = c["by_agent"]

    run_meta_path = run_dir / "run.json"
    if run_meta_path.is_file():
        meta = json.loads(run_meta_path.read_text(encoding="utf-8"))
        for k in ("status", "duration_s", "time_to_first_plan_s", "plan_revisions", "tool_errors", "agent_calls", "agent_time_s"):
            if k in meta:
                scores[k] = meta[k]

    failed = [k for k, rule in PASS_RULES.items() if not rule(scores.get(k))]
    if scores.get("status") not in (None, "completed"):
        failed.insert(0, "status")
    scores["core_pass"] = not failed
    scores["core_fail_reasons"] = failed
    return {"scores": scores, "details": details}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir")
    ap.add_argument("--scenario")
    ap.add_argument("--csv")
    ap.add_argument("--answer-key")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    csv_path, key_path = args.csv, args.answer_key
    if args.scenario:
        s = load_scenario(args.scenario)
        csv_path, key_path = csv_path or s["csv"], key_path or s["answer_key"]

    result = grade_run(args.run_dir, csv_path, key_path)
    out = Path(args.run_dir) / "scores.json"
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if not args.quiet:
        s = result["scores"]
        print(json.dumps(s, indent=2, ensure_ascii=False))
        print(f"\n{'PASS' if s['core_pass'] else 'FAIL'}  → {out}")
    return 0 if result["scores"]["core_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
