"""corpus_search / corpus_inventory CodedTools."""
from typing import Any, Dict

from backend.app import db, embeddings


class CorpusSearch:
    """Vector + keyword retrieval over the ingested corpus."""

    def invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Any:
        return embeddings.search(args.get("query", ""),
                                 top_k=int(args.get("top_k", 6) or 6),
                                 doc_type=args.get("doc_type") or None)


class CorpusInventory:
    def invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Any:
        return db.corpus_inventory()
