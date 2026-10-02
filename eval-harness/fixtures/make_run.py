"""Build synthetic run folders for grader tests.

`make_clean_run` writes a run whose artifacts are all mutually consistent and
correct for the moon_market_kr dataset. Each `corrupt_*` helper then breaks
exactly one thing, so a test can assert that the matching grader notices.
Built at test time so no binary .docx fixtures live in git.
"""
import json
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[2]
CSV = REPO / "managed-agentcore" / "data" / "moon_market" / "kr" / "moon-market-fresh-food-sales.csv"


def _calcs():
    df = pd.read_csv(CSV, encoding="utf-8-sig")
    src = "./data/moon-market-fresh-food-sales.csv"
    top = df.groupby("Category")["Amount"].sum().sort_values(ascending=False)
    return [
        {"id": "calc_001", "value": float(df["Amount"].sum()), "description": "총 매출", "formula": "SUM(Amount)", "source_file": src},
        {"id": "calc_002", "value": float(len(df)), "description": "주문 건수", "formula": "COUNT(*)", "source_file": src},
        {"id": "calc_003", "value": float(df["Amount"].mean()), "description": "평균 주문 금액", "formula": "AVG(Amount)", "source_file": src},
        {"id": "calc_004", "value": float(top.iloc[0]), "description": f"{top.index[0]} 매출", "formula": "SUM(Amount) GROUP BY Category", "source_file": src},
        {"id": "calc_005", "value": float(df["Product"].nunique()), "description": "상품 수", "formula": "COUNT(DISTINCT Product)", "source_file": src},
    ], top.index[0]


def _body(calcs, top_name):
    v = {c["id"]: c["value"] for c in calcs}
    return [
        "Executive Summary",
        f"분석 기간 동안 총 {v['calc_002']:,.0f}건 [2]의 주문이 발생했고 총 매출은 {v['calc_001']:,.0f}원 [1]입니다.",
        f"평균 주문 금액(객단가)은 {v['calc_003']:,.0f}원 [3]이며, 2025년 5월 1일부터 14일까지의 데이터입니다.",
        f"{top_name} 카테고리 매출이 {v['calc_004']:,.0f}원 [4]으로 1위입니다.",
        f"판매된 상품 수는 {v['calc_005']:,.0f}개 [5]입니다.",
    ]


def write_docx(path, body, refs):
    import docx
    doc = docx.Document()
    for line in body:
        doc.add_paragraph(line)
    doc.add_paragraph("참고문헌 및 데이터 출처")
    for line in refs:
        doc.add_paragraph(line)
    doc.save(str(path))


def make_clean_run(run_dir):
    run_dir = Path(run_dir)
    a = run_dir / "artifacts"
    a.mkdir(parents=True, exist_ok=True)
    calcs, top_name = _calcs()
    cites = [
        {"citation_id": f"[{i}]", "calculation_id": c["id"], "value": c["value"], "description": c["description"], "verification_status": "verified"}
        for i, c in enumerate(calcs, 1)
    ]
    (a / "calculation_metadata.json").write_text(json.dumps({"calculations": calcs}, ensure_ascii=False), encoding="utf-8")
    (a / "citations.json").write_text(json.dumps({"metadata": {}, "citations": cites}, ensure_ascii=False), encoding="utf-8")
    (a / "validation_report.txt").write_text("Total: 5, Verified: 5, Needs review: 0\n", encoding="utf-8")
    (a / "audit_findings.json").write_text(json.dumps({"verdict": "pass", "stats": {"type_a": 0, "type_b": 0, "type_c": 0, "type_d": 0}, "findings": [], "audit_metadata": {"retry_count": 0}}), encoding="utf-8")
    refs = [f"[{i}] {c['description']}: {c['value']} | Formula: {c['formula']}" for i, c in enumerate(calcs, 1)]
    write_docx(a / "final_report_with_citations.docx", _body(calcs, top_name), refs)
    debug = run_dir / "debug"
    debug.mkdir(exist_ok=True)
    (debug / "execution_1.json").write_text(json.dumps({"execution_num": 1, "status": "failed", "stderr": "cat: ./data/x.csv: No such file or directory\n", "execution_time_ms": 3}))
    (debug / "execution_2.json").write_text(json.dumps({"execution_num": 2, "status": "completed", "stdout": "ok", "execution_time_ms": 1200}))
    return run_dir


def _edit_json(path, fn):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    fn(data)
    Path(path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def corrupt_citation_value(run_dir):
    """citations.json [1] no longer equals the computed value."""
    _edit_json(Path(run_dir) / "artifacts" / "citations.json", lambda d: d["citations"][0].update(value=12345678.0))


def corrupt_metadata_value(run_dir):
    """The Coder's total revenue is wrong (and the citation faithfully copies it)."""
    def bump(d):
        d["calculations"][0]["value"] += 1000
    _edit_json(Path(run_dir) / "artifacts" / "calculation_metadata.json", bump)
    _edit_json(Path(run_dir) / "artifacts" / "citations.json", lambda d: d["citations"][0].update(value=d["citations"][0]["value"] + 1000))


def corrupt_report_number(run_dir):
    """The report prints a number next to [4] that is not the cited value."""
    calcs, top_name = _calcs()
    body = _body(calcs, top_name)
    body[4] = f"{top_name} 카테고리 매출이 2,686,000원 [4]으로 1위입니다."
    write_docx(Path(run_dir) / "artifacts" / "final_report_with_citations.docx", body, [])


def corrupt_broken_ref(run_dir):
    """The report cites [9], which does not exist."""
    calcs, top_name = _calcs()
    body = _body(calcs, top_name) + ["추가로 재구매율은 87.8% [9]입니다."]
    write_docx(Path(run_dir) / "artifacts" / "final_report_with_citations.docx", body, [])


def corrupt_missing_file(run_dir):
    (Path(run_dir) / "artifacts" / "validation_report.txt").unlink()
