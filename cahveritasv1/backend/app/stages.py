"""CAH VERITAS — the four stage engines.

Each stage is executed by the AAOSA network (pipeline.py drives the protocol);
this module holds the grounded work each agent performs: retrieval-augmented
LLM calls with strict-JSON contracts, deterministic heuristic fallbacks (so
the flow demos with no API key), persistence, and trace-bus emissions.

Latency: stage 2 fans its three analysis agents out concurrently; stage 3
generates cases per-requirement concurrently and batches Text2SQL per
requirement (one call yields all of that requirement's SQL); stage 4 executes
cases concurrently in threads. The corpus digest + mart schema ride in the
prompt-cached context block.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Dict, List, Optional

from . import (chunker, config, db, discovery, embeddings, ingest, llm, reqenum,
               sqlexec, sttm, volumes)
from .trace import TraceBus

# Product attribution is resolved by the lexicon in products.py — branded
# name, molecule, abbreviated/shorthand name and full product name per
# requirement — which replaced the flat BRANDS list this module used to hold.
# Add a product for a new launch by appending to products.CATALOG.

SYSTEM = ("You are an agent in CAH VERITAS, the Gilead Commercial Data & Analytics "
          "(Commercial Analytics Hub) launch-validation network. Ground every "
          "claim in the supplied corpus context; cite [document :: section]. "
          "Emit STRICT JSON when asked for JSON — no prose around it.")


def _cached_context() -> str:
    """Stable context prefix reused (and prompt-cached) across all stages."""
    inv = db.corpus_inventory()
    inv_lines = "\n".join(f"- {d['name']} ({d['doc_type']}, {d['chunks']} chunks)"
                          for d in inv)
    return ("CORPUS INVENTORY:\n" + inv_lines +
            "\n\nANALYTICS STORE SCHEMA (the UAT SQL runs here):\n" + sqlexec.schema_text())


def _refines(sid: str, stage: str) -> str:
    notes = [n["note"] for n in (db.get_session(sid) or {}).get("refine_notes", [])
             if n["stage"] == stage]
    if not notes:
        return ""
    return ("\n\nUSER REFINEMENT FEEDBACK (honor every point, and say what you "
            "changed):\n- " + "\n- ".join(notes))


# =========================================================================
# PHASE 0 — Ingestion & Corpus Preparation
#
# User-initiated ingestion with optional inputs. This phase inventories every
# artifact family, plans the strategy based on what is available, and then
# chunk/indexes the present corpus before Phase 1 begins.
# =========================================================================
async def run_phase0(sid: str, bus: TraceBus) -> Dict[str, Any]:
    if config.INPUTS_VOLUME and not config.INPUTS_VOLUME_MOUNTED:
        # inputs live in a Unity Catalog Volume: refresh the local mirror first
        await bus.emit("intake_librarian", "start", f"syncing inputs from {config.INPUTS_VOLUME}")
        loop = asyncio.get_running_loop()

        def progress(msg: str) -> None:
            asyncio.run_coroutine_threadsafe(bus.emit("intake_librarian", "info", msg), loop)
        res = await asyncio.to_thread(volumes.sync_inputs, progress)
        await bus.emit("intake_librarian", "finish",
                       f"Volume sync: {res.get('files', 0)} files · {res.get('downloaded', 0)} "
                       f"downloaded · {res.get('removed', 0)} removed · "
                       f"{res.get('unchanged', 0)} unchanged")
    await bus.emit("intake_librarian", "start",
                   "reviewing BRD, design, code, source, and UAT inputs")
    inventory = ingest._collect(db.config.INPUTS_DIR)
    by_type: Dict[str, int] = {}
    for _, doc_type in inventory:
        by_type[doc_type] = by_type.get(doc_type, 0) + 1
    await bus.emit("intake_librarian", "finish",
                   f"inventory complete: {sum(by_type.values())} files identified "
                   f"across {len(by_type)} categories")

    await bus.emit("veritas_advisor", "info",
                   "planning ingestion strategy using the available artifact set")
    # Incremental: only documents that are not fully chunked are (re-)ingested,
    # so a re-run or a gap-fill never discards the corpus already indexed.
    # The Re-ingest button (/api/ingest) remains the full rebuild.
    todo = await asyncio.to_thread(discovery.targets)
    if todo:
        result = await discovery.chunk_documents(todo, bus)
    else:
        await bus.emit("corpus_chunker", "info",
                       "every supported document is already fully chunked")
        result = {"documents": 0, "chunks": 0, "failed": []}

    audit = await asyncio.to_thread(db.corpus_gap_audit)
    ready = bool(audit.get("ready"))
    payload = {
        "status": "ready_for_phase_1" if ready else "needs_review",
        "inventory": by_type,
        "chunked_now": result.get("documents", 0),
        "failed": result.get("failed", []),
        "chunks": audit.get("chunks", 0),
        "ready": ready,
        "audit": audit,
    }
    db.set_stage(sid, "P0_review", payload)
    return payload


# =========================================================================
# STAGE 1 — Requirements Discovery & Analysis
#
# Four agents, in order:
#   intake_librarian          corpus inventory
#   corpus_chunker            chunk-embedding index health
#   requirements_enumerator   deterministic R#/R#.i traversal of the BRD
#   requirements_rag_analyst  per-group RAG enrichment + product attribution
#
# The count is fixed by the enumerator before any LLM runs. This is the whole
# point of the split: asking a model for "all the requirements" returns the
# ones it had room for, which is how a 27-section BRD yields one requirement.
# =========================================================================
async def run_stage1(sid: str, bus: TraceBus) -> Dict[str, Any]:
    await bus.emit("intake_librarian", "start", "verifying corpus inventory")
    inv = db.corpus_inventory()
    brd_docs = [d for d in inv if d["doc_type"] == "brd"]
    await bus.emit(
        "intake_librarian", "finish",
        f"{len(inv)} documents · {sum(d['chunks'] for d in inv)} chunks · "
        f"{len(brd_docs)} BRD")

    # --- chunk-embedding agent: the index the RAG pass will read through ---
    await chunker.reindex(bus)

    # --- structural enumeration: exhaustive, deterministic, LLM-free ---------
    await bus.emit("requirements_enumerator", "start",
                   "structural traversal of the BRD (R# / R#.i)")
    register: List[Dict[str, Any]] = []
    blocks_by_doc: Dict[str, List[Dict[str, Any]]] = {}
    for b in db.get_blocks("brd"):
        blocks_by_doc.setdefault(b["doc_name"], []).append(b)

    offset = 0
    for doc_name, blocks in blocks_by_doc.items():
        part = reqenum.enumerate_structural(blocks, doc_name)
        if offset:                       # keep ids unique across several BRDs
            part = _renumber(part, offset)
        register.extend(part)
        offset += sum(1 for r in part if r["level"] == 1)

    if not register:
        await bus.emit("requirements_enumerator", "error",
                       "no requirement-bearing sections found in the BRD corpus")
        return {"requirements": [], "count": 0, "stats": {},
                "error": "No BRD content was enumerable. Check inputs/brd/ and "
                         "re-run ingest."}

    stats_struct = reqenum.register_stats(register)
    await bus.emit(
        "requirements_enumerator", "finish",
        f"{stats_struct['requirements']} requirements · "
        f"{stats_struct['sub_requirements']} sub-requirements · "
        f"{stats_struct['total']} records")

    # --- secondary enumeration: RAG-grounded semantics per requirement ------
    register = await reqenum.enrich_register(
        register, cached=_cached_context(), refine=_refines(sid, "S1"), bus=bus)

    await bus.emit("product_steward", "start",
                   "Gilead product attribution (branded · molecule · shorthand)")
    stats = reqenum.register_stats(register)
    await bus.emit(
        "product_steward", "finish",
        " · ".join(f"{k}: {v}" for k, v in list(stats["by_product"].items())[:6]))

    for i, r in enumerate(register):
        r["req_no"] = r["req_id"]
        r["seq"] = i
    db.replace_requirements(sid, register)
    return {"requirements": register, "count": len(register), "stats": stats}


def _renumber(part: List[Dict[str, Any]], offset: int) -> List[Dict[str, Any]]:
    """Shift a second document's R-ids so several BRDs share one register."""
    remap: Dict[str, str] = {}
    for r in part:
        if r["level"] == 1:
            old = r["req_id"]
            new = "R" + str(int(old[1:]) + offset)
            remap[old] = new
    for r in part:
        if r["level"] == 1:
            r["req_id"] = remap[r["req_id"]]
        else:
            parent = remap.get(r["parent_req"], r["parent_req"])
            r["req_id"] = parent + r["req_id"][len(r["parent_req"]):]
            r["parent_req"] = parent
    return part


# =========================================================================
# STAGE 2 — Design & Environment Understanding + Traceability
# =========================================================================
_S2_DESIGN_PROMPT = """Requirement {req_no}: "{title}" (product {brand})
Business rule: {rule}

Sub-requirements — EVERY one must be covered by at least one spec below:
{subs}

STTM CATALOG — mappings and KPI definitions stated in the design documents and
BRD. Select the rows that implement this requirement BY ID; never restate or
invent a catalog row:
{catalog}

RETRIEVED DESIGN / CODE / SOURCE CONTEXT (cite as [document :: section]):
{context}

Produce the design understanding for {req_no}. Return STRICT JSON only:
{{"design_features": ["<feature the design must deliver>", ...],
  "specs": [{{"id": "DS-{req_no}.1", "sub_req": "<R#.x this spec satisfies, or {req_no}>",
             "feature": "<short name>",
             "specification": "<what the design specifies: tables, columns, logic, rules>",
             "evidence": "[document :: section] or STTM-#### ids",
             "status": "specified|partial|gap"}}],
  "sttm_ids": ["STTM-####", ...],
  "sttm_additional": [{{"source": "<table.column>", "transformation": "<logic>",
                       "target": "<table.column>", "evidence": "[document :: section]"}}],
  "source_entities": [{{"entity": "<table>", "columns": ["..."]}}],
  "joins": [{{"left": "...", "right": "...", "keys": ["..."], "type": "inner|left"}}],
  "filters": ["..."], "inclusions": ["..."], "exclusions": ["..."],
  "transformations": [{{"target_column": "...", "expression": "...", "note": "..."}}],
  "code_files": ["<file names from the context>"],
  "target_marts": ["<target tables/marts/reports>"],
  "output_columns": ["..."],
  "trace_status": "TRACED|PARTIAL|UNTRACED",
  "notes": "<gaps, conflicts, what the corpus does not specify>"}}

Rules: use only what the context and catalog support; a sub-requirement with no
design evidence still gets a spec with status "gap" saying what is missing;
TRACED only if every sub-requirement has a specified spec.
{refine}"""

_SQL_FROM = re.compile(r"\b(?:FROM|JOIN)\s+([a-zA-Z_][\w\.]*)", re.IGNORECASE)
_SQL_TARGET = re.compile(
    r"\b(?:INSERT\s+(?:INTO|OVERWRITE\s+TABLE)|CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW))"
    r"\s+([a-zA-Z_][\w\.]*)", re.IGNORECASE)
_SQL_WHERE = re.compile(r"\bWHERE\b(.{5,220}?)(?:\bGROUP\b|\bORDER\b|;|$)",
                        re.IGNORECASE | re.DOTALL)
_NOT_TABLES = {"select", "the", "a", "each", "where", "values", "dual", "lateral"}


def _req_text(req: Dict[str, Any]) -> str:
    subs = req.get("sub_requirements") or []
    return " ".join([req.get("title") or "", req.get("raw_text") or "",
                     " ".join(req.get("features") or [])]
                    + [(s.get("raw_text") or "") for s in subs])


def _s2_retrieve(req: Dict[str, Any]) -> Dict[str, Any]:
    """Grounding for one requirement: catalog candidates + retrieved chunks."""
    text = _req_text(req)
    query = (f"{req.get('title', '')} {req.get('brand', '')} "
             + " ".join((s.get("title") or "") for s in (req.get("sub_requirements") or [])[:8]))
    hits = {dt: embeddings.search(query, top_k=k, doc_type=dt)
            for dt, k in (("design", 6), ("code", 5), ("sources", 2), ("brd", 2))}
    return {"catalog": sttm.candidates(text, limit=30), "hits": hits}


def _context_from(hits: Dict[str, List[Dict[str, Any]]], budget: int = 9000) -> str:
    out, used = [], 0
    for dt in ("design", "code", "sources", "brd"):
        for h in hits.get(dt, []):
            seg = f"[{h['doc_name']} :: {h['section']}]\n{h['text'].strip()[:1600]}\n"
            if used + len(seg) > budget:
                break
            out.append(seg)
            used += len(seg)
    return "\n".join(out) or "(no corpus matches)"


def _complete_specs(req: Dict[str, Any], specs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Guarantee one spec per sub-requirement: the register is the contract."""
    specs = [s for s in specs if isinstance(s, dict)]
    covered = {s.get("sub_req") for s in specs}
    n = len(specs)
    for sub in req.get("sub_requirements") or []:
        if sub["req_no"] not in covered:
            n += 1
            specs.append({"id": f"DS-{req['req_no']}.{n}", "sub_req": sub["req_no"],
                          "feature": sub.get("title") or sub["req_no"],
                          "specification": "No design evidence found in the corpus for this "
                                           "sub-requirement.",
                          "evidence": "", "status": "gap"})
    for i, s in enumerate(specs, 1):
        s.setdefault("id", f"DS-{req['req_no']}.{i}")
        s.setdefault("sub_req", req["req_no"])
        if s.get("status") not in ("specified", "partial", "gap"):
            s["status"] = "partial"
    return specs


def _finish_trace(req: Dict[str, Any], t: Dict[str, Any], ground: Dict[str, Any],
                  method: str) -> Dict[str, Any]:
    """Resolve catalog ids, complete the spec list, and record coverage."""
    rows, unknown = sttm.lookup([str(i) for i in t.get("sttm_ids") or []])
    extra = [dict(x, id="", kind="derived", citation=x.get("evidence", ""))
             for x in t.get("sttm_additional") or [] if isinstance(x, dict)]
    specs = _complete_specs(req, t.get("specs") or [])
    subs = req.get("sub_requirements") or []
    specified = {s["sub_req"] for s in specs if s["status"] == "specified"}
    if subs:
        covered, n_subs = sum(1 for s in subs if s["req_no"] in specified), len(subs)
    else:
        covered, n_subs = int(req["req_no"] in specified), 1
    status = t.get("trace_status")
    if status not in ("TRACED", "PARTIAL", "UNTRACED"):
        status = "PARTIAL"
    if status == "TRACED" and covered < n_subs:
        status = "PARTIAL"                   # TRACED means every sub-requirement
    notes = t.get("notes") or ""
    if unknown:
        notes += f" (ignored unknown catalog ids: {', '.join(unknown[:6])})"
    citations = sorted({f"{h['doc_name']} :: {h['section']}"
                        for hs in ground["hits"].values() for h in hs})
    return {
        "req_no": req["req_no"],
        "design_features": t.get("design_features") or [],
        "specs": specs,
        "sttm": rows + extra,
        "source_entities": t.get("source_entities") or [],
        "joins": t.get("joins") or [], "filters": t.get("filters") or [],
        "inclusions": t.get("inclusions") or [], "exclusions": t.get("exclusions") or [],
        "transformations": t.get("transformations") or [],
        "code_files": t.get("code_files") or [],
        "target_marts": t.get("target_marts") or [],
        "output_columns": t.get("output_columns") or [],
        "trace_status": status, "notes": notes.strip(),
        "meta": {"method": method, "subs_total": len(subs), "subs_specified": covered,
                 "catalog_offered": len(ground["catalog"]), "retrieved": citations},
    }


def _s2_heuristic(req: Dict[str, Any], ground: Dict[str, Any]) -> Dict[str, Any]:
    """No-LLM fallback: only evidence actually found — catalog rows with
    strong term overlap and what the retrieved code chunks read and write.
    Every spec is marked partial/gap; nothing is invented."""
    code = ground["hits"].get("code", [])
    sources = sorted({m.group(1) for h in code for m in _SQL_FROM.finditer(h["text"])
                      if m.group(1).lower() not in _NOT_TABLES})
    targets = sorted({m.group(1) for h in code for m in _SQL_TARGET.finditer(h["text"])})
    rows = ground["catalog"][:12]
    specs = [{"id": f"DS-{req['req_no']}.{i}", "sub_req": s["req_no"],
              "feature": s.get("title") or s["req_no"],
              "specification": "Candidate evidence only (no LLM): "
                               + ("; ".join(r["target"] for r in rows[:3]) or "none matched"),
              "evidence": ", ".join(r["id"] for r in rows[:3]),
              "status": "partial" if rows else "gap"}
             for i, s in enumerate(req.get("sub_requirements") or [], 1)]
    t = {"design_features": req.get("features") or [], "specs": specs,
         "sttm_ids": [r["id"] for r in rows],
         "source_entities": [{"entity": x, "columns": []} for x in sources[:15]],
         "filters": [w.group(1).strip()[:160] for h in code
                     for w in _SQL_WHERE.finditer(h["text"])][:6],
         "code_files": sorted({h["doc_name"] for h in code}),
         "target_marts": targets[:8],
         "trace_status": "PARTIAL" if (rows or code) else "UNTRACED",
         "notes": "Heuristic trace (the design model was unavailable or its answer could "
                  "not be parsed): candidate STTM rows and code by term overlap — verify, "
                  "or Re-trace this requirement."}
    return _finish_trace(req, t, ground, "heuristic")


async def _s2_one(sid: str, req: Dict[str, Any], cached: str,
                  extra_note: str = "") -> Dict[str, Any]:
    ground = await asyncio.to_thread(_s2_retrieve, req)
    refine = _refines(sid, "S2")
    if extra_note:
        refine += f"\n\nREVIEWER NOTE FOR {req['req_no']}: {extra_note}"
    raw = await llm.smart(
        _S2_DESIGN_PROMPT.format(req_no=req["req_no"], title=req.get("title", ""),
                                 brand=req.get("brand", ""), rule=req.get("business_rule") or "",
                                 subs=_sub_context(req, limit=40) or "  (none)",
                                 catalog=sttm.prompt_lines(ground["catalog"]),
                                 context=_context_from(ground["hits"]), refine=refine),
        system=SYSTEM, cached_context=cached, max_tokens=12000, timeout=300.0)
    parsed = llm.parse_json(raw)
    if isinstance(parsed, dict) and (parsed.get("specs") or parsed.get("design_features")):
        return _finish_trace(req, parsed, ground, "llm")
    return _s2_heuristic(req, ground)


def _groups(sid: str) -> List[Dict[str, Any]]:
    """Top-level requirements, each carrying its sub-requirements.

    Stages 2-4 trace and test at requirement granularity, not sub-requirement
    granularity: R7's fourteen unaligned-fact rows share one design trace and
    one family of test cases. The sub-requirements ride along as context so
    nothing is lost — but the fan-out stays at ~27 calls, not ~160.
    """
    reqs = db.get_requirements(sid)
    kids: Dict[str, List[Dict[str, Any]]] = {}
    for r in reqs:
        if r.get("parent_req"):
            kids.setdefault(r["parent_req"], []).append(r)
    out = []
    for r in reqs:
        if r.get("level", 1) == 1:
            r = dict(r)
            r["sub_requirements"] = kids.get(r["req_no"], [])
            out.append(r)
    return out


def _sub_context(req: Dict[str, Any], limit: int = 14) -> str:
    subs = req.get("sub_requirements") or []
    return "\n".join("  " + s["req_no"] + " — " + (s.get("raw_text") or "")[:220]
                     for s in subs[:limit])


async def run_stage2(sid: str, bus: TraceBus) -> Dict[str, Any]:
    reqs = _groups(sid)
    cached = _cached_context()
    cat = await asyncio.to_thread(sttm.stats)

    await bus.emit("sttm_mapper", "start",
                   f"STTM catalog: {cat['mapping']} mappings · {cat['kpi']} KPI definitions")
    await bus.emit("design_navigator", "start",
                   f"design specs for {len(reqs)} requirements "
                   f"({sum(len(r['sub_requirements']) for r in reqs)} sub-requirements)")
    await bus.emit("data_environment_scout", "start",
                   "S3 raw sources, refresh cadence, mart schemas")
    done = {"n": 0}

    async def one(r: Dict[str, Any]) -> Dict[str, Any]:
        try:
            t = await _s2_one(sid, r, cached)
        except Exception as e:                               # pragma: no cover
            await bus.emit("design_navigator", "error", f"{r['req_no']}: {e}")
            t = _s2_heuristic(r, await asyncio.to_thread(_s2_retrieve, r))
        done["n"] += 1
        m = t["meta"]
        await bus.emit("design_navigator", "info",
                       f"{done['n']}/{len(reqs)} {r['req_no']}: {len(t['specs'])} specs "
                       f"({m['subs_specified']}/{m['subs_total']} sub-reqs specified), "
                       f"{len(t['sttm'])} STTM rows · {t['trace_status']}"
                       + (" · heuristic" if m["method"] == "heuristic" else ""))
        return t

    rows = await asyncio.gather(*[one(r) for r in reqs])
    await bus.emit("design_navigator", "finish",
                   f"{sum(len(r['specs']) for r in rows)} design specs across {len(rows)} requirements")
    await bus.emit("sttm_mapper", "finish",
                   f"{sum(len(r['sttm']) for r in rows)} STTM rows linked to requirements")
    await bus.emit("data_environment_scout", "finish",
                   f"{len(sqlexec.schema())} live tables profiled")

    await bus.emit("trace_auditor", "start", "assembling traceability matrix")
    traced = sum(1 for r in rows if r.get("trace_status") == "TRACED")
    partial = sum(1 for r in rows if r.get("trace_status") == "PARTIAL")
    untraced = len(rows) - traced - partial
    heur = sum(1 for r in rows if r["meta"]["method"] == "heuristic")
    db.replace_design_trace(sid, rows)
    await bus.emit("trace_auditor", "finish",
                   f"{traced} TRACED · {partial} PARTIAL · {untraced} UNTRACED"
                   + (f" · {heur} heuristic" if heur else ""))
    return {"summary": {"traced": traced, "partial": partial, "untraced": untraced,
                        "specs": sum(len(r["specs"]) for r in rows),
                        "sttm": sum(len(r["sttm"]) for r in rows), "heuristic": heur}}


async def retrace_one(sid: str, req_no: str, note: str, bus: TraceBus) -> Dict[str, Any]:
    """Re-run the design trace for a single requirement (reviewer-driven)."""
    req = next((r for r in _groups(sid) if r["req_no"] == req_no), None)
    if req is None:
        raise ValueError(f"unknown requirement {req_no}")
    await bus.emit("design_navigator", "start", f"re-tracing {req_no}"
                   + (f" — note: {note[:80]}" if note else ""))
    t = await _s2_one(sid, req, _cached_context(), extra_note=note)
    db.upsert_design_trace(sid, t)
    await bus.emit("design_navigator", "finish",
                   f"{req_no}: {len(t['specs'])} specs, {len(t['sttm'])} STTM rows · "
                   f"{t['trace_status']}")
    return t


# =========================================================================
# STAGE 3 — UAT Test Case Generation
# =========================================================================
_S3_PROMPT = """Design UAT test cases for requirement {req_no} — "{title}" (product {brand}).
Business rule: {rule}

Sub-requirements — aim for coverage of every one of these:
{subs}

Design specs (each case should name the spec it verifies):
{specs}

STTM rows for this requirement (source -> target | logic):
{sttm}

Design trace: {trace}

Rules:
- Cases numbered {tc_start} onward; use sub-cases ({tc_start}.1, .2) when the
  requirement has separable assertions.
- Categories: reconciliation | transformation | filter_exclusion | completeness |
  variance_accuracy | referential_integrity | refresh_timeliness.
- Each case needs portable ANSI SQL runnable on BOTH DuckDB and Databricks SQL
  against the schema in the cached context (never invent columns).
- "expectation" MUST begin with one machine token:
  ZERO_ROWS (violations query returns nothing) | ROWS_EXIST |
  MATCH_COUNTS (query returns column diff which must be 0) |
  THRESHOLD<=<number> (query returns single numeric column value) —
  then a human explanation.
- "relevance_check": one sentence proving the case validates {req_no};
  if you cannot prove it, DISCARD the case.
Enumerate beyond the obvious: boundary dates, restatements, late-arriving
actuals, brand vs total reconciliation, unit/currency conversion, forecast-cycle joins.

Return STRICT JSON: {{"cases": [{{"tc_no": "...", "parent_tc": null_or_parent,
 "name": "...", "description": "...", "category": "...", "req_no": "{req_no}",
 "sub_req": "<R#.x the case verifies>", "spec_ref": "<DS-... id>",
 "sql": "...", "expectation": "...", "relevance_check": "..."}}]}}
{refine}"""


def _s3_heuristic(req: Dict[str, Any], trace_row: Dict[str, Any],
                  tc_start: int) -> List[Dict[str, Any]]:
    mart = "mart_revenue_actual_vs_forecast"
    b = req["brand"]
    bfilter = f"WHERE brand = '{b}'" if b != "ALL" else ""
    n = tc_start
    cases = [
        {"tc_no": f"TC-{n:03d}", "parent_tc": None,
         "name": f"{req['req_no']} rows delivered",
         "description": f"Mart rows exist for the scope of {req['req_no']}"
                        f" ({b}).",
         "category": "completeness", "req_no": req["req_no"],
         "sql": f"SELECT COUNT(*) AS n FROM {mart} {bfilter}",
         "expectation": "ROWS_EXIST — the mart must contain in-scope rows",
         "relevance_check": f"Directly probes delivery of {req['req_no']} scope."},
        {"tc_no": f"TC-{n:03d}.1", "parent_tc": f"TC-{n:03d}",
         "name": f"{req['req_no']} no null keys",
         "description": "Grain keys (brand, geography, month) are never null.",
         "category": "referential_integrity", "req_no": req["req_no"],
         "sql": f"SELECT * FROM {mart} WHERE brand IS NULL OR geography_id IS NULL "
                f"OR month_start IS NULL" + (f" AND brand='{b}'" if b != "ALL" else ""),
         "expectation": "ZERO_ROWS — violations query must return nothing",
         "relevance_check": "A mart with null grain keys cannot satisfy the requirement."},
        {"tc_no": f"TC-{n+1:03d}", "parent_tc": None,
         "name": f"{req['req_no']} actual reconciles to fact",
         "description": "Mart actual_revenue reconciles to fct_revenue_actual.",
         "category": "reconciliation", "req_no": req["req_no"],
         "sql": ("SELECT ROUND(SUM(m.actual_revenue) - "
                 "(SELECT SUM(f.net_revenue) FROM fct_revenue_actual f"
                 + (f" WHERE f.brand='{b}'" if b != "ALL" else "") + "), 2) AS diff "
                 f"FROM {mart} m " + bfilter),
         "expectation": "MATCH_COUNTS — diff must be 0",
         "relevance_check": "Reconciliation to the governed fact proves mart figures."},
        {"tc_no": f"TC-{n+2:03d}", "parent_tc": None,
         "name": f"{req['req_no']} variance formula",
         "description": "variance_amount equals actual - forecast on every row.",
         "category": "variance_accuracy", "req_no": req["req_no"],
         "sql": (f"SELECT * FROM {mart} WHERE ABS(COALESCE(variance_amount,0) - "
                 f"(COALESCE(actual_revenue,0) - COALESCE(forecast_revenue,0))) > 0.01"
                 + (f" AND brand='{b}'" if b != "ALL" else "")),
         "expectation": "ZERO_ROWS — no row may violate the variance formula",
         "relevance_check": "Recomputes the requirement's core variance rule row-by-row."},
    ]
    return cases


async def _s3_one(sid: str, req: Dict[str, Any], trace_row: Dict[str, Any],
                  tc_start: int, cached: str) -> List[Dict[str, Any]]:
    raw = await llm.smart(
        _S3_PROMPT.format(req_no=req["req_no"], title=req["title"],
                          brand=req["brand"], rule=req["business_rule"],
                          subs=_sub_context(req),
                          specs="\n".join(
                              f"  {x.get('id')} [{x.get('sub_req')}, {x.get('status')}] "
                              f"{(x.get('specification') or '')[:220]}"
                              for x in trace_row.get("specs") or [])[:5000] or "  (none)",
                          sttm="\n".join(
                              f"  {x.get('id') or 'derived'}: {x.get('source')} -> "
                              f"{x.get('target')} | {(x.get('transformation') or '')[:140]}"
                              for x in trace_row.get("sttm") or [])[:3500] or "  (none)",
                          trace=json.dumps({k: trace_row.get(k) for k in
                                            ("source_entities", "target_marts",
                                             "transformations", "filters")},
                                           default=str)[:1800],
                          tc_start=f"TC-{tc_start:03d}",
                          refine=_refines(sid, "S3")),
        system=SYSTEM, cached_context=cached, max_tokens=12000, timeout=300.0)
    parsed = llm.parse_json(raw)
    cases = parsed.get("cases", []) if isinstance(parsed, dict) else []
    if not cases:
        # a reply cut off mid-list still holds every case that completed
        cases = llm.salvage_objects(raw, require_key="tc_no")
    good = []
    for c in cases:
        if not c.get("sql") or not c.get("relevance_check"):
            continue
        if sqlexec.guard(c["sql"]):
            continue
        c["req_no"] = req["req_no"]
        good.append(c)
    return good or _s3_heuristic(req, trace_row, tc_start)


def _coverage_heuristic(cases: List[Dict[str, Any]]) -> None:
    base = db.corpus_all_chunks("uat_baseline")
    base_text = "\n".join(ch["text"].lower() for ch in base)
    for c in cases:
        toks = set(re.findall(r"[a-z_]{5,}", (c["name"] + " " + c["description"]).lower()))
        hits = sum(1 for t in toks if t in base_text)
        ratio = hits / max(len(toks), 1)
        if not base_text:
            c["coverage"], c["coverage_ref"] = "NOVEL", "no team baseline supplied"
        elif ratio > 0.55:
            c["coverage"], c["coverage_ref"] = "MATCH", "closely mirrors a baseline case"
        elif ratio > 0.3:
            c["coverage"], c["coverage_ref"] = "EXTENDED", "strengthens a baseline case"
        else:
            c["coverage"], c["coverage_ref"] = "NOVEL", "beyond the team baseline"


async def run_stage3(sid: str, bus: TraceBus) -> Dict[str, Any]:
    reqs = _groups(sid)
    trace_rows = {t["req_no"]: t for t in db.get_design_trace(sid)}
    cached = _cached_context()

    await bus.emit("uat_test_designer", "start",
                   f"designing cases for {len(reqs)} requirements")
    await bus.emit("text2sql_engineer", "start", "schema-grounded SQL generation")
    # stable numbering blocks, wide enough for requirements with many sub-requirements
    starts = [1 + i * 50 for i in range(len(reqs))]
    batches = await llm.gather_bounded([
        _s3_one(sid, r, trace_rows.get(r["req_no"], {}), starts[i], cached)
        for i, r in enumerate(reqs)])
    cases: List[Dict[str, Any]] = []
    for i, b in enumerate(batches):
        cases.extend(b if isinstance(b, list) else
                     _s3_heuristic(reqs[i], trace_rows.get(reqs[i]["req_no"], {}),
                                   starts[i]))
    await bus.emit("text2sql_engineer", "finish", f"{len(cases)} SQLs written")
    await bus.emit("uat_test_designer", "finish",
                   f"{len(cases)} cases (incl. sub-cases) survived relevance self-check")

    await bus.emit("coverage_referee", "start", "classifying vs team baseline")
    _coverage_heuristic(cases)
    n_match = sum(1 for c in cases if c["coverage"] == "MATCH")
    n_ext = sum(1 for c in cases if c["coverage"] == "EXTENDED")
    n_new = sum(1 for c in cases if c["coverage"] == "NOVEL")
    await bus.emit("coverage_referee", "finish",
                   f"{n_match} MATCH · {n_ext} EXTENDED · {n_new} NOVEL")
    db.replace_test_cases(sid, cases)
    return {"cases": cases,
            "coverage": {"match": n_match, "extended": n_ext, "novel": n_new}}


# =========================================================================
# STAGE 4 — Agentic Execution + Explainability
# =========================================================================
def _verdict(expectation: str, result: Dict[str, Any]) -> str:
    if result["error"]:
        return "ERROR"
    exp = (expectation or "").strip().upper()
    n = result["row_count"]
    rows, cols = result["rows"], [c.lower() for c in result["columns"]]
    if exp.startswith("ZERO_ROWS"):
        return "PASS" if n == 0 else "FAIL"
    if exp.startswith("ROWS_EXIST"):
        return "PASS" if n > 0 else "FAIL"
    if exp.startswith("MATCH_COUNTS"):
        if rows and rows[0]:
            for i, c in enumerate(cols):
                if c in ("diff", "variance", "delta"):
                    v = rows[0][i]
                    try:
                        return "PASS" if abs(float(v or 0)) < 0.005 else "FAIL"
                    except (TypeError, ValueError):
                        return "FAIL"
            try:  # single value → must be ~0
                return "PASS" if abs(float(rows[0][0] or 0)) < 0.005 else "FAIL"
            except (TypeError, ValueError):
                return "FAIL"
        return "FAIL"
    m = re.match(r"THRESHOLD\s*<=\s*([\d\.]+)", exp)
    if m and rows and rows[0]:
        try:
            return "PASS" if float(rows[0][0]) <= float(m.group(1)) else "FAIL"
        except (TypeError, ValueError):
            return "FAIL"
    return "PASS" if n == 0 else "FAIL"  # conservative default: violations query


_S4_EXPLAIN_PROMPT = """Test {tc_no} ("{name}") validating {req_no} was executed.
Expectation: {expectation}
Verdict: {verdict} · rows returned: {n} · sample: {sample}
SQL: {sql}

In <=90 words explain WHY the verdict holds and whether the case genuinely
validated its requirement. If FAIL, name the most likely defect locus:
source_data | transformation_code | sttm_mapping | test_itself, and one next
action. Return STRICT JSON:
{{"explanation": "...", "defect_locus": "<locus or none>"}}"""


async def _s4_one(sid: str, case: Dict[str, Any], cached: str,
                  bus: TraceBus) -> Dict[str, Any]:
    result = await asyncio.to_thread(sqlexec.execute, case["sql"], 25)
    verdict = _verdict(case.get("expectation", ""), result)
    sample = result["rows"][:3]
    explanation, locus = "", "none"
    raw = await llm.fast(
        _S4_EXPLAIN_PROMPT.format(tc_no=case["tc_no"], name=case["name"],
                                  req_no=case["req_no"],
                                  expectation=case.get("expectation", ""),
                                  verdict=verdict, n=result["row_count"],
                                  sample=json.dumps(sample, default=str)[:500],
                                  sql=case["sql"][:600]),
        system=SYSTEM, cached_context=cached, max_tokens=260)
    parsed = llm.parse_json(raw)
    if parsed and isinstance(parsed, dict):
        explanation = parsed.get("explanation", "")
        locus = parsed.get("defect_locus", "none")
    if not explanation:
        if verdict == "PASS":
            explanation = (f"Expectation '{case.get('expectation', '')[:60]}' met: "
                           f"{result['row_count']} row(s) observed, consistent with "
                           f"the rule under test.")
        elif verdict == "ERROR":
            explanation = f"Execution error: {result['error']}"
            locus = "test_itself"
        else:
            explanation = (f"Expectation violated: {result['row_count']} row(s) "
                           f"returned; sample {json.dumps(sample, default=str)[:200]}.")
            locus = "transformation_code"
    db.record_execution(sid, case["tc_no"], verdict, result["row_count"],
                        result["elapsed_ms"], sample, explanation, locus)
    await bus.emit("test_executor", "info",
                   f"{case['tc_no']} → {verdict} ({result['elapsed_ms']} ms)")
    return {"tc_no": case["tc_no"], "verdict": verdict,
            "row_count": result["row_count"], "elapsed_ms": result["elapsed_ms"],
            "sample_rows": sample, "explanation": explanation,
            "defect_locus": locus, "error": result["error"]}


async def run_stage4(sid: str, bus: TraceBus) -> Dict[str, Any]:
    cases = [c for c in db.get_test_cases(sid) if c["status"] in ("accepted", "proposed")]
    cached = _cached_context()
    await bus.emit("test_executor", "start",
                   f"executing {len(cases)} accepted cases (read-only, bounded)")
    results = await llm.gather_bounded([_s4_one(sid, c, cached, bus) for c in cases])
    results = [r for r in results if isinstance(r, dict)]
    n_pass = sum(1 for r in results if r["verdict"] == "PASS")
    n_fail = sum(1 for r in results if r["verdict"] == "FAIL")
    n_err = sum(1 for r in results if r["verdict"] == "ERROR")
    await bus.emit("test_executor", "finish",
                   f"{n_pass} PASS · {n_fail} FAIL · {n_err} ERROR")
    await bus.emit("verdict_explainer", "start", "explainability records")
    await bus.emit("verdict_explainer", "finish",
                   f"{len(results)} verdicts explained with defect loci")
    return {"executions": results,
            "scoreboard": {"pass": n_pass, "fail": n_fail, "error": n_err,
                           "total": len(results)}}


RUNNERS = {"P0": run_phase0, "S1": run_stage1, "S2": run_stage2, "S3": run_stage3, "S4": run_stage4}
