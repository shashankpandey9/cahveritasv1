"""CAH VERITAS — Gilead therapeutic-product lexicon.

Requirements in a CAH BRD name their product in whatever register the author
was thinking in: the shorthand a working group uses in Slack ("B/L", "BVY"),
the branded name Commercial reports on ("Biktarvy", "Sunlenca"), the molecule
Regulatory and Medical use ("bictegravir", "lenacapavir"), or a *product
group* that exists only as an analytics construct ("tBIC"). A requirements
register that records only one of those cannot be joined to anything.

This module resolves all four for every mention, and distinguishes three
kinds of entity that a naive brand-matcher conflates:

  ``product``        a marketed (or pre-launch) therapeutic product
  ``product_group``  an analytics roll-up of products (tBIC = BVY + B/L)
  ``market``         a market basket used for share math (RGMN, NRTI, II…)

Markets matter because "add B/L to RGMN, NRTI, 3AGNT, II" is a requirement
*about* a product, scoped *by* markets — recording RGMN as the product would
be wrong.

Editing for a new launch: add an entry to ``CATALOG``. ``aliases`` are matched
case-insensitively on word boundaries; put the longest/most specific first —
resolution prefers the longest alias that matches.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

TBD = "TBD"

# ---------------------------------------------------------------- catalogue
# branded_name / full_product_name / molecule are what the UI shows per
# requirement; abbrev is the shorthand the BRD actually writes.
CATALOG: List[Dict[str, Any]] = [
    {
        "key": "bik_len",
        "kind": "product",
        "branded_name": TBD,                    # brand name not yet assigned
        "abbrev": "B/L",
        "full_product_name": "Bictegravir/Lenacapavir fixed-dose combination "
                             "(oral loading dose + maintenance dose)",
        "molecule": "bictegravir + lenacapavir",
        "molecule_class": "INSTI + capsid inhibitor",
        "therapeutic_area": "HIV Treatment",
        "aliases": ["Bik-Len", "BikLen", "Bic-Len", "BicLen", "B/L", "BL",
                    "bictegravir/lenacapavir", "bictegravir lenacapavir"],
        "note": "Launch product of this BRD. Brand name, maintenance-dose NDC, "
                "form strength, bottle factor and product group ID are TBD in "
                "the source document.",
    },
    {
        "key": "biktarvy",
        "kind": "product",
        "branded_name": "Biktarvy",
        "abbrev": "BVY",
        "full_product_name": "Biktarvy (bictegravir/emtricitabine/tenofovir "
                             "alafenamide) tablets",
        "molecule": "bictegravir + emtricitabine + tenofovir alafenamide",
        "molecule_class": "INSTI-based single-tablet regimen",
        "therapeutic_area": "HIV Treatment",
        "aliases": ["Biktarvy", "BVY", "BIC/FTC/TAF", "Bik"],
    },
    {
        "key": "sunlenca",
        "kind": "product",
        "branded_name": "Sunlenca",
        "abbrev": "SUN",
        "full_product_name": "Sunlenca (lenacapavir) oral tablets and "
                             "subcutaneous injection",
        "molecule": "lenacapavir",
        "molecule_class": "capsid inhibitor",
        "therapeutic_area": "HIV Treatment (heavily treatment-experienced)",
        "aliases": ["Sunlenca", "SUN oral", "SUN injection", "SUN", "LEN oral",
                    "LEN", "lenacapavir"],
        "note": "Shares the oral loading-dose NDC 61958-3001-1 with B/L, which "
                "is why the sales-split engine exists.",
    },
    {
        "key": "yeztugo",
        "kind": "product",
        "branded_name": "Yeztugo",
        "abbrev": "YTG",
        "full_product_name": "Yeztugo (lenacapavir) for HIV pre-exposure "
                             "prophylaxis",
        "molecule": "lenacapavir",
        "molecule_class": "capsid inhibitor",
        "therapeutic_area": "HIV Prevention (PrEP)",
        "aliases": ["Yeztugo", "YTG"],
        "note": "Carries its own oral loading-dose NDC 61958-3401-1 and is "
                "explicitly excluded from the B/L sales split.",
    },
    {
        "key": "descovy",
        "kind": "product",
        "branded_name": "Descovy",
        "abbrev": "DVY",
        "full_product_name": "Descovy (emtricitabine/tenofovir alafenamide) tablets",
        "molecule": "emtricitabine + tenofovir alafenamide",
        "molecule_class": "NRTI backbone",
        "therapeutic_area": "HIV Treatment and Prevention",
        "aliases": ["Descovy", "DVY", "FTC/TAF"],
    },
    {
        "key": "tbic",
        "kind": "product_group",
        "branded_name": "tBIC",
        "abbrev": "tBIC",
        "full_product_name": "tBIC product group — total bictegravir-containing "
                             "regimens (Biktarvy + Bik-Len)",
        "molecule": "bictegravir-containing regimens",
        "molecule_class": "analytics roll-up",
        "therapeutic_area": "HIV Treatment",
        "members": ["biktarvy", "bik_len"],
        "aliases": ["tBIC", "t-BIC", "total BIC"],
        "note": "Created by this BRD as a new product group; must not appear in "
                "the Monthly Sales Report product list.",
    },
]

MARKETS: List[Dict[str, str]] = [
    {"code": "RGMN", "name": "Regimen market"},
    {"code": "NRTI", "name": "NRTI backbone market"},
    {"code": "3AGNT", "name": "Three-agent regimen market"},
    {"code": "INSTI", "name": "Integrase strand-transfer inhibitor market"},
    {"code": "II", "name": "Integrase inhibitor market"},
]

# Alias -> catalogue entry, longest alias first so "LEN oral" beats "LEN".
_ALIAS_INDEX: List[Any] = sorted(
    ((a, e) for e in CATALOG for a in e["aliases"]),
    key=lambda pair: -len(pair[0]))


def _alias_pattern(alias: str) -> re.Pattern:
    """Word-boundary match that tolerates '/' and '-' inside an alias.

    ``\\b`` misbehaves around '/', so "B/L" is anchored on non-word-ish
    neighbours explicitly. Without this, "B/L" matches inside "TBL" and
    "Bik-Len" fails to match at all.
    """
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(alias) + r"(?![A-Za-z0-9])",
                      re.IGNORECASE)


_COMPILED = [(_alias_pattern(a), e) for a, e in _ALIAS_INDEX]
_MARKET_RE = re.compile(
    r"(?<![A-Za-z0-9])(" + "|".join(m["code"] for m in MARKETS) + r")(?![A-Za-z0-9])")
# "INSTI" is both a market basket and a drug class. A lone code in prose
# ("an INSTI (bictegravir)") is the class; a code is read as a market only
# when the sentence is talking about markets, or when codes appear in a list.
_MARKET_CUE = re.compile(r"\b(market|markets|basket|baskets|share)\b", re.IGNORECASE)


def _public(entry: Dict[str, Any]) -> Dict[str, Any]:
    """The metadata shape carried on every requirement."""
    return {"key": entry["key"], "kind": entry["kind"],
            "branded_name": entry["branded_name"], "abbrev": entry["abbrev"],
            "full_product_name": entry["full_product_name"],
            "molecule": entry["molecule"],
            "molecule_class": entry.get("molecule_class", ""),
            "therapeutic_area": entry.get("therapeutic_area", ""),
            "note": entry.get("note", "")}


def resolve(*texts: str) -> Dict[str, Any]:
    """Product metadata for one or more spans of requirement text.

    Returns the full set of mentioned products/groups, the market baskets that
    scope them, and a ``primary`` — the most specific product mentioned, which
    is what the requirement is *about*. A product beats a product group; a
    launch product beats an incumbent, because a Bik-Len BRD sentence that
    names both is virtually always a Bik-Len requirement.
    """
    blob = "\n".join(t for t in texts if t)
    seen: Dict[str, Dict[str, Any]] = {}
    for pattern, entry in _COMPILED:
        if entry["key"] in seen:
            continue
        if pattern.search(blob):
            seen[entry["key"]] = _public(entry)
    found = sorted(set(_MARKET_RE.findall(blob)))
    markets = found if (len(found) > 1 or _MARKET_CUE.search(blob)) else []

    primary: Optional[Dict[str, Any]] = None
    if seen:
        order = ["bik_len", "tbic"]                 # launch scope first
        for key in order:
            if key in seen:
                primary = seen[key]
                break
        if primary is None:
            products = [v for v in seen.values() if v["kind"] == "product"]
            primary = products[0] if products else list(seen.values())[0]

    return {
        "products": list(seen.values()),
        "markets": [{"code": c, "name": next(m["name"] for m in MARKETS
                                             if m["code"] == c)} for c in markets],
        "primary": primary,
        # Flat fields so the register, the KB markdown and downstream stages
        # can read product attribution without unpacking `primary`.
        "branded_name": (primary or {}).get("branded_name", "PORTFOLIO"),
        "abbrev": (primary or {}).get("abbrev", "ALL"),
        "full_product_name": (primary or {}).get("full_product_name",
                                                 "Portfolio-wide / not product specific"),
        "molecule": (primary or {}).get("molecule", "n/a"),
        "therapeutic_area": (primary or {}).get("therapeutic_area", ""),
    }


def brand_label(meta: Dict[str, Any]) -> str:
    """Short attribution string for panels, badges and stage 2/3 prompts."""
    if not meta or not meta.get("primary"):
        return "ALL"
    p = meta["primary"]
    branded = p["branded_name"]
    return p["abbrev"] if branded == TBD else branded


def catalog_summary() -> List[Dict[str, Any]]:
    """Lexicon as served to the UI / /api/config."""
    return [_public(e) for e in CATALOG]
