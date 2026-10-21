"""CAH VERITAS — structural document parsing.

Why this module exists
----------------------
Requirement enumeration is a *structural* problem before it is a language
problem. A BRD states its obligations as bullets and table rows hanging off a
numbered heading spine; if the parser flattens that spine — or, worse, never
reads the file at all — no amount of prompt engineering recovers the register.

The previous ingest path imported ``python-docx`` and, on ImportError,
silently fell back to ``path.read_bytes().decode("utf-8", "ignore")`` — which
for a .docx is raw ZIP binary. That garbage was chunked, embedded and handed
to the requirements agent. This module removes the dependency entirely: a
.docx is an OOXML zip, and the stdlib reads it.

It emits an ordered, typed **block stream** that preserves:
  * the heading spine (with a materialized ``section_path``),
  * list nesting (``w:numPr/w:ilvl``),
  * tables **in document position**, row by row, with header cells carried
    onto every row so a row is self-describing out of context,
  * a stable ``ord`` so downstream agents can cite an exact span.

``python-docx`` is still honored when installed (some Gilead images have it)
but is never required.
"""
from __future__ import annotations

import logging
import re
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from xml.etree import ElementTree as ET

from . import figures, officeparse

log = logging.getLogger("veritas.docparse")

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# Paragraph styles that are navigation furniture, never content.
_TOC_STYLES = re.compile(r"^(toc\d*|tocheading|index\d*)$", re.IGNORECASE)

# Sections that are document metadata rather than obligations.
NON_REQUIREMENT_SECTIONS = re.compile(
    r"^\s*(contents|table of contents|revision history|approvals?|"
    r"approval\s*\(phase\s*\d+\)|sign[- ]?off|document control|glossary|"
    r"appendix\s*[a-z]?|references|document history)\s*$", re.IGNORECASE)

# A heading that opens with an outline number: "2.3.5 Deciling", "1. DATA ONBOARDING"
_HEADING_NUM = re.compile(r"^\s*(\d+(?:\.\d+)*)\.?\s+(.*)$")

# A body line carrying its own inline number: "1. Oral Lead-in Dose will ..."
_INLINE_NUM = re.compile(r"^\s*(\d{1,2})[\.\)]\s+(?=\S)")

BlockList = List[Dict[str, Any]]


# ---------------------------------------------------------------- docx (xml)
def _text_of(elem: ET.Element) -> str:
    """Concatenate visible run text in document order.

    Deliberately reads only ``w:t``. Field instructions (``w:instrText``) and
    tracked deletions (``w:delText``) are different tags and are therefore
    excluded for free — which is what keeps TOC field codes out of the corpus.
    """
    out: List[str] = []
    for node in elem.iter():
        tag = node.tag
        if tag == W + "t":
            out.append(node.text or "")
        elif tag == W + "tab":
            out.append("\t")
        elif tag in (W + "br", W + "cr"):
            out.append(" ")
    return re.sub(r"[ \t]+", " ", "".join(out)).strip()


def _para_style(p: ET.Element) -> str:
    pr = p.find(W + "pPr")
    if pr is None:
        return ""
    st = pr.find(W + "pStyle")
    return (st.get(W + "val") or "") if st is not None else ""


def _list_level(p: ET.Element) -> Optional[int]:
    """Indent level when the paragraph is a numbered/bulleted list item."""
    pr = p.find(W + "pPr")
    if pr is None:
        return None
    num = pr.find(W + "numPr")
    if num is None:
        return None
    ilvl = num.find(W + "ilvl")
    try:
        return int(ilvl.get(W + "val")) if ilvl is not None else 0
    except (TypeError, ValueError):
        return 0


def _heading_level(style: str, text: str, allow_caps: bool = True) -> Optional[int]:
    """Heading depth, or None when the paragraph is body text.

    ``allow_caps`` enables the all-caps fallback for documents that carry no
    styled headings at all. It is switched off once a document is known to use
    real Heading styles — otherwise a stray body line like "B/L" or "NDC:"
    gets promoted to a section and fractures the requirement spine.
    """
    s = (style or "").lower().replace(" ", "")
    if s in ("title", "subtitle"):
        return 1
    m = re.match(r"^heading(\d+)$", s)
    if m:
        return min(int(m.group(1)), 6)
    if (allow_caps and not s and text and len(text) < 90
            and text.isupper() and not text.endswith(":")
            and len(text.split()) >= 2):
        return 2
    return None


def _table_blocks(tbl: ET.Element) -> List[Dict[str, Any]]:
    """Rows of a table, header cells carried onto every row.

    A requirement expressed as a table row ("2 | Consumer OOP | Overall OOP
    ... | LAAD data | Filters ... | New KPIs on Databricks") is meaningless
    without its header. Pairing them here makes every row independently
    citable and independently enumerable.
    """
    rows: List[List[str]] = []
    for tr in tbl.findall(W + "tr"):
        cells = [_text_of(tc) for tc in tr.findall(W + "tc")]
        if any(c.strip() for c in cells):
            rows.append(cells)
    if not rows:
        return []
    header, body = rows[0], rows[1:]
    header_is_labels = bool(body) and all(len(c) < 60 for c in header)
    out: List[Dict[str, Any]] = []
    if header_is_labels:
        out.append({"kind": "table_header", "text": " | ".join(header),
                    "cells": header, "header": [], "list_level": None})
    else:
        header, body = [], rows
    for cells in body:
        if header and len(cells) == len(header):
            pairs = [h.strip() + ": " + c.strip()
                     for h, c in zip(header, cells) if c.strip()]
            text = " · ".join(pairs)
        else:
            text = " | ".join(c.strip() for c in cells if c.strip())
        if text.strip():
            out.append({"kind": "table_row", "text": text, "cells": cells,
                        "header": header, "list_level": None})
    return out


def _body_elements(path: Path) -> Iterable[ET.Element]:
    with zipfile.ZipFile(str(path)) as z:
        xml = z.read("word/document.xml")
    root = ET.fromstring(xml)
    body = root.find(W + "body")
    return list(body) if body is not None else []


def parse_docx_blocks(path: Path) -> BlockList:
    """Ordered block stream for a .docx — stdlib only, tables in place."""
    blocks: BlockList = []
    spine: List[str] = []          # heading stack, index = level-1
    elements = list(_body_elements(path))
    # Embedded pictures are transcribed by the vision agents; their cached
    # transcripts are placed where the picture sits in the document.
    with zipfile.ZipFile(path) as z:
        rels = figures.part_rels(z, "word/document.xml")
        pics = {rid: figures.ooxml_image(z, rels, rid, "", 0)
                for el in elements for rid in figures.blip_targets(el)}
    pics = {k: v for k, v in pics.items() if v}
    figures.with_cache(list(pics.values()))
    # Does this document use real Heading styles? If so, never guess from caps.
    styled = any(el.tag == W + "p"
                 and re.match(r"^heading\d+$", _para_style(el).lower().replace(" ", ""))
                 for el in elements)

    def push(kind: str, text: str, **extra: Any) -> None:
        blocks.append({"ord": len(blocks), "kind": kind, "text": text,
                       "section": spine[-1] if spine else "Preamble",
                       "section_path": " > ".join(spine) or "Preamble",
                       "heading_level": len(spine), **extra})

    for el in elements:
        if el.tag == W + "tbl":
            for b in _table_blocks(el):
                push(b["kind"], b["text"], cells=b.get("cells"),
                     list_level=None)
            continue
        if el.tag != W + "p":
            continue
        style = _para_style(el)
        if _TOC_STYLES.match(style):      # navigation furniture
            continue
        for rid in figures.blip_targets(el):
            fig = pics.get(rid)
            if fig:
                fig["location"] = " > ".join(spine) or "Preamble"
                for b in figures.figure_blocks(fig, fig["location"]):
                    blocks.append({"ord": len(blocks), **b})
        text = _text_of(el)
        if not text:
            continue
        hl = _heading_level(style, text, allow_caps=not styled)
        if hl is not None:
            del spine[hl - 1:]
            spine.append(text[:160])
            push("heading", text[:160], list_level=None, style=style)
            continue
        lvl = _list_level(el)
        kind = "bullet" if lvl is not None else "para"
        push(kind, text, list_level=lvl, style=style)
    return blocks


def _parse_docx_via_python_docx(path: Path) -> BlockList:  # pragma: no cover
    """Used only when the OOXML path fails and python-docx happens to exist."""
    import docx
    d = docx.Document(str(path))
    blocks: BlockList = []
    spine: List[str] = []
    for p in d.paragraphs:
        text = p.text.strip()
        if not text:
            continue
        style = (p.style.name or "") if p.style else ""
        if _TOC_STYLES.match(style.replace(" ", "")):
            continue
        hl = _heading_level(style, text)
        if hl is not None:
            del spine[hl - 1:]
            spine.append(text[:160])
        blocks.append({"ord": len(blocks),
                       "kind": "heading" if hl is not None else "para",
                       "text": text, "section": spine[-1] if spine else "Preamble",
                       "section_path": " > ".join(spine) or "Preamble",
                       "heading_level": len(spine), "list_level": None,
                       "style": style})
    return blocks


# ------------------------------------------------------------------ markdown
def parse_markdown_blocks(text: str, name: str) -> BlockList:
    blocks: BlockList = []
    spine: List[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            lvl = len(m.group(1))
            del spine[lvl - 1:]
            spine.append(m.group(2).strip()[:160])
            kind, body, llvl = "heading", spine[-1], None
        elif re.match(r"^\s*([-*+]|\d+[.)])\s+", line):
            indent = len(line) - len(line.lstrip())
            kind = "bullet"
            body = re.sub(r"^\s*([-*+]|\d+[.)])\s+", "", line).strip()
            llvl = indent // 2
        elif line.lstrip().startswith("|") and line.count("|") >= 2:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c or "-") for c in cells):
                continue
            kind, body, llvl = "table_row", " | ".join(c for c in cells if c), None
        else:
            kind, body, llvl = "para", line.strip(), None
        if not body:
            continue
        blocks.append({"ord": len(blocks), "kind": kind, "text": body,
                       "section": spine[-1] if spine else name,
                       "section_path": " > ".join(spine) or name,
                       "heading_level": len(spine), "list_level": llvl})
    return blocks


def parse_plain_blocks(text: str, name: str) -> BlockList:
    return [{"ord": i, "kind": "para", "text": ln.strip(), "section": name,
             "section_path": name, "heading_level": 0, "list_level": None}
            for i, ln in enumerate(l for l in text.splitlines() if l.strip())]


# ----------------------------------------------------------------------- pdf
def parse_pdf_blocks(path: Path) -> BlockList:
    try:
        from pypdf import PdfReader
    except ImportError:
        log.warning("pypdf not installed — %s skipped", path.name)
        return []
    blocks: BlockList = []
    reader = PdfReader(str(path))
    by_page: Dict[int, List[Dict[str, Any]]] = {}
    for pno, fig in figures.pdf_figures(path):
        by_page.setdefault(pno, []).append(fig)
    figures.with_cache([f for fs in by_page.values() for f in fs])
    for i, page in enumerate(reader.pages, 1):
        try:
            txt = page.extract_text() or ""
        except Exception:                                   # pragma: no cover
            txt = ""
        for line in (l.strip() for l in txt.splitlines()):
            if line:
                blocks.append({"ord": len(blocks), "kind": "para", "text": line,
                               "section": "page " + str(i),
                               "section_path": "page " + str(i),
                               "heading_level": 1, "list_level": None})
        done = set()
        for fig in by_page.get(i, []):
            if fig["sha"] in done:
                continue
            done.add(fig["sha"])
            for b in figures.figure_blocks(fig, "page " + str(i)):
                blocks.append({"ord": len(blocks), **b})
    return blocks


# -------------------------------------------------------------------- driver
CODE_EXT = {".sql", ".py", ".scala", ".hql", ".sparksql", ".ipynb", ".sh"}


def parse_blocks(path: Path) -> BlockList:
    """Ordered block stream for any supported input file."""
    ext = path.suffix.lower()
    if ext == ".docx":
        try:
            blocks = parse_docx_blocks(path)
            if blocks:
                return blocks
            log.warning("%s produced no blocks via OOXML", path.name)
        except Exception as e:
            log.warning("OOXML parse of %s failed (%s) — trying python-docx",
                        path.name, e)
        try:                                                # pragma: no cover
            return _parse_docx_via_python_docx(path)
        except Exception as e:
            log.error("could not parse %s: %s", path.name, e)
            return []
    if ext == ".pdf":
        return parse_pdf_blocks(path)
    if ext in (".xlsx", ".xlsm"):
        return officeparse.parse_xlsx_blocks(path)
    if ext == ".pptx":
        return officeparse.parse_pptx_blocks(path)
    text = path.read_text(encoding="utf-8", errors="ignore")
    if ext == ".md":
        return parse_markdown_blocks(text, path.name)
    return parse_plain_blocks(text, path.name)


# ------------------------------------------------------------------ helpers
LINES_PER_PAGE = 50
CHARS_PER_PAGE = 3000


def page_count(path: Path, blocks: Optional[BlockList] = None) -> tuple:
    """(pages, estimated). PDFs are exact; a .docx uses the page count Word
    stores in docProps/app.xml; everything else is estimated from its text."""
    ext = path.suffix.lower()
    try:
        if ext in (".pptx", ".xlsx", ".xlsm"):
            n = officeparse.page_count(path)     # slides / sheets
            if n:
                return n, False
        if ext == ".pdf":
            from pypdf import PdfReader
            return len(PdfReader(str(path)).pages), False
        if ext == ".docx":
            with zipfile.ZipFile(path) as z:
                if "docProps/app.xml" in z.namelist():
                    m = re.search(r"<Pages>(\d+)</Pages>",
                                  z.read("docProps/app.xml").decode("utf-8", "ignore"))
                    if m and int(m.group(1)) > 0:
                        return int(m.group(1)), False
    except Exception as e:                                  # pragma: no cover
        log.warning("page count failed for %s: %s", path.name, e)
    if blocks is None:
        blocks = parse_blocks(path)
    if ext == ".docx":
        chars = sum(len(b.get("text") or "") for b in blocks)
        return max(1, -(-chars // CHARS_PER_PAGE)), True
    return max(1, -(-len(blocks) // LINES_PER_PAGE)), True


def heading_number(text: str) -> str:
    """'2.3.5 Deciling' -> '2.3.5'  ·  'SCOPE' -> ''."""
    m = _HEADING_NUM.match(text or "")
    return m.group(1) if m else ""


def heading_title(text: str) -> str:
    """'2.3.5 Deciling' -> 'Deciling'."""
    m = _HEADING_NUM.match(text or "")
    return (m.group(2) if m else (text or "")).strip()


def strip_inline_number(text: str) -> str:
    """'1. Oral Lead-in Dose ...' -> 'Oral Lead-in Dose ...'"""
    return _INLINE_NUM.sub("", text or "").strip()


def is_non_requirement_section(section: str) -> bool:
    return bool(NON_REQUIREMENT_SECTIONS.match(heading_title(section)))
