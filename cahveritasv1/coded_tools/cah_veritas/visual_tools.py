"""office_extract / figure_inventory / figure_transcribe CodedTools."""
import asyncio
from typing import Any, Dict

from backend.app import discovery, figures, officeparse, vision


def _path(args: Dict[str, Any]):
    return discovery._resolve(args.get("rel_path", ""))


class OfficeExtract:
    """Deterministic block stream for a workbook or deck."""

    def invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Any:
        path = _path(args)
        ext = path.suffix.lower()
        if ext in (".xlsx", ".xlsm"):
            blocks = officeparse.parse_xlsx_blocks(path)
        elif ext == ".pptx":
            blocks = officeparse.parse_pptx_blocks(path)
        else:
            return {"error": f"{ext} is not a workbook or deck"}
        kinds: Dict[str, int] = {}
        for b in blocks:
            kinds[b["kind"]] = kinds.get(b["kind"], 0) + 1
        return {"blocks": len(blocks), "by_kind": kinds,
                "sections": len({b["section_path"] for b in blocks}),
                "first_blocks": [{k: b[k] for k in ("kind", "section_path", "text")}
                                 for b in blocks[:20]]}


class FigureInventory:
    def invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Any:
        path = _path(args)
        figs = figures.document_figures(path)
        return {"status": figures.status(figs), "figures": vision.describe(path)}


class FigureTranscribe:
    def invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Any:
        return asyncio.run(vision.enrich_document(_path(args)))
