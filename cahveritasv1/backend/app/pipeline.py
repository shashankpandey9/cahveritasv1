"""CAH VERITAS — orchestration.

The staged conversation machine + the AAOSA protocol driver.

AAOSA: when the `neuro-san` package is installed the registered network
(registries/cah_veritas.hocon) is served through DirectAgentSession. When it
is not (locked-down laptops, Databricks Apps images), the built-in
**AAOSA-lite** driver runs the same protocol: the front man polls its
down-chain agents in `Determine` mode (fast tier, tiny token budgets, fired
concurrently), selects by Relevant/Strength, then invokes the winners in
`Fulfill` mode — which lands in stages.py where the grounded work happens.

Stage machine (per session):
    S0 ──begin──► S1 ──► S1_review ──accept──► S2 ──► S2_review ──accept──►
    S3 ──► S3_review ──accept──► S4_confirm ──confirm──► S4 ──► done
Any *_review gate also takes `refine` + note: the note is persisted and the
stage re-runs with the critique injected into every agent prompt.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from . import config, db, discovery, kb, llm, stages
from .trace import TraceBus

log = logging.getLogger("veritas.pipeline")

STAGE_TITLES = {
    "P0": "Phase 0 — Ingestion & Corpus Preparation",
    "S1": "Stage 1 — Requirements Discovery & Analysis",
    "S2": "Stage 2 — Design & Environment Understanding",
    "S3": "Stage 3 — UAT Test Case Generation",
    "S4": "Stage 4 — Agentic Test Execution",
}
REVIEW_OF = {"P0": "P0_review", "S1": "S1_review", "S2": "S2_review", "S3": "S3_review", "S4": "done"}
NEXT_STAGE = {"P0_review": "S1", "S1_review": "S2", "S2_review": "S3", "S3_review": "S4_confirm"}

# Agents polled in Determine mode per stage (AAOSA-lite routing table — the
# same down-chain sets declared in the hocon registry).
DOWNCHAIN = {
    "P0": ["intake_librarian", "office_structure_extractor", "figure_extractor",
           "image_triage", "vision_transcriber", "corpus_chunker"],
    "S1": ["intake_librarian", "corpus_chunker", "requirements_enumerator",
           "requirements_rag_analyst", "product_steward"],
    "S2": ["design_navigator", "sttm_mapper", "data_environment_scout", "trace_auditor"],
    "S3": ["uat_test_designer", "text2sql_engineer", "coverage_referee"],
    "S4": ["test_executor", "verdict_explainer"],
}

_DETERMINE_PROMPT = """You are agent "{agent}" in the CAH VERITAS AAOSA network.
Inquiry: {inquiry}
Mode: Determine
Return STRICT JSON only:
{{"Name": "{agent}", "Inquiry": "...", "Mode": "Determine",
  "Relevant": "Yes|No", "Strength": <1-10>}}"""


async def _determine(agent: str, inquiry: str) -> Tuple[str, bool, int]:
    """AAOSA Determine poll — fast tier, 120-token budget."""
    raw = await llm.fast(_DETERMINE_PROMPT.format(agent=agent, inquiry=inquiry[:400]),
                         max_tokens=120)
    parsed = llm.parse_json(raw)
    if isinstance(parsed, dict):
        return (agent, str(parsed.get("Relevant", "Yes")).lower().startswith("y"),
                int(parsed.get("Strength", 5) or 5))
    return (agent, True, 5)  # offline: every registered agent is presumed relevant


async def aaosa_route(stage: str, inquiry: str, bus: TraceBus) -> List[str]:
    agents = DOWNCHAIN[stage]
    await bus.emit("veritas_advisor", "start",
                   f"AAOSA Determine poll → {', '.join(agents)}")
    votes = await asyncio.gather(*[_determine(a, inquiry) for a in agents])
    chosen = [a for a, rel, s in votes if rel and s >= 3] or agents
    await bus.emit("veritas_advisor", "finish",
                   "delegating (Fulfill) → " + ", ".join(chosen))
    return chosen


def _neuro_san_session():
    """Optional full neuro-san runtime."""
    try:  # pragma: no cover
        from neuro_san.session.direct_agent_session import DirectAgentSession
        return DirectAgentSession(registry_path=str(config.REGISTRY),
                                  agent_name="veritas_advisor")
    except Exception:
        return None


async def run_stage(sid: str, stage: str, bus: TraceBus,
                    refine_note: str = "") -> Dict[str, Any]:
    rid = db.start_run(sid, stage, refine_note)
    inquiry = f"{STAGE_TITLES[stage]} for session {sid[:8]}"
    if refine_note:
        inquiry += f" — user refinement: {refine_note}"
    try:
        await aaosa_route(stage, inquiry, bus)          # AAOSA Determine phase
        payload = await stages.RUNNERS[stage](sid, bus)  # Fulfill phase (grounded)
        db.finish_run(rid, "succeeded", bus.timings())
        db.save_run_events(rid, bus.snapshot())
        db.set_stage(sid, REVIEW_OF[stage], payload)
        return payload
    except Exception as e:
        log.exception("stage %s failed", stage)
        await bus.emit("veritas_advisor", "error", str(e))
        db.finish_run(rid, "failed", bus.timings())
        db.save_run_events(rid, bus.snapshot())
        db.set_stage(sid, f"{stage}_review")
        return {"error": str(e)}


# ------------------------------------------------------------- chat surface
def stage_summary_md(stage: str, payload: Dict[str, Any]) -> str:
    """Chat-facing markdown summary of a finished stage (panels carry detail)."""
    if payload.get("error"):
        return f"⚠️ {STAGE_TITLES[stage]} hit an error: `{payload['error']}` — you can retry or refine."
    if stage == "P0":
        audit = payload.get("audit", {})
        ready = payload.get("ready")
        failed = payload.get("failed") or []
        return (f"**{STAGE_TITLES[stage]} complete.** Discovered **{audit.get('discovered', 0)} documents** "
                f"({audit.get('supported', 0)} ingestible); **{payload.get('chunked_now', 0)}** were chunked "
                f"in this run. **{audit.get('complete', 0)} fully chunked**, {audit.get('partial', 0)} partial, "
                f"{audit.get('not_chunked', 0)} not chunked — **{payload.get('chunks', 0)} chunks** indexed."
                + (f" {len(failed)} document(s) produced no content." if failed else "")
                + "\n\nThe requirements document is "
                + ("**fully chunked** — review the **0 · Document Discovery** tab, then **Accept** to "
                   "proceed to Phase 1." if ready else
                   "**not fully chunked** — Phase 1 needs it complete. Use **Analyze** / **Chunk** "
                   "in the Document Discovery tab, or **Refine** to re-run Phase 0."))
    if stage == "S1":
        s = payload.get("stats", {})
        n_top = s.get("requirements", 0)
        n_sub = s.get("sub_requirements", 0)
        prods = ", ".join(f"{k} ({v})" for k, v in
                          list(s.get("by_product", {}).items())[:5])
        lines = [
            f"**{STAGE_TITLES[stage]} complete.** Enumerated **R1–R{n_top}** "
            f"({n_top} requirements) with **{n_sub} sub-requirements** "
            f"(R#.i, R#.ii, …) — **{s.get('total', 0)} records** in total.",
            "",
            f"- **Normative obligations:** {s.get('normative', 0)}  ·  "
            f"**notes:** {s.get('notes', 0)}",
            f"- **Open / TBD in the source:** {s.get('open_items', 0)}  ·  "
            f"**Phase 2 scope:** {s.get('phase_2', 0)}",
            f"- **Product attribution:** {prods or 'n/a'}",
            f"- **RAG-enriched records:** {s.get('rag_enriched', 0)} of "
            f"{s.get('total', 0)} (the remainder use the deterministic analyst)",
            "",
            "Each record carries its identifier, the **verbatim extracted text**, "
            "a plain-language summary, a business rule, and Gilead product "
            "metadata (branded name · molecule · shorthand · full product name). "
            "Review them in the Requirements panel.",
            "",
            "**Accept** to proceed to design understanding, or **Refine** with "
            "your critique.",
        ]
        return "\n".join(lines)
    if stage == "S2":
        s = payload.get("summary", {})
        return (f"**{STAGE_TITLES[stage]} complete.** **{s.get('specs', 0)} design specs** "
                f"(one or more per sub-requirement) and **{s.get('sttm', 0)} STTM rows** "
                f"linked to requirements. Traceability: **{s.get('traced', 0)} TRACED**, "
                f"{s.get('partial', 0)} PARTIAL, {s.get('untraced', 0)} UNTRACED"
                + (f" ({s['heuristic']} fell back to the heuristic tracer — Re-trace them "
                   f"in the Design panel)" if s.get("heuristic") else "")
                + ". Review each requirement in the **2 · Design Trace** tab — specs, STTM, "
                  "entities, joins, filters, code — give feedback or **Re-trace** any one."
                  "\n\n**Accept** to generate UAT, or **Refine**.")
    if stage == "S3":
        cov = payload.get("coverage", {})
        n = len(payload.get("cases", []))
        return (f"**{STAGE_TITLES[stage]} complete.** **{n} test cases** generated "
                f"(with sub-cases), each with description, SQL, expectation and a "
                f"relevance self-check. Coverage vs your team baseline: "
                f"**{cov.get('match', 0)} MATCH · {cov.get('extended', 0)} EXTENDED · "
                f"{cov.get('novel', 0)} NOVEL** — the NOVEL set is what agentic "
                f"enumeration found beyond the team.\n\n**Accept** to arm execution, "
                f"or **Refine**.")
    if stage == "S4":
        sb = payload.get("scoreboard", {})
        return (f"**{STAGE_TITLES[stage]} complete.** "
                f"**{sb.get('pass', 0)} PASS · {sb.get('fail', 0)} FAIL · "
                f"{sb.get('error', 0)} ERROR** of {sb.get('total', 0)} executed. Every "
                f"verdict carries explainability and, for failures, a defect locus "
                f"(source data / transformation code / STTM / test). See the "
                f"Execution panel; give feedback to close the loop into the KB.")
    return "Stage complete."


def gate_action(sid: str, action: str, comment: str = "") -> Dict[str, Any]:
    """Handle accept / refine / confirm_execute at the current gate.
    Returns {next_stage_to_run | None, message}."""
    sess = db.get_session(sid)
    if not sess:
        return {"error": "unknown session"}
    stage = sess["stage"]

    if action == "accept" and stage == "P0_review":
        audit = discovery.summary()
        if not audit.get("required_ready"):
            pending = [r["name"] for r in audit.get("required", []) if not r["complete"]]
            return {"run": None, "message":
                    "Phase 0 cannot be accepted yet — the requirements document must be "
                    "fully chunked first"
                    + (f" (not complete: {', '.join(pending)})" if pending
                       else " (no BRD was discovered under inputs/brd/)")
                    + ". Chunk it from the **0 · Document Discovery** tab and accept again."}

    if action == "accept" and stage in ("P0_review", "S1_review", "S2_review", "S3_review"):
        base = "P0" if stage == "P0_review" else stage[:2]
        db.record_feedback(sid, base, base, "positive", "accept", comment)
        if base == "S1":
            db.mark_requirements(sid, "accepted")
        if base == "S3":
            db.mark_test_cases(sid, "accepted")
        path = kb.write_for_stage(sid, base) if base != "P0" else "phase0_audit"
        nxt = NEXT_STAGE[stage]
        if nxt == "S4_confirm":
            db.set_stage(sid, "S4_confirm")
            n = len(db.get_test_cases(sid))
            return {"run": None, "message":
                    f"UAT set accepted and written to the knowledge base "
                    f"(`{path}`). **Execute {n} test cases against the analytics "
                    f"store now?** Reply **Execute** to confirm, or **Refine** to "
                    f"go back."}
        db.set_stage(sid, nxt)
        return {"run": nxt, "message":
                f"Accepted — {STAGE_TITLES[base]} completed. Proceeding to "
                f"{STAGE_TITLES[nxt]}…"}

    if action == "refine" and stage in ("P0_review", "S1_review", "S2_review", "S3_review",
                                        "S4_confirm"):
        base = "P0" if stage == "P0_review" else ("S3" if stage == "S4_confirm" else stage[:2])
        db.record_feedback(sid, base, base, "negative", "refine", comment)
        db.add_refine_note(sid, base, comment or "user requested refinement")
        db.set_stage(sid, base)
        return {"run": base, "message":
                f"Refining {STAGE_TITLES[base]} with your feedback — re-running "
                f"the agent network…"}

    if action == "confirm_execute" and stage == "S4_confirm":
        db.record_feedback(sid, "S4", "execution", "positive", "confirm", comment)
        db.set_stage(sid, "S4")
        return {"run": "S4", "message":
                "Execution confirmed — the test executor is running the accepted "
                "cases (read-only, bounded)…"}

    return {"error": f"action '{action}' is not valid at gate '{stage}'"}


BEGIN_WORDS = ("begin", "start", "review the materials", "review materials",
               "kick off", "go", "begin phase 0", "start phase 0",
               "initiate phase 0")
EXEC_WORDS = ("execute", "run the tests", "run tests", "yes")
ACCEPT_WORDS = ("accept", "approved", "looks good", "lgtm")
REFINE_WORDS = ("refine", "revise", "redo", "not right", "change")


def interpret_message(sid: str, text: str) -> Dict[str, Any]:
    """Map a free-text user message onto the stage machine (the UI buttons call
    /gate directly; this keeps the chat box equally in control)."""
    sess = db.get_session(sid) or {}
    stage = sess.get("stage", "S0")
    low = text.strip().lower()
    if stage == "S0" and any(w in low for w in BEGIN_WORDS):
        db.set_stage(sid, "P0")
        return {"run": "P0",
                "message": "Beginning Phase 0 ingestion — reviewing sources, "
                           "planning the corpus strategy, and chunking available "
                           "artifacts before Phase 1 requirements discovery…"}
    if stage == "P0_review" and ("continue" in low or "proceed" in low or "next phase" in low or "phase 1" in low):
        return gate_action(sid, "accept", text)
    if stage == "P0_review" and ("missing" in low or "gap" in low or "ingest" in low):
        return {"run": "P0",
                "message": "Routing to the intake librarian and corpus chunker to keep ingesting only the missing corpus gaps without clearing the DB."}
    if stage == "S4_confirm" and any(w in low for w in EXEC_WORDS):
        return gate_action(sid, "confirm_execute", text)
    if stage.endswith("_review"):
        if any(w in low for w in ACCEPT_WORDS):
            return gate_action(sid, "accept", text)
        if any(w in low for w in REFINE_WORDS):
            return gate_action(sid, "refine", text)
    if any(w in low for w in ("ingest gap", "missing chunks", "continue phase 0", "fill gaps", "gap sections")):
        return {"run": "P0",
                "message": "Routing to Phase 0 gap-fill — the intake librarian and corpus chunker will ingest only the missing sections without discarding the already indexed corpus."}
    return {"run": None, "message": None}  # falls through to advisor Q&A


_QA_PROMPT = """User question (session stage: {stage}): {question}

Answer as the VERITAS front man using ONLY the context below. Cite
[document :: section]. If the answer is not in context, say what is missing
and which stage would produce it. <=180 words.

CONTEXT:
{context}"""


async def advisor_answer(sid: str, question: str) -> str:
    from . import embeddings
    sess = db.get_session(sid) or {}
    ctx = embeddings.context_block(question, top_k=6, char_budget=5200)
    raw = await llm.smart(_QA_PROMPT.format(stage=sess.get("stage", "S0"),
                                            question=question, context=ctx),
                          system=stages.SYSTEM, max_tokens=600)
    if raw:
        return raw
    hits = embeddings.search(question, top_k=3)
    if not hits:
        return ("I could not ground an answer in the ingested corpus. Add the "
                "relevant document under inputs/ and re-ingest, or begin the "
                "staged review.")
    lines = [f"Closest corpus passages (heuristic mode — no LLM configured):"]
    for h in hits:
        lines.append(f"- **{h['doc_name']} :: {h['section']}** — "
                     f"{h['text'][:220].strip()}…")
    return "\n".join(lines)
