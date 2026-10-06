"""Build answer keys (ground-truth facts) for the eval datasets with pandas.

Each fact describes one quantity a report on the dataset may state, precisely
enough for the fact checker (factcheck.py) to find it in a report and to tell
a wrong statement from one about a different period or subset:

  id           stable name; scenarios.yaml `pass:` refers to it
  kind         value     a number (value, unit, rel_tol)
               ranking   an order of names, best first (order); "중구 1위"
                         is checked against the position of 중구
               relation  a comparison (relation: above / below / equal)
  description  what the quantity is and how it was computed. Shown to the
               model while it reads the report, so it never holds the answer
  note         answer-side detail (its rank, how it compares), shown only
               when a statement is adjudicated against the answer
  scope        period and filter the value holds for. A statement about
               another period or subset is out of scope, not wrong
  unit         KRW, count, percent, ratio, rank or relation

Values are for the whole file (all rows, the full period, no filters) unless
`scope` says otherwise. Numbers that depend on a report's own formulas
(opportunity scores, scenario uplifts) are not here: they have no dataset
answer, and the recompute and citation graders already check them.

Usage:
    python answer_keys/build_answer_key.py          # writes answer_keys/*.json
Review the generated values once by hand before trusting them.
moon_market was checked by hand against the CSV on 2026-10-05.
"""
import json
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "managed-agentcore" / "data"
OUT = Path(__file__).resolve().parent

FULL = {"period": "full period of the file", "filter": "none"}


def value(id_, v, description, unit, rel_tol=0.005, scope=None, note=""):
    return {"id": id_, "kind": "value", "description": description, "scope": scope or FULL,
            "unit": unit, "value": round(float(v), 6), "rel_tol": rel_tol, "note": note}


def ranking(id_, order, description, scope=None):
    return {"id": id_, "kind": "ranking", "description": description, "scope": scope or FULL,
            "unit": "rank", "order": [str(n) for n in order]}


def relation(id_, rel, description, scope=None, note=""):
    return {"id": id_, "kind": "relation", "description": description, "scope": scope or FULL,
            "unit": "relation", "relation": rel, "note": note}


def moon_market(lang):
    csv = DATA / "moon_market" / lang / "moon-market-fresh-food-sales.csv"
    df = pd.read_csv(csv, encoding="utf-8-sig")
    amount = df["Amount"]
    promo = df["promotion-ids"].notna() & (df["promotion-ids"].astype(str).str.strip() != "")
    week2 = pd.to_datetime(df["Date"], format="%m/%d/%y") >= "2025-05-08"
    full = {"period": "full period 2025-05-01 to 2025-05-14", "filter": "none"}
    wk2 = {"period": "week 2 only, 2025-05-08 to 2025-05-14", "filter": "none"}
    f = []

    # Totals
    f += [
        value("total_revenue", amount.sum(), "Total revenue: SUM(Amount) over all rows", "KRW", scope=full),
        value("order_count", len(df), "Number of orders / transactions: rows in the file (there is no order id)", "count", rel_tol=0, scope=full),
        value("avg_order_value", amount.mean(), "Average order value (AOV, 객단가): SUM(Amount) / rows", "KRW", scope=full),
        value("category_count", df["Category"].nunique(), "Number of distinct product categories", "count", rel_tol=0, scope=full),
        value("product_count", df["Product"].nunique(), "Number of distinct products", "count", rel_tol=0, scope=full),
    ]

    # Promotions: a row has a promotion when promotion-ids is not empty
    f += [
        value("promo_order_share_pct", promo.mean() * 100, "Promotion penetration: share of rows with a promotion-id", "percent", rel_tol=0.01, scope=full),
        value("promo_aov_lift_pct", (amount[promo].mean() / amount[~promo].mean() - 1) * 100,
              "How much higher the AOV of promotion rows is than the AOV of non-promotion rows", "percent", rel_tol=0.01, scope=full),
        value("promo_order_count", int(promo.sum()), "Rows with a promotion applied", "count", rel_tol=0, scope=full),
        value("promo_revenue_share_pct", amount[promo].sum() / amount.sum() * 100, "Share of revenue from rows with a promotion", "percent", rel_tol=0.01, scope=full),
        value("promo_aov", amount[promo].mean(), "AOV of rows with a promotion", "KRW", scope=full),
        value("non_promo_aov", amount[~promo].mean(), "AOV of rows without a promotion", "KRW", scope=full),
    ]
    codes = df[promo].groupby("promotion-ids")["Amount"].sum().sort_values(ascending=False)
    for rank, (code, v) in enumerate(codes.head(3).items(), 1):
        f.append(value(f"promo_code_rank{rank}_revenue", v, f"Revenue of promotion code {code}", "KRW", scope=full, note=f"rank {rank} by revenue among codes"))

    # Categories, gender, age
    by_cat = df.groupby("Category")["Amount"].sum().sort_values(ascending=False)
    for rank, (name, v) in enumerate(by_cat.head(3).items(), 1):
        f.append(value(f"category_rank{rank}_revenue", v, f"Revenue of category {name}", "KRW", scope=full, note=f"rank {rank} by revenue"))
        f.append(value(f"category_rank{rank}_share_pct", v / by_cat.sum() * 100, f"Share of total revenue from category {name}", "percent", rel_tol=0.01, scope=full))
    for g, v in df.groupby("Gender")["Amount"].sum().items():
        f.append(value(f"gender_{g}_revenue_share_pct", v / amount.sum() * 100,
                       f"Share of total revenue from {'female' if g == 'F' else 'male'} customers (Gender = {g})", "percent", rel_tol=0.01, scope=full))
    by_age = df.groupby("Age Group")["Amount"].sum().sort_values(ascending=False)
    f.append(value("top_age_group_revenue", by_age.iloc[0], f"Revenue of age group {by_age.index[0]}", "KRW", scope=full, note="the top age group by revenue"))
    age40 = next(a for a in df["Age Group"].unique() if a.startswith("40"))
    f40 = amount[(df["Gender"] == "F") & (df["Age Group"] == age40)]
    f += [
        value("female_40s_revenue", f40.sum(), f"Revenue of female customers in age group {age40}", "KRW", scope=full),
        value("female_40s_aov", f40.mean(), f"AOV of female customers in age group {age40}", "KRW", scope=full),
    ]

    # Regions (ship-city), full period. 중구 is 3rd by revenue; it is 1st only in week 2.
    city = df.groupby("ship-city")["Amount"]
    rev = city.sum().sort_values(ascending=False)
    f.append(ranking("region_revenue_ranking", rev.index, "Regions (ship-city) ranked by revenue, highest first", scope=full))
    for rank, (name, v) in enumerate(rev.head(3).items(), 1):
        f.append(value(f"region_rank{rank}_revenue", v, f"Revenue of region {name}", "KRW", scope=full, note=f"rank {rank} by revenue"))
    f.append(value("region_revenue_median", rev.median(), "Median of revenue across the 25 regions", "KRW", scope=full))
    share = rev.cumsum() / rev.sum()
    n80 = int((share < 0.8).sum()) + 1
    f.append(value("regions_to_80pct_revenue", n80,
                   "Fewest top regions (by revenue) that together reach 80% of total revenue", "count", rel_tol=0, scope=full,
                   note=f"the top {n80 - 1} hold {share.iloc[n80 - 2] * 100:.2f}%, the top {n80} hold {share.iloc[n80 - 1] * 100:.2f}%"))
    aov = city.mean()
    seocho = next(c for c in aov.index if c in ("서초구", "Seocho-gu"))
    f += [
        value("seocho_aov", aov[seocho], f"AOV of region {seocho}", "KRW", scope=full),
        relation("seocho_aov_vs_overall", "below" if aov[seocho] < amount.mean() else "above",
                 f"AOV of region {seocho} compared with the overall AOV (above or below average)", scope=full,
                 note=f"{seocho} AOV {aov[seocho]:.2f} vs overall {amount.mean():.2f}; regional median {aov.median():.2f}"),
    ]

    # Week 1 (5/1-5/7) vs week 2 (5/8-5/14). Growth over all 25 regions, no row-count filter.
    w1, w2 = df[~week2].groupby("ship-city")["Amount"].sum(), df[week2].groupby("ship-city")["Amount"].sum()
    growth = ((w2 / w1 - 1) * 100).sort_values(ascending=False)
    growth_scope = {"period": "week 1 (5/1-5/7) to week 2 (5/8-5/14)", "filter": "all 25 regions, no minimum row count"}
    f.append(ranking("region_growth_ranking", growth.index, "Regions ranked by revenue growth from week 1 to week 2, highest first", scope=growth_scope))
    for rank, (name, v) in enumerate(growth.head(3).items(), 1):
        f.append(value(f"region_growth_rank{rank}_pct", v, f"Week-1 to week-2 revenue growth of region {name}", "percent", rel_tol=0.01, scope=growth_scope, note=f"rank {rank}"))
    f += [
        value("week1_revenue", amount[~week2].sum(), "Revenue in week 1", "KRW", scope={"period": "week 1 only, 2025-05-01 to 2025-05-07", "filter": "none"}),
        value("week2_revenue", amount[week2].sum(), "Revenue in week 2", "KRW", scope=wk2),
        value("week_over_week_growth_pct", (amount[week2].sum() / amount[~week2].sum() - 1) * 100,
              "Total revenue growth from week 1 to week 2", "percent", rel_tol=0.01, scope={"period": "week 1 to week 2", "filter": "none"}),
    ]
    w2_rank = w2.sort_values(ascending=False)
    week2_aov = df[week2].groupby("ship-city")["Amount"].mean()
    f += [
        ranking("region_week2_revenue_ranking", w2_rank.index, "Regions ranked by week-2 revenue, highest first", scope=wk2),
        value("region_week2_rank1_revenue", w2_rank.iloc[0], f"Week-2 revenue of region {w2_rank.index[0]}", "KRW", scope=wk2, note="the top region in week 2"),
        value("seocho_week2_aov", week2_aov[seocho], f"Week-2 AOV of region {seocho}", "KRW", scope=wk2, note=f"above the week-2 overall AOV {amount[week2].mean():.2f}"),
    ]

    # Cross segments: region x age group x gender x promotion applied, observed combinations.
    seg = df.groupby(["ship-city", "Age Group", "Gender", promo.rename("promo")])["Amount"].agg(["sum", "size"]).sort_values("sum", ascending=False)
    seg_def = "combinations of region x age group x gender x promotion applied (yes/no)"
    min3 = {"period": "full period", "filter": "combinations with at least 3 rows"}
    f += [
        value("cross_segment_count", len(seg), f"Number of observed {seg_def} (500 possible)", "count", rel_tol=0, scope=full),
        value("cross_segment_min3_count", int((seg["size"] >= 3).sum()), f"Number of {seg_def} with at least 3 rows", "count", rel_tol=0, scope=min3),
    ]
    for rank, ((c, age, g, _), row) in enumerate(seg[seg["size"] >= 3].head(3).iterrows(), 1):
        f.append(value(f"cross_segment_rank{rank}_revenue", row["sum"],
                       f"Revenue of the cross segment {c} / {age} / {'female' if g == 'F' else 'male'} / promotion applied "
                       f"({row['size']} rows)", "KRW", scope=min3, note=f"rank {rank} by revenue among combinations with at least 3 rows"))
    return {"dataset": f"moon_market_{lang}", "csv": str(csv.relative_to(REPO)), "rows": len(df), "facts": f}


def yummy_food():
    csv = DATA / "yummy_food" / "yummy-food-market.csv"
    df = pd.read_csv(csv, encoding="utf-8-sig")
    rev, cost = df["매출액"].sum(), df["광고비용"].sum()
    f = [
        value("total_revenue", rev, "Total revenue: SUM(매출액)", "KRW"),
        value("total_ad_spend", cost, "Total ad spend: SUM(광고비용)", "KRW"),
        value("overall_roas", rev / cost, "Overall ROAS: SUM(매출액) / SUM(광고비용); may be printed as a ratio or a percent", "ratio", rel_tol=0.01),
        value("overall_ctr_pct", df["클릭수"].sum() / df["노출수"].sum() * 100, "Overall CTR: SUM(클릭수) / SUM(노출수)", "percent", rel_tol=0.01),
        value("overall_cvr_pct", df["전환수"].sum() / df["클릭수"].sum() * 100, "Overall conversion rate: SUM(전환수) / SUM(클릭수)", "percent", rel_tol=0.01),
        value("total_conversions", df["전환수"].sum(), "Total conversions: SUM(전환수)", "count"),
        value("row_count", len(df), "Number of rows (records) in the file", "count", rel_tol=0),
    ]
    by_media = df.groupby("매체")["매출액"].sum().sort_values(ascending=False)
    for rank, (name, v) in enumerate(by_media.head(3).items(), 1):
        f.append(value(f"media_rank{rank}_revenue", v, f"Revenue of media channel {name}", "KRW", note=f"rank {rank} by revenue"))
    by_cat = df.groupby("카테고리")["매출액"].sum().sort_values(ascending=False)
    f.append(value("category_rank1_revenue", by_cat.iloc[0], f"Revenue of category {by_cat.index[0]}", "KRW", note="the top category by revenue"))
    return {"dataset": "yummy_food", "csv": str(csv.relative_to(REPO)), "rows": len(df), "facts": f}


if __name__ == "__main__":
    for key in (moon_market("kr"), moon_market("en"), yummy_food()):
        path = OUT / f"{key['dataset']}.json"
        path.write_text(json.dumps(key, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{path.name}: {len(key['facts'])} facts")
