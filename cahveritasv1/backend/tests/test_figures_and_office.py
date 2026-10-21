"""Office extraction, figure triage rules and the vision flow (model mocked).

Runs against the real inputs/ documents and a throwaway DuckDB file, so no
application state and no API calls are touched.
"""
import asyncio
import os
import shutil
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
DESIGN = ROOT / "inputs" / "design"
CLAIM = DESIGN / "Claim_Classification_Design_Document.xlsx"
PLAN = DESIGN / "Bic-Len Launch Project Plan.xlsx"
DECK = DESIGN / "Bic-Len Launch Planning - Project Kick off 05.20.2026.pptx"

_TMP = tempfile.mkdtemp()
os.environ["VERITAS_DB_PATH"] = str(Path(_TMP) / "test.duckdb")

from backend.app import config, db, docparse, figures, llm, officeparse, vision  # noqa: E402

config.DB_PATH = Path(os.environ["VERITAS_DB_PATH"])


def _png(w: int, h: int) -> bytes:
    def chunk(tag, body):
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body))
    raw = b"".join(b"\x00" + b"\xff" * w * 3 for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def setUpModule():
    db.init()


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


@unittest.skipUnless(CLAIM.exists() and PLAN.exists() and DECK.exists(), "design inputs missing")
class OfficeExtractionTest(unittest.TestCase):
    def test_sttm_rows_are_header_aware(self):
        blocks = officeparse.parse_xlsx_blocks(CLAIM)
        rows = [b["text"] for b in blocks if b["kind"] == "table_row"
                and b["section_path"].startswith("Sheet: STTM_Summary")]
        self.assertGreater(len(rows), 150)
        self.assertTrue(rows[0].startswith("Source Field Name: acnt_eid"))

    def test_diagram_shape_text_is_extracted_without_ocr(self):
        blocks = officeparse.parse_xlsx_blocks(CLAIM)
        dfd = " ".join(b["text"] for b in blocks if b["section_path"] == "Sheet: DFD > Diagram")
        self.assertIn("wt_len_oral_loading_doses", dfd)
        self.assertIn("TRVD_PEP_FLG", dfd)
        self.assertNotIn("   ", dfd)                      # padding collapsed

    def test_excel_dates_and_percentages_are_decoded(self):
        rows = [b["text"] for b in officeparse.parse_xlsx_blocks(PLAN) if b["kind"] == "table_row"]
        self.assertTrue(any("Planned Start Date: 2024-05-20" in r for r in rows))
        self.assertTrue(any("%Completion: 100%" in r for r in rows))

    def test_slides_are_sections_with_titles(self):
        secs = {b["section_path"] for b in officeparse.parse_pptx_blocks(DECK)}
        self.assertIn("Slide 2: Agenda", secs)
        self.assertIn("Slide 5: Phase 1 – Scope and Key Deliverables", secs)
        self.assertEqual(officeparse.page_count(DECK), 10)

    def test_figure_rules(self):
        deck = figures.document_figures(DECK)
        self.assertTrue(deck and all(f["verdict"] == "icon" for f in deck))
        claim = figures.status(figures.document_figures(CLAIM))
        self.assertEqual(claim["total"], 5)
        self.assertEqual(claim["pending"], 5)


class FigureRuleTest(unittest.TestCase):
    def test_rule_verdicts(self):
        self.assertEqual(figures.make_figure(_png(40, 40), "i", "", 0)["rule"], "icon")
        self.assertIsNone(figures.make_figure(_png(400, 300), "d", "", 0)["rule"])
        self.assertEqual(figures.make_figure(b"\x01\x00\x00\x00rest", "e.emf", "", 0)["rule"],
                         "unsupported_format")


@unittest.skipUnless(CLAIM.exists(), "design inputs missing")
class VisionFlowTest(unittest.TestCase):
    def test_transcript_is_cached_and_placed_in_the_sheet(self):
        async def fake_vision(prompt, image, media_type, *, model, **kw):
            if model == config.MODEL_FAST:
                return '{"informative": true, "category": "flow_diagram", "reason": "boxes"}'
            return ('{"title": "LEN Oral Loading Dose Sales Split Engine", "category": "flow_diagram",'
                    ' "summary": "Classifies loading-dose claims as SUN_HTE or BL.",'
                    ' "structure": [{"type": "rule", "text": "Pre B/L launch date -> SUN_HTE (Rule 1)"}],'
                    ' "entities": {"tables": ["FACT_RX_CLAM_HIV"], "codes": ["61958300101"]},'
                    ' "text_verbatim": ["1. DATA EXTRACTION", "2. REFERENCE IDENTIFICATION"]}')

        with patch.object(llm, "vision_available", return_value=True), \
                patch.object(llm, "vision", side_effect=fake_vision) as called:
            res = asyncio.run(vision.enrich_document(CLAIM))
            self.assertEqual(res["figures"]["captured"], 5)
            n_calls = called.call_count
            asyncio.run(vision.enrich_document(CLAIM))           # cached: no new calls
            self.assertEqual(called.call_count, n_calls)

        blocks = docparse.parse_blocks(CLAIM)
        fig = [b for b in blocks if "Figure image1.png" in b["section_path"]]
        self.assertTrue(fig[0]["section_path"].startswith("Sheet: Flow_Diagram"))
        text = " ".join(b["text"] for b in fig)
        self.assertIn("Rule 1", text)
        self.assertIn("FACT_RX_CLAM_HIV", text)


if __name__ == "__main__":
    unittest.main()
