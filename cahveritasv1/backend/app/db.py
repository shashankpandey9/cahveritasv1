"""CAH VERITAS — persistence (DuckDB).

One file owns every table. Access is short-lived connections; DuckDB handles a
single-process app comfortably. On VERITAS_RUNTIME=databricks the *analytics*
store (mart tables the tests run against) moves to Unity Catalog via
sqlexec.py — but application state stays here, which keeps the Databricks App
stateless-friendly (mount a volume for the .duckdb file).
"""
from __future__ import annotations

import json
import re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

import duckdb

from . import config

_APP_TABLES = """
CREATE SEQUENCE IF NOT EXISTS seq_chunk START 1;
CREATE TABLE IF NOT EXISTS sessions (
    session_id VARCHAR PRIMARY KEY, created_at TIMESTAMP, title VARCHAR,
    stage VARCHAR DEFAULT 'S0',           -- S0 idle, S1..S4, S1_review.., done
    stage_payload JSON, refine_notes JSON
);
CREATE TABLE IF NOT EXISTS messages (
    message_id VARCHAR PRIMARY KEY, session_id VARCHAR, role VARCHAR,
    content VARCHAR, created_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS stage_runs (
    run_id VARCHAR PRIMARY KEY, session_id VARCHAR, stage VARCHAR,
    status VARCHAR, started_at TIMESTAMP, finished_at TIMESTAMP,
    elapsed_ms BIGINT, agent_timings JSON, refine_note VARCHAR
);
CREATE TABLE IF NOT EXISTS requirements (
    session_id VARCHAR,
    req_no VARCHAR,              -- R1, R1.i, R1.ii ...
    parent_req VARCHAR,          -- NULL for R#, 'R1' for R1.i
    level INTEGER,               -- 1 = requirement, 2 = sub-requirement
    seq INTEGER,                 -- document order (R-id sorts wrong as text)
    title VARCHAR,
    raw_text VARCHAR,            -- verbatim from the source document
    plain_language VARCHAR,
    business_description VARCHAR,
    business_rule VARCHAR,
    brand VARCHAR,               -- short product label (back-compat)
    product_meta JSON,           -- branded / molecule / abbrev / full name
    features JSON,
    data_objects JSON,
    req_kind VARCHAR,            -- requirement | note | context
    normative BOOLEAN,
    phase VARCHAR,
    open_item BOOLEAN,
    section_no VARCHAR,
    section_path VARCHAR,
    doc_name VARCHAR,
    block_start INTEGER,
    block_end INTEGER,
    enrichment VARCHAR,          -- rag | heuristic
    source_ref VARCHAR,
    status VARCHAR DEFAULT 'proposed'
);
CREATE TABLE IF NOT EXISTS design_trace (
    session_id VARCHAR, req_no VARCHAR, design_features JSON, source_entities JSON,
    joins JSON, filters JSON, inclusions JSON, exclusions JSON,
    transformations JSON, sttm JSON, code_files JSON, target_marts JSON,
    output_columns JSON, trace_status VARCHAR, notes VARCHAR
);
CREATE TABLE IF NOT EXISTS test_cases (
    session_id VARCHAR, tc_no VARCHAR, parent_tc VARCHAR, name VARCHAR,
    description VARCHAR, category VARCHAR, req_no VARCHAR, sql VARCHAR,
    expectation VARCHAR, relevance_check VARCHAR, coverage VARCHAR,
    coverage_ref VARCHAR, status VARCHAR DEFAULT 'proposed'
);
CREATE TABLE IF NOT EXISTS executions (
    session_id VARCHAR, tc_no VARCHAR, executed_at TIMESTAMP, verdict VARCHAR,
    row_count BIGINT, elapsed_ms BIGINT, sample_rows JSON, explanation VARCHAR,
    defect_locus VARCHAR
);
CREATE TABLE IF NOT EXISTS feedback (
    feedback_id VARCHAR PRIMARY KEY, session_id VARCHAR, stage VARCHAR,
    artifact_ref VARCHAR, sentiment VARCHAR,   -- positive | negative
    action VARCHAR,                            -- accept | refine | thumbs
    comment VARCHAR, created_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS kb_entries (
    kb_id VARCHAR PRIMARY KEY, session_id VARCHAR, kind VARCHAR, ref VARCHAR,
    title VARCHAR, path VARCHAR, created_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS corpus_docs (
    doc_id VARCHAR PRIMARY KEY, name VARCHAR, doc_type VARCHAR, path VARCHAR,
    sections INTEGER, chunks INTEGER, ingested_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS corpus_chunks (
    chunk_id BIGINT DEFAULT nextval('seq_chunk') PRIMARY KEY,
    doc_id VARCHAR, doc_name VARCHAR, doc_type VARCHAR, section VARCHAR,
    section_path VARCHAR, ord INTEGER, block_start INTEGER, block_end INTEGER,
    n_blocks INTEGER, text VARCHAR, embedding FLOAT[]
);
-- Result of the per-document "Analyze" check (source re-parsed and compared
-- against what is stored). Keyed by input-relative path; cleared on re-ingest.
CREATE TABLE IF NOT EXISTS doc_analysis (
    rel_path VARCHAR PRIMARY KEY, doc_type VARCHAR, analyzed_at TIMESTAMP,
    complete BOOLEAN, result JSON
);
-- Vision-model results per distinct image (content hash): triage verdict and
-- transcript. Read by document parsing, written only by the vision agents,
-- so re-parsing a document never calls a model.
CREATE TABLE IF NOT EXISTS figure_transcripts (
    sha VARCHAR PRIMARY KEY, verdict VARCHAR, category VARCHAR, title VARCHAR,
    result JSON, model VARCHAR, created_at TIMESTAMP
);
-- The structural block stream, kept so the requirements enumerator can walk
-- the document in order without re-parsing it, and so a requirement's block
-- span stays resolvable back to verbatim source after ingest.
CREATE TABLE IF NOT EXISTS corpus_blocks (
    doc_id VARCHAR, doc_name VARCHAR, doc_type VARCHAR, ord INTEGER,
    kind VARCHAR, text VARCHAR, section VARCHAR, section_path VARCHAR,
    heading_level INTEGER, list_level INTEGER
);
"""

# Bumped whenever a table's columns change. App state (sessions, registers,
# corpus) is derived from inputs and is rebuilt on the next run, so a schema
# change drops and recreates rather than migrating — but only for the tables
# that actually changed shape.
SCHEMA_VERSION = 2
_VERSIONED_TABLES = ["requirements", "corpus_chunks", "corpus_blocks"]


def connect() -> duckdb.DuckDBPyConnection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(config.DB_PATH))


@contextmanager
def held_open() -> Iterator[None]:
    """Keep the database instance alive across many short-lived connections.

    Closing the last connection to a DuckDB file checkpoints it (~0.6 s on
    the full corpus), so a loop of connect/close pays that per document.
    While this is held, :func:`connect` reuses the cached instance and
    closing is cheap; the one checkpoint happens when the block exits."""
    keeper = connect()
    try:
        yield
    finally:
        keeper.close()


def init() -> None:
    """Create tables, dropping the ones whose shape changed since last boot.

    Without this, an existing data/veritas.duckdb keeps the old 10-column
    `requirements` table (CREATE TABLE IF NOT EXISTS is a no-op against it)
    and every insert fails on arity — which looks exactly like "the agent
    found no requirements".
    """
    with connect() as con:
        con.execute("CREATE TABLE IF NOT EXISTS schema_meta "
                    "(key VARCHAR PRIMARY KEY, value VARCHAR)")
        row = con.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
        current = int(row[0]) if row and str(row[0]).isdigit() else 0
        if current < SCHEMA_VERSION:
            for t in _VERSIONED_TABLES:
                con.execute("DROP TABLE IF EXISTS " + t)
            con.execute("DELETE FROM schema_meta WHERE key='version'")
            con.execute("INSERT INTO schema_meta VALUES ('version', ?)",
                        [str(SCHEMA_VERSION)])
        con.execute(_APP_TABLES)
        # Additive columns on corpus_docs — safe on an existing file, no rebuild.
        for col, typ in (("rel_path", "VARCHAR"), ("pages", "INTEGER"),
                         ("pages_estimated", "BOOLEAN"),
                         ("expected_chunks", "INTEGER"),
                         ("figures_total", "INTEGER"), ("figures_captured", "INTEGER"),
                         ("figures_skipped", "INTEGER"), ("figures_pending", "INTEGER")):
            con.execute(f"ALTER TABLE corpus_docs ADD COLUMN IF NOT EXISTS {col} {typ}")
        # design specs linked to sub-requirements, and trace metadata
        con.execute("ALTER TABLE design_trace ADD COLUMN IF NOT EXISTS specs JSON")
        con.execute("ALTER TABLE design_trace ADD COLUMN IF NOT EXISTS meta JSON")
        # the agent events of each run, so the trace survives a page refresh
        con.execute("ALTER TABLE stage_runs ADD COLUMN IF NOT EXISTS events JSON")
        # which sub-requirement and design spec a test case verifies
        con.execute("ALTER TABLE test_cases ADD COLUMN IF NOT EXISTS sub_req VARCHAR")
        con.execute("ALTER TABLE test_cases ADD COLUMN IF NOT EXISTS spec_ref VARCHAR")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _j(v: Any) -> str:
    return json.dumps(v, default=str)


# ------------------------------------------------------------- sessions/chat
def create_session(title: str = "New launch review") -> str:
    sid = str(uuid.uuid4())
    with connect() as con:
        con.execute("INSERT INTO sessions (session_id, created_at, title) VALUES (?,?,?)",
                    [sid, _now(), title])
    return sid


def get_session(sid: str) -> Optional[Dict[str, Any]]:
    with connect() as con:
        row = con.execute(
            "SELECT session_id, created_at, title, stage, stage_payload, refine_notes "
            "FROM sessions WHERE session_id=?", [sid]).fetchone()
    if not row:
        return None
    return {"session_id": row[0], "created_at": str(row[1]), "title": row[2],
            "stage": row[3],
            "stage_payload": json.loads(row[4]) if row[4] else None,
            "refine_notes": json.loads(row[5]) if row[5] else []}


def set_stage(sid: str, stage: str, payload: Optional[Any] = None) -> None:
    with connect() as con:
        if payload is None:
            con.execute("UPDATE sessions SET stage=? WHERE session_id=?", [stage, sid])
        else:
            con.execute("UPDATE sessions SET stage=?, stage_payload=? WHERE session_id=?",
                        [stage, _j(payload), sid])


def add_refine_note(sid: str, stage: str, note: str) -> None:
    sess = get_session(sid) or {}
    notes = sess.get("refine_notes") or []
    notes.append({"stage": stage, "note": note, "at": _now().isoformat()})
    with connect() as con:
        con.execute("UPDATE sessions SET refine_notes=? WHERE session_id=?",
                    [_j(notes), sid])


def add_message(sid: str, role: str, content: str) -> str:
    mid = str(uuid.uuid4())
    with connect() as con:
        con.execute("INSERT INTO messages VALUES (?,?,?,?,?)",
                    [mid, sid, role, content, _now()])
    return mid


def get_messages(sid: str) -> List[Dict[str, Any]]:
    with connect() as con:
        rows = con.execute(
            "SELECT message_id, role, content, created_at FROM messages "
            "WHERE session_id=? ORDER BY created_at", [sid]).fetchall()
    return [{"id": r[0], "role": r[1], "content": r[2], "timestamp": str(r[3])}
            for r in rows]


# --------------------------------------------------------------- stage runs
def start_run(sid: str, stage: str, refine_note: str = "") -> str:
    rid = str(uuid.uuid4())
    with connect() as con:
        con.execute(
            "INSERT INTO stage_runs (run_id, session_id, stage, status, started_at, refine_note) "
            "VALUES (?,?,?,?,?,?)", [rid, sid, stage, "running", _now(), refine_note])
    return rid


def finish_run(rid: str, status: str, agent_timings: Dict[str, int]) -> None:
    with connect() as con:
        started = con.execute("SELECT started_at FROM stage_runs WHERE run_id=?",
                              [rid]).fetchone()
        elapsed = 0
        if started:
            elapsed = int((_now() - started[0].replace(tzinfo=timezone.utc))
                          .total_seconds() * 1000)
        con.execute("UPDATE stage_runs SET status=?, finished_at=?, elapsed_ms=?, "
                    "agent_timings=? WHERE run_id=?",
                    [status, _now(), elapsed, _j(agent_timings), rid])


# ------------------------------------------------------------- requirements
_REQ_COLS = ["req_no", "parent_req", "level", "seq", "title", "raw_text",
             "plain_language", "business_description", "business_rule", "brand",
             "product_meta", "features", "data_objects", "req_kind", "normative",
             "phase", "open_item", "section_no", "section_path", "doc_name",
             "block_start", "block_end", "enrichment", "source_ref", "status"]
_REQ_JSON = {"product_meta", "features", "data_objects"}


def replace_requirements(sid: str, reqs: List[Dict[str, Any]]) -> None:
    """Persist the full register. `seq` preserves document order — 'R10.ii'
    sorts before 'R2' as text, so ordering must not come from the id."""
    with connect() as con:
        con.execute("DELETE FROM requirements WHERE session_id=?", [sid])
        placeholders = ",".join("?" * (len(_REQ_COLS) + 1))
        for i, r in enumerate(reqs):
            con.execute(
                "INSERT INTO requirements VALUES (" + placeholders + ")",
                [sid,
                 r.get("req_no") or r.get("req_id"), r.get("parent_req"),
                 int(r.get("level", 1)), int(r.get("seq", i)), r.get("title"),
                 r.get("raw_text", ""), r.get("plain_language", ""),
                 r.get("business_description", ""), r.get("business_rule", ""),
                 r.get("brand", "ALL"), _j(r.get("product_meta", {})),
                 _j(r.get("features", [])), _j(r.get("data_objects", [])),
                 r.get("req_kind", "requirement"), bool(r.get("normative", True)),
                 r.get("phase", ""), bool(r.get("open_item", False)),
                 r.get("section_no", ""), r.get("section_path", ""),
                 r.get("doc_name", ""), int(r.get("block_start") or 0),
                 int(r.get("block_end") or 0), r.get("enrichment", "heuristic"),
                 r.get("source_ref", ""), r.get("status", "proposed")])


def get_requirements(sid: str) -> List[Dict[str, Any]]:
    with connect() as con:
        rows = con.execute(
            "SELECT " + ", ".join(_REQ_COLS) + " FROM requirements "
            "WHERE session_id=? ORDER BY seq", [sid]).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(zip(_REQ_COLS, r))
        for k in _REQ_JSON:
            d[k] = json.loads(d[k]) if d[k] else ({} if k == "product_meta" else [])
        d["req_id"] = d["req_no"]          # both names, so panels and stages agree
        out.append(d)
    return out


def mark_requirements(sid: str, status: str) -> None:
    with connect() as con:
        con.execute("UPDATE requirements SET status=? WHERE session_id=?", [status, sid])


# ------------------------------------------------------------- design trace
_TRACE_JSON = ["design_features", "source_entities", "joins", "filters", "inclusions",
               "exclusions", "transformations", "sttm", "code_files", "target_marts",
               "output_columns", "specs", "meta"]
_TRACE_COLS = ["req_no"] + _TRACE_JSON + ["trace_status", "notes"]


def _trace_values(sid: str, t: Dict[str, Any]) -> List[Any]:
    vals: List[Any] = [sid, t.get("req_no")]
    for c in _TRACE_JSON:
        vals.append(_j(t.get(c) if t.get(c) is not None else ({} if c == "meta" else [])))
    vals += [t.get("trace_status", "PARTIAL"), t.get("notes", "")]
    return vals


def replace_design_trace(sid: str, rows: List[Dict[str, Any]]) -> None:
    cols = "session_id, " + ", ".join(_TRACE_COLS)
    marks = ",".join("?" * (len(_TRACE_COLS) + 1))
    with connect() as con:
        con.execute("DELETE FROM design_trace WHERE session_id=?", [sid])
        for t in rows:
            con.execute(f"INSERT INTO design_trace ({cols}) VALUES ({marks})",
                        _trace_values(sid, t))


def upsert_design_trace(sid: str, row: Dict[str, Any]) -> None:
    """Replace one requirement's trace (per-requirement re-trace)."""
    cols = "session_id, " + ", ".join(_TRACE_COLS)
    marks = ",".join("?" * (len(_TRACE_COLS) + 1))
    with connect() as con:
        con.execute("DELETE FROM design_trace WHERE session_id=? AND req_no=?",
                    [sid, row.get("req_no")])
        con.execute(f"INSERT INTO design_trace ({cols}) VALUES ({marks})",
                    _trace_values(sid, row))


def _req_sort_key(req_no: str) -> List[Any]:
    """R2 before R10; R7.ii after R7."""
    return [int(p) if p.isdigit() else p for p in re.findall(r"\d+|[a-z]+", (req_no or "").lower())]


def get_design_trace(sid: str) -> List[Dict[str, Any]]:
    with connect() as con:
        rows = con.execute("SELECT " + ", ".join(_TRACE_COLS) + " FROM design_trace "
                           "WHERE session_id=?", [sid]).fetchall()
    out = []
    for r in rows:
        d: Dict[str, Any] = {}
        for c, v in zip(_TRACE_COLS, r):
            if c in _TRACE_JSON:
                d[c] = json.loads(v) if v else ({} if c == "meta" else [])
            else:
                d[c] = v
        out.append(d)
    out.sort(key=lambda d: _req_sort_key(d["req_no"]))
    return out


def save_run_events(rid: str, events: List[Dict[str, Any]]) -> None:
    with connect() as con:
        con.execute("UPDATE stage_runs SET events=? WHERE run_id=?", [_j(events), rid])


def get_runs(sid: str) -> List[Dict[str, Any]]:
    """Every stage run of a session, oldest first, with its agent events."""
    with connect() as con:
        rows = con.execute("SELECT run_id, stage, status, started_at, finished_at, events "
                           "FROM stage_runs WHERE session_id=? ORDER BY started_at",
                           [sid]).fetchall()
    return [{"run_id": r[0], "stage": r[1], "status": r[2], "started_at": str(r[3]),
             "finished_at": str(r[4]) if r[4] else None,
             "events": json.loads(r[5]) if r[5] else []} for r in rows]


def list_sessions(limit: int = 20) -> List[Dict[str, Any]]:
    """Recent sessions with how far each got — for restoring work after a refresh."""
    with connect() as con:
        rows = con.execute("""
            SELECT s.session_id, s.created_at, s.stage,
                   (SELECT count(*) FROM requirements r WHERE r.session_id = s.session_id),
                   (SELECT count(*) FROM design_trace d WHERE d.session_id = s.session_id),
                   (SELECT count(*) FROM test_cases t WHERE t.session_id = s.session_id),
                   (SELECT count(*) FROM executions e WHERE e.session_id = s.session_id),
                   (SELECT max(m.created_at) FROM messages m WHERE m.session_id = s.session_id)
            FROM sessions s ORDER BY s.created_at DESC LIMIT ?""", [limit]).fetchall()
    return [{"session_id": r[0], "created_at": str(r[1]), "stage": r[2],
             "requirements": r[3], "design_trace": r[4], "test_cases": r[5],
             "executions": r[6], "last_activity": str(r[7]) if r[7] else str(r[1])}
            for r in rows]


def replace_test_cases(sid: str, cases: List[Dict[str, Any]]) -> None:
    with connect() as con:
        con.execute("DELETE FROM test_cases WHERE session_id=?", [sid])
        for c in cases:
            con.execute(
                "INSERT INTO test_cases (session_id, tc_no, parent_tc, name, description, "
                "category, req_no, sql, expectation, relevance_check, coverage, coverage_ref, "
                "status, sub_req, spec_ref) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [sid, c.get("tc_no"), c.get("parent_tc"), c.get("name"),
                 c.get("description"), c.get("category"), c.get("req_no"),
                 c.get("sql"), c.get("expectation"), c.get("relevance_check"),
                 c.get("coverage", "NOVEL"), c.get("coverage_ref", ""),
                 c.get("status", "proposed"), c.get("sub_req"), c.get("spec_ref")])


def get_test_cases(sid: str) -> List[Dict[str, Any]]:
    with connect() as con:
        rows = con.execute(
            "SELECT tc_no, parent_tc, name, description, category, req_no, sql, "
            "expectation, relevance_check, coverage, coverage_ref, status, sub_req, spec_ref "
            "FROM test_cases WHERE session_id=? ORDER BY tc_no", [sid]).fetchall()
    keys = ["tc_no", "parent_tc", "name", "description", "category", "req_no",
            "sql", "expectation", "relevance_check", "coverage", "coverage_ref",
            "status", "sub_req", "spec_ref"]
    out = [dict(zip(keys, r)) for r in rows]
    out.sort(key=lambda c: _req_sort_key(c["tc_no"]))        # TC-51 before TC-101
    return out


def mark_test_cases(sid: str, status: str) -> None:
    with connect() as con:
        con.execute("UPDATE test_cases SET status=? WHERE session_id=?", [status, sid])


# --------------------------------------------------------------- executions
def record_execution(sid: str, tc_no: str, verdict: str, row_count: int,
                     elapsed_ms: int, sample_rows: List[Any], explanation: str,
                     defect_locus: str) -> None:
    with connect() as con:
        con.execute("INSERT INTO executions VALUES (?,?,?,?,?,?,?,?,?)",
                    [sid, tc_no, _now(), verdict, row_count, elapsed_ms,
                     _j(sample_rows), explanation, defect_locus])


def get_executions(sid: str) -> List[Dict[str, Any]]:
    with connect() as con:
        rows = con.execute(
            "SELECT tc_no, executed_at, verdict, row_count, elapsed_ms, sample_rows, "
            "explanation, defect_locus FROM executions WHERE session_id=? "
            "ORDER BY executed_at", [sid]).fetchall()
    return [{"tc_no": r[0], "executed_at": str(r[1]), "verdict": r[2],
             "row_count": r[3], "elapsed_ms": r[4],
             "sample_rows": json.loads(r[5]) if r[5] else [],
             "explanation": r[6], "defect_locus": r[7]} for r in rows]


# ----------------------------------------------------------------- feedback
def record_feedback(sid: str, stage: str, artifact_ref: str, sentiment: str,
                    action: str, comment: str) -> str:
    fid = str(uuid.uuid4())
    with connect() as con:
        con.execute("INSERT INTO feedback VALUES (?,?,?,?,?,?,?,?)",
                    [fid, sid, stage, artifact_ref, sentiment, action, comment, _now()])
    return fid


def get_feedback(sid: str) -> List[Dict[str, Any]]:
    with connect() as con:
        rows = con.execute(
            "SELECT stage, artifact_ref, sentiment, action, comment, created_at "
            "FROM feedback WHERE session_id=? ORDER BY created_at", [sid]).fetchall()
    return [{"stage": r[0], "artifact_ref": r[1], "sentiment": r[2], "action": r[3],
             "comment": r[4], "created_at": str(r[5])} for r in rows]


# ----------------------------------------------------------------------- KB
def record_kb(sid: str, kind: str, ref: str, title: str, path: str) -> str:
    kid = str(uuid.uuid4())
    with connect() as con:
        con.execute("INSERT INTO kb_entries VALUES (?,?,?,?,?,?,?)",
                    [kid, sid, kind, ref, title, path, _now()])
    return kid


def get_kb(limit: int = 200) -> List[Dict[str, Any]]:
    with connect() as con:
        rows = con.execute(
            "SELECT kind, ref, title, path, created_at, session_id FROM kb_entries "
            "ORDER BY created_at DESC LIMIT ?", [limit]).fetchall()
    return [{"kind": r[0], "ref": r[1], "title": r[2], "path": r[3],
             "created_at": str(r[4]), "session_id": r[5]} for r in rows]


# ------------------------------------------------------------------- corpus
def clear_corpus() -> None:
    with connect() as con:
        con.execute("DELETE FROM corpus_chunks")
        con.execute("DELETE FROM corpus_blocks")
        con.execute("DELETE FROM corpus_docs")
        con.execute("DELETE FROM doc_analysis")


def add_doc(name: str, doc_type: str, path: str, sections: int, chunks: int,
            rel_path: Optional[str] = None, pages: Optional[int] = None,
            pages_estimated: bool = False) -> str:
    did = str(uuid.uuid4())
    with connect() as con:
        con.execute("INSERT INTO corpus_docs (doc_id, name, doc_type, path, sections, "
                    "chunks, ingested_at, rel_path, pages, pages_estimated) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [did, name, doc_type, path, sections, chunks, _now(),
                     rel_path, pages, pages_estimated])
    return did


def find_doc(rel_path: str, doc_type: str) -> Optional[str]:
    with connect() as con:
        row = con.execute("SELECT doc_id FROM corpus_docs WHERE rel_path=? AND doc_type=?",
                          [rel_path, doc_type]).fetchone()
    return row[0] if row else None


def reset_doc(doc_id: str, path: str, sections: int, pages: Optional[int],
              pages_estimated: bool) -> None:
    """Re-ingest in place: keep the doc_id, drop its blocks/chunks/analysis."""
    with connect() as con:
        rel = con.execute("SELECT rel_path FROM corpus_docs WHERE doc_id=?",
                          [doc_id]).fetchone()
        con.execute("UPDATE corpus_docs SET path=?, sections=?, chunks=0, "
                    "expected_chunks=NULL, pages=?, pages_estimated=?, ingested_at=? "
                    "WHERE doc_id=?",
                    [path, sections, pages, pages_estimated, _now(), doc_id])
        con.execute("DELETE FROM corpus_blocks WHERE doc_id=?", [doc_id])
        con.execute("DELETE FROM corpus_chunks WHERE doc_id=?", [doc_id])
        if rel and rel[0]:
            con.execute("DELETE FROM doc_analysis WHERE rel_path=?", [rel[0]])


def set_doc_chunks(doc_id: str, chunks: int) -> None:
    """Chunk count is known only after chunking; the row is written before.
    Called after the chunks are stored, so a crash mid-ingest leaves chunks=0
    — which the coverage check reads as a partial document."""
    with connect() as con:
        con.execute("UPDATE corpus_docs SET chunks=? WHERE doc_id=?",
                    [chunks, doc_id])


def set_doc_figures(doc_id: str, st: Optional[Dict[str, int]]) -> None:
    """Figure counts for a figure-bearing document; None for other types."""
    st = st or {}
    with connect() as con:
        con.execute("UPDATE corpus_docs SET figures_total=?, figures_captured=?, "
                    "figures_skipped=?, figures_pending=? WHERE doc_id=?",
                    [st.get("total"), st.get("captured"), st.get("skipped"),
                     st.get("pending"), doc_id])


def get_figures(shas: List[str]) -> Dict[str, Dict[str, Any]]:
    if not shas:
        return {}
    with connect() as con:
        rows = con.execute("SELECT sha, verdict, category, title, result, model "
                           "FROM figure_transcripts WHERE sha IN (SELECT unnest(CAST(?::JSON AS VARCHAR[])))",
                           [json.dumps(sorted(set(shas)))]).fetchall()
    return {r[0]: {"verdict": r[1], "category": r[2], "title": r[3],
                   "result": json.loads(r[4]) if r[4] else None, "model": r[5]}
            for r in rows}


def put_figure(sha: str, verdict: str, category: str = "", title: str = "",
               result: Optional[Dict[str, Any]] = None, model: str = "") -> None:
    with connect() as con:
        con.execute("DELETE FROM figure_transcripts WHERE sha=?", [sha])
        con.execute("INSERT INTO figure_transcripts VALUES (?,?,?,?,?,?,?)",
                    [sha, verdict, category, title, _j(result) if result else None,
                     model, _now()])


def set_doc_expected_chunks(doc_id: str, n: int) -> None:
    with connect() as con:
        con.execute("UPDATE corpus_docs SET expected_chunks=? WHERE doc_id=?",
                    [n, doc_id])


# Bulk writes go in as ONE JSON parameter unpacked in SQL. DuckDB's Python
# binding of list-typed parameters costs ~100 ms per embedding and ~4 ms per
# row under executemany — hours for the code tree; the JSON path is ~1000x
# faster and round-trips FLOAT[] losslessly.
_CHUNK_STRUCT = ("STRUCT(doc_id VARCHAR, doc_name VARCHAR, doc_type VARCHAR, "
                 "section VARCHAR, section_path VARCHAR, ord INTEGER, "
                 "block_start INTEGER, block_end INTEGER, n_blocks INTEGER, "
                 "text VARCHAR, embedding FLOAT[])[]")
_BLOCK_STRUCT = ("STRUCT(doc_id VARCHAR, doc_name VARCHAR, doc_type VARCHAR, "
                 "ord INTEGER, kind VARCHAR, text VARCHAR, section VARCHAR, "
                 "section_path VARCHAR, heading_level INTEGER, list_level INTEGER)[]")


def _floats(v: Any) -> Optional[List[float]]:
    return None if v is None else [float(x) for x in v]


def add_chunks(rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    payload = json.dumps([{
        "doc_id": r["doc_id"], "doc_name": r["doc_name"], "doc_type": r["doc_type"],
        "section": r["section"], "section_path": r.get("section_path", r["section"]),
        "ord": r["ord"], "block_start": r.get("block_start", 0),
        "block_end": r.get("block_end", 0), "n_blocks": r.get("n_blocks", 1),
        "text": r["text"], "embedding": _floats(r["embedding"]),
    } for r in rows])
    with connect() as con:
        con.execute(
            "INSERT INTO corpus_chunks (doc_id, doc_name, doc_type, section, "
            "section_path, ord, block_start, block_end, n_blocks, text, embedding) "
            "SELECT r.doc_id, r.doc_name, r.doc_type, r.section, r.section_path, "
            "r.ord, r.block_start, r.block_end, r.n_blocks, r.text, r.embedding "
            f"FROM (SELECT unnest(CAST(?::JSON AS {_CHUNK_STRUCT})) AS r)",
            [payload])


def update_chunk_embedding(chunk_id: int, embedding: List[float]) -> None:
    with connect() as con:
        con.execute("UPDATE corpus_chunks SET embedding=CAST(?::JSON AS FLOAT[]) "
                    "WHERE chunk_id=?", [json.dumps(_floats(embedding)), chunk_id])


def add_blocks(doc_id: str, doc_name: str, doc_type: str,
               blocks: List[Dict[str, Any]]) -> None:
    if not blocks:
        return
    payload = json.dumps([{
        "doc_id": doc_id, "doc_name": doc_name, "doc_type": doc_type,
        "ord": b.get("ord", i), "kind": b.get("kind", "para"),
        "text": b.get("text", ""), "section": b.get("section", ""),
        "section_path": b.get("section_path", ""),
        "heading_level": int(b.get("heading_level") or 0),
        "list_level": None if b.get("list_level") is None else int(b["list_level"]),
    } for i, b in enumerate(blocks)])
    with connect() as con:
        con.execute(
            "INSERT INTO corpus_blocks (doc_id, doc_name, doc_type, ord, kind, text, "
            "section, section_path, heading_level, list_level) "
            "SELECT r.doc_id, r.doc_name, r.doc_type, r.ord, r.kind, r.text, "
            "r.section, r.section_path, r.heading_level, r.list_level "
            f"FROM (SELECT unnest(CAST(?::JSON AS {_BLOCK_STRUCT})) AS r)",
            [payload])


def get_blocks(doc_type: Optional[str] = None,
               doc_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """The structural block stream, in document order."""
    q = ("SELECT doc_id, doc_name, doc_type, ord, kind, text, section, "
         "section_path, heading_level, list_level FROM corpus_blocks")
    where, args = [], []
    if doc_type:
        where.append("doc_type=?")
        args.append(doc_type)
    if doc_name:
        where.append("doc_name=?")
        args.append(doc_name)
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY doc_name, ord"
    keys = ["doc_id", "doc_name", "doc_type", "ord", "kind", "text", "section",
            "section_path", "heading_level", "list_level"]
    with connect() as con:
        rows = con.execute(q, args).fetchall()
    return [dict(zip(keys, r)) for r in rows]


def corpus_inventory() -> List[Dict[str, Any]]:
    with connect() as con:
        rows = con.execute(
            "SELECT name, doc_type, path, sections, chunks, ingested_at "
            "FROM corpus_docs ORDER BY doc_type, name").fetchall()
    return [{"name": r[0], "doc_type": r[1], "path": r[2], "sections": r[3],
             "chunks": r[4], "ingested_at": str(r[5])} for r in rows]


def corpus_gap_audit() -> Dict[str, Any]:
    """Discovery summary for the Phase 0 payload — see :mod:`discovery`."""
    from . import discovery   # late import: discovery depends on this module
    return discovery.summary()


def corpus_coverage() -> List[Dict[str, Any]]:
    """Per ingested document: what is stored and how much of it the chunks
    cover. Cheap (no re-parse) — the Analyze action does the source check."""
    q = """
    WITH blk AS (
        SELECT doc_id,
               count(*) FILTER (WHERE trim(coalesce(text, '')) <> '') AS blocks,
               count(DISTINCT coalesce(nullif(section_path, ''), doc_name)) AS block_sections
        FROM corpus_blocks GROUP BY doc_id),
    chk AS (
        SELECT doc_id, count(*) AS chunk_rows,
               count(DISTINCT section_path) AS chunk_sections
        FROM corpus_chunks GROUP BY doc_id),
    unc AS (
        SELECT b.doc_id, count(*) AS uncovered_blocks,
               count(DISTINCT coalesce(nullif(b.section_path, ''), b.doc_name)) AS uncovered_sections
        FROM corpus_blocks b
        WHERE trim(coalesce(b.text, '')) <> ''
          AND NOT EXISTS (SELECT 1 FROM corpus_chunks c WHERE c.doc_id = b.doc_id
                          AND b.ord BETWEEN c.block_start AND c.block_end)
        GROUP BY b.doc_id)
    SELECT d.doc_id, d.name, d.doc_type, d.path, d.rel_path, d.sections, d.chunks,
           d.expected_chunks, d.pages, d.pages_estimated, d.ingested_at,
           d.figures_total, d.figures_captured, d.figures_skipped, d.figures_pending,
           coalesce(blk.blocks, 0), coalesce(blk.block_sections, 0),
           coalesce(chk.chunk_rows, 0), coalesce(chk.chunk_sections, 0),
           coalesce(unc.uncovered_blocks, 0), coalesce(unc.uncovered_sections, 0)
    FROM corpus_docs d
    LEFT JOIN blk ON blk.doc_id = d.doc_id
    LEFT JOIN chk ON chk.doc_id = d.doc_id
    LEFT JOIN unc ON unc.doc_id = d.doc_id
    """
    keys = ["doc_id", "name", "doc_type", "path", "rel_path", "sections", "chunks",
            "expected_chunks", "pages", "pages_estimated", "ingested_at",
            "figures_total", "figures_captured", "figures_skipped", "figures_pending",
            "blocks",
            "block_sections", "chunk_rows", "chunk_sections", "uncovered_blocks",
            "uncovered_sections"]
    with connect() as con:
        rows = con.execute(q).fetchall()
    return [dict(zip(keys, r)) for r in rows]


def doc_chunk_spans(doc_id: str) -> List[Dict[str, Any]]:
    with connect() as con:
        rows = con.execute("SELECT block_start, block_end, section_path FROM corpus_chunks "
                           "WHERE doc_id=? ORDER BY ord", [doc_id]).fetchall()
    return [{"block_start": r[0], "block_end": r[1], "section_path": r[2]} for r in rows]


def save_analysis(rel_path: str, doc_type: str, complete: bool,
                  result: Dict[str, Any]) -> None:
    with connect() as con:
        con.execute("DELETE FROM doc_analysis WHERE rel_path=?", [rel_path])
        con.execute("INSERT INTO doc_analysis VALUES (?,?,?,?,?)",
                    [rel_path, doc_type, _now(), complete, _j(result)])


def get_analyses() -> Dict[str, Dict[str, Any]]:
    with connect() as con:
        rows = con.execute("SELECT rel_path, analyzed_at, complete, result "
                           "FROM doc_analysis").fetchall()
    return {r[0]: {**(json.loads(r[3]) if r[3] else {}),
                   "analyzed_at": str(r[1]), "complete": bool(r[2])} for r in rows}


def corpus_all_chunks(doc_type: Optional[str] = None) -> List[Dict[str, Any]]:
    q = ("SELECT chunk_id, doc_name, doc_type, section, text, embedding, "
         "section_path, block_start, block_end FROM corpus_chunks")
    args: List[Any] = []
    if doc_type:
        q += " WHERE doc_type=?"
        args.append(doc_type)
    q += " ORDER BY doc_name, ord"
    with connect() as con:
        rows = con.execute(q, args).fetchall()
    return [{"chunk_id": r[0], "doc_name": r[1], "doc_type": r[2], "section": r[3],
             "text": r[4], "embedding": r[5], "section_path": r[6],
             "block_start": r[7], "block_end": r[8]} for r in rows]
