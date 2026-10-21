"""CAH VERITAS — markdown knowledge base of affirmed artifacts.

Written ONLY when an artifact is accepted at its gate and/or receives positive
feedback. The KB is what future therapeutic-product launches are seeded from —
affirmed truth instead of static templates.

Layout:
    kb/requirements/<session-short>_requirements.md
    kb/design/<session-short>_design_trace.md
    kb/uat/<session-short>_test_cases.md
    kb/executions/<session-short>_execution_report.md
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List

from . import config, db


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _write(kind: str, sid: str, ref: str, title: str, body: str) -> str:
    folder = config.KB_DIR / kind
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sid[:8]}_{ref}.md"
    header = (f"# {title}\n\n"
              f"*Affirmed via CAH VERITAS on {_stamp()} — session `{sid[:8]}`.*\n"
              f"*Status: ACCEPTED by user gate with positive feedback.*\n\n---\n\n")
    path.write_text(header + body, encoding="utf-8")
    db.record_kb(sid, kind, ref, title, str(path))
    return str(path)


def _product_line(meta: Dict[str, Any]) -> str:
    if not meta or not meta.get("primary"):
        return "portfolio-wide / not product specific"
    p = meta["primary"]
    bits = [f"**Branded name:** {p.get('branded_name') or '—'}",
            f"**Shorthand:** {p.get('abbrev') or '—'}",
            f"**Molecule:** {p.get('molecule') or '—'}",
            f"**Full product name:** {p.get('full_product_name') or '—'}"]
    if p.get("therapeutic_area"):
        bits.append(f"**Therapeutic area:** {p['therapeutic_area']}")
    markets = ", ".join(m["code"] for m in meta.get("markets", []))
    if markets:
        bits.append(f"**Market baskets:** {markets}")
    return "  \n  ".join(bits)


def write_requirements(sid: str) -> str:
    reqs = db.get_requirements(sid)
    tops = [r for r in reqs if r.get("level", 1) == 1]
    lines = [f"**Register:** {len(tops)} requirements · "
             f"{len(reqs) - len(tops)} sub-requirements · {len(reqs)} records.",
             f"**Open / TBD in source:** "
             f"{sum(1 for r in reqs if r.get('open_item'))}.", ""]
    for r in reqs:
        if r.get("level", 1) == 1:
            lines += ["", "---", "",
                      f"## {r['req_no']} — {r['title']}",
                      f"*{r.get('section_path') or ''}*", "",
                      f"- **Product:**  \n  {_product_line(r.get('product_meta'))}",
                      f"- **Phase:** {r.get('phase') or '—'}"
                      + ("  ·  **OPEN (TBD in source)**" if r.get("open_item") else ""),
                      f"- **Plain language:** {r['plain_language']}",
                      f"- **Business description:** {r['business_description']}",
                      f"- **Business rule:** {r['business_rule']}",
                      "- **Design features required:** "
                      + ", ".join(r.get("features") or ["—"]),
                      f"- **Source:** `{r['source_ref']}`",
                      f"- **Enrichment:** {r.get('enrichment', 'heuristic')}", ""]
        else:
            lines += [f"### {r['req_no']} — {r['title']}",
                      f"- **Kind:** {r.get('req_kind', 'requirement')}"
                      f"  ·  **Product:** {r.get('brand', 'ALL')}"
                      f"  ·  **Phase:** {r.get('phase') or '—'}"
                      + ("  ·  **OPEN**" if r.get("open_item") else ""),
                      "- **Extracted text (verbatim):**",
                      "  > " + (r.get("raw_text") or "").replace("\n", "\n  > "),
                      f"- **Plain language:** {r['plain_language']}",
                      f"- **Business rule:** {r['business_rule']}",
                      f"- **Source:** `{r['source_ref']}`", ""]
    title = "Requirements Register"
    doc = next((r.get("doc_name") for r in reqs if r.get("doc_name")), "")
    if doc:
        title += " — " + doc
    return _write("requirements", sid, "requirements", title, "\n".join(lines))


def write_design(sid: str) -> str:
    rows = db.get_design_trace(sid)
    lines = []
    for t in rows:
        lines += [f"## {t['req_no']} — trace ({t['trace_status']})",
                  f"- **Design features:** {', '.join(t['design_features'] or ['—'])}",
                  f"- **Source entities:** " + "; ".join(
                      f"{e.get('entity')}({', '.join(e.get('columns', []))})"
                      if isinstance(e, dict) else str(e)
                      for e in t["source_entities"] or []),
                  f"- **Joins:** {json.dumps(t['joins'])}",
                  f"- **Filters:** {json.dumps(t['filters'])}",
                  f"- **Inclusions:** {json.dumps(t['inclusions'])}",
                  f"- **Exclusions:** {json.dumps(t['exclusions'])}",
                  f"- **Transformations:** {json.dumps(t['transformations'])}",
                  f"- **STTM:** {json.dumps(t['sttm'])}",
                  f"- **Code files:** {', '.join(t['code_files'] or ['—'])}",
                  f"- **Target marts:** {', '.join(t['target_marts'] or ['—'])}",
                  f"- **Output columns:** {', '.join(t['output_columns'] or ['—'])}",
                  f"- **Notes:** {t['notes'] or '—'}", ""]
    return _write("design", sid, "design_trace",
                  "Design & Environment Trace — Revenue Actual vs Forecast",
                  "\n".join(lines))


def write_uat(sid: str) -> str:
    cases = db.get_test_cases(sid)
    lines = []
    for c in cases:
        lines += [f"## {c['tc_no']} — {c['name']}",
                  f"- **Validates:** {c['req_no']}  ·  **Category:** {c['category']}"
                  f"  ·  **Coverage vs team baseline:** {c['coverage']}"
                  + (f" ({c['coverage_ref']})" if c.get("coverage_ref") else ""),
                  f"- **Description:** {c['description']}",
                  f"- **Expectation:** {c['expectation']}",
                  f"- **Relevance self-check:** {c['relevance_check']}",
                  "", "```sql", (c["sql"] or "").strip(), "```", ""]
    return _write("uat", sid, "test_cases",
                  "UAT Test Cases — Revenue Actual vs Forecast", "\n".join(lines))


def write_execution(sid: str) -> str:
    execs = db.get_executions(sid)
    n_pass = sum(1 for e in execs if e["verdict"] == "PASS")
    n_fail = sum(1 for e in execs if e["verdict"] == "FAIL")
    n_err = sum(1 for e in execs if e["verdict"] == "ERROR")
    lines = [f"**Scoreboard:** {n_pass} PASS · {n_fail} FAIL · {n_err} ERROR "
             f"of {len(execs)} executed.", ""]
    for e in execs:
        lines += [f"## {e['tc_no']} — {e['verdict']}",
                  f"- **Rows:** {e['row_count']}  ·  **Elapsed:** {e['elapsed_ms']} ms",
                  f"- **Explanation:** {e['explanation']}",
                  f"- **Defect locus (if FAIL):** {e['defect_locus'] or '—'}", ""]
    return _write("executions", sid, "execution_report",
                  "UAT Execution Report — Revenue Actual vs Forecast",
                  "\n".join(lines))


WRITERS = {"S1": write_requirements, "S2": write_design,
           "S3": write_uat, "S4": write_execution}


def write_for_stage(sid: str, stage: str) -> str:
    fn = WRITERS.get(stage)
    return fn(sid) if fn else ""
