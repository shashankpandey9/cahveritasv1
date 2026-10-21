"""CAH VERITAS — document discovery & chunk-coverage audit (Phase 0).

Three questions, answered at two levels of cost:

    What is there?        every file under inputs/<type>/, supported or not
    What is stored?       corpus_docs / corpus_blocks / corpus_chunks
    Is it all chunked?    quick check  — stored blocks vs stored chunk spans
                          Analyze      — the source re-parsed and compared,
                                         block by block, with what is stored

The quick check runs for the whole inventory on every listing (one SQL pass,
no parsing). It catches an interrupted ingest: blocks written but chunks
missing, or the chunk count never recorded. It cannot see a document that
changed on disk after ingest, or tell how many chunks a never-ingested
document needs; Analyze answers both. Its verdict is kept until the
document's stored chunks change (a re-ingest, or damage) — a stale verdict
is dropped rather than trusted.

A document's identity is its path relative to inputs/ — file names repeat
across the code tree.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import chunker, config, db, docparse, figures, ingest, vision
from .trace import TraceBus

log = logging.getLogger("veritas.discovery")

DOC_TYPES = ["brd", "design", "sources", "code", "uat_baseline"]
# Documents that must be fully chunked before Phase 0 can be accepted.
REQUIRED_TYPES = ("brd",)


# ------------------------------------------------------------------ discovery
def _discover() -> List[Dict[str, Any]]:
    """Every file in the typed input folders, flagged supported when the
    ingest plan would pick it up (plus any samples the plan falls back to)."""
    root = config.INPUTS_DIR
    plan = ingest._collect(root)
    supported = {(ingest.rel_key(p), t) for p, t in plan}
    found: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def add(p: Path, doc_type: str) -> None:
        rel = ingest.rel_key(p)
        found.setdefault((rel, doc_type), {
            "rel_path": rel, "name": p.name, "doc_type": doc_type,
            "ext": p.suffix.lower(), "on_disk": True,
            "supported": (rel, doc_type) in supported,
        })

    for doc_type in DOC_TYPES:
        folder = root / doc_type
        if folder.is_dir():
            for p in sorted(folder.rglob("*")):
                if p.is_file() and not p.name.startswith("."):
                    add(p, doc_type)
    for p, doc_type in plan:          # sample fallbacks live outside the folders
        add(p, doc_type)
    return list(found.values())


def _backfill_rel_paths(cov: List[Dict[str, Any]]) -> None:
    """Rows ingested before rel_path existed get it derived from their path."""
    todo = [{"doc_id": c["doc_id"], "rel": ingest.rel_key(Path(c["path"]))} for c in cov
            if not c.get("rel_path") and c.get("path")]
    if not todo:
        return
    with db.connect() as con:   # one JSON batch — executemany is ~4 ms a row
        con.execute("UPDATE corpus_docs SET rel_path = m.rel FROM "
                    "(SELECT unnest(CAST(?::JSON AS STRUCT(doc_id VARCHAR, rel VARCHAR)[]), "
                    "recursive := true)) AS m "
                    "WHERE corpus_docs.doc_id = m.doc_id", [json.dumps(todo)])
    for c in cov:
        if not c.get("rel_path") and c.get("path"):
            c["rel_path"] = ingest.rel_key(Path(c["path"]))


def _quick_status(c: Dict[str, Any]) -> Dict[str, Any]:
    """Completeness from what is stored — no parsing."""
    rows = c["chunk_rows"]
    expected = c["expected_chunks"]
    recorded = c["chunks"] or 0
    uncovered = c["uncovered_blocks"]
    # expected_chunks is recorded before the chunks are written; older rows
    # only have the post-write count, and 0 there with chunks present means
    # the write never finished.
    if expected is not None:
        remaining_chunks: Optional[int] = max(0, expected - rows)
    elif recorded and recorded >= rows:
        remaining_chunks = recorded - rows
    else:
        remaining_chunks = None if uncovered else 0
    # A document with embedded figures is complete only once every figure is
    # transcribed or deliberately skipped. Rows ingested before figures were
    # tracked (figures_total NULL) have not been assessed yet.
    fig_ok = (not figures.supports(Path(c["rel_path"] or c["name"]))
              or (c["figures_total"] is not None and not c["figures_pending"]))
    complete = (rows > 0 and uncovered == 0 and recorded == rows
                and (remaining_chunks == 0) and fig_ok)
    return {
        "status": "complete" if complete else "partial",
        "complete": complete,
        "remaining_sections": c["uncovered_sections"],
        "remaining_chunks": 0 if complete else remaining_chunks,
    }


def inventory() -> List[Dict[str, Any]]:
    """One row per discovered or stored document — the Document Discovery grid."""
    discovered = _discover()
    cov = db.corpus_coverage()
    _backfill_rel_paths(cov)
    cov_by_key = {(c["rel_path"], c["doc_type"]): c for c in cov}
    analyses = db.get_analyses()

    disc_by_key = {(d["rel_path"], d["doc_type"]): d for d in discovered}
    keys = list(disc_by_key) + [k for k in cov_by_key if k not in disc_by_key]

    rows: List[Dict[str, Any]] = []
    for key in keys:
        d = disc_by_key.get(key)
        c = cov_by_key.get(key)
        rel, doc_type = key
        row: Dict[str, Any] = {
            "key": rel + "::" + doc_type,
            "rel_path": rel,
            "doc_type": doc_type,
            "name": (d or c)["name"],
            "on_disk": bool(d),
            "supported": bool(d and d["supported"]) or bool(c),
            "doc_id": c["doc_id"] if c else None,
            "pages": c["pages"] if c else None,
            "pages_estimated": bool(c and c["pages_estimated"]),
            "sections": c["sections"] if c else None,
            "chunks": c["chunk_rows"] if c else 0,
            "figures": ({"total": c["figures_total"], "captured": c["figures_captured"],
                         "skipped": c["figures_skipped"], "pending": c["figures_pending"]}
                        if c and c["figures_total"] is not None else None),
            "ingested_at": str(c["ingested_at"]) if c else None,
            "verified": False,
            "analyzed_at": None,
        }
        if c:
            row.update(_quick_status(c))
        elif row["supported"]:
            row.update(status="not_chunked", complete=False,
                       remaining_sections=None, remaining_chunks=None)
        else:
            row.update(status="unsupported", complete=False,
                       remaining_sections=None, remaining_chunks=None)
        a = analyses.get(rel)
        # An analysis describes the chunks as they were when it ran; if the
        # stored chunks have changed since, it is stale and the quick check
        # stands — a stale "verified" must never hide a gap.
        stale = bool(a) and (a.get("doc_id") != (c["doc_id"] if c else None)
                             or a.get("stored_chunks") != (c["chunk_rows"] if c else 0))
        if a and a.get("doc_type") == doc_type and not stale:
            row.update(
                verified=True, analyzed_at=a["analyzed_at"], complete=a["complete"],
                remaining_sections=a.get("remaining_sections"),
                remaining_chunks=a.get("remaining_chunks"),
                verdict=a.get("verdict"),
                status="complete" if a["complete"] else
                       ("partial" if c else "not_chunked"))
            if row["pages"] is None:
                row["pages"] = a.get("pages")
                row["pages_estimated"] = a.get("pages_estimated", False)
            if row["sections"] is None:
                row["sections"] = a.get("expected_sections")
            if row["figures"] is None and a.get("figures"):
                row["figures"] = a["figures"]
        if not d and c:
            row["status"] = "removed"
        rows.append(row)

    order = {t: i for i, t in enumerate(DOC_TYPES)}
    rows.sort(key=lambda r: (order.get(r["doc_type"], 99), r["rel_path"].lower()))
    return rows


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_type: Dict[str, Dict[str, int]] = {}
    for r in rows:
        t = by_type.setdefault(r["doc_type"], {
            "discovered": 0, "supported": 0, "complete": 0, "partial": 0,
            "not_chunked": 0, "unsupported": 0, "removed": 0, "chunks": 0})
        t["discovered"] += 1
        t["supported"] += int(r["supported"])
        t[r["status"]] = t.get(r["status"], 0) + 1
        t["chunks"] += r["chunks"] or 0
        f = r.get("figures") or {}
        t["figures_captured"] = t.get("figures_captured", 0) + (f.get("captured") or 0)
        t["figures_pending"] = t.get("figures_pending", 0) + (f.get("pending") or 0)

    def tot(k: str) -> int:
        return sum(t.get(k, 0) for t in by_type.values())

    required = [r for r in rows if r["doc_type"] in REQUIRED_TYPES and r["supported"]]
    required_ready = bool(required) and all(r["complete"] for r in required)
    return {
        "discovered": len(rows),
        "supported": tot("supported"),
        "complete": tot("complete"),
        "partial": tot("partial"),
        "not_chunked": tot("not_chunked"),
        "unsupported": tot("unsupported"),
        "removed": tot("removed"),
        "chunks": tot("chunks"),
        "figures_captured": tot("figures_captured"),
        "figures_pending": tot("figures_pending"),
        "by_type": by_type,
        "required": [{"name": r["name"], "rel_path": r["rel_path"],
                      "doc_type": r["doc_type"], "status": r["status"],
                      "complete": r["complete"], "verified": r["verified"]}
                     for r in required],
        "required_ready": required_ready,
        # kept for the Phase 0 payload / stage summary
        "ready": required_ready,
        "missing": tot("not_chunked"),
    }


def summary() -> Dict[str, Any]:
    return summarize(inventory())


# -------------------------------------------------------------------- analyze
def _resolve(rel_path: str) -> Path:
    """inputs/-relative path -> file, refusing anything that escapes inputs/."""
    root = config.INPUTS_DIR.resolve()
    p = (root / rel_path).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        raise ValueError("path is outside the inputs folder")
    return p


def _text_hash(texts: List[str]) -> str:
    h = hashlib.sha1()
    for t in texts:
        h.update((t or "").encode("utf-8", "ignore"))
        h.update(b"\x00")
    return h.hexdigest()


def analyze(rel_path: str, doc_type: str) -> Dict[str, Any]:
    """Re-parse the source and check that every block of it is stored and
    covered by a chunk. The verdict is persisted for the grid."""
    path = _resolve(rel_path)
    if not path.is_file():
        raise FileNotFoundError(rel_path)
    blocks = docparse.parse_blocks(path)
    name = path.name
    text_blocks = [b for b in blocks if (b.get("text") or "").strip()]
    expected = chunker.build_chunks(blocks, doc_id="", doc_name=name,
                                    doc_type=doc_type, embed=False)
    expected_sections: List[str] = []
    for b in text_blocks:
        s = b.get("section_path") or name
        if s not in expected_sections:
            expected_sections.append(s)
    pages, pages_est = docparse.page_count(path, blocks)
    fig_list = vision.describe(path) if figures.supports(path) else []
    fig_st = {"total": len(fig_list),
              "captured": sum(1 for f in fig_list if f["verdict"] in figures.CAPTURED),
              "skipped": sum(1 for f in fig_list if f["verdict"] in figures.SKIPPED),
              "pending": sum(1 for f in fig_list if f["verdict"] in ("pending", "failed"))}

    doc_id = db.find_doc(rel_path, doc_type)
    spans = db.doc_chunk_spans(doc_id) if doc_id else []
    with db.connect() as con:
        stored = con.execute("SELECT text FROM corpus_blocks WHERE doc_id=? ORDER BY ord",
                             [doc_id]).fetchall() if doc_id else []
        recorded = con.execute("SELECT chunks FROM corpus_docs WHERE doc_id=?",
                               [doc_id]).fetchone() if doc_id else None
    stored_texts = [r[0] for r in stored]
    changed = bool(doc_id) and (
        len(stored_texts) != len(blocks)
        or _text_hash(stored_texts) != _text_hash([b.get("text") for b in blocks]))

    covered = [False] * len(blocks)
    if not changed:
        for s in spans:
            for i in range(max(0, s["block_start"]), min(len(blocks), s["block_end"] + 1)):
                covered[i] = True
    uncovered = [b for i, b in enumerate(blocks)
                 if (b.get("text") or "").strip() and not covered[i]]
    uncovered_sections = []
    for b in uncovered:
        s = b.get("section_path") or name
        if s not in uncovered_sections:
            uncovered_sections.append(s)
    chunks_needing = sum(
        1 for ch in expected
        if any(not covered[i] and (blocks[i].get("text") or "").strip()
               for i in range(ch["block_start"], min(len(blocks), ch["block_end"] + 1))))
    remaining_chunks = max(len(expected) - len(spans), chunks_needing)
    complete = (bool(doc_id) and not changed and not uncovered
                and remaining_chunks == 0 and not fig_st["pending"]
                and (recorded[0] if recorded else 0) == len(spans))

    n_text = len(text_blocks)
    coverage_pct = round(100.0 * (n_text - len(uncovered)) / n_text, 1) if n_text else 0.0
    if not doc_id:
        verdict = (f"Not chunked. The source has {n_text} content blocks in "
                   f"{len(expected_sections)} sections, which needs "
                   f"{len(expected)} chunks.")
    elif changed:
        verdict = ("The file on disk has changed since it was ingested "
                   f"({len(stored_texts)} stored blocks vs {len(blocks)} now). "
                   "Re-chunk it to bring the corpus up to date.")
    elif complete:
        verdict = (f"Fully chunked. All {n_text} content blocks in "
                   f"{len(expected_sections)} sections are covered by "
                   f"{len(spans)} chunks.")
    elif not uncovered and remaining_chunks == 0 and fig_st["pending"]:
        verdict = (f"Text fully chunked ({n_text} content blocks, {len(spans)} chunks), "
                   f"but {fig_st['pending']} of {fig_st['total']} embedded figures are not "
                   "transcribed yet — Chunk runs the vision agents on them.")
    else:
        verdict = (f"Partially chunked: {coverage_pct}% of content blocks covered. "
                   f"{len(uncovered)} blocks in {len(uncovered_sections)} sections "
                   f"are not in any chunk; {remaining_chunks} of "
                   f"{len(expected)} chunks are missing.")
    if fig_st["total"] and (complete or not doc_id):
        verdict += (f" Figures: {fig_st['captured']} transcribed, {fig_st['skipped']} "
                    f"skipped (icons/decorative/furniture), {fig_st['pending']} pending.")

    result = {
        "rel_path": rel_path, "doc_type": doc_type, "name": name,
        "doc_id": doc_id, "pages": pages, "pages_estimated": pages_est,
        "source_blocks": n_text, "stored_blocks": len(stored_texts),
        "expected_sections": len(expected_sections),
        "remaining_sections": len(expected_sections) if not doc_id or changed
        else len(uncovered_sections),
        "remaining_section_names": (expected_sections if not doc_id or changed
                                    else uncovered_sections)[:25],
        "expected_chunks": len(expected), "stored_chunks": len(spans),
        "remaining_chunks": len(expected) if not doc_id or changed else remaining_chunks,
        "uncovered_blocks": n_text if not doc_id or changed else len(uncovered),
        "coverage_pct": 0.0 if not doc_id or changed else coverage_pct,
        "changed_since_ingest": changed,
        "figures": fig_st, "figure_list": fig_list,
        "complete": complete, "verdict": verdict,
    }
    db.save_analysis(rel_path, doc_type, complete, result)
    return result


# --------------------------------------------------------------------- chunk
def targets(rel_paths: Optional[List[str]] = None) -> List[Tuple[Path, str]]:
    """Files to (re-)chunk: the given ones, or every supported document on
    disk that is not complete."""
    rows = inventory()
    if rel_paths:
        wanted = set(rel_paths)
        rows = [r for r in rows if r["rel_path"] in wanted]
    else:
        rows = [r for r in rows if r["status"] in ("partial", "not_chunked")]
    return [(_resolve(r["rel_path"]), r["doc_type"]) for r in rows
            if r["supported"] and r["on_disk"]]


async def chunk_documents(items: List[Tuple[Path, str]], bus: TraceBus) -> Dict[str, Any]:
    """Ingest the given documents one at a time, off the event loop, without
    touching the rest of the corpus."""
    await bus.emit("corpus_chunker", "start",
                   f"chunking {len(items)} document(s) — the rest of the corpus is kept")
    failed: List[str] = []
    totals = {"done": 0, "chunks": 0}
    step = max(1, len(items) // 20)
    with db.held_open():       # one checkpoint for the batch, not one per file
        await _chunk_loop(items, bus, step, failed, totals)
    done, chunks = totals["done"], totals["chunks"]
    await bus.emit("corpus_chunker", "finish",
                   f"{done} document(s) chunked · {chunks} chunks"
                   + (f" · {len(failed)} failed" if failed else ""))
    return {"documents": done, "chunks": chunks, "failed": failed}


async def _chunk_loop(items: List[Tuple[Path, str]], bus: TraceBus, step: int,
                      failed: List[str], totals: Dict[str, int]) -> None:
    for i, (path, doc_type) in enumerate(items, 1):
        try:
            if figures.supports(path):
                # figure extractor -> image triage -> vision transcriber; the
                # transcripts land in the cache the parse below reads from
                await vision.enrich_document(path, bus)
            res = await asyncio.to_thread(ingest.ingest_file, path, doc_type)
            if res:
                totals["done"] += 1
                totals["chunks"] += res["chunks"]
            else:
                failed.append(f"{path.name}: no content parsed")
        except Exception as e:                              # pragma: no cover
            log.exception("chunking %s failed", path)
            failed.append(f"{path.name}: {e}")
        if len(items) <= 20 or i % step == 0 or i == len(items):
            await bus.emit("corpus_chunker", "info",
                           f"{i}/{len(items)} · {path.name}")
