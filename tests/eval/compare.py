"""Compare eval results between tags (e.g. baseline vs a candidate change).

    python compare.py eval_results/baseline eval_results/opus55 [--scenario moon_market_kr] [--out compare.md]

Each tag folder holds run folders with scores.json (from run_eval.py or
grade.py). Prints one markdown table per scenario: mean ± std per metric,
delta vs the first tag, and a flag when the change is worse by more than the
baseline's own run-to-run spread.
"""
import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

# (metric, label, direction, kind) — direction +1 = higher is better, -1 = lower is better.
# kind: "rate" prints as %, "num" as a plain number, "usd", "s".
METRICS = [
    ("core_pass", "core pass rate", +1, "rate"),
    ("all_required_ok", "required artifacts ok", +1, "rate"),
    ("citation_value_match_rate", "citation = metadata", +1, "rate"),
    ("recompute_match_rate", "recompute matches CSV", +1, "rate"),
    ("cited_value_match_rate", "printed value = citation", +1, "rate"),
    ("broken_citation_refs", "broken [n] refs", -1, "num"),
    ("citation_coverage", "citation coverage", +1, "rate"),
    ("core_fact_recall", "core facts correct", +1, "rate"),
    ("other_facts_found", "other facts correct", +1, "num"),
    ("audit_pass", "auditor pass", +1, "rate"),
    ("audit_block_findings", "auditor block findings", -1, "num"),
    ("citation_count", "citations", +1, "num"),
    ("chart_count", "charts", 0, "num"),
    ("cost_usd", "cost", -1, "usd"),
    ("cache_hit_rate", "cache hit rate", +1, "rate"),
    ("tokens_input_total", "input tokens", -1, "num"),
    ("tokens_output", "output tokens", 0, "num"),
    ("duration_s", "duration", -1, "s"),
    ("time_to_first_plan_s", "time to first plan", -1, "s"),
    ("tool_errors", "tool errors (heuristic)", -1, "num"),
]


def load_runs(tag_dir):
    runs = []
    for scores_path in sorted(Path(tag_dir).glob("*/scores.json")):
        data = json.loads(scores_path.read_text(encoding="utf-8"))
        s = data["scores"]
        run_meta = scores_path.parent / "run.json"
        scenario = json.loads(run_meta.read_text())["scenario"] if run_meta.is_file() else scores_path.parent.name.rsplit("-", 3)[0]
        s["_agent_calls"] = json.loads(run_meta.read_text()).get("agent_calls", {}) if run_meta.is_file() else {}
        s["_cost_by_agent"] = data.get("details", {}).get("cost_by_agent", {})
        runs.append((scenario, s))
    return runs


def values(runs, metric):
    out = []
    for s in runs:
        v = s.get(metric)
        if isinstance(v, bool):
            v = float(v)
        if isinstance(v, (int, float)):
            out.append(float(v))
    return out


def summarize(vals):
    if not vals:
        return None
    return {"mean": statistics.fmean(vals), "std": statistics.stdev(vals) if len(vals) > 1 else 0.0, "n": len(vals)}


def fmt(v, kind):
    if v is None:
        return "–"
    if kind == "rate":
        return f"{v * 100:.1f}%"
    if kind == "usd":
        return f"${v:.2f}"
    if kind == "s":
        return f"{v:.0f}s"
    return f"{v:.1f}" if abs(v) < 100 else f"{v:,.0f}"


def fmt_stat(st, kind):
    if st is None:
        return "–"
    spread = f" ± {fmt(st['std'], kind)}" if st["n"] > 1 else ""
    return f"{fmt(st['mean'], kind)}{spread} (n={st['n']})"


def delta(base, cand, kind, direction):
    if base is None or cand is None:
        return "–", ""
    d = cand["mean"] - base["mean"]
    if kind == "rate":
        text = f"{d * 100:+.1f}%p"
    elif base["mean"]:
        text = f"{d / abs(base['mean']) * 100:+.0f}%"
    else:
        text = f"{d:+.2f}"
    # Worse by more than the baseline's own noise (or any amount if no spread known).
    noise = max(base["std"], cand["std"])
    worse = direction != 0 and d * direction < 0 and abs(d) > noise + 1e-9
    better = direction != 0 and d * direction > 0 and abs(d) > noise + 1e-9
    return text, "▼ worse" if worse else ("▲ better" if better else "")


def table(scenario, tags, runs_by_tag):
    lines = [f"### {scenario}", ""]
    header = ["metric"] + [Path(t).name for t in tags] + [f"Δ {Path(t).name}" for t in tags[1:]]
    lines += ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for metric, label, direction, kind in METRICS:
        stats = [summarize(values(runs_by_tag[t], metric)) for t in tags]
        if all(s is None for s in stats):
            continue
        row = [label] + [fmt_stat(s, kind) for s in stats]
        for s in stats[1:]:
            text, flag = delta(stats[0], s, kind, direction)
            row.append(f"{text} {flag}".strip())
        lines.append("| " + " | ".join(row) + " |")

    # Per-agent cost: where the money goes and what a model change moved.
    agents = sorted({a for t in tags for s in runs_by_tag[t] for a in s["_cost_by_agent"]})
    if agents:
        lines += ["", "| agent cost (mean) | " + " | ".join(Path(t).name for t in tags) + " |", "|" + "---|" * (len(tags) + 1)]
        for a in agents:
            cells = []
            for t in tags:
                v = [s["_cost_by_agent"][a]["cost_usd"] for s in runs_by_tag[t] if a in s["_cost_by_agent"] and s["_cost_by_agent"][a]["cost_usd"] is not None]
                models = {s["_cost_by_agent"][a]["model_id"] for s in runs_by_tag[t] if a in s["_cost_by_agent"]}
                short = ", ".join(sorted(m.split("anthropic.")[-1].split("-v1")[0] for m in models if m))
                cells.append(f"{fmt(statistics.fmean(v), 'usd')} ({short})" if v else "–")
            lines.append(f"| {a} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tags", nargs="+", help="tag folders; the first is the baseline")
    ap.add_argument("--scenario", action="append")
    ap.add_argument("--out")
    args = ap.parse_args()

    by_scenario = defaultdict(lambda: defaultdict(list))
    for t in args.tags:
        if not Path(t).is_dir():
            ap.error(f"no such tag folder: {t}")
        for scenario, s in load_runs(t):
            by_scenario[scenario][t].append(s)

    scenarios = args.scenario or sorted(by_scenario)
    parts = [f"## Eval comparison: {' vs '.join(Path(t).name for t in args.tags)}", "",
             "Δ is vs the first tag. ▼/▲ only when the difference exceeds the run-to-run std of either side.", ""]
    for sc in scenarios:
        parts += [table(sc, args.tags, by_scenario[sc]), ""]
    text = "\n".join(parts)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
