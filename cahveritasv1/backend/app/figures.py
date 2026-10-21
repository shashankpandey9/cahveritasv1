"""CAH VERITAS — the figure extractor agent (deterministic).

Finds every image embedded in a document (.docx, .xlsx, .pptx, .pdf),
anchors it to the place it appears (section, sheet, slide, page), and
decides what needs a vision model:

    icon              too small to carry information (bullets, logos)
    page_furniture    the same image on most pages of a PDF (headers, footers)
    unsupported_format  EMF/WMF/TIFF/JPX/CMYK — no vision-model input for it
    too_large         over the API's per-image limits
    None              candidate — goes to image_triage, then vision_transcriber

Images are identified by content hash, so an image repeated across pages or
documents is triaged and transcribed once. Model results are cached in
`figure_transcripts`; this module only ever *reads* that cache, which keeps
document parsing deterministic (the same bytes always yield the same blocks)
and lets Analyze re-parse without calling a model.
"""
from __future__ import annotations

import hashlib
import logging
import posixpath
import re
import struct
import zipfile
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from . import db

log = logging.getLogger("veritas.figures")

FIGURE_EXT = {".docx", ".xlsx", ".xlsm", ".pptx", ".pdf"}
VISION_MEDIA = {"image/png", "image/jpeg", "image/gif", "image/webp"}
MIN_SIDE = 64                     # below this on either side: an icon
MIN_AREA = 120 * 120
MAX_BYTES = 3_750_000             # base64 of this stays under the 5 MB API cap
MAX_SIDE = 8000
FURNITURE_SHARE = 0.5             # on >= half the pages (and >= 3) -> furniture

# Verdicts that mean "this figure's information is in the corpus".
CAPTURED = {"transcribed"}
# Verdicts that mean "nothing to capture" — skipped on purpose.
SKIPPED = {"icon", "page_furniture", "unsupported_format", "too_large", "decorative"}

A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
R_EMBED = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
PR = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def supports(path: Path) -> bool:
    return Path(path).suffix.lower() in FIGURE_EXT


# ------------------------------------------------------------------ sniffing
def sniff(data: bytes) -> Tuple[str, int, int]:
    """(media_type, width, height) from the header bytes; 0 when unknown."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return "image/png", w, h
    if data[:3] == b"\xff\xd8\xff":
        return ("image/jpeg",) + _jpeg_size(data)
    if data[:6] in (b"GIF87a", b"GIF89a"):
        w, h = struct.unpack("<HH", data[6:10])
        return "image/gif", w, h
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", 0, 0
    if data[:4] in (b"\x01\x00\x00\x00", b"\xd7\xcd\xc6\x9a"):
        return "image/x-emf", 0, 0
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff", 0, 0
    if data.lstrip()[:5] in (b"<?xml", b"<svg ") or b"<svg" in data[:300]:
        return "image/svg+xml", 0, 0
    return "application/octet-stream", 0, 0


def _jpeg_size(data: bytes) -> Tuple[int, int]:
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg = struct.unpack(">H", data[i + 2:i + 4])[0]
        i += 2 + seg
    return 0, 0


def _png(width: int, height: int, channels: int, pixels: bytes) -> bytes:
    """Minimal PNG encoder for 8-bit gray/RGB rows (PDF images, no Pillow)."""
    def chunk(tag: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))
    stride = width * channels
    raw = b"".join(b"\x00" + pixels[y * stride:(y + 1) * stride] for y in range(height))
    color = {1: 0, 3: 2}[channels]
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def rule_verdict(media_type: str, width: int, height: int, size: int) -> Optional[str]:
    if media_type not in VISION_MEDIA:
        return "unsupported_format"
    if width and height and (min(width, height) < MIN_SIDE or width * height < MIN_AREA):
        return "icon"
    if size > MAX_BYTES or max(width, height) > MAX_SIDE:
        return "too_large"
    return None


def make_figure(data: bytes, name: str, location: str, index: int) -> Dict[str, Any]:
    media, w, h = sniff(data)
    return {"sha": hashlib.sha256(data).hexdigest(), "name": name, "location": location,
            "index": index, "media_type": media, "width": w, "height": h,
            "size": len(data), "data": data,
            "rule": rule_verdict(media, w, h, len(data))}


# ------------------------------------------------------------- OOXML helpers
def part_rels(z: zipfile.ZipFile, part: str) -> Dict[str, str]:
    """rId -> absolute part name, for one OOXML part."""
    d, b = posixpath.split(part)
    rp = posixpath.join(d, "_rels", b + ".rels")
    if rp not in z.namelist():
        return {}
    out = {}
    for r in ET.fromstring(z.read(rp)).iter(PR + "Relationship"):
        if r.get("TargetMode") == "External":
            continue
        out[r.get("Id")] = posixpath.normpath(posixpath.join(d, r.get("Target")))
    return out


def blip_targets(elem: ET.Element) -> List[str]:
    """rIds of the raster images (a:blip r:embed) under an element."""
    return [b.get(R_EMBED) for b in elem.iter(A + "blip") if b.get(R_EMBED)]


def ooxml_image(z: zipfile.ZipFile, rels: Dict[str, str], rid: str,
                location: str, index: int) -> Optional[Dict[str, Any]]:
    target = rels.get(rid)
    if not target or target not in z.namelist():
        return None
    return make_figure(z.read(target), target.split("/")[-1], location, index)


# ----------------------------------------------------------------------- pdf
def pdf_figures(path: Path) -> List[Tuple[int, Dict[str, Any]]]:
    """[(page_no, figure)] for every image XObject, decoded without Pillow."""
    try:
        from pypdf import PdfReader
    except ImportError:                                      # pragma: no cover
        return []
    out: List[Tuple[int, Dict[str, Any]]] = []
    reader = PdfReader(str(path))
    seen_on_page: Dict[str, set] = {}
    raw: List[Tuple[int, str, Any]] = []
    for pno, page in enumerate(reader.pages, 1):
        res = page.get("/Resources")
        xo = res.get_object().get("/XObject") if res else None
        if not xo:
            continue
        for key, ref in xo.get_object().items():
            obj = ref.get_object()
            if obj.get("/Subtype") != "/Image":
                continue
            raw.append((pno, str(key).lstrip("/"), obj))
    for idx, (pno, key, obj) in enumerate(raw):
        data = _pdf_image_bytes(obj)
        name = f"page{pno}_{key}"
        if data is None:
            fig = {"sha": hashlib.sha256(obj._data).hexdigest(), "name": name,
                   "location": f"page {pno}", "index": idx,
                   "media_type": "application/pdf-image", "width": int(obj.get("/Width", 0)),
                   "height": int(obj.get("/Height", 0)), "size": len(obj._data),
                   "data": b"", "rule": "unsupported_format"}
        else:
            fig = make_figure(data, name, f"page {pno}", idx)
        seen_on_page.setdefault(fig["sha"], set()).add(pno)
        out.append((pno, fig))
    n_pages = len(reader.pages)
    for _, fig in out:
        pages = len(seen_on_page[fig["sha"]])
        if fig["rule"] is None and pages >= 3 and pages >= FURNITURE_SHARE * n_pages:
            fig["rule"] = "page_furniture"
    return out


def _pdf_image_bytes(obj: Any) -> Optional[bytes]:
    filters = obj.get("/Filter")
    flist = [str(f) for f in (filters if isinstance(filters, list) else [filters] if filters else [])]
    if "/DCTDecode" in flist:
        return obj._data                                     # already a JPEG
    if any(f in flist for f in ("/JPXDecode", "/JBIG2Decode", "/CCITTFaxDecode")):
        return None
    if int(obj.get("/BitsPerComponent", 8) or 8) != 8:
        return None
    cs = obj.get("/ColorSpace")
    cs = cs.get_object() if hasattr(cs, "get_object") else cs
    if isinstance(cs, list) and cs and str(cs[0]) == "/ICCBased":
        channels = int(cs[1].get_object().get("/N", 3))
    elif str(cs) == "/DeviceRGB":
        channels = 3
    elif str(cs) == "/DeviceGray":
        channels = 1
    else:
        return None
    if channels not in (1, 3):
        return None
    w, h = int(obj["/Width"]), int(obj["/Height"])
    try:
        pixels = obj.get_data()
    except Exception:                                        # pragma: no cover
        return None
    if len(pixels) < w * h * channels:
        return None
    return _png(w, h, channels, pixels)


# -------------------------------------------------------- inventory + blocks
def with_cache(figs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Attach the cached model verdict (if any) to each figure."""
    cache = db.get_figures([f["sha"] for f in figs if f["rule"] is None])
    for f in figs:
        c = cache.get(f["sha"])
        f["verdict"] = f["rule"] or (c["verdict"] if c else None)
        f["transcript"] = c if c and c["verdict"] in CAPTURED else None
    return figs


def status(figs: List[Dict[str, Any]]) -> Dict[str, int]:
    """Per-document counts over distinct images."""
    seen: Dict[str, Optional[str]] = {}
    for f in figs:
        seen.setdefault(f["sha"], f.get("verdict"))
    v = list(seen.values())
    return {"total": len(v),
            "captured": sum(1 for x in v if x in CAPTURED),
            "skipped": sum(1 for x in v if x in SKIPPED),
            "pending": sum(1 for x in v if x is None or x == "failed")}


def figure_blocks(fig: Dict[str, Any], section_path: str) -> List[Dict[str, Any]]:
    """The transcript of one figure as blocks, in its own section so it is
    chunked on its own with a self-describing header. Empty when the figure
    has no transcript (skipped, or not transcribed yet)."""
    t = fig.get("transcript")
    if not t:
        return []
    r = t.get("result") or {}
    sec = f"{section_path} > Figure {fig['name']}"
    title = r.get("title") or fig["name"]
    out: List[Dict[str, Any]] = []

    def push(text: str, kind: str = "figure") -> None:
        text = (text or "").strip()
        if text:
            out.append({"kind": kind, "text": text, "section": f"Figure {fig['name']}",
                        "section_path": sec, "heading_level": 0, "list_level": None})

    push(f"Figure: {title} ({r.get('category', 'figure')}; {fig['location']})", "figure_title")
    push(r.get("summary", ""))
    for item in r.get("structure") or []:
        if isinstance(item, dict):
            label = item.get("type", "")
            push(f"{label}: {item.get('text', '')}" if label else item.get("text", ""))
        else:
            push(str(item))
    ent = r.get("entities") or {}
    parts = [f"{k}: {', '.join(str(x) for x in v)}" for k, v in ent.items()
             if isinstance(v, list) and v]
    if parts:
        push("Entities in figure — " + "; ".join(parts))
    lines = [ln for ln in (r.get("text_verbatim") or []) if str(ln).strip()]
    for i in range(0, len(lines), 15):
        push("Text in figure: " + " / ".join(str(x) for x in lines[i:i + 15]))
    return out


def document_figures(path: Path) -> List[Dict[str, Any]]:
    """Every figure in a document with its verdict — for status and enrichment."""
    path = Path(path)
    ext = path.suffix.lower()
    figs: List[Dict[str, Any]] = []
    try:
        if ext == ".pdf":
            figs = [f for _, f in pdf_figures(path)]
        elif ext in (".docx", ".xlsx", ".xlsm", ".pptx"):
            from . import officeparse
            figs = officeparse.ooxml_figures(path)
    except Exception as e:                                   # pragma: no cover
        log.warning("figure scan failed for %s: %s", path.name, e)
    return with_cache(figs)


def dedupe(figs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """First occurrence of each distinct image."""
    seen, out = set(), []
    for f in figs:
        if f["sha"] not in seen:
            seen.add(f["sha"])
            out.append(f)
    return out


_WS = re.compile(r"\s+")


def clean(text: str) -> str:
    return _WS.sub(" ", text or "").strip()
