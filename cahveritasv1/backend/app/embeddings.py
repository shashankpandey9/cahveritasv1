"""CAH VERITAS — embeddings & retrieval.

Local runtime: a deterministic 256-d hashed TF embedding — zero network calls,
zero model downloads, milliseconds per chunk. Retrieval quality is adequate
for section-level grounding of a UAT prototype; swap `embed()` for
sentence-transformers or the Anthropic-adjacent embedding provider of choice
in one place if higher recall is needed.

Databricks runtime: `search()` routes to Databricks Vector Search
(DATABRICKS_VS_INDEX) which serves the chunk-embedded BRD / design / STTM /
code corpus built by databricks/notebooks/01_ingest_and_index.py.
"""
from __future__ import annotations

import hashlib
import math
import re
import threading
from typing import Any, Dict, List, Optional

import numpy as np

from . import config, db

DIM = 256
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}")


def embed(text: str) -> List[float]:
    vec = np.zeros(DIM, dtype=np.float32)
    tokens = _TOKEN_RE.findall(text.lower())
    for i, tok in enumerate(tokens):
        h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "big")
        idx = h % DIM
        sign = 1.0 if (h >> 32) & 1 else -1.0
        vec[idx] += sign
        if i + 1 < len(tokens):  # bigram signal
            big = tok + "_" + tokens[i + 1]
            h2 = int.from_bytes(hashlib.blake2b(big.encode(), digest_size=8).digest(), "big")
            vec[h2 % DIM] += 0.5 if (h2 >> 32) & 1 else -0.5
    n = float(np.linalg.norm(vec))
    if n > 0:
        vec /= n
    return vec.tolist()


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b:
        return 0.0
    return float(np.dot(np.asarray(a, dtype=np.float32),
                        np.asarray(b, dtype=np.float32)))


def _keyword_score(query: str, text: str) -> float:
    q = set(_TOKEN_RE.findall(query.lower()))
    if not q:
        return 0.0
    t = set(_TOKEN_RE.findall(text.lower()))
    return len(q & t) / math.sqrt(len(q) + 1)


# ------------------------------------------------------------ local index
# The corpus is ~100k chunks. Loading every embedding from DuckDB per query
# (as search() once did) cost seconds per call and blocked the server; the
# matrix is loaded once (~0.6 s) and reused until the corpus changes, and a
# query is one matrix-vector product (~5 ms).
_INDEX: Dict[str, Any] = {"version": None}
_INDEX_LOCK = threading.Lock()
_CANDIDATES = 600          # cosine shortlist the keyword score re-ranks


def _index() -> Dict[str, Any]:
    with db.connect() as con:
        version = con.execute("SELECT count(*), max(chunk_id) FROM corpus_chunks").fetchone()
    with _INDEX_LOCK:
        if _INDEX["version"] == version:
            return _INDEX
        with db.connect() as con:
            arr = con.execute(
                f"SELECT chunk_id, embedding::FLOAT[{DIM}] AS e FROM corpus_chunks "
                f"WHERE len(embedding) = {DIM} ORDER BY chunk_id").fetchnumpy()
            meta = con.execute(
                f"SELECT doc_name, doc_type, section, text FROM corpus_chunks "
                f"WHERE len(embedding) = {DIM} ORDER BY chunk_id").fetchall()
        mat = (np.stack(arr["e"]).astype(np.float32) if len(arr["e"])
               else np.zeros((0, DIM), dtype=np.float32))
        _INDEX.update(version=version, matrix=mat, meta=meta,
                      types=np.array([m[1] for m in meta], dtype=object))
        return _INDEX


def search(query: str, top_k: int = 6,
           doc_type: Optional[str] = None) -> List[Dict[str, Any]]:
    """Hybrid (vector + keyword) retrieval. On Databricks, tries Vector
    Search first and falls back to the local index."""
    if config.VECTOR_SEARCH:
        hits = _dbx_search(query, top_k, doc_type)
        if hits:
            return hits
    idx = _index()
    if not len(idx["meta"]):
        return []
    qv = np.asarray(embed(query), dtype=np.float32)
    cos = idx["matrix"] @ qv
    if doc_type:
        cos = np.where(idx["types"] == doc_type, cos, -np.inf)
    n = min(_CANDIDATES, int(np.isfinite(cos).sum()))
    if n <= 0:
        return []
    shortlist = np.argpartition(-cos, n - 1)[:n]
    scored = sorted(((0.72 * float(cos[i]) + 0.28 * _keyword_score(query, idx["meta"][i][3]), i)
                     for i in shortlist), reverse=True)
    return [{"doc_name": idx["meta"][i][0], "doc_type": idx["meta"][i][1],
             "section": idx["meta"][i][2], "text": idx["meta"][i][3], "score": round(s, 4)}
            for s, i in scored[:top_k]]


def _dbx_search(query: str, top_k: int,
                doc_type: Optional[str]) -> List[Dict[str, Any]]:  # pragma: no cover
    try:
        from databricks.vector_search.client import VectorSearchClient
        from . import dbx
        cfg = dbx.sdk_config()
        vsc = VectorSearchClient(workspace_url=dbx.host(),
                                 personal_access_token=config.DBX_TOKEN or None,
                                 service_principal_client_id=cfg.client_id,
                                 service_principal_client_secret=cfg.client_secret,
                                 disable_notice=True)
        index = vsc.get_index(endpoint_name=config.DBX_VS_ENDPOINT,
                              index_name=config.DBX_VS_INDEX)
        filters = {"doc_type": doc_type} if doc_type else None
        res = index.similarity_search(
            query_text=query, num_results=top_k, filters=filters,
            columns=["doc_name", "doc_type", "section", "text"])
        rows = res.get("result", {}).get("data_array", []) or []
        return [{"doc_name": r[0], "doc_type": r[1], "section": r[2],
                 "text": r[3], "score": float(r[-1])} for r in rows]
    except Exception:
        return []


def context_block(query: str, top_k: int = 6, doc_type: Optional[str] = None,
                  char_budget: int = 6000) -> str:
    """Formatted, citable retrieval block for prompts — keeps prompts small so
    generation stays fast."""
    out, used = [], 0
    for h in search(query, top_k=top_k, doc_type=doc_type):
        seg = f"[{h['doc_name']} :: {h['section']}]\n{h['text'].strip()}\n"
        if used + len(seg) > char_budget:
            break
        out.append(seg)
        used += len(seg)
    return "\n".join(out) if out else "(no corpus matches)"
