"""CAH VERITAS — the office structure extractor agent (deterministic).

Turns .xlsx/.xlsm workbooks and .pptx decks into the same ordered block
stream docparse produces for .docx, so the chunker treats them alike.

Workbooks
    * one section per sheet ("Sheet: STTM_Summary"); a short single-cell row
      ("Target Table: fct_clm_splt", "Key Business Considerations") opens a
      sub-section and ends the current table
    * the first multi-cell row of a table is its header; every data row is
      rendered header-aware — "Source Field Name: acnt_eid | Data Type:
      string | ..." — so a row retrieved alone still says what each value is
    * Excel date serials under a date-like header become ISO dates, fractions
      under a %/completion header become percentages
    * text inside drawing shapes (flow-diagram boxes — the DFD's SQL steps)
      is extracted in reading order: it is real text, no OCR needed
    * embedded pictures become figure blocks (see figures.py)

Decks
    * one section per slide, titled by the slide title; body paragraphs keep
      their outline level as bullets; slide tables are header + rows;
      pictures become figure blocks; speaker notes follow in a Notes section
"""
from __future__ import annotations

import re
import zipfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from . import figures

M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
RID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
XDR = "{http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
A = figures.A

BlockList = List[Dict[str, Any]]
_CELL = re.compile(r"([A-Z]+)(\d+)")
_BULLET = re.compile(r"^\s*[·•o\-–▪■◦*]\s+")
_DATE_HDR = re.compile(r"date|eta|\bdt\b|start|end", re.IGNORECASE)
_PCT_HDR = re.compile(r"%|percent|completion|compl", re.IGNORECASE)
_EXCEL_EPOCH = date(1899, 12, 30)


class _Out:
    """Block accumulator that keeps ord and section bookkeeping in one place."""

    def __init__(self) -> None:
        self.blocks: BlockList = []

    def push(self, kind: str, text: str, section_path: str, *, heading_level: int = 0,
             list_level: Optional[int] = None) -> None:
        text = (text or "").strip()
        if not text:
            return
        self.blocks.append({"ord": len(self.blocks), "kind": kind, "text": text,
                            "section": section_path.split(" > ")[-1],
                            "section_path": section_path,
                            "heading_level": heading_level, "list_level": list_level})

    def figures(self, figs: List[Dict[str, Any]], section_path: str) -> None:
        for f in figures.with_cache(figs):
            for b in figures.figure_blocks(f, section_path):
                self.push(b["kind"], b["text"], b["section_path"],
                          heading_level=b["heading_level"])


# ------------------------------------------------------------------- values
def _fmt_number(v: str, header: str) -> str:
    try:
        x = float(v)
    except ValueError:
        return v
    if _DATE_HDR.search(header) and 20000 <= x <= 80000 and x == int(x):
        return (_EXCEL_EPOCH + timedelta(days=int(x))).isoformat()
    if _PCT_HDR.search(header) and 0 <= x <= 1:
        return f"{round(x * 100, 1):g}%"
    if x == int(x) and abs(x) < 1e15:
        return str(int(x))
    return f"{x:.6g}"


def _col_index(col: str) -> int:
    n = 0
    for ch in col:
        n = n * 26 + ord(ch) - 64
    return n


# ------------------------------------------------------------------- xlsx
def _shared_strings(z: zipfile.ZipFile) -> List[str]:
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    return ["".join(t.text or "" for t in si.iter(M + "t")) for si in root.iter(M + "si")]


def _sheet_rows(root: ET.Element, ss: List[str]) -> List[Tuple[int, List[Tuple[str, str]]]]:
    rows: Dict[int, List[Tuple[str, str]]] = {}
    for c in root.iter(M + "c"):
        m = _CELL.match(c.get("r") or "")
        if not m:
            continue
        v, t = c.find(M + "v"), c.get("t")
        if t == "s" and v is not None:
            val = ss[int(v.text)]
        elif t == "inlineStr":
            val = "".join(x.text or "" for x in c.iter(M + "t"))
        elif v is not None:
            val = v.text or ""
        else:
            continue
        val = val.strip()
        if val:
            rows.setdefault(int(m.group(2)), []).append((m.group(1), val))
    return [(r, sorted(cells, key=lambda x: _col_index(x[0]))) for r, cells in sorted(rows.items())]


def _is_numeric(v: str) -> bool:
    try:
        float(v)
        return True
    except ValueError:
        return False


def _sheets(z: zipfile.ZipFile) -> List[Tuple[str, str]]:
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = figures.part_rels(z, "xl/workbook.xml")
    return [(s.get("name"), rels[s.get(RID)]) for s in wb.iter(M + "sheet")
            if s.get(RID) in rels]


def _drawing_items(z: zipfile.ZipFile, sheet_part: str, root: ET.Element, sheet_loc: str
                   ) -> Tuple[List[str], List[Dict[str, Any]]]:
    """(shape texts in reading order, pictures) for one sheet's drawings."""
    texts: List[Tuple[int, int, str]] = []
    pics: List[Tuple[int, int, Dict[str, Any]]] = []
    srels = figures.part_rels(z, sheet_part)
    for dr in root.iter(M + "drawing"):
        dpart = srels.get(dr.get(RID))
        if not dpart or dpart not in z.namelist():
            continue
        drels = figures.part_rels(z, dpart)
        for anc in list(ET.fromstring(z.read(dpart))):
            frm = anc.find(XDR + "from")
            row = int(frm.find(XDR + "row").text) if frm is not None else 0
            col = int(frm.find(XDR + "col").text) if frm is not None else 0
            for sp in anc.iter(XDR + "sp"):
                paras = [figures.clean("".join(t.text or "" for t in p.iter(A + "t")))
                         for p in sp.iter(A + "p")]
                body = "\n".join(p for p in paras if p)
                if body.strip():
                    texts.append((row, col, body))
            for pic in anc.iter(XDR + "pic"):
                for rid in figures.blip_targets(pic):
                    f = figures.ooxml_image(z, drels, rid, f"{sheet_loc} (row {row + 1})", 0)
                    if f:
                        pics.append((row, col, f))
    texts.sort(key=lambda x: (x[0], x[1]))
    pics.sort(key=lambda x: (x[0], x[1]))
    return [t for _, _, t in texts], [p for _, _, p in pics]


def parse_xlsx_blocks(path: Path) -> BlockList:
    out = _Out()
    with zipfile.ZipFile(path) as z:
        ss = _shared_strings(z)
        for name, part in _sheets(z):
            if part not in z.namelist():
                continue
            root = ET.fromstring(z.read(part))
            sheet_sec = f"Sheet: {name}"
            out.push("heading", sheet_sec, sheet_sec, heading_level=1)
            section = sheet_sec
            header: Dict[str, str] = {}
            last_row = 0
            for rno, cells in _sheet_rows(root, ss):
                if rno - last_row > 2:           # a gap of blank rows ends a table
                    header = {}
                last_row = rno
                if len(cells) == 1:
                    val = cells[0][1]
                    if len(val) <= 60 and not _BULLET.match(val) and "\n" not in val:
                        section = f"{sheet_sec} > {val}"
                        out.push("heading", val, section, heading_level=2)
                        header = {}
                    else:
                        kind = "bullet" if _BULLET.match(val) else "para"
                        out.push(kind, _BULLET.sub("", val).replace("\n", " / "), section,
                                 list_level=0 if kind == "bullet" else None)
                    continue
                if not header and not any(_is_numeric(v) for _, v in cells):
                    header = {c: v.replace("\n", " ") for c, v in cells}
                    out.push("table_header", " | ".join(header.values()), section)
                    continue
                parts = []
                for col, v in cells:
                    h = header.get(col, "")
                    v = _fmt_number(v, h).replace("\n", " / ")
                    parts.append(f"{h}: {v}" if h else v)
                out.push("table_row", " | ".join(parts), section)
            shapes, pics = _drawing_items(z, part, root, sheet_sec)
            if shapes:
                dsec = f"{sheet_sec} > Diagram"
                out.push("heading", "Diagram shapes", dsec, heading_level=2)
                for t in shapes:
                    out.push("para", t.replace("\n", " / "), dsec)
            out.figures(pics, sheet_sec)
    return out.blocks


def xlsx_figures(z: zipfile.ZipFile) -> List[Dict[str, Any]]:
    figs: List[Dict[str, Any]] = []
    for name, part in _sheets(z):
        if part in z.namelist():
            figs += _drawing_items(z, part, ET.fromstring(z.read(part)), f"Sheet: {name}")[1]
    return figs


# ------------------------------------------------------------------- pptx
def _slides(z: zipfile.ZipFile) -> List[str]:
    pres = ET.fromstring(z.read("ppt/presentation.xml"))
    rels = figures.part_rels(z, "ppt/presentation.xml")
    lst = pres.find(P + "sldIdLst")
    return [rels[s.get(RID)] for s in (lst if lst is not None else []) if s.get(RID) in rels]


def _para_text(p: ET.Element) -> str:
    return "".join(t.text or "" for t in p.iter(A + "t")).strip()


def _walk_shapes(tree: ET.Element):
    """Shapes, tables and pictures in z-order, descending into groups."""
    for el in tree:
        if el.tag == P + "grpSp":
            yield from _walk_shapes(el)
        elif el.tag in (P + "sp", P + "graphicFrame", P + "pic"):
            yield el


def _title_shape(root: ET.Element) -> Optional[ET.Element]:
    """The title placeholder; failing that (decks built from free text boxes)
    the short text shape set in the largest font — what a reader sees as the
    title."""
    best, best_sz = None, 0
    for sp in root.iter(P + "sp"):
        ph = sp.find(f"{P}nvSpPr/{P}nvPr/{P}ph")
        if ph is not None and ph.get("type") in ("title", "ctrTitle"):
            return sp
    for sp in root.iter(P + "sp"):       # a shape the author named "Title …"
        nv = sp.find(f"{P}nvSpPr/{P}cNvPr")
        if nv is not None and (nv.get("name") or "").lower().startswith("title") and _shape_text(sp):
            return sp
    for sp in root.iter(P + "sp"):
        text = _shape_text(sp)
        if not text or len(text) > 100:
            continue
        sizes = [int(r.get("sz")) for r in sp.iter(A + "rPr") if r.get("sz", "").isdigit()]
        sz = max(sizes) if sizes else 0
        if sz > best_sz:
            best, best_sz = sp, sz
    return best if best_sz >= 2000 else None      # 20pt and up


def _shape_text(sp: ET.Element) -> str:
    return " ".join(_para_text(p) for p in sp.iter(A + "p") if _para_text(p))


def _notes_text(z: zipfile.ZipFile, slide_part: str) -> List[str]:
    for rid, target in figures.part_rels(z, slide_part).items():
        if "notesSlide" in target and target in z.namelist():
            root = ET.fromstring(z.read(target))
            out = []
            for sp in root.iter(P + "sp"):
                ph = sp.find(f"{P}nvSpPr/{P}nvPr/{P}ph")
                if ph is not None and ph.get("type") != "body":
                    continue                     # skip slide image / number placeholders
                out += [_para_text(p) for p in sp.iter(A + "p") if _para_text(p)]
            return out
    return []


def parse_pptx_blocks(path: Path) -> BlockList:
    out = _Out()
    with zipfile.ZipFile(path) as z:
        for n, part in enumerate(_slides(z), 1):
            if part not in z.namelist():
                continue
            root = ET.fromstring(z.read(part))
            title_sp = _title_shape(root)
            title = figures.clean(_shape_text(title_sp)) if title_sp is not None else ""
            sec = f"Slide {n}" + (f": {title[:80]}" if title else "")
            out.push("heading", sec, sec, heading_level=1)
            rels = figures.part_rels(z, part)
            tree = root.find(f"{P}cSld/{P}spTree")
            pics: List[Dict[str, Any]] = []
            for el in _walk_shapes(tree if tree is not None else root):
                if el.tag == P + "sp":
                    if el is title_sp:
                        continue
                    ph = el.find(f"{P}nvSpPr/{P}nvPr/{P}ph")
                    if ph is not None and ph.get("type") in ("title", "ctrTitle", "sldNum", "dt", "ftr"):
                        continue
                    for p in el.iter(A + "p"):
                        text = _para_text(p)
                        ppr = p.find(A + "pPr")
                        lvl = int(ppr.get("lvl", 0)) if ppr is not None and ppr.get("lvl") else None
                        out.push("bullet" if lvl is not None else "para", text, sec, list_level=lvl)
                elif el.tag == P + "graphicFrame":
                    for tbl in el.iter(A + "tbl"):
                        rows = [[figures.clean(" ".join(_para_text(p) for p in tc.iter(A + "p")))
                                 for tc in tr.iter(A + "tc")] for tr in tbl.iter(A + "tr")]
                        rows = [r for r in rows if any(r)]
                        if not rows:
                            continue
                        hdr = rows[0]
                        out.push("table_header", " | ".join(h for h in hdr if h), sec)
                        for r in rows[1:]:
                            out.push("table_row", " | ".join(
                                f"{hdr[i]}: {v}" if i < len(hdr) and hdr[i] else v
                                for i, v in enumerate(r) if v), sec)
                elif el.tag == P + "pic":
                    for rid in figures.blip_targets(el):
                        f = figures.ooxml_image(z, rels, rid, sec, len(pics))
                        if f:
                            pics.append(f)
            out.figures(pics, sec)
            notes = _notes_text(z, part)
            if notes:
                nsec = f"{sec} > Notes"
                for t in notes:
                    out.push("para", "Speaker notes: " + t, nsec)
    return out.blocks


def pptx_figures(z: zipfile.ZipFile) -> List[Dict[str, Any]]:
    figs: List[Dict[str, Any]] = []
    for n, part in enumerate(_slides(z), 1):
        if part not in z.namelist():
            continue
        root = ET.fromstring(z.read(part))
        rels = figures.part_rels(z, part)
        for pic in root.iter(P + "pic"):
            for rid in figures.blip_targets(pic):
                f = figures.ooxml_image(z, rels, rid, f"Slide {n}", len(figs))
                if f:
                    figs.append(f)
    return figs


# ------------------------------------------------------------------- docx
def docx_figures(z: zipfile.ZipFile) -> List[Dict[str, Any]]:
    rels = figures.part_rels(z, "word/document.xml")
    root = ET.fromstring(z.read("word/document.xml"))
    figs: List[Dict[str, Any]] = []
    for rid in figures.blip_targets(root):
        f = figures.ooxml_image(z, rels, rid, "document", len(figs))
        if f:
            figs.append(f)
    return figs


def ooxml_figures(path: Path) -> List[Dict[str, Any]]:
    ext = Path(path).suffix.lower()
    with zipfile.ZipFile(path) as z:
        if ext in (".xlsx", ".xlsm"):
            return xlsx_figures(z)
        if ext == ".pptx":
            return pptx_figures(z)
        if ext == ".docx":
            return docx_figures(z)
    return []


def page_count(path: Path) -> Optional[int]:
    """Slides for a deck, sheets for a workbook (exact)."""
    ext = Path(path).suffix.lower()
    with zipfile.ZipFile(path) as z:
        if ext == ".pptx":
            return len(_slides(z))
        if ext in (".xlsx", ".xlsm"):
            return len(_sheets(z))
    return None
