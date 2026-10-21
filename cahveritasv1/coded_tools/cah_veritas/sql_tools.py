"""schema_probe / sql_execute CodedTools."""
from typing import Any, Dict

from backend.app import sqlexec


class SchemaProbe:
    def invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Any:
        return sqlexec.schema(args.get("table") or None)


class SqlExecute:
    def invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Any:
        return sqlexec.execute(args.get("sql", ""),
                               max_rows=int(args.get("max_rows", 50) or 50))
