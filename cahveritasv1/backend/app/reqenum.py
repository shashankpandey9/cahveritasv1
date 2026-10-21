"""CAH VERITAS — requirement enumeration.

Two agents, deliberately split, because they fail in opposite directions.

``requirements_enumerator`` (structural, deterministic)
    Walks the BRD block stream and emits the register skeleton: every
    content-bearing section becomes **R1, R2, R3 …**, and every bullet,
    paragraph and table row inside it becomes **R1.i, R1.ii, R1.iii …**.
    It never summarizes and never judges — it only finds and numbers, and it
    carries each requirement's *verbatim* source text. Completeness is a
    traversal property here, not a recall property: an LLM asked for "all the
    requirements" silently returns the ones it found room for, which is how a
    30-requirement BRD becomes a 1-requirement register.

``requirements_rag_analyst`` (semantic, retrieval-grounded)
    Takes each R# and, for the sections around it, fetches document parts
    through the embedded chunk index, then writes the plain-language summary,
    the business rule and the product attribution. It may reason freely — it
    cannot change the count, because the skeleton is fixed before it runs.

Everything the analyst produces has a deterministic fallback, so the full
register (text, rules, product metadata and all) is still produced when no
API key is configured.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from . import db, docparse, embeddings, llm, products
from .trace import TraceBus

log = logging.getLogger("veritas.reqenum")

# ---------------------------------------------------------------- language
_MODAL = re.compile(
    r"\b(shall|must|will|should|to be|needs? to|required to|is required|"
    r"has to|have to|are to|is to)\b", re.IGNORECASE)
_IMPERATIVE = re.compile(
    r"^\s*(configure|add|include|update|create|ensure|rename|apply|track|"
    r"integrate|split|exclude|aggregate|rank|divide|remove|populate|map|load|"
    r"flag|provide|generate|report|calculate|restrict|filter|incorporate|"
    r"maintain|assign|build|extend|enable|support|deliver)\b", re.IGNORECASE)
_NEGATIVE = re.compile(
    r"^\s*(no\s+(additional|change|modification|new|modifications)|"
    r"there\s+(is|are)\s+no\s+change)", re.IGNORECASE)
_NOTE = re.compile(r"^\s*(note|important|caveat)\s*[:\-]", re.IGNORECASE)
# Sections that frame the programme rather than oblige a build. Their prose
# is full of "will" and would otherwise read as normative. SCOPE is *not*
# here: its bullets are the build list.
_CONTEXT_SECTION = re.compile(
    r"^\s*(purpose|objective|purpose\s*&?\s*objective[s]?|background|"
    r"introduction|overview|executive summary|assumptions)\s*$", re.IGNORECASE)
_LEAD_IN = re.compile(r":\s*$")
_PHASE = re.compile(r"\bphase\s*([12])\b", re.IGNORECASE)
_TBD = re.compile(r"\bTBD\b")

_ROMAN = ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"]


def roman(n: int) -> str:
    """1 -> 'i' … lowercase roman numerals for sub-requirement ids."""
    if 1 <= n <= len(_ROMAN):
        return _ROMAN[n - 1]
    vals = [(1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"),
            (90, "xc"), (50, "l"), (40, "xl"), (10, "x"), (9, "ix"),
            (5, "v"), (4, "iv"), (1, "i")]
    out = []
    for v, s in vals:
        while n >= v:
            out.append(s)
            n -= v
    return "".join(out)


def _first_sentences(text: str, n: int = 2, limit: int = 320) -> str:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", (text or "").strip())
    out = " ".join(parts[:n]).strip()
    return (out[:limit].rsplit(" ", 1)[0] + "…") if len(out) > limit else out


def _title_of(text: str, limit: int = 110) -> str:
    t = docparse.strip_inline_number((text or "").strip())
    t = re.split(r"(?<=[.;:])\s", t)[0].strip(" .;:")
    if len(t) > limit:
        t = t[:limit].rsplit(" ", 1)[0] + "…"
    return t or "(untitled)"


def _phase_of(*texts: str) -> str:
    blob = " ".join(t for t in texts if t)
    m = _PHASE.search(blob)
    if m:
        return "Phase " + m.group(1)
    return "Phase 1"


def _is_normative(text: str, kind: str) -> bool:
    if kind == "table_row":
        return True
    t = (text or "").strip()
    if not t:
        return False
    return bool(_MODAL.search(t) or _IMPERATIVE.match(t) or _NEGATIVE.match(t))


def _classify(text: str, kind: str, section: str = "") -> str:
    if _NOTE.match(text or ""):
        return "note"
    if section and _CONTEXT_SECTION.match(docparse.heading_title(section)):
        return "context"
    if _is_normative(text, kind):
        return "requirement"
    return "context"


# =========================================================================
# AGENT 1 — structural enumeration (deterministic, exhaustive)
# =========================================================================
def enumerate_structural(blocks: List[Dict[str, Any]], doc_name: str
                         ) -> List[Dict[str, Any]]:
    """The complete R#/R#.i register skeleton for one document.

    A section with direct content becomes an R#. Container headings that only
    introduce sub-headings ("2. BUSINESS REQUIREMENTS") carry no obligations
    of their own, so they stay in ``section_path`` as ancestry rather than
    becoming empty requirements.
    """
    # Group content blocks under the heading that owns them.
    sections: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for b in blocks:
        # Figure transcripts (vision agents) are evidence *about* a section,
        # not obligations stated by the author: they are retrievable through
        # the chunks but never enumerated as requirements.
        if b.get("kind") in ("figure", "figure_title"):
            continue
        if b.get("kind") == "heading":
            current = {"heading": b.get("text", ""),
                       "section_path": b.get("section_path", ""),
                       "heading_level": b.get("heading_level", 1),
                       "blocks": [], "columns": []}
            sections.append(current)
            continue
        if current is None:
            current = {"heading": b.get("section") or doc_name,
                       "section_path": b.get("section_path") or doc_name,
                       "heading_level": b.get("heading_level", 0),
                       "blocks": [], "columns": []}
            sections.append(current)
        if b.get("kind") == "table_header":
            current["columns"].append(b.get("text", ""))
            continue
        if (b.get("text") or "").strip():
            current["blocks"].append(b)

    register: List[Dict[str, Any]] = []
    r_no = 0
    for sec in sections:
        if not sec["blocks"]:
            continue                                  # container heading
        if docparse.is_non_requirement_section(sec["heading"]):
            continue                                  # revision history, approvals
        r_no += 1
        req_id = "R" + str(r_no)
        sec_no = docparse.heading_number(sec["heading"])
        sec_title = docparse.heading_title(sec["heading"])

        subs: List[Dict[str, Any]] = []
        lead_in = ""
        sub_n = 0
        for b in sec["blocks"]:
            text = (b.get("text") or "").strip()
            kind = b.get("kind", "para")
            # A short line ending in ':' introduces the bullets beneath it
            # ("NDC:", "Decile Calculation:", "Business Rule Hierarchy") — it
            # is scope for what follows, not an obligation of its own.
            if _LEAD_IN.search(text) and len(text) < 80 and kind != "table_row":
                lead_in = text.rstrip(":").strip()
                continue
            sub_n += 1
            sub_id = req_id + "." + roman(sub_n)
            req_kind = _classify(text, kind, sec["heading"])
            subs.append({
                "req_id": sub_id,
                "parent_req": req_id,
                "level": 2,
                "title": _title_of(text),
                "raw_text": text,
                "context_lead_in": lead_in,
                "block_kind": kind,
                "list_level": b.get("list_level"),
                "section_no": sec_no,
                "section_title": sec_title,
                "section_path": sec["section_path"],
                "doc_name": doc_name,
                "block_start": b.get("ord", 0),
                "block_end": b.get("ord", 0),
                "req_kind": req_kind,
                "normative": req_kind == "requirement",
                "phase": _phase_of(text, sec["heading"]),
                "open_item": bool(_TBD.search(text) or _TBD.search(sec["heading"])),
            })

        raw_parent = "\n".join(
            ([sec["heading"]] if sec["heading"] else [])
            + (["(columns) " + c for c in sec["columns"]])
            + [s["raw_text"] for s in subs])
        register.append({
            "req_id": req_id,
            "parent_req": None,
            "level": 1,
            "title": sec_title or _title_of(subs[0]["raw_text"] if subs else doc_name),
            "raw_text": raw_parent,
            "context_lead_in": "",
            "block_kind": "section",
            "list_level": None,
            "section_no": sec_no,
            "section_title": sec_title,
            "section_path": sec["section_path"],
            "doc_name": doc_name,
            "block_start": sec["blocks"][0].get("ord", 0),
            "block_end": sec["blocks"][-1].get("ord", 0),
            "table_columns": sec["columns"],
            "req_kind": ("requirement"
                         if any(s["normative"] for s in subs) else "context"),
            "normative": any(s["normative"] for s in subs),
            "phase": _phase_of(sec["heading"], raw_parent[:400]),
            "open_item": bool(_TBD.search(sec["heading"])),
            "sub_count": len(subs),
        })
        register.extend(subs)
    return register


def source_ref(req: Dict[str, Any]) -> str:
    sec = req.get("section_no") or ""
    title = req.get("section_title") or req.get("section_path") or ""
    label = (sec + " " + title).strip() if sec else title
    span = req.get("block_start")
    end = req.get("block_end")
    blocks = ("blocks " + str(span) + "–" + str(end)) if span != end else ("block " + str(span))
    return req.get("doc_name", "BRD") + " :: " + label + " (" + blocks + ")"


# =========================================================================
# AGENT 2 — RAG enrichment (retrieval-grounded semantics)
# =========================================================================
_ENRICH_PROMPT = """You are the requirements RAG analyst for CAH VERITAS.

A structural pass has ALREADY enumerated the register. Your job is to explain
each item — never to add, drop, merge, split or renumber one. Return exactly
one object per input id, with the same id.

REQUIREMENT GROUP {req_id} — "{title}"
Document section: {section}
Phase marker: {phase}

ITEMS (verbatim from the BRD; `context` is the lead-in line above the item):
{items}

RETRIEVED DOCUMENT CONTEXT (nearest embedded chunks — use for surrounding
scope, definitions and cross-references; cite as [document :: section]):
{context}

For every id produce:
  plain_language      1-2 sentences a business user would sign. Expand the
                      document's shorthand (B/L, BVY, tBIC, SUN, LEN, XPO,
                      DDD, TRx, NBRx, R4W) into words on first use. No jargon
                      the reader must already know.
  business_description  what this means for Commercial Analytics — which
                      layer, fact, denorm, report, extract or KPI it touches,
                      and why the business needs it.
  business_rule       ONE testable rule, phrased so a UAT case could assert
                      it. Start with the subject. Name the exact fields,
                      flags, markets, NDCs, windows or thresholds stated in
                      the source. If the source is genuinely undecided (TBD),
                      say what must be decided and mark it OPEN.
  features            list of design features/build items it implies.
  data_objects        list of tables, facts, denorms, reports, extracts, KPIs
                      or files named or clearly implied.
  products            Gilead products named or implied, by BRANDED name when
                      one exists, else the shorthand (e.g. "B/L").

Do not invent numbers, NDCs, percentages or field names that are not in the
verbatim text or the retrieved context.

Be compact: keep plain_language and business_description to at most 45 words
each, business_rule to at most 55, and each list to at most 5 short entries.
Emit the JSON and nothing else — no preamble, no commentary, no code fence.

Return STRICT JSON:
{{"items": [{{"req_id": "...", "plain_language": "...",
  "business_description": "...", "business_rule": "...",
  "features": ["..."], "data_objects": ["..."], "products": ["..."]}}]}}
{refine}"""

# The analyst writes roughly 350-450 output tokens per record. Batch size and
# token budget are set from that measurement, with headroom: a batch that
# overruns its budget returns severed JSON, and every record in it falls back
# to the deterministic analyst.
BATCH_SIZE = 5
TOKENS_PER_ITEM = 520
TOKENS_BASE = 900
RETRIEVE_K = 6


def _retrieval_query(parent: Dict[str, Any], subs: List[Dict[str, Any]]) -> str:
    bits = [parent.get("section_title") or "", parent.get("section_path") or ""]
    bits += [s.get("raw_text", "")[:160] for s in subs[:6]]
    return " ".join(b for b in bits if b)[:900]


def _items_blob(reqs: List[Dict[str, Any]]) -> str:
    lines = []
    for r in reqs:
        ctx = r.get("context_lead_in") or ""
        lines.append(json.dumps({
            "req_id": r["req_id"],
            "kind": r.get("req_kind"),
            "context": ctx,
            "text": r.get("raw_text", "")[:900],
        }, ensure_ascii=False))
    return "\n".join(lines)


async def _enrich_batch(parent: Dict[str, Any], batch: List[Dict[str, Any]],
                        context: str, refine: str, cached: str
                        ) -> Dict[str, Dict[str, Any]]:
    raw = await llm.smart(
        _ENRICH_PROMPT.format(
            req_id=parent["req_id"], title=parent.get("title", ""),
            section=parent.get("section_path", ""), phase=parent.get("phase", ""),
            items=_items_blob(batch), context=context, refine=refine),
        system=SYSTEM_ANALYST, cached_context=cached,
        max_tokens=TOKENS_BASE + TOKENS_PER_ITEM * len(batch))
    out: Dict[str, Dict[str, Any]] = {}
    parsed = llm.parse_json(raw)
    items: List[Any] = []
    if isinstance(parsed, dict):
        items = parsed.get("items", []) or []
    if not items:
        # Truncated or fenced oddly — keep whatever objects did complete.
        items = llm.salvage_objects(raw, require_key="req_id")
    for item in items:
        if isinstance(item, dict) and item.get("req_id"):
            out[str(item["req_id"]).strip()] = item
    return out


SYSTEM_ANALYST = (
    "You are the requirements RAG analyst in CAH VERITAS, the Gilead "
    "Commercial Analytics Hub (CAH) launch-validation network. You explain "
    "requirements that a structural pass has already enumerated; you never "
    "change the enumeration. Ground every statement in the verbatim text and "
    "the retrieved chunks — never in prior knowledge about Gilead products. "
    "Emit STRICT JSON when asked for JSON, with no prose around it.")


# ------------------------------------------------------- heuristic fallback
_SHORTHAND = {
    "TRx": "total prescriptions (TRx)", "NBRx": "new-to-brand prescriptions (NBRx)",
    "NRx": "new prescriptions (NRx)", "R4W": "rolling 4 weeks (R4W)",
    "R13W": "rolling 13 weeks (R13W)", "R6M": "rolling 6 months (R6M)",
    "R12M": "rolling 12 months (R12M)", "QTD": "quarter to date (QTD)",
    "PQ": "prior quarter (PQ)", "YTD": "year to date (YTD)",
    "XPO": "retail prescription data (XPO)", "DDD": "non-retail shipment data (DDD)",
    "XPD": "prescription detail data (XPD)", "NPA": "national prescription audit (NPA)",
    "SOB": "source of business (SOB)", "OOP": "out-of-pocket cost (OOP)",
    "HCP": "healthcare professional (HCP)", "APO": "Analytics Planning & Operations (APO)",
    "IIA": "Integrated Insights & Analytics (IIA)", "IC": "Incentive Compensation (IC)",
    "CL": "Clinical Liaison (CL)", "VSCR": "virologically suppressed / care-retained (VSCR)",
    "LCH": "Launch Control Hub (LCH)", "EID": "employee identifier (EID)",
    "HTE": "heavily treatment-experienced (HTE)", "ADAP": "AIDS Drug Assistance Program (ADAP)",
}
_SHORTHAND_RE = re.compile(
    r"(?<![A-Za-z0-9])(" + "|".join(sorted(_SHORTHAND, key=len, reverse=True)) + r")(?![A-Za-z0-9])")


def expand_shorthand(text: str) -> str:
    seen: set = set()

    def sub(m: re.Match) -> str:
        tok = m.group(1)
        if tok in seen:
            return tok
        seen.add(tok)
        return _SHORTHAND[tok]
    return _SHORTHAND_RE.sub(sub, text or "")


# Nouns that name something buildable/testable, with at most three qualifying
# words in front. Bounded deliberately: an unbounded prefix runs across clause
# boundaries and yields "No modifications to existing unaligned facts" as if
# it were a table name.
_DATA_OBJECT = re.compile(
    r"\b((?:[\w-]+\s+){0,3}?(?:unaligned fact|align fact|fact|facts|denorms?|"
    r"marts?|extracts?|reports?|tables?|feeds?|dashboards?|aggregates?|"
    r"KPIs?|flags?|claims|shipments|inventory|denormali[sz]ed))\b",
    re.IGNORECASE)
_OBJ_STOP = re.compile(
    r"^(no|not|we|this|that|these|those|the|a|an|all|and|or|it|its|there|"
    r"existing|new|additional|following|only|any|each|both|such|"
    r"to|for|in|of|with|through|from|on|by|into|as|be|is|are|will|shall)\b",
    re.IGNORECASE)


def _data_objects(text: str) -> List[str]:
    out: List[str] = []
    lower: set = set()
    for m in _DATA_OBJECT.finditer(text or ""):
        o = " ".join(m.group(1).split()).strip(" ,.;:")
        while o and _OBJ_STOP.match(o):
            parts = o.split(" ", 1)
            o = parts[1] if len(parts) > 1 else ""
        if not o or len(o) < 4 or len(o) > 48 or "," in o:
            continue
        if o.lower() in lower:
            continue
        lower.add(o.lower())
        out.append(o)
    return out


def _rule_source(text: str) -> str:
    """The clause of a requirement that actually carries the obligation."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text or "") if s.strip()]
    if not sentences:
        return (text or "").strip()
    modal = next((s for s in sentences if _MODAL.search(s)), "")
    src = modal or sentences[0]
    return docparse.strip_inline_number(src)


def _heuristic_enrich(req: Dict[str, Any], meta: Dict[str, Any],
                      children: Optional[List[Dict[str, Any]]] = None
                      ) -> Dict[str, Any]:
    """Deterministic enrichment — the no-API-key path, and the safety net.

    Produces a genuinely usable register rather than echoing the raw text into
    every field, which is what the previous heuristic did.

    A level-1 requirement is summarized from its *children*, not from its own
    concatenated blob: that blob starts with the heading and the table's
    column labels, and sentence-splitting it yields "| Unaligned Fact | Next
    Steps" as a business rule.
    """
    is_group = req.get("level") == 1
    lead = (req.get("context_lead_in") or "").strip()
    kids = [c for c in (children or []) if c.get("req_kind") != "context"] or (children or [])

    if is_group:
        body = " ".join(c.get("raw_text", "") for c in kids[:4])
    else:
        raw = (req.get("raw_text") or "").strip()
        body = (lead + ": " + raw) if lead else raw

    label = products.brand_label(meta)
    markets = ", ".join(m["code"] for m in meta.get("markets", []))
    section = (str(req.get("section_no") or "") + " "
               + str(req.get("section_title") or "")).strip()
    where = section or req.get("section_path", "") or req.get("doc_name", "BRD")

    spoken = expand_shorthand(_first_sentences(body, 2))
    if is_group:
        n = len(children or [])
        spoken = (where + " — " + spoken).strip(" —")
        if n:
            spoken += (" Covers " + str(n) + " sub-requirement"
                       + ("s" if n != 1 else "") + " ("
                       + req["req_id"] + ".i–" + req["req_id"] + "."
                       + roman(n) + ").")

    # ----------------------------------------------------------- the rule
    open_self = bool(_TBD.search(req.get("raw_text") or ""))
    if req.get("req_kind") == "context":
        rule = ("Contextual statement — carries no independently testable "
                "assertion; validated indirectly through the sibling "
                "requirements of " + where + ".")
    elif is_group:
        gists = [_title_of(_rule_source(c.get("raw_text", "")), 90) for c in kids[:3]]
        gists = [g for g in gists if g and g != "(untitled)"]
        rule = ("Every sub-requirement of " + where + " must hold"
                + (": " + "; ".join(gists) + (" …" if len(kids) > 3 else "") if gists else "")
                + ".")
        if req.get("open_item"):
            rule += (" Section is marked TBD in the source — acceptance "
                     "criteria are provisional until the outstanding items "
                     "are supplied.")
    else:
        rule = _rule_source(req.get("raw_text") or "").rstrip(".") + "."
        if markets and markets not in rule:
            rule += " Scope: market baskets " + markets + "."
        if label not in ("ALL", "PORTFOLIO") and label not in rule:
            rule += " Applies to " + label + "."
        if open_self:
            rule += (" OPEN — the source leaves this TBD, so the expected "
                     "value cannot be asserted until it is supplied.")
        elif req.get("open_item"):
            rule += " (Parent section is marked TBD; treat as provisional.)"

    # ---------------------------------------------------- the description
    objs = _data_objects(req.get("raw_text") if not is_group else body)
    desc_bits = ["Commercial Analytics Hub scope: " + where]
    if label not in ("ALL", "PORTFOLIO"):
        desc_bits.append("product attribution " + label)
    if markets:
        desc_bits.append("market baskets " + markets)
    if objs:
        desc_bits.append("touches " + ", ".join(objs[:4]))
    desc_bits.append(req.get("phase", "Phase 1"))
    description = "; ".join(desc_bits) + "."

    return {
        "plain_language": spoken or _title_of(body, 200),
        "business_description": description,
        "business_rule": rule,
        "features": objs[:6] or ([where] if where else []),
        "data_objects": objs[:8],
        "products": [p["branded_name"] if p["branded_name"] != products.TBD
                     else p["abbrev"] for p in meta.get("products", [])],
        "enrichment": "heuristic",
    }


# --------------------------------------------------------------- the agent
async def enrich_register(register: List[Dict[str, Any]], *, cached: str = "",
                          refine: str = "", bus: Optional[TraceBus] = None
                          ) -> List[Dict[str, Any]]:
    """Attach product metadata + RAG-grounded semantics to every requirement.

    Product attribution runs first and deterministically, for every item,
    including the ones the LLM never sees — it is lexicon lookup, not
    inference, and it must not depend on an API key.
    """
    by_id = {r["req_id"]: r for r in register}
    for r in register:
        parent = by_id.get(r.get("parent_req") or "")
        # A sub-requirement inherits its section's product scope when its own
        # sentence names no product ("Configure product to ensure data flows
        # correctly" is a Bik-Len requirement only by virtue of its section).
        meta = products.resolve(r.get("context_lead_in", ""), r.get("raw_text", ""))
        if not meta.get("primary") and parent:
            inherited = products.resolve(parent.get("raw_text", "")[:1500],
                                         parent.get("title", ""))
            if inherited.get("primary"):
                inherited["inherited_from"] = parent["req_id"]
                meta = inherited
        r["product_meta"] = meta
        r["brand"] = products.brand_label(meta)
        r["source_ref"] = source_ref(r)

    parents = [r for r in register if r["level"] == 1]
    if bus:
        await bus.emit("requirements_rag_analyst", "start",
                       "RAG enrichment over " + str(len(parents)) +
                       " requirement groups (" + str(len(register)) + " records)")

    async def _one_group(parent: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        subs = [r for r in register if r.get("parent_req") == parent["req_id"]]
        context = embeddings.context_block(
            _retrieval_query(parent, subs), top_k=RETRIEVE_K, doc_type="brd",
            char_budget=4200)
        merged: Dict[str, Dict[str, Any]] = {}
        targets = [parent] + subs
        for i in range(0, len(targets), BATCH_SIZE):
            batch = targets[i:i + BATCH_SIZE]
            try:
                merged.update(await _enrich_batch(parent, batch, context,
                                                  refine, cached))
            except Exception as e:                        # pragma: no cover
                log.warning("enrichment failed for %s: %s", parent["req_id"], e)
        return merged

    results = await llm.gather_bounded([_one_group(p) for p in parents])
    enriched: Dict[str, Dict[str, Any]] = {}
    for res in results:
        if isinstance(res, dict):
            enriched.update(res)

    kids_of: Dict[str, List[Dict[str, Any]]] = {}
    for r in register:
        if r.get("parent_req"):
            kids_of.setdefault(r["parent_req"], []).append(r)

    n_llm = 0
    for r in register:
        got = enriched.get(r["req_id"])
        fallback = _heuristic_enrich(r, r["product_meta"],
                                     kids_of.get(r["req_id"]))
        if got and got.get("business_rule") and got.get("plain_language"):
            n_llm += 1
            r["plain_language"] = got.get("plain_language") or fallback["plain_language"]
            r["business_description"] = (got.get("business_description")
                                         or fallback["business_description"])
            r["business_rule"] = got.get("business_rule") or fallback["business_rule"]
            r["features"] = got.get("features") or fallback["features"]
            r["data_objects"] = got.get("data_objects") or fallback["data_objects"]
            r["enrichment"] = "rag"
            named = got.get("products") or []
            if named and not r["product_meta"].get("primary"):
                r["product_meta"]["llm_named_products"] = named
        else:
            r.update(fallback)
    if bus:
        await bus.emit("requirements_rag_analyst", "finish",
                       str(n_llm) + " of " + str(len(register)) +
                       " records enriched via RAG · " +
                       str(len(register) - n_llm) + " deterministic")
    return register


# ------------------------------------------------------------------ summary
def register_stats(register: List[Dict[str, Any]]) -> Dict[str, Any]:
    tops = [r for r in register if r["level"] == 1]
    subs = [r for r in register if r["level"] == 2]
    prods: Dict[str, int] = {}
    for r in register:
        prods[r.get("brand", "ALL")] = prods.get(r.get("brand", "ALL"), 0) + 1
    return {
        "total": len(register),
        "requirements": len(tops),
        "sub_requirements": len(subs),
        "normative": sum(1 for r in register if r.get("normative")),
        "notes": sum(1 for r in register if r.get("req_kind") == "note"),
        "open_items": sum(1 for r in register if r.get("open_item")),
        "phase_2": sum(1 for r in register if r.get("phase") == "Phase 2"),
        "by_product": dict(sorted(prods.items(), key=lambda kv: -kv[1])),
        "rag_enriched": sum(1 for r in register if r.get("enrichment") == "rag"),
    }
