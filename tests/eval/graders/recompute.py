"""Independent recomputation of calculation_metadata.json against the source CSV.

The Validator checks the Coder with code the agents wrote themselves; this
grader re-derives the simple aggregates with pandas, outside the agent loop.
Formulas are free-form pseudo-SQL, so only a small whitelist is recomputed and
everything else is counted as unsupported rather than guessed.
"""
import re
from pathlib import Path

from .citations import load_calculations

COL = r"\s*`?([^()`*]+?)`?\s*"
PATTERNS = [
    (re.compile(rf"^SUM\({COL}\)$", re.I), lambda df, a: df[a].sum()),
    (re.compile(rf"^(?:AVG|MEAN)\({COL}\)$", re.I), lambda df, a: df[a].mean()),
    (re.compile(rf"^MEDIAN\({COL}\)$", re.I), lambda df, a: df[a].median()),
    (re.compile(rf"^MIN\({COL}\)$", re.I), lambda df, a: df[a].min()),
    (re.compile(rf"^MAX\({COL}\)$", re.I), lambda df, a: df[a].max()),
    (re.compile(r"^COUNT\(\s*\*\s*\)$", re.I), lambda df: len(df)),
    (re.compile(rf"^COUNT\(\s*DISTINCT{COL}\)$", re.I), lambda df, a: df[a].nunique()),
    (re.compile(rf"^SUM\({COL}\)\s*/\s*COUNT\(\s*\*\s*\)$", re.I), lambda df, a: df[a].sum() / len(df)),
    (re.compile(rf"^SUM\({COL}\)\s*/\s*COUNT\(\s*DISTINCT{COL}\)$", re.I), lambda df, a, b: df[a].sum() / df[b].nunique()),
]


def load_csv(path):
    import pandas as pd
    return pd.read_csv(path, encoding="utf-8-sig")


def recompute(formula, df):
    """Return the recomputed value, or None if the formula is not supported."""
    f = (formula or "").strip()
    for pattern, fn in PATTERNS:
        m = pattern.match(f)
        if not m:
            continue
        cols = [g.strip() for g in m.groups()]
        if any(c not in df.columns for c in cols):
            return None
        return float(fn(df, *cols))
    return None


def _tolerance(expected):
    """Allow the metadata to store a display-rounded value, nothing more.

    Large aggregates may be rounded to an integer (19655.0 for 19655.41),
    small ones to 2 decimals. A wrong sum (off by 1,000 on 16M) must fail,
    so the relative part stays tiny.
    """
    return max(abs(expected) * 1e-6, 0.5 if abs(expected) >= 100 else 0.005)


def grade(artifacts_dir, csv_path):
    calcs = load_calculations(artifacts_dir)
    if calcs is None:
        return {"recompute_supported": 0, "details": ["calculation_metadata.json unusable"]}
    df = load_csv(csv_path)
    source_name = Path(csv_path).name
    details = []
    supported = ok = 0
    for cid, c in calcs.items():
        # Only calculations made directly on the raw file; derived artifacts
        # (.pkl, intermediate CSVs) have columns we cannot reproduce.
        if Path(str(c.get("source_file", ""))).name != source_name:
            continue
        expected = recompute(c.get("formula"), df)
        if expected is None:
            continue
        supported += 1
        try:
            stored = float(c.get("value"))
        except (TypeError, ValueError):
            details.append(f"{cid}: non-numeric value {c.get('value')!r}")
            continue
        if abs(stored - expected) <= _tolerance(expected):
            ok += 1
        else:
            details.append(f"{cid}: {c.get('formula')} stored {stored} but recomputed {expected}")
    return {
        "recompute_supported": supported,
        "recompute_total": len(calcs),
        "recompute_match_rate": ok / supported if supported else None,
        "details": details,
    }
