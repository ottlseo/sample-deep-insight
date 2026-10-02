"""Report grader: reads final_report_with_citations.docx and checks what the reader sees.

Checks, in order of how much they say about correctness:
  * every [n] in the body resolves to a citation, and the number printed right
    before it equals that citation's value
  * share of significant numbers in the body that carry a citation
  * core facts from the dataset's answer key appear with the correct value
"""
import re
from pathlib import Path

from .citations import load_citations
from .numbers import appears_in, matches, parse_numbers

MARKER_RE = re.compile(r"\[(\d+)\]")
# Heading that starts the reference list; everything after it is not "body".
REFERENCES_RE = re.compile(r"^\s*(\d+[.)]?\s*)?(참고\s*문헌|참고자료|데이터\s*출처|인용|references?\b|citations?\b|data\s+citations)", re.I)
# How far before a marker to look for the number it supports.
CLAIM_WINDOW = 40


def read_docx(path):
    """Paragraph texts in document order, including table cells (one row per entry)."""
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = docx.Document(str(path))
    out = []
    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            text = Paragraph(child, doc).text
            if text.strip():
                out.append(text)
        elif tag == "tbl":
            for row in Table(child, doc).rows:
                cells = []
                for cell in row.cells:
                    if not cells or cell.text != cells[-1]:  # merged cells repeat their text
                        cells.append(cell.text)
                text = " | ".join(c.strip() for c in cells)
                if text.strip(" |"):
                    out.append(text)
    return out


def split_body(paragraphs):
    for i, p in enumerate(paragraphs):
        if i > 0 and len(p) < 80 and REFERENCES_RE.match(p):
            return paragraphs[:i], paragraphs[i:]
    return paragraphs, []


def _is_significant(text, start, end, decimals):
    """Numbers a reader would take as a data claim, not dates, years or headings."""
    raw = text[start:end]
    after = text[end:end + 2]
    before = text[max(0, start - 1):start]
    if re.match(r"\s*(월|일|년|시|분|주차|단계|위|장|절|개월|days?\b|weeks?\b)", after, re.I):
        return False
    if before in ("/", "-") or after[:1] in ("/",):
        return False  # 5/1/25, 2025-05-01
    if re.fullmatch(r"(19|20)\d\d", raw):
        return False  # bare year
    return "," in raw or decimals or after[:1] == "%" or before in ("₩", "$") or after[:1] in ("원", "%") or any(s in raw for s in "만억KMB")


def grade(artifacts_dir, answer_key=None):
    path = Path(artifacts_dir) / "final_report_with_citations.docx"
    if not path.is_file():
        return {"report_ok": False, "details": ["final_report_with_citations.docx missing"]}
    try:
        paragraphs = read_docx(path)
    except Exception as e:
        return {"report_ok": False, "details": [f"docx unreadable: {e}"]}

    body, refs = split_body(paragraphs)
    body_text = "\n".join(body)
    cites = load_citations(artifacts_dir) or {}
    details = []

    # --- markers and the value printed right before each one ---------------
    markers = [(int(m.group(1)), m.start()) for m in MARKER_RE.finditer(body_text)]
    used = {n for n, _ in markers}
    broken = sorted(n for n in used if cites and n not in cites)
    unused = sorted(n for n in cites if n not in used)
    if broken:
        details.append(f"body cites numbers missing from citations.json: {broken}")

    checked = ok = 0
    for n, pos in markers:
        c = cites.get(n)
        if c is None:
            continue
        window_start = max(0, pos - CLAIM_WINDOW)
        window = MARKER_RE.sub(lambda m: " " * len(m.group(0)), body_text[window_start:pos])
        window = re.split(r"[.!?。\n](?=\s|$)", window)[-1]  # stay inside the sentence
        nums = parse_numbers(window)
        if not nums:
            continue
        checked += 1
        if any(matches(v, d, c.get("value")) for v, _, _, d in nums):
            ok += 1
        else:
            details.append(f"[{n}] printed {[v for v, *_ in nums]} but citation value is {c.get('value')!r}")

    # --- citation coverage over significant numbers --------------------------
    plain = MARKER_RE.sub(lambda m: "\x00" * len(m.group(0)), body_text)
    significant = cited = 0
    for v, s, e, d in parse_numbers(plain):
        if not _is_significant(plain, s, e, d):
            continue
        significant += 1
        tail = body_text[e:e + CLAIM_WINDOW]
        tail = re.split(r"[.!?。\n](?=\s|$)", tail)[0]
        if MARKER_RE.search(tail):
            cited += 1

    metrics = {
        "report_ok": True,
        "report_chars": len(body_text),
        "report_paragraphs": len(body),
        "has_reference_section": bool(refs),
        "body_citations_used": len(used),
        "broken_citation_refs": len(broken),
        "unused_citations": len(unused),
        "cited_value_checked": checked,
        "cited_value_match_rate": ok / checked if checked else None,
        "significant_numbers": significant,
        "citation_coverage": cited / significant if significant else None,
    }

    # --- answer key ----------------------------------------------------------
    if answer_key:
        metrics.update(_grade_facts(body, answer_key, details))
    metrics["details"] = details
    return metrics


def _grade_facts(body, answer_key, details):
    core_total = core_found = other_found = 0
    for fact in answer_key.get("facts", []):
        keywords = [k.lower() for k in fact.get("keywords", [])]
        hit = False
        for p in body:
            if keywords and not any(k in p.lower() for k in keywords):
                continue
            if appears_in(MARKER_RE.sub(" ", p), fact["value"], rel_tol=fact.get("rel_tol", 0.005)):
                hit = True
                break
        if fact.get("core"):
            core_total += 1
            core_found += hit
            if not hit:
                details.append(f"core fact not found with correct value: {fact['id']} = {fact['value']}")
        else:
            other_found += hit
    return {
        "core_fact_recall": core_found / core_total if core_total else None,
        "core_facts_found": core_found,
        "core_facts_total": core_total,
        "other_facts_found": other_found,
    }
