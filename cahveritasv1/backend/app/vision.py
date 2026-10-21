"""CAH VERITAS — the image_triage and vision_transcriber agents.

Figures the figure extractor could not rule out go through two model calls:

    image_triage        fast tier, ~200 tokens: is there information here
                        (diagram, table, flow, mock-up, chart, code), or is it
                        decoration (photo, background, logo, divider)?
    vision_transcriber  vision tier: describe, transcribe and structure the
                        figure into text a reader of the corpus can rely on —
                        steps, decision rules, column layouts, codes and
                        parameters, plus every legible label verbatim

Both results are cached by image hash in `figure_transcripts`, so a figure is
paid for once however many documents or re-chunks include it. Nothing here
writes blocks: the next parse of the document picks the transcript up from
the cache and places it where the figure sits (see figures.figure_blocks).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config, db, figures, llm
from .trace import TraceBus

log = logging.getLogger("veritas.vision")

_SYSTEM = ("You read figures embedded in Gilead Commercial Analytics Hub project "
           "documents (BRDs, design documents, source listings, UAT plans) so that "
           "analysts and downstream agents can use their content as text. Be exact: "
           "copy identifiers, codes, table and column names, numbers and parameters "
           "as written. Never invent content that is not legible in the image.")

_TRIAGE = """Figure "{name}" from document "{doc}" ({location}).

Does this image carry information someone analysing the document would need —
a diagram, flowchart, decision tree, data-flow, table, report or screen
mock-up, chart, code or SQL, annotated screenshot — or is it decorative
(photo, background art, logo, icon, divider, stock illustration)?

Return STRICT JSON only:
{{"informative": true|false,
  "category": "flow_diagram|decision_tree|data_flow|table|report_mockup|chart|code|screenshot|photo|decorative|logo|other",
  "reason": "<= 20 words"}}"""

_TRANSCRIBE = """Figure "{name}" from document "{doc}" ({location}).
Triage: {category}.

Transcribe this figure into structured text. Return STRICT JSON only:
{{
  "title": "the figure's own title, or a short descriptive one",
  "category": "{category}",
  "summary": "3-6 sentences: what the figure shows and why it matters",
  "structure": [
    {{"type": "step|decision|rule|flow|mapping|column|group|control|metric|note",
      "text": "one self-contained statement"}}
  ],
  "entities": {{"tables": [], "columns": [], "codes": [], "parameters": [],
               "products": [], "metrics": []}},
  "text_verbatim": ["every legible label or line of text, in reading order"]
}}

Guidance for "structure":
- Flow / data-flow diagrams: one "step" per box in order, naming inputs and
  outputs; one "flow" per arrow that is not obvious from the step order.
- Decision trees: one "decision" per question with each branch and where it
  leads; one "rule" per terminal outcome stating the full condition.
- Tables and report mock-ups: one "column" per column header (with its group,
  e.g. "Biktarvy > TRx Bottles Growth"), "control" for buttons, filters and
  drop-downs, "metric" for KPI tiles. Do NOT transcribe data rows beyond two
  example rows — capture the layout, not the sample data.
- Charts: axes, series and the notable values.
Keep identifiers exactly as written (e.g. FACT_RX_CLAM_HIV, NDC 61958300101,
BR1285)."""


def _context(fig: Dict[str, Any], doc: str) -> Dict[str, str]:
    return {"name": fig["name"], "doc": doc, "location": fig.get("location") or "document"}


async def triage(fig: Dict[str, Any], doc: str) -> Optional[Dict[str, Any]]:
    raw = await llm.vision(_TRIAGE.format(**_context(fig, doc)), fig["data"], fig["media_type"],
                           model=config.MODEL_FAST, system=_SYSTEM, max_tokens=200,
                           timeout=120.0)
    parsed = llm.parse_json(raw)
    return parsed if isinstance(parsed, dict) and "informative" in parsed else None


async def transcribe(fig: Dict[str, Any], doc: str, category: str) -> Optional[Dict[str, Any]]:
    raw = await llm.vision(_TRANSCRIBE.format(category=category, **_context(fig, doc)),
                           fig["data"], fig["media_type"], model=config.MODEL_VISION,
                           system=_SYSTEM, max_tokens=16000)
    parsed = llm.parse_json(raw)
    if isinstance(parsed, dict) and (parsed.get("summary") or parsed.get("structure")):
        return parsed
    return None


async def _process(fig: Dict[str, Any], doc: str, bus: Optional[TraceBus]) -> str:
    """Triage then (if informative) transcribe one figure; cache the verdict."""
    async def emit(agent: str, event: str, detail: str) -> None:
        if bus:
            await bus.emit(agent, event, detail)

    await emit("image_triage", "start", f"{fig['name']} ({fig['width']}x{fig['height']})")
    t = await triage(fig, doc)
    if t is None:
        await emit("image_triage", "error", f"{fig['name']}: no triage result — left pending")
        return "failed"
    category = str(t.get("category") or "other")
    if not t.get("informative"):
        db.put_figure(fig["sha"], "decorative", category, "", {"reason": t.get("reason")},
                      config.MODEL_FAST)
        await emit("image_triage", "finish", f"{fig['name']}: {category} — skipped")
        return "decorative"
    await emit("image_triage", "finish", f"{fig['name']}: {category} — informative")

    await emit("vision_transcriber", "start", f"transcribing {fig['name']} ({category})")
    r = await transcribe(fig, doc, category)
    if r is None:
        await emit("vision_transcriber", "error", f"{fig['name']}: transcription failed — left pending")
        return "failed"
    db.put_figure(fig["sha"], "transcribed", str(r.get("category") or category),
                  str(r.get("title") or ""), r, config.MODEL_VISION)
    await emit("vision_transcriber", "finish",
               f"{fig['name']}: \"{r.get('title', '')}\" — {len(r.get('structure') or [])} "
               f"statements, {len(r.get('text_verbatim') or [])} text lines")
    return "transcribed"


async def enrich_document(path: Path, bus: Optional[TraceBus] = None) -> Dict[str, Any]:
    """Run the vision agents over every pending figure of one document."""
    figs = await asyncio.to_thread(figures.document_figures, Path(path))
    pending = [f for f in figures.dedupe(figs) if f["verdict"] in (None, "failed")]
    result: Dict[str, Any] = {"figures": figures.status(figs), "processed": {}}
    if not pending:
        return result
    if not llm.vision_available():
        if bus:
            await bus.emit("image_triage", "info",
                           f"{Path(path).name}: {len(pending)} figure(s) need a vision model "
                           "(set ANTHROPIC_API_KEY) — left pending")
        return result
    outcomes = await asyncio.gather(*[_process(f, Path(path).name, bus) for f in pending],
                                    return_exceptions=True)
    for f, o in zip(pending, outcomes):
        if isinstance(o, Exception):
            log.exception("vision processing failed for %s", f["name"], exc_info=o)
            o = "failed"
        result["processed"][f["name"]] = o
    result["figures"] = figures.status(
        await asyncio.to_thread(figures.document_figures, Path(path)))
    return result


def describe(path: Path) -> List[Dict[str, Any]]:
    """Per-figure view for the Analyze panel."""
    out = []
    for f in figures.dedupe(figures.document_figures(Path(path))):
        t = f.get("transcript") or {}
        out.append({"name": f["name"], "location": f["location"],
                    "size": f"{f['width']}x{f['height']}" if f["width"] else "",
                    "verdict": f["verdict"] or "pending",
                    "category": t.get("category") or "",
                    "title": t.get("title") or ""})
    return out
