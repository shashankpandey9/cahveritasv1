"""CAH VERITAS — the chunk-embedding agent.

The retrieval quality of everything downstream is decided here. Stage 1's
secondary (RAG) enumeration pass fetches document parts through this index,
so a chunk that severs a requirement from its heading, or splits a table row
down the middle, becomes a requirement the register gets wrong.

Three properties this agent guarantees, none of which the previous
fixed-width character slicer provided:

**Blocks are atomic.** A chunk boundary never falls inside a bullet, a
paragraph or a table row. Requirements are stated one block at a time.

**Every chunk is self-describing.** Each chunk is prefixed with its heading
path, so a retrieved fragment carries the section that scopes it — the
difference between "Configure product to ensure data flows correctly" (which
appears twelve times) and "2.2 UNALIGN FACTS > row 4: XPD Core and SOB".

**Spans are addressable.** A chunk records the ``ord`` range of the blocks it
covers, so the structural enumerator and the RAG enumerator talk about the
same document in the same coordinates, and a requirement can cite an exact
span rather than a page.

Chunks overlap by one block, which is enough to keep a bullet's lead-in
sentence with it without inflating the index.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from . import db, embeddings
from .trace import TraceBus

log = logging.getLogger("veritas.chunker")

CHUNK_CHARS = 1400
MIN_CHUNK_CHARS = 120
OVERLAP_BLOCKS = 1

# A chunk holding this many blocks is a long flat list (the GPro KPI table);
# splitting it keeps individual rows retrievable instead of averaged away.
MAX_BLOCKS_PER_CHUNK = 14


def _render(block: Dict[str, Any]) -> str:
    """One block as it should read inside a chunk."""
    kind = block.get("kind")
    text = (block.get("text") or "").strip()
    if kind == "bullet":
        indent = "  " * int(block.get("list_level") or 0)
        return indent + "- " + text
    if kind == "table_row":
        return "* " + text
    if kind == "table_header":
        return "(columns) " + text
    if kind in ("heading", "figure_title"):
        return "## " + text
    return text


def build_chunks(blocks: List[Dict[str, Any]], *, doc_id: str, doc_name: str,
                 doc_type: str, char_budget: int = CHUNK_CHARS,
                 embed: bool = True) -> List[Dict[str, Any]]:
    """Section-scoped, block-atomic chunks with one block of overlap.
    ``embed=False`` skips embedding — used by the coverage check, which only
    needs the chunk layout."""
    chunks: List[Dict[str, Any]] = []
    if not blocks:
        return chunks

    def flush(group: List[Dict[str, Any]], section_path: str) -> None:
        body = [_render(b) for b in group if (b.get("text") or "").strip()]
        if not body:
            return
        header = "[" + doc_name + " :: " + (section_path or doc_name) + "]"
        text = header + "\n" + "\n".join(body)
        chunks.append({
            "doc_id": doc_id, "doc_name": doc_name, "doc_type": doc_type,
            "section": group[0].get("section") or doc_name,
            "section_path": section_path or doc_name,
            "ord": len(chunks),
            "block_start": group[0].get("ord", 0),
            "block_end": group[-1].get("ord", 0),
            "n_blocks": len(group),
            "text": text,
            "embedding": embeddings.embed(text) if embed else None,
        })

    group: List[Dict[str, Any]] = []
    size = 0
    section = blocks[0].get("section_path") or doc_name

    for b in blocks:
        b_section = b.get("section_path") or doc_name
        b_text = _render(b)
        # A new section always starts a new chunk: sections are the unit a
        # requirement lives in.
        section_changed = b_section != section
        too_big = group and (size + len(b_text) > char_budget
                             or len(group) >= MAX_BLOCKS_PER_CHUNK)
        if group and (section_changed or too_big):
            flush(group, section)
            if section_changed:
                group, size = [], 0
            else:
                group = group[-OVERLAP_BLOCKS:] if OVERLAP_BLOCKS else []
                size = sum(len(_render(g)) for g in group)
            section = b_section
        elif not group:
            section = b_section
        group.append(b)
        size += len(b_text) + 1

    if group:
        flush(group, section)

    # Fold a trailing scrap into its predecessor rather than indexing a stub.
    if len(chunks) > 1 and len(chunks[-1]["text"]) < MIN_CHUNK_CHARS:
        tail = chunks.pop()
        prev = chunks[-1]
        if prev["section_path"] == tail["section_path"]:
            prev["text"] = prev["text"] + "\n" + tail["text"].split("\n", 1)[-1]
            prev["block_end"] = tail["block_end"]
            prev["n_blocks"] += tail["n_blocks"]
            prev["embedding"] = embeddings.embed(prev["text"]) if embed else None
        else:
            chunks.append(tail)
    return chunks


async def reindex(bus: Optional[TraceBus] = None) -> Dict[str, Any]:
    """Re-embed the persisted corpus in place.

    Used when the index is present but stale or empty — for instance after a
    dimension change in :mod:`embeddings`, or when an older DuckDB file is
    opened by a newer build. Ingest builds the index; this repairs it.
    """
    if bus:
        await bus.emit("corpus_chunker", "start", "verifying chunk embeddings")
    # Find stale rows in SQL — loading 100k embeddings to check them took
    # seconds and blocked the server.
    with db.connect() as con:
        total = con.execute("SELECT count(*) FROM corpus_chunks").fetchone()[0]
        stale = con.execute("SELECT chunk_id, text FROM corpus_chunks WHERE embedding IS NULL "
                            "OR len(embedding) <> ?", [embeddings.DIM]).fetchall()
    for chunk_id, text in stale:
        db.update_chunk_embedding(chunk_id, embeddings.embed(text))
    detail = (str(total) + " chunks indexed"
              + (" · " + str(len(stale)) + " re-embedded" if stale else " · all current"))
    if bus:
        await bus.emit("corpus_chunker", "finish", detail)
    return {"chunks": total, "reembedded": len(stale)}


def index_stats() -> Dict[str, Any]:
    # Counted in SQL: loading every chunk with its embedding (as this once
    # did) takes tens of seconds on a 100k-chunk corpus and blocks the server.
    with db.connect() as con:
        by_type = dict(con.execute("SELECT doc_type, count(*) FROM corpus_chunks "
                                   "GROUP BY doc_type").fetchall())
    return {"chunks": sum(by_type.values()), "by_doc_type": by_type, "dim": embeddings.DIM}
