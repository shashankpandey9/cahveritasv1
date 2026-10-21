"""CAH VERITAS — the STTM catalog (sttm_mapper's ground truth).

Every source-to-target mapping the corpus states explicitly, parsed
deterministically from ingested table rows, each with a stable id and a
citation:

    mapping   a row of a table whose header has both a source and a target
              column (e.g. the design document's STTM_Summary:
              Source Field Name | Source Table Name | Target Field Name | ...)
    kpi       a row of a KPI definition table (KPI Name | Definition |
              Data Source | Business Rules), the BRD's way of specifying a
              derived measure and where it comes from

Stage 2 offers each requirement its best-matching catalog rows and the model
selects from them by id, so an STTM row in a design trace is always a row the
design actually states — never a paraphrase invented to fill the field.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

from . import db

_SPLIT = re.compile(r"\s+\|\s+|\s+·\s+")
_TOKEN = re.compile(r"[a-z][a-z0-9_]{2,}")
_STOP = set("the and for with from this that are will into all any not per each "
            "data table field column name type value values source target flag "
            "should must include including based new existing".split())

# header label -> catalog field, first match wins
_ROLES: List[Tuple[str, re.Pattern]] = [
    ("source_table", re.compile(r"source.*(table|entity|object|file)", re.I)),
    ("source_column", re.compile(r"source.*(field|column|attribute)", re.I)),
    ("target_table", re.compile(r"target.*(table|entity|object)", re.I)),
    ("target_column", re.compile(r"target.*(field|column|attribute)", re.I)),
    ("target_type", re.compile(r"target.*data\s*type", re.I)),
    ("source_type", re.compile(r"^data\s*type|source.*data\s*type", re.I)),
    ("transformation", re.compile(r"type of target|transform|derivation|logic|rule", re.I)),
    ("description", re.compile(r"description|definition|comment", re.I)),
    ("kpi", re.compile(r"kpi", re.I)),
    ("data_source", re.compile(r"data\s*source", re.I)),
    ("use_case", re.compile(r"use\s*case", re.I)),
]

_CACHE: Dict[str, Any] = {"version": None, "rows": []}


def _pairs(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in _SPLIT.split(text or ""):
        if ": " in part:
            k, v = part.split(": ", 1)
            out.setdefault(k.strip(), v.strip())
    return out


def _roles(pairs: Dict[str, str]) -> Dict[str, str]:
    got: Dict[str, str] = {}
    for label, value in pairs.items():
        for role, rx in _ROLES:
            if role not in got and rx.search(label):
                got[role] = value.strip().rstrip(",").strip()
                break
    return got


def _target_from_section(section_path: str) -> str:
    m = re.search(r"target table:\s*([\w\.]+)", section_path or "", re.I)
    return m.group(1) if m else ""


def build() -> List[Dict[str, Any]]:
    """All mapping and KPI rows in the corpus (cached until the corpus changes)."""
    with db.connect() as con:
        version = con.execute("SELECT count(*), max(ord) FROM corpus_blocks "
                              "WHERE kind='table_row'").fetchone()
        if _CACHE["version"] == version:
            return _CACHE["rows"]
        blocks = con.execute(
            "SELECT doc_name, doc_type, section_path, ord, text FROM corpus_blocks "
            "WHERE kind='table_row' AND doc_type IN ('brd','design','sources','uat_baseline') "
            "ORDER BY doc_type, doc_name, ord").fetchall()
    rows: List[Dict[str, Any]] = []
    for doc_name, doc_type, section, ord_, text in blocks:
        r = _roles(_pairs(text))
        cite = f"[{doc_name} :: {section}]"
        if (r.get("source_column") or r.get("source_table")) and \
                (r.get("target_column") or r.get("target_table")):
            rows.append({
                "kind": "mapping",
                "source": ".".join(x for x in (r.get("source_table"), r.get("source_column")) if x),
                "target": ".".join(x for x in (r.get("target_table") or _target_from_section(section),
                                               r.get("target_column")) if x),
                "transformation": r.get("transformation") or "",
                "data_type": r.get("target_type") or r.get("source_type") or "",
                "description": r.get("description") or "",
                "citation": cite, "doc_type": doc_type})
        elif r.get("kpi") and (r.get("data_source") or r.get("description")):
            rows.append({
                "kind": "kpi",
                "source": r.get("data_source") or "",
                "target": "KPI: " + r["kpi"],
                "transformation": r.get("transformation") or "",
                "data_type": "",
                "description": r.get("description") or r.get("use_case") or "",
                "citation": cite, "doc_type": doc_type})
    for i, row in enumerate(rows, 1):
        row["id"] = f"STTM-{i:04d}"
        row["_tokens"] = _tokens(" ".join(str(row[k]) for k in
                                          ("source", "target", "transformation", "description")))
    _CACHE.update(version=version, rows=rows)
    return rows


def _tokens(text: str) -> set:
    return {t for t in _TOKEN.findall((text or "").lower()) if t not in _STOP}


def candidates(text: str, limit: int = 30, min_overlap: int = 2) -> List[Dict[str, Any]]:
    """Catalog rows sharing the most distinctive terms with `text`."""
    rows = build()
    if not rows:
        return []
    q = _tokens(text)
    # weight rare terms: a term that appears in half the catalog says little
    df: Dict[str, int] = {}
    for r in rows:
        for t in r["_tokens"] & q:
            df[t] = df.get(t, 0) + 1
    scored = []
    for r in rows:
        common = r["_tokens"] & q
        if len(common) < min_overlap:
            continue
        score = sum(1.0 / df[t] for t in common)
        scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    return [public(r) for _, r in scored[:limit]]


def public(row: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in row.items() if not k.startswith("_")}


def by_id() -> Dict[str, Dict[str, Any]]:
    return {r["id"]: public(r) for r in build()}


def prompt_lines(rows: List[Dict[str, Any]]) -> str:
    return "\n".join(
        f"{r['id']} [{r['kind']}] {r['source']} -> {r['target']}"
        + (f" | {r['transformation']}" if r["transformation"] else "")
        + (f" | {r['description'][:120]}" if r["description"] else "")
        + f" {r['citation']}" for r in rows) or "(no catalog rows matched)"


def stats() -> Dict[str, int]:
    rows = build()
    return {"mapping": sum(1 for r in rows if r["kind"] == "mapping"),
            "kpi": sum(1 for r in rows if r["kind"] == "kpi")}


def lookup(ids: List[str]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """(rows found, ids not in the catalog)."""
    idx = by_id()
    found, missing = [], []
    for i in ids or []:
        (found.append(idx[i]) if i in idx else missing.append(str(i)))
    return found, missing
