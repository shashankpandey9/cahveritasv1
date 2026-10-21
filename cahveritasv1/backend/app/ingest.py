"""CAH VERITAS — corpus ingestion.

Scans inputs/ for:
    brd/*.docx            MS-Word BRD
    design/*.docx         MS-Word Design Documents
    */*.xlsx|.xlsm|.pptx  workbooks and decks (officeparse) in brd/, design/,
                          sources/ and uat_baseline/
    sources/*.pdf         PDF Source Data Listings (S3 raw sources)
    code/**               SparkSQL / Python / SQL / Scala
    uat_baseline/*        the team's existing UAT cases (optional)
Also accepts .md/.txt anywhere above (the shipped samples), and falls back to
inputs/samples/ when a folder is empty so the app runs out-of-the-box.

Pipeline: :mod:`docparse` produces an ordered structural block stream ->
:mod:`chunker` (the chunk-embedding agent) turns it into block-atomic,
section-scoped, embedded chunks -> DuckDB (`corpus_docs`, `corpus_blocks`,
`corpus_chunks`).

Both representations are persisted on purpose. The **chunks** serve retrieval
(the RAG enumerator, stages 2-4). The **blocks** serve traversal — the
structural requirements enumerator walks the document in order, and a
requirement cites the exact block span it came from. Reconstructing block
order from overlapping chunks is lossy; storing it is nearly free.

Ingestion is persisted: restarts and later stages never re-read documents.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import chunker, config, db, docparse, figures

log = logging.getLogger("veritas.ingest")

CODE_EXT = docparse.CODE_EXT
TEXT_EXT = {".md", ".txt", ".csv"}
OFFICE_EXT = {".xlsx", ".xlsm", ".pptx"}


def _collect(root: Path) -> List[Tuple[Path, str]]:
    """[(path, doc_type)]"""
    plan: List[Tuple[Path, str]] = []

    def grab(sub: str, doc_type: str, exts: set) -> int:
        found = 0
        folder = root / sub
        if folder.is_dir():
            for p in sorted(folder.rglob("*")):
                if p.is_file() and p.suffix.lower() in exts and not p.name.startswith("."):
                    plan.append((p, doc_type))
                    found += 1
        return found

    n_brd = grab("brd", "brd", {".docx"} | OFFICE_EXT | TEXT_EXT)
    n_des = grab("design", "design", {".docx"} | OFFICE_EXT | TEXT_EXT)
    n_src = grab("sources", "sources", {".pdf"} | OFFICE_EXT | TEXT_EXT)
    n_code = grab("code", "code", CODE_EXT | TEXT_EXT)
    grab("uat_baseline", "uat_baseline", {".docx", ".csv"} | OFFICE_EXT | TEXT_EXT)

    # samples fallback so the flow always demos
    samples = root / "samples"
    if samples.is_dir():
        mapping = {"brd": n_brd, "design": n_des, "sources": n_src, "code": n_code}
        for p in sorted(samples.rglob("*")):
            if not p.is_file():
                continue
            dt = None
            low = p.name.lower()
            if "brd" in low:
                dt = "brd" if mapping["brd"] == 0 else None
            elif "design" in low:
                dt = "design" if mapping["design"] == 0 else None
            elif "source" in low or "s3" in low:
                dt = "sources" if mapping["sources"] == 0 else None
            elif p.suffix.lower() in CODE_EXT:
                dt = "code" if mapping["code"] == 0 else None
            elif "baseline" in low or "uat" in low:
                dt = "uat_baseline"
            if dt:
                plan.append((p, dt))
    return plan


def rel_key(path: Path) -> str:
    """A document's identity: its path relative to inputs/. File names alone
    collide (the code tree repeats names across folders), so they are not a
    safe key."""
    path = Path(path)
    if not path.is_absolute():    # stored by older ingests, relative to the project
        path = config.PROJECT_ROOT / path
    try:  # paths found by walking INPUTS_DIR need no resolve() (slow on Windows)
        return path.relative_to(config.INPUTS_DIR).as_posix()
    except ValueError:
        pass
    try:
        return path.resolve().relative_to(_inputs_root()).as_posix()
    except ValueError:
        return path.as_posix()


_INPUTS_ROOT: Dict[str, Path] = {}


def _inputs_root() -> Path:
    key = str(config.INPUTS_DIR)
    if key not in _INPUTS_ROOT:
        _INPUTS_ROOT[key] = config.INPUTS_DIR.resolve()
    return _INPUTS_ROOT[key]


def ingest_file(path: Path, doc_type: str) -> Optional[Dict[str, Any]]:
    """Parse, store and chunk one document. Re-ingesting a document replaces
    its blocks and chunks in place (same doc_id). Returns None if the file
    produced no content."""
    try:
        blocks = docparse.parse_blocks(path)
    except Exception as e:
        log.error("parse failed for %s: %s", path.name, e)
        return None
    if not blocks:
        log.warning("no content parsed from %s — skipped", path.name)
        return None

    rel = rel_key(path)
    sections = len({b.get("section_path") for b in blocks})
    pages, pages_est = docparse.page_count(path, blocks)
    doc_id = db.find_doc(rel, doc_type)
    if doc_id:
        db.reset_doc(doc_id, str(path), sections, pages, pages_est)
    else:
        doc_id = db.add_doc(path.name, doc_type, str(path), sections, 0,
                            rel_path=rel, pages=pages, pages_estimated=pages_est)
    db.add_blocks(doc_id, path.name, doc_type, blocks)
    if figures.supports(path):
        db.set_doc_figures(doc_id, figures.status(figures.document_figures(path)))

    chunks = chunker.build_chunks(blocks, doc_id=doc_id, doc_name=path.name,
                                  doc_type=doc_type)
    # Expected count goes in before the chunks, the actual count after — a
    # crash between the two leaves a visible gap rather than a silent one.
    db.set_doc_expected_chunks(doc_id, len(chunks))
    if chunks:
        db.add_chunks(chunks)
    db.set_doc_chunks(doc_id, len(chunks))
    log.info("ingested %s (%s): %d blocks -> %d chunks",
             rel, doc_type, len(blocks), len(chunks))
    return {"name": path.name, "rel_path": rel, "doc_type": doc_type,
            "blocks": len(blocks), "chunks": len(chunks)}


def ingest_all(force: bool = True) -> Dict[str, Any]:
    root = config.INPUTS_DIR
    plan = _collect(root)
    if force:
        db.clear_corpus()
    counts: Dict[str, int] = {}
    docs: List[Dict[str, Any]] = []
    total_chunks = 0
    seen: set[tuple[str, str]] = set()

    with db.held_open():          # one checkpoint for the run, not one per file
        for path, doc_type in plan:
            key = (rel_key(path), doc_type)
            if key in seen:
                continue
            seen.add(key)
            res = ingest_file(path, doc_type)
            if not res:
                continue
            counts[doc_type] = counts.get(doc_type, 0) + 1
            total_chunks += res["chunks"]
            docs.append(res)

    if not any(d["doc_type"] == "brd" for d in docs):
        log.warning("no BRD ingested — requirements discovery will have nothing "
                    "to enumerate. Drop a .docx into inputs/brd/.")
    return {"by_type": counts, "documents": docs, "chunks": total_chunks}
