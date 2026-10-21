"""CAH VERITAS — analytics-store access (schema probe + bounded read-only SQL).

The analytics store is where the UAT test SQL runs. VERITAS_SQL_BACKEND picks it:

duckdb      (default) the app's DuckDB file — seed a demo mart with
            VERITAS_SEED_DEMO_MART=1 or data/seed_mart_db.py
databricks  Unity Catalog through a SQL warehouse: DATABRICKS_SQL_WAREHOUSE_ID,
            DATABRICKS_CATALOG and DATABRICKS_SCHEMA (one or several, comma
            separated). Tables are presented to the SQL-writing agents fully
            qualified (catalog.schema.table).

Both paths enforce: SELECT/WITH only, row cap, wall-clock timing. In
databricks mode a warehouse failure is reported, never silently replaced by
the local tables — tests must not pass against the wrong data.
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple

from . import config, db

_FORBIDDEN = re.compile(
    r"^\s*(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|copy|attach)\b",
    re.IGNORECASE)

MART_TABLES = ["dim_product", "dim_geography", "dim_forecast_cycle",
               "raw_s3_gross_sales", "raw_s3_forecast",
               "stg_sales_conformed", "stg_forecast_conformed",
               "fct_revenue_actual", "fct_revenue_forecast",
               "mart_revenue_actual_vs_forecast"]


def guard(sql: str) -> Optional[str]:
    body = sql.strip().rstrip(";")
    if _FORBIDDEN.match(body):
        return "write/DDL statements are not permitted (read-only test execution)"
    if not re.match(r"^\s*(select|with)\b", body, re.IGNORECASE):
        return "only SELECT / WITH statements are permitted"
    return None


# --------------------------------------------------------------------- local
def _local_schema(table: Optional[str]) -> List[Dict[str, Any]]:
    with db.connect() as con:
        tables = [r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main' ORDER BY table_name").fetchall()]
        tables = [t for t in tables if not t.startswith(("corpus_", "seq_"))
                  and t not in ("sessions", "messages", "stage_runs", "requirements",
                                "design_trace", "test_cases", "executions",
                                "feedback", "kb_entries", "doc_analysis",
                                "figure_transcripts", "schema_meta")]
        if table:
            tables = [t for t in tables if t.lower() == table.lower()]
        out = []
        for t in tables:
            cols = con.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name=? ORDER BY ordinal_position", [t]).fetchall()
            try:
                n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            except Exception:
                n = None
            out.append({"table": t,
                        "columns": [{"name": c[0], "type": c[1]} for c in cols],
                        "row_count": n})
        return out


def _local_execute(sql: str, max_rows: int) -> Tuple[List[str], List[List[Any]], int, int]:
    t0 = time.time()
    with db.connect() as con:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(max_rows)
        # true row count when the result was truncated
        n = len(rows)
        if n == max_rows:
            try:
                n = con.execute(f"SELECT COUNT(*) FROM ({sql.rstrip(';')}) _q").fetchone()[0]
            except Exception:
                pass
    elapsed = int((time.time() - t0) * 1000)
    return cols, [list(r) for r in rows], int(n), elapsed


# ---------------------------------------------------------------- databricks
_DBX: Dict[str, Any] = {"schema": None, "at": 0.0, "error": None}
_SCHEMA_TTL = 600.0


def _q(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def _dbx_schema(table: Optional[str]) -> List[Dict[str, Any]]:  # pragma: no cover
    """Tables and columns of every configured schema — one information_schema
    query, cached for ten minutes."""
    now = time.time()
    if _DBX["schema"] is None or now - _DBX["at"] > _SCHEMA_TTL:
        from . import dbx
        schemas = config.DBX_SCHEMAS
        marks = ", ".join(["?"] * len(schemas))
        sql = (f"SELECT table_schema, table_name, column_name, data_type "
               f"FROM {_q(config.DBX_CATALOG)}.information_schema.columns "
               f"WHERE table_schema IN ({marks}) "
               f"ORDER BY table_schema, table_name, ordinal_position")
        with dbx.sql_connect() as conn, conn.cursor() as cur:
            cur.execute(sql, schemas)
            rows = cur.fetchall()
        tables: Dict[str, Dict[str, Any]] = {}
        for sch, tbl, col, typ in rows:
            fq = f"{config.DBX_CATALOG}.{sch}.{tbl}"
            tables.setdefault(fq, {"table": fq, "columns": [], "row_count": None})
            tables[fq]["columns"].append({"name": col, "type": typ})
        _DBX.update(schema=list(tables.values()), at=now, error=None)
    out = _DBX["schema"]
    if table:
        t = table.lower()
        out = [x for x in out if x["table"].lower() == t or x["table"].lower().endswith("." + t)]
    return out


def _dbx_execute(sql: str, max_rows: int):  # pragma: no cover
    from . import dbx
    t0 = time.time()
    with dbx.sql_connect() as conn, conn.cursor() as cur:
        cur.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(max_rows)
    elapsed = int((time.time() - t0) * 1000)
    return cols, [list(r) for r in rows], len(rows), elapsed


def use_databricks() -> bool:
    return config.SQL_BACKEND == "databricks"


def backend_status() -> Dict[str, Any]:
    """What the analytics store is, and whether it answered last time."""
    if not use_databricks():
        return {"backend": "duckdb", "path": str(config.DB_PATH)}
    missing = [k for k, v in (("DATABRICKS_SQL_WAREHOUSE_ID", config.DBX_WAREHOUSE),
                              ("DATABRICKS_CATALOG", config.DBX_CATALOG),
                              ("DATABRICKS_SCHEMA", config.DBX_SCHEMA)) if not v]
    return {"backend": "databricks", "warehouse_id": config.DBX_WAREHOUSE,
            "catalog": config.DBX_CATALOG, "schemas": config.DBX_SCHEMAS,
            "missing_settings": missing, "last_error": _DBX["error"],
            "tables": len(_DBX["schema"]) if _DBX["schema"] is not None else None}


def dialect() -> str:
    return ("Databricks SQL (Spark SQL) — reference tables fully qualified as catalog.schema.table"
            if use_databricks() else "DuckDB SQL")


# ----------------------------------------------------------------- public
def schema(table: Optional[str] = None) -> List[Dict[str, Any]]:
    if use_databricks():
        try:
            return _dbx_schema(table)
        except Exception as e:                               # pragma: no cover
            _DBX["error"] = f"{type(e).__name__}: {e}"
            return []
    return _local_schema(table)


def schema_text(char_budget: int = 4200) -> str:
    """Compact schema block for Text2SQL prompts (cache-friendly)."""
    lines = [f"-- dialect: {dialect()}"]
    tables = schema()
    if not tables:
        lines.append("-- (no analytics tables available"
                     + (f": {_DBX['error']}" if use_databricks() and _DBX["error"] else "") + ")")
    for t in tables:
        cols = ", ".join(f"{c['name']}:{c['type']}" for c in t["columns"])
        rc = f" -- ~{t['row_count']} rows" if t.get("row_count") is not None else ""
        lines.append(f"TABLE {t['table']} ({cols}){rc}")
    text = "\n".join(lines)
    return text[:char_budget]


def execute(sql: str, max_rows: int = 50) -> Dict[str, Any]:
    err = guard(sql)
    if err:
        return {"error": err, "columns": [], "rows": [], "row_count": 0,
                "elapsed_ms": 0}
    try:
        if use_databricks():
            cols, rows, n, ms = _dbx_execute(sql, max_rows)
        else:
            cols, rows, n, ms = _local_execute(sql, max_rows)
        return {"error": None, "columns": cols, "rows": rows, "row_count": n,
                "elapsed_ms": ms}
    except Exception as e:
        return {"error": str(e), "columns": [], "rows": [], "row_count": 0,
                "elapsed_ms": 0}
