"""kb_write CodedTool."""
from datetime import datetime, timezone
from typing import Any, Dict

from backend.app import config, db


class KbWrite:
    def invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Any:
        kind = args.get("kind", "misc")
        ref = args.get("ref", "artifact")
        title = args.get("title", ref)
        md = args.get("markdown", "")
        sid = (sly_data or {}).get("session_id", "network")
        folder = config.KB_DIR / kind
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{datetime.now(timezone.utc):%Y%m%d%H%M%S}_{ref}.md"
        path.write_text(md, encoding="utf-8")
        db.record_kb(sid, kind, ref, title, str(path))
        return {"written": str(path)}
