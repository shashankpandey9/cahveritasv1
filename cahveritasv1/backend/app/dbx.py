"""CAH VERITAS — Databricks connectivity (one place for auth).

Credentials come from the Databricks SDK's unified auth, in its usual order:
explicit DATABRICKS_HOST + DATABRICKS_TOKEN, the OAuth client credentials a
Databricks App injects for its service principal (DATABRICKS_CLIENT_ID /
DATABRICKS_CLIENT_SECRET), or a CLI profile. Nothing here runs unless a
Databricks feature is switched on, and the SDKs are imported lazily so the
DuckDB-only configuration needs neither installed.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from . import config

log = logging.getLogger("veritas.dbx")

_CFG: Dict[str, Any] = {}


def sdk_config():
    """databricks.sdk Config, created once."""
    if "cfg" not in _CFG:
        from databricks.sdk.core import Config
        kwargs: Dict[str, Any] = {}
        if config.DBX_HOST:
            kwargs["host"] = config.DBX_HOST
        if config.DBX_TOKEN:
            kwargs["token"] = config.DBX_TOKEN
        _CFG["cfg"] = Config(**kwargs)
    return _CFG["cfg"]


def workspace_client():
    if "ws" not in _CFG:
        from databricks.sdk import WorkspaceClient
        _CFG["ws"] = WorkspaceClient(config=sdk_config())
    return _CFG["ws"]


def auth_headers() -> Dict[str, str]:
    """Authorization headers for direct REST calls (e.g. Model Serving)."""
    return sdk_config().authenticate()


def host() -> str:
    h = sdk_config().host or ""
    return h if h.startswith("http") else "https://" + h


def sql_connect(schema: Optional[str] = None):
    """A databricks-sql-connector connection to the configured SQL warehouse,
    defaulting to DATABRICKS_CATALOG / the first DATABRICKS_SCHEMA."""
    if not config.DBX_WAREHOUSE:
        raise RuntimeError("DATABRICKS_SQL_WAREHOUSE_ID is not set")
    from databricks import sql as dbsql
    cfg = sdk_config()
    kwargs: Dict[str, Any] = {
        "server_hostname": host().replace("https://", "").rstrip("/"),
        "http_path": f"/sql/1.0/warehouses/{config.DBX_WAREHOUSE}",
        "credentials_provider": lambda: cfg.authenticate,
    }
    if config.DBX_CATALOG:
        kwargs["catalog"] = config.DBX_CATALOG
    if schema or config.DBX_SCHEMA:
        kwargs["schema"] = schema or config.DBX_SCHEMA
    return dbsql.connect(**kwargs)
