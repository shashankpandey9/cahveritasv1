"""code_scan CodedTool — static scan of ingested code chunks."""
import re
from typing import Any, Dict

from backend.app import db

_FROM = re.compile(r"\b(?:FROM|JOIN)\s+([a-zA-Z_][\w\.]*)", re.IGNORECASE)
_TARGET = re.compile(
    r"\b(?:INSERT\s+INTO|CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW))\s+([a-zA-Z_][\w\.]*)",
    re.IGNORECASE)
_WHERE = re.compile(r"\bWHERE\b(.{5,200}?)(?:\bGROUP\b|\bORDER\b|;|$)",
                    re.IGNORECASE | re.DOTALL)


class CodeScan:
    def invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Any:
        table = (args.get("table") or "").lower()
        glob = (args.get("path_glob") or "").lower().replace("*", "")
        out = []
        for ch in db.corpus_all_chunks("code"):
            if glob and glob not in ch["doc_name"].lower():
                continue
            text = ch["text"]
            if table and table not in text.lower():
                continue
            out.append({
                "file": ch["doc_name"],
                "reads": sorted({m.group(1) for m in _FROM.finditer(text)}),
                "writes": sorted({m.group(1) for m in _TARGET.finditer(text)}),
                "filters": [w.group(1).strip()[:160] for w in _WHERE.finditer(text)][:6],
            })
        return out[:20]
