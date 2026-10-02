"""Build answer keys (ground-truth facts) for the eval datasets with pandas.

Each fact is a number a reasonable report on the dataset is likely to state.
`core` facts are expected in any report (total revenue, order count, ...);
the rest are scored as a bonus. `keywords` must appear in the same paragraph
for a match, so that a coincidentally equal number elsewhere does not count.

Usage:
    python answer_keys/build_answer_key.py          # writes answer_keys/*.json
Review the generated values once by hand before trusting them.
"""
import json
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "managed-agentcore" / "data"
OUT = Path(__file__).resolve().parent


def fact(id_, value, keywords, core=False, rel_tol=0.005, note=""):
    return {"id": id_, "value": round(float(value), 6), "keywords": keywords, "core": core, "rel_tol": rel_tol, "note": note}


def moon_market(lang):
    csv = DATA / "moon_market" / lang / "moon-market-fresh-food-sales.csv"
    df = pd.read_csv(csv, encoding="utf-8-sig")
    kr = lang == "kr"
    promo = df["promotion-ids"].notna() & (df["promotion-ids"].astype(str).str.strip() != "")
    facts = [
        fact("total_revenue", df["Amount"].sum(), ["매출"] if kr else ["revenue", "sales"], core=True, note="SUM(Amount)"),
        fact("order_count", len(df), ["건", "주문", "거래"] if kr else ["order", "transaction"], core=True, rel_tol=0, note="COUNT(*)"),
        fact("avg_order_value", df["Amount"].mean(), ["객단가", "평균", "주문 금액", "주문금액"] if kr else ["aov", "average", "order value"], core=True, note="MEAN(Amount)"),
        fact("category_count", df["Category"].nunique(), ["카테고리"] if kr else ["categor"], rel_tol=0),
        fact("product_count", df["Product"].nunique(), ["상품", "제품"] if kr else ["product"], rel_tol=0),
        fact("promo_order_share_pct", promo.mean() * 100, ["프로모션"] if kr else ["promo"], rel_tol=0.01),
        fact("promo_aov_lift_pct", (df.loc[promo, "Amount"].mean() / df.loc[~promo, "Amount"].mean() - 1) * 100, ["프로모션"] if kr else ["promo"], rel_tol=0.01),
    ]
    by_cat = df.groupby("Category")["Amount"].sum().sort_values(ascending=False)
    for rank, (name, value) in enumerate(by_cat.head(3).items(), 1):
        facts.append(fact(f"category_rank{rank}_revenue", value, [name.lower()], note=name))
        facts.append(fact(f"category_rank{rank}_share_pct", value / by_cat.sum() * 100, [name.lower()], rel_tol=0.01, note=name))
    for g, value in df.groupby("Gender")["Amount"].sum().items():
        facts.append(fact(f"gender_{g}_revenue_share_pct", value / df["Amount"].sum() * 100, ["여성", "여"] if (kr and g == "F") else ["남성", "남"] if kr else ["female", "women"] if g == "F" else ["male", "men"], rel_tol=0.01))
    by_age = df.groupby("Age Group")["Amount"].sum().sort_values(ascending=False)
    facts.append(fact("top_age_group_revenue", by_age.iloc[0], [str(by_age.index[0]).lower()], note=str(by_age.index[0])))
    return {"dataset": f"moon_market_{lang}", "csv": str(csv.relative_to(REPO)), "rows": len(df), "facts": facts}


def yummy_food():
    csv = DATA / "yummy_food" / "yummy-food-market.csv"
    df = pd.read_csv(csv, encoding="utf-8-sig")
    rev, cost = df["매출액"].sum(), df["광고비용"].sum()
    facts = [
        fact("total_revenue", rev, ["매출"], core=True, note="SUM(매출액)"),
        fact("total_ad_spend", cost, ["광고비", "광고 비용", "비용"], core=True, note="SUM(광고비용)"),
        fact("overall_roas", rev / cost, ["roas"], core=True, rel_tol=0.01, note="SUM(매출액)/SUM(광고비용); may be printed as ratio or %"),
        fact("overall_ctr_pct", df["클릭수"].sum() / df["노출수"].sum() * 100, ["ctr", "클릭률"], rel_tol=0.01),
        fact("overall_cvr_pct", df["전환수"].sum() / df["클릭수"].sum() * 100, ["전환율", "cvr"], rel_tol=0.01),
        fact("total_conversions", df["전환수"].sum(), ["전환"]),
        fact("row_count", len(df), ["건", "행", "레코드"], rel_tol=0),
    ]
    by_media = df.groupby("매체")["매출액"].sum().sort_values(ascending=False)
    for rank, (name, value) in enumerate(by_media.head(3).items(), 1):
        facts.append(fact(f"media_rank{rank}_revenue", value, [name.lower()], note=name))
    by_cat = df.groupby("카테고리")["매출액"].sum().sort_values(ascending=False)
    facts.append(fact("category_rank1_revenue", by_cat.iloc[0], [by_cat.index[0].lower()], note=by_cat.index[0]))
    return {"dataset": "yummy_food", "csv": str(csv.relative_to(REPO)), "rows": len(df), "facts": facts}


if __name__ == "__main__":
    for key in (moon_market("kr"), moon_market("en"), yummy_food()):
        path = OUT / f"{key['dataset']}.json"
        path.write_text(json.dumps(key, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{path.name}: {len(key['facts'])} facts")
