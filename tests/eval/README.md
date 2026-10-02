# Deep Insight eval harness

Numbers for two questions:

1. **Does core functionality still work after a change?** (`grade.py`, pytest)
2. **What got better or worse, and by how much?** (`run_eval.py` + `compare.py`)

```
L2  E2E scenarios     run_eval.py → grade.py → compare.py   needs the deployed runtime
L0  static checks     pytest                                 seconds, no AWS
    grader tests      pytest                                 proves the graders catch defects
```

## Setup

```bash
cd tests/eval
uv venv .venv --python 3.12
uv pip install -p .venv/bin/python -r requirements.txt
```

## L0 + grader tests (no AWS)

```bash
.venv/bin/python -m pytest -q
```

`test_no_legacy_human_stop_sequence` is an expected failure (`xfail`, strict)
documenting a known bug; it turns into an error once the bug is fixed, so the
marker gets removed with the fix.

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
| `cost_usd`, `cache_hit_rate` | token usage × `pricing.yaml` | `cost.py` |

`core_pass` is defined once, in `PASS_RULES` in `grade.py`.

### Answer keys

`answer_keys/build_answer_key.py` computes facts from the datasets with
pandas. Regenerate after a dataset changes and review the values by hand.
A fact only counts when its `keywords` appear in the same paragraph, so a
coincidentally equal number elsewhere does not score.

### Prices

`pricing.yaml` entries marked `verified: false` are list prices not yet
checked against Bedrock for the region. `null` prices are reported as
`unpriced_tokens` instead of guessed.
