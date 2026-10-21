import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app import db, discovery, ingest, stages


class FakeBus:
    async def emit(self, *args, **kwargs):
        return None


async def _slow_tick():
    await asyncio.sleep(0.05)
    return "tick"


class Phase0BackgroundTest(unittest.IsolatedAsyncioTestCase):
    async def test_run_phase0_does_not_block_event_loop(self):
        def fake_ingest_file(path, doc_type):
            import time
            time.sleep(0.5)
            return {"name": "x", "rel_path": "x", "doc_type": doc_type,
                    "blocks": 0, "chunks": 0}

        with (
            patch.object(ingest, "_collect", return_value=[]),
            patch.object(discovery, "targets", return_value=[(Path("x.md"), "brd")]),
            patch.object(ingest, "ingest_file", side_effect=fake_ingest_file),
            patch.object(db, "corpus_gap_audit", return_value={"ready": True}),
            patch.object(db, "set_stage"),
        ):
            phase0_task = asyncio.create_task(stages.run_phase0("sid-123", FakeBus()))
            tick_task = asyncio.create_task(_slow_tick())

            done, _ = await asyncio.wait({phase0_task, tick_task}, timeout=0.2)

            self.assertIn(tick_task, done,
                          "the event loop was blocked while Phase 0 was ingesting")
            await asyncio.wait_for(phase0_task, timeout=1.0)


if __name__ == "__main__":
    unittest.main()
