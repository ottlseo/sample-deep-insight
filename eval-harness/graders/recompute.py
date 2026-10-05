"""Independent recomputation of calculation_metadata.json against the source CSV.

The Validator checks the Coder with code the agents wrote themselves; this
grader re-derives the values with pandas, outside the agent loop. Formulas are
free-form pseudo-SQL, so only the shapes agents actually write are parsed, and
everything else is counted as unsupported rather than guessed:

    AGG(col)                              SUM / AVG / MEAN / MEDIAN / MIN / MAX / COUNT
    COUNT(*), COUNT(DISTINCT col)
    SUM(a) / COUNT(*), SUM(a) / COUNT(DISTINCT b)
    AGG(col) GROUP BY key[+key2]          also "grouped by"; key may be `weekday`
    AGG(col) WHERE c1=v1 AND c2=v2        literal values filter; placeholders act like GROUP BY
    MAX(AGG(col) GROUP BY key)            min / max over the groups
    SUM(col)/TOTAL(col)*100 GROUP BY key  each group's share in percent
    … *100                                trailing multiplier

For grouped values, which group a calculation means is read from its
description ("2025-05-02 일별 매출", "월요일 총매출", "M_30대 평균"). When the
description names exactly one group, the value is checked against that group.
When it names none, the value only has to equal some group's value; those
looser checks are counted in `recompute_lenient`.
"""
import re
from pathlib import Path

from .citations import load_calculations

AGGS = {
    "SUM": lambda s: s.sum(), "AVG": lambda s: s.mean(), "MEAN": lambda s: s.mean(),
    "MEDIAN": lambda s: s.median(), "MIN": lambda s: s.min(), "MAX": lambda s: s.max(),
    "COUNT": lambda s: s.count(),
}
WEEKDAY_KEYS = {"weekday", "dayofweek", "day_of_week", "요일", "dow"}
WEEKDAY_LABELS = [("월요일", "monday", "mon"), ("화요일", "tuesday", "tue"), ("수요일", "wednesday", "wed"),
                  ("목요일", "thursday", "thu"), ("금요일", "friday", "fri"), ("토요일", "saturday", "sat"),
                  ("일요일", "sunday", "sun")]
GENDER_ALIASES = {"M": ("남성", "남자", "male", "men"), "F": ("여성", "여자", "female", "women")}

AGG_RE = r"(SUM|AVG|MEAN|MEDIAN|MIN|MAX|COUNT)\(\s*(DISTINCT\s+)?([^()]*?)\s*\)"


def load_csv(path):
    import pandas as pd
    return pd.read_csv(path, encoding="utf-8-sig")


def _norm(name):
    return re.sub(r"[\s_\-`'\"]", "", str(name)).lower()


def _column(df, name):
    """Match a formula's column name to the CSV header, ignoring case, spaces, _ and -."""
    want = _norm(name)
    return next((c for c in df.columns if _norm(c) == want), None)


def _agg(df, func, distinct, col):
    col = col.strip()
    if func == "COUNT" and (col in ("*", "") or _norm(col) in ("rows", "row", "1", "orders", "transactions")):
        return float(len(df))
    real = _column(df, col)
    if real is None:
        return None
    if distinct:
        return float(df[real].nunique()) if func == "COUNT" else None
    return float(AGGS[func](df[real]))


def _key_series(df, key):
    """Series to group by for one key, plus whether it is a derived weekday."""
    if _norm(key) in WEEKDAY_KEYS:
        import pandas as pd
        date_col = _column(df, "Date") or _column(df, "날짜")
        if date_col is None:
            return None
        dates = pd.to_datetime(df[date_col], format="%m/%d/%y", errors="coerce")
        if dates.isna().all():
            dates = pd.to_datetime(df[date_col], format="mixed", errors="coerce")
        return dates.dt.dayofweek.rename("weekday")
    real = _column(df, key)
    return None if real is None else df[real]


def _labels(key_name, value):
    """Strings that would name this group value in a description."""
    v = str(value)
    out = [v]
    if key_name == "weekday" and value == value:  # not NaN
        out = list(WEEKDAY_LABELS[int(value)])
    elif v in GENDER_ALIASES:
        out = [v, *GENDER_ALIASES[v]]
    elif re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}", v):  # 5/1/25 → 2025-05-01, 5월 1일
        import pandas as pd
        d = pd.to_datetime(v, format="%m/%d/%y", errors="coerce")
        if d is not pd.NaT:
            out += [d.strftime("%Y-%m-%d"), f"{d.month}월 {d.day}일", f"{d.month}/{d.day}"]
    elif v == "nan":
        out = ["없음", "미적용", "no promotion", "none"]
    return out


def _mentions(text, label):
    label = label.lower()
    if re.fullmatch(r"[a-z0-9]{1,3}", label):  # short codes like M, F, 30s need word edges
        return re.search(rf"(?<![a-z0-9]){re.escape(label)}(?![a-z0-9])", text) is not None
    return label in text


def _grouped(df, keys, inner, description, outer=None, share=False):
    """Per-group values, then pick the group the description names (or all as candidates)."""
    series = [_key_series(df, k) for k in keys]
    if any(s is None for s in series):
        return None
    groups = df.groupby(series, dropna=False)
    total = inner(df) if share else None
    values = {}
    for gkey, part in groups:
        gkey = gkey if isinstance(gkey, tuple) else (gkey,)
        v = inner(part)
        if v is None:
            return None
        values[gkey] = v / total * 100 if share else v
    if outer:
        return {"value": outer(list(values.values())), "lenient": False}
    text = (description or "").lower()
    names = [s.name for s in series]
    named = [g for g in values if all(any(_mentions(text, lbl) for lbl in _labels(n, part)) for n, part in zip(names, g))]
    if len(named) == 1:
        return {"value": values[named[0]], "lenient": False}
    candidates = named or list(values)
    return {"candidates": [values[g] for g in candidates], "lenient": True}


def recompute(formula, df, description=""):
    """Recomputed value as {"value": x} or {"candidates": [...], "lenient": True}; None if unsupported."""
    f = re.sub(r"\s+", " ", (formula or "").strip())
    scale = 1.0
    m = re.fullmatch(r"(.*?)\s*\*\s*100", f)
    if m and not re.search(r"/\s*TOTAL", f, re.I):
        f, scale = m.group(1).strip(), 100.0

    def scaled(r):
        if r is None:
            return None
        if "value" in r:
            return {**r, "value": r["value"] * scale}
        return {**r, "candidates": [c * scale for c in r["candidates"]]}

    # MAX(AGG(col) GROUP BY key) / MIN(...)
    m = re.fullmatch(rf"(MAX|MIN)\(\s*{AGG_RE}\s+(?:GROUP(?:ED)? BY)\s+([^()]+?)\s*\)", f, re.I)
    if m:
        outer, func, distinct, col, key = m.group(1).upper(), m.group(2).upper(), m.group(3), m.group(4), m.group(5)
        return scaled(_grouped(df, _keys(key), lambda d: _agg(d, func, distinct, col), description, max if outer == "MAX" else min))

    # SUM(col)/TOTAL(col)*100 GROUP BY key
    m = re.fullmatch(rf"SUM\(\s*([^()]+?)\s*\)\s*/\s*TOTAL\(\s*[^()]+?\s*\)\s*\*\s*100\s+(?:GROUP(?:ED)? BY)\s+(.+)", f, re.I)
    if m:
        col, key = m.group(1), m.group(2)
        return _grouped(df, _keys(key), lambda d: _agg(d, "SUM", None, col), description, share=True)

    # AGG(col) GROUP BY key / grouped by key
    m = re.fullmatch(rf"{AGG_RE}\s+(?:GROUP(?:ED)? BY)\s+(.+)", f, re.I)
    if m:
        func, distinct, col, key = m.group(1).upper(), m.group(2), m.group(3), m.group(4)
        return scaled(_grouped(df, _keys(key), lambda d: _agg(d, func, distinct, col), description))

    # AGG(col) WHERE c1=v1 AND c2=v2
    m = re.fullmatch(rf"{AGG_RE}\s+WHERE\s+(.+)", f, re.I)
    if m:
        func, distinct, col, cond = m.group(1).upper(), m.group(2), m.group(3), m.group(4)
        sub, placeholder_keys = df, []
        for part in re.split(r"\s+AND\s+", cond, flags=re.I):
            cm = re.fullmatch(r"\s*([^=]+?)\s*=\s*['\"]?(.+?)['\"]?\s*", part)
            if not cm:
                return None
            real = _column(df, cm.group(1))
            if real is None:
                return None
            val = cm.group(2)
            if val in set(df[real].astype(str)):
                sub = sub[sub[real].astype(str) == val]
            elif re.fullmatch(r"[a-z_]{1,3}", val):  # placeholder such as g / a
                placeholder_keys.append(real)
            else:
                return None
        inner = lambda d: _agg(d, func, distinct, col)
        if placeholder_keys:
            return scaled(_grouped(sub, placeholder_keys, inner, description))
        v = inner(sub)
        return None if v is None else scaled({"value": v, "lenient": False})

    # ratios of plain aggregates
    m = re.fullmatch(rf"SUM\(\s*([^()]+?)\s*\)\s*/\s*COUNT\(\s*(DISTINCT\s+)?([^()]*?)\s*\)", f, re.I)
    if m:
        num, den = _agg(df, "SUM", None, m.group(1)), _agg(df, "COUNT", m.group(2), m.group(3))
        return None if num is None or not den else scaled({"value": num / den, "lenient": False})

    m = re.fullmatch(AGG_RE, f, re.I)
    if m:
        v = _agg(df, m.group(1).upper(), m.group(2), m.group(3))
        return None if v is None else scaled({"value": v, "lenient": False})
    return None


def _keys(key_text):
    return [k for k in re.split(r"\s*[+,&]\s*|\s+and\s+", key_text.strip(), flags=re.I) if k]


def same_value(stored, expected):
    """Stored equals expected rounded to the precision the stored value was written with.

    19655.0 matches 19655.41 (stored as a whole number), 18.6 matches 18.5517
    (one decimal), a full-precision float must match to ~1e-9, and 16,432,923
    does not match 16,431,923.
    """
    text = repr(float(stored))
    if "e" in text or "." not in text:
        decimals = 6
    else:
        decimals = 0 if text.endswith(".0") else len(text.split(".")[1])
    if decimals >= 6:  # written at full precision: compare as floats
        return abs(stored - expected) <= max(abs(expected) * 1e-9, 1e-9)
    return abs(round(expected, decimals) - stored) <= max(abs(stored) * 1e-12, 1e-9)


def grade(artifacts_dir, csv_path):
    calcs = load_calculations(artifacts_dir)
    if calcs is None:
        return {"recompute_supported": 0, "details": ["calculation_metadata.json unusable"]}
    df = load_csv(csv_path)
    source_name = Path(csv_path).name
    details = []
    supported = ok = lenient = 0
    for cid, c in calcs.items():
        # Only calculations made directly on the raw file; derived artifacts
        # (.pkl, intermediate CSVs) have columns we cannot reproduce.
        if Path(str(c.get("source_file", ""))).name != source_name:
            continue
        try:
            r = recompute(c.get("formula"), df, c.get("description", ""))
        except Exception:
            r = None  # a formula we can't evaluate is unsupported, not wrong
        if r is None:
            continue
        try:
            stored = float(c.get("value"))
        except (TypeError, ValueError):
            supported += 1
            details.append(f"{cid}: non-numeric value {c.get('value')!r}")
            continue
        supported += 1
        lenient += r.get("lenient", False)
        if "value" in r:
            hit = same_value(stored, r["value"])
            want = r["value"]
        else:
            hit = any(same_value(stored, v) for v in r["candidates"])
            want = f"one of {len(r['candidates'])} group values"
        if hit:
            ok += 1
        else:
            details.append(f"{cid}: {c.get('formula')} [{c.get('description', '')[:40]}] stored {stored} but recomputed {want}")
    return {
        "recompute_supported": supported,
        "recompute_total": len(calcs),
        "recompute_lenient": lenient,
        "recompute_match_rate": ok / supported if supported else None,
        "details": details,
    }
