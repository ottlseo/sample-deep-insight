# Deep Insight Eval Harness

**→ Back to [Main README](../README.md)**

An automatic scorecard for Deep Insight. When you change code, prompts or
models, it answers two questions with numbers instead of "the report reads
better":

1. **Does core functionality still work?** → PASS / FAIL per run
2. **What got better or worse, and by how much?** → before/after table

## What it is

| Part | What it does |
|---|---|
| **Runner** `run_eval.py` | Sends a fixed analysis request to the deployed runtime, approves the plan review automatically (no human), and saves the report, citations, code-execution logs and token usage |
| **Grader** `grade.py` | Scores one run: files produced, numbers correct, cost, time → `scores.json` with PASS / FAIL |
| **Comparer** `compare.py` | Puts two versions side by side: mean ± std per metric and the difference |
| **LLM judge** `judge.py`, `pairwise.py` | Grades what numbers can't: did the report answer the request, and is it better than the baseline's? (paid, opt-in) |
| **Static checks** `pytest` | Seconds, no AWS: prompt templates render, model request fields are valid, and the graders really catch planted defects |

Example `compare.py` output:

```
                       baseline        opus55          Δ
printed value = citation  100.0%        100.0%          +0.0%p
core facts correct        100.0%        100.0%          +0.0%p
cost                      $10.20        $8.50           -17% ▲ better
duration                  1920s         1680s           -12% ▲ better
```

## Workflow

```bash
# 0. once: setup (below)
# 1. measure the current version → reference point
.venv/bin/python run_eval.py --scenario moon_market_kr --repeat 3 --tag baseline
# 2. change code / prompts / models, redeploy the runtime, measure again
.venv/bin/python run_eval.py --scenario moon_market_kr --repeat 3 --tag my-change
# 3. compare
.venv/bin/python compare.py eval_results/baseline eval_results/my-change
```

Each run costs real money (about $4–5 for `moon_market_kr_simple`, more for
the full query) and takes 15–40 minutes, so start with
`moon_market_kr_simple` when you only need a smoke test.

## LLM judge: analysis quality

The graders above check that numbers are right. They can't see whether the
report answers the request: a report that only states total revenue correctly
passes all of them. The judge closes that gap. It runs on an **OpenAI model in
Amazon Bedrock** (Converse API, `judge.yaml`), a different family from the
Claude agents, so it isn't grading its own family's writing.

**Pointwise** (`grade.py --judge`, `run_eval.py --judge`) reads the request,
the scenario's `requirements` (`scenarios.yaml`) and the report, and returns:

- each requirement as met / partial / missing → `judge_requirement_coverage`,
  `judge_requirements_missing` (**a missing requirement fails `core_pass`**)
- 1-5 scores with anchored definitions: `evidence_linkage` (claims tied to
  findings), `strategy_specificity` (target, steps, quantified effect,
  priority), `insight_depth` (beyond descriptive stats), `reasoning_soundness`
  (no overclaiming, data limits stated)

Left out on purpose: numeric accuracy (the graders do it deterministically),
chart quality (the judge reads text only), length and formatting.

**Pairwise** (`pairwise.py`) is the main A/B signal: judges are better at
"which of these two" than at absolute scores. Each candidate report is
compared with a frozen baseline report, twice with the order swapped; a
verdict that flips with the order counts as a tie. Results show in
`compare.py`.

```bash
.venv/bin/python judge_sanity.py eval_results/baseline/<run> --scenario moon_market_kr  # does the judge fail what must fail?
.venv/bin/python grade.py eval_results/baseline/<run> --scenario moon_market_kr --judge
.venv/bin/python pairwise.py eval_results/baseline eval_results/baseline   # noise floor, expect ~50%
.venv/bin/python pairwise.py eval_results/baseline eval_results/my-change
```

- **Use enough pairs.** Measured on the baseline (3 runs vs each other): win
  rate 50%, but one of 3 pairs flipped with the order (position consistency
  67%). With 3 pairs a single pair moves the win rate by ±33 points, so
  compare with `--pairs all` (3 × 3 = 9 pairs) or more before acting on it.
- **Trust, then use.** `judge_sanity.py` feeds the judge a real report and
  variants with known answers (numbers only, strategies removed, a different
  question, empty, a prompt injection) and fails if it scores them wrong. The
  baseline-vs-baseline pairwise run shows how far from 50% noise alone moves
  the win rate.
- **Calibrate once with people.** `calibrate.py export` writes a blind label
  sheet (no judge scores in it); label 10-20 reports, then
  `calibrate.py score` prints agreement per criterion (exact, ±1, Spearman,
  bias). Re-check whenever the rubric or judge model changes.
- Judge results are cached (`judge.json`, `pairwise/`) by report hash and
  judge model, so re-grading doesn't pay twice. A reply that breaks the schema
  is recorded as `judge_error`, never as a score.

## Setup

```bash
cd eval-harness
uv venv .venv --python 3.12
uv pip install -p .venv/bin/python -r requirements.txt
```

The runner reads `RUNTIME_ARN`, `AWS_REGION` and `S3_BUCKET_NAME` from
`managed-agentcore/.env` (written by the deploy scripts), or take
`--runtime-arn`, `--region`, `--bucket`.

## Static checks (no AWS)

```bash
.venv/bin/python -m pytest -q
```

`test_no_legacy_human_stop_sequence` is an expected failure (`xfail`, strict)
documenting a known bug; it turns into an error once the bug is fixed, so the
marker gets removed with the fix.

## Scenarios

`scenarios.yaml` defines dataset × query pairs:

| Scenario | Use |
|---|---|
| `moon_market_kr` | Main benchmark: full growth-opportunity analysis |
| `moon_market_kr_simple` | Cheap smoke test: totals, AOV, category share |
| `moon_market_kr_revision` | Same as simple, but requests one plan revision first (tests `planner_revise`) |
| `moon_market_en` | English data and query |
| `yummy_food` | **Hold-out**: don't tune prompts against it; needs `--allow-holdout` |

## Runs against the deployed runtime

```bash
.venv/bin/python run_eval.py --scenario moon_market_kr --repeat 3 --tag baseline \
    --runtime-arn arn:aws:bedrock-agentcore:<region>:<account>:runtime/<id> --region us-west-2
```

Each run is saved to `eval_results/<tag>/<scenario>-<timestamp>-<n>/`:
`events.jsonl` (every streamed event), `run.json` (status, timings),
`usage.json`, `config.json` (git SHA + the runtime's model IDs), the
downloaded session under `s3/` (`artifacts/` links to its artifacts), and
`scores.json`.

- Plan reviews are answered by writing the S3 feedback file the runtime polls
  (the same mechanism as `02_invoke_agentcore_runtime_vpc.py`), so there is no
  human wait and no 300 s timeout. A scenario's `hitl` list sends scripted
  revision requests first.
- Token usage is summed from the `usage_metadata` events in the stream; it
  matches the runtime's own `token_usage.json`.
- Model IDs come from the **runtime's** environment, not the local checkout,
  so comparing model configs means redeploying (or one runtime per config).
- The runtime doesn't stream tool calls. `agent_calls` counts `usage_metadata`
  events (one per agent invocation), and code failures come from the
  executor's `debug/execution_*.json`.

## Comparing tags

```bash
.venv/bin/python compare.py eval_results/baseline eval_results/candidate --out compare.md
```

One table per scenario with mean ± std per metric and Δ vs the first tag. A
metric is flagged ▼/▲ only when the difference exceeds the run-to-run std of
either side; with n=1 any difference is flagged, so use `--repeat 3` or more.
Also prints per-agent cost with the model each agent ran on.

## Grading a run

```bash
.venv/bin/python grade.py <run_dir> --scenario moon_market_kr
```

`<run_dir>` holds an `artifacts/` folder (as downloaded from
`s3://<bucket>/deep-insight/fargate_sessions/<session_id>/`). Writes
`<run_dir>/scores.json`. Exit code 0 = core pass.

### What is scored

| Metric | Meaning | Source |
|---|---|---|
| `all_required_ok` | report docx, citations.json, calculation_metadata.json, validation_report.txt exist and parse | `graders/artifacts.py` |
| `citation_value_match_rate` | citations.json value == calculation_metadata value | `graders/citations.py` |
| `recompute_match_rate` | metadata values re-derived from the raw CSV with pandas (SUM/AVG/COUNT/… only; `recompute_supported` says how many) | `graders/recompute.py` |
| `cited_value_match_rate` | the number printed right before `[n]` in the report equals citation n | `graders/report.py` |
| `broken_citation_refs` | `[n]` in the body with no citation n | `graders/report.py` |
| `citation_coverage` | share of significant numbers (amounts, %, decimals) in the body that carry a citation. Approximate | `graders/report.py` |
| `core_fact_recall` | core answer-key facts (total revenue, orders, AOV) stated with the right value | `graders/report.py` + `answer_keys/` |
| `audit_pass`, `audit_block_findings` | the Auditor's own verdict, when the version has an Auditor | `graders/audit.py` |
| `code_exec_failed`, `code_exec_fail_causes` | agent code that failed in the Fargate sandbox, bucketed by cause (missing file, AttributeError, …) | `graders/executions.py` |
| `cost_usd`, `cache_hit_rate` | token usage × `pricing.yaml` | `cost.py` |

`core_pass` is defined once, in `PASS_RULES` in `grade.py`.

### Answer keys

`answer_keys/build_answer_key.py` computes facts from the datasets with
pandas. Regenerate after a dataset changes and review the values by hand.
A fact only counts when its `keywords` appear in the same paragraph, so a
coincidentally equal number elsewhere does not score.

### Prices

`pricing.yaml` holds Bedrock on-demand prices per model family. Entries marked
`verified: true` were read from the AWS Pricing API for us-west-2 global
inference; `verified: false` entries are list prices not yet checked. `null`
prices are reported as `unpriced_tokens` instead of guessed.
