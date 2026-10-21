"""CAH VERITAS — central configuration.

Runtime switch (VERITAS_RUNTIME=local|databricks), paths, model tiering.
Everything env-driven so the Databricks App deploys the identical codebase.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

# ---- runtime ----
RUNTIME = os.getenv("VERITAS_RUNTIME", "local").strip().lower()  # local | databricks
IS_DATABRICKS = RUNTIME == "databricks"

# ---- LLM ----
# VERITAS_LLM_PROVIDER picks where the Claude models are called:
#   databricks  Databricks Model Serving. VERITAS_MODEL_FAST / _SMART / _VISION
#               are serving-endpoint names (the model name shown on the
#               workspace's Serving page). Auth is the app's own Databricks
#               credentials — no Anthropic key.
#   anthropic   the Anthropic API directly with ANTHROPIC_API_KEY; the
#               VERITAS_MODEL_* values are Anthropic model ids.
# Unset: databricks when a serving endpoint/model is configured on the
# Databricks runtime, else anthropic.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
_LEGACY_ENDPOINT = os.getenv("DATABRICKS_SERVING_ENDPOINT", "").strip()
LLM_PROVIDER = os.getenv("VERITAS_LLM_PROVIDER", "").strip().lower()
if LLM_PROVIDER not in ("databricks", "anthropic"):
    LLM_PROVIDER = ("databricks" if IS_DATABRICKS and (_LEGACY_ENDPOINT or os.getenv("VERITAS_MODEL_SMART"))
                    and not ANTHROPIC_API_KEY else "anthropic")
if LLM_PROVIDER == "databricks":
    # endpoint names; a tier left unset uses the smart endpoint
    # (or DATABRICKS_SERVING_ENDPOINT), so one endpoint can serve every tier
    MODEL_SMART = os.getenv("VERITAS_MODEL_SMART", "").strip() or _LEGACY_ENDPOINT
    MODEL_FAST = os.getenv("VERITAS_MODEL_FAST", "").strip() or MODEL_SMART
    # figure transcription; figure triage uses MODEL_FAST, which must accept images too
    MODEL_VISION = os.getenv("VERITAS_MODEL_VISION", "").strip() or MODEL_SMART
else:
    MODEL_FAST = os.getenv("VERITAS_MODEL_FAST", "claude-haiku-4-5-20251001")
    MODEL_SMART = os.getenv("VERITAS_MODEL_SMART", "claude-sonnet-4-6")
    # figure transcription; figure triage uses MODEL_FAST
    MODEL_VISION = os.getenv("VERITAS_MODEL_VISION", "claude-opus-5")
VISION_ENABLED = os.getenv("VERITAS_VISION", "1") not in ("0", "false", "no")
CONCURRENCY = int(os.getenv("VERITAS_CONCURRENCY", "6"))
PROMPT_CACHE = os.getenv("VERITAS_PROMPT_CACHE", "1") not in ("0", "false", "no")

# ---- local paths ----
def _project_path(env: str, default: str) -> Path:
    """Relative paths (as in .env) are relative to the project root, not the
    process working directory — uvicorn started from another folder (e.g. with
    --app-dir) otherwise reads an empty database and finds no inputs."""
    p = Path(os.getenv(env, default))
    return p if p.is_absolute() else PROJECT_ROOT / p


def _flag(env: str, default: str = "0") -> bool:
    return os.getenv(env, default).strip().lower() in ("1", "true", "yes", "on")


# App state (sessions, registers, corpus, figure cache) always lives in DuckDB.
DB_PATH = _project_path("VERITAS_DB_PATH", "data/veritas.duckdb")
KB_DIR = _project_path("VERITAS_KB_DIR", "kb")
REGISTRY = PROJECT_ROOT / "registries" / "cah_veritas.hocon"

# ---- Databricks connection ----
# Inside a Databricks App the host and the app service principal's OAuth
# credentials are injected (DATABRICKS_HOST / _CLIENT_ID / _CLIENT_SECRET);
# elsewhere set DATABRICKS_HOST + DATABRICKS_TOKEN, or use a CLI profile.
DBX_HOST = os.getenv("DATABRICKS_HOST", "")
DBX_TOKEN = os.getenv("DATABRICKS_TOKEN", "")
DBX_WAREHOUSE = os.getenv("DATABRICKS_SQL_WAREHOUSE_ID", "").strip()
DBX_CATALOG = os.getenv("DATABRICKS_CATALOG", "").strip()
# one schema, or several separated by commas: the test SQL can use them all
DBX_SCHEMAS = [s.strip() for s in os.getenv("DATABRICKS_SCHEMA", "").split(",") if s.strip()]
DBX_SCHEMA = DBX_SCHEMAS[0] if DBX_SCHEMAS else ""
DBX_VS_ENDPOINT = os.getenv("DATABRICKS_VS_ENDPOINT", "")
DBX_VS_INDEX = os.getenv("DATABRICKS_VS_INDEX", "")
DBX_SERVING_ENDPOINT = os.getenv("DATABRICKS_SERVING_ENDPOINT", "")

# ---- Analytics store the UAT SQL runs against: duckdb | databricks ----
# duckdb (default): the local file above (seed a demo mart with
#   VERITAS_SEED_DEMO_MART=1). databricks: Unity Catalog tables through the SQL
#   warehouse DATABRICKS_SQL_WAREHOUSE_ID, in DATABRICKS_CATALOG.DATABRICKS_SCHEMA.
SQL_BACKEND = os.getenv("VERITAS_SQL_BACKEND", "duckdb").strip().lower()
if SQL_BACKEND not in ("duckdb", "databricks"):
    SQL_BACKEND = "duckdb"
SEED_DEMO_MART = _flag("VERITAS_SEED_DEMO_MART")

# ---- Inputs: local folder, or a Unity Catalog Volume ----
# VERITAS_INPUTS_VOLUME=/Volumes/<catalog>/<schema>/<volume>/<path> — the
# folder holding brd/, design/, sources/, code/, uat_baseline/. Where the
# Volume is mounted (clusters, notebooks) it is read in place; in a Databricks
# App (no /Volumes mount) it is mirrored through the Files API into
# VERITAS_INPUTS_CACHE, which discovery and ingestion then read.
INPUTS_VOLUME = os.getenv("VERITAS_INPUTS_VOLUME", "").strip().rstrip("/")
INPUTS_CACHE = _project_path("VERITAS_INPUTS_CACHE", "data/volume_inputs")
INPUTS_VOLUME_MOUNTED = bool(INPUTS_VOLUME) and Path(INPUTS_VOLUME).is_dir()
if INPUTS_VOLUME:
    INPUTS_DIR = Path(INPUTS_VOLUME) if INPUTS_VOLUME_MOUNTED else INPUTS_CACHE
else:
    INPUTS_DIR = _project_path("VERITAS_INPUTS_DIR", "inputs")
INPUTS_SOURCE = "volume" if INPUTS_VOLUME else "local"
INPUTS_SYNC_ON_START = _flag("VERITAS_INPUTS_SYNC_ON_START", "1")

# ---- Optional: keep the DuckDB state across app restarts ----
# A Databricks App's disk is ephemeral. With a Volume folder set here the
# DuckDB file is restored from it at start-up and copied back after every
# stage run and chunking job. Empty = no backup (state lasts while the app runs).
STATE_BACKUP_VOLUME = os.getenv("VERITAS_STATE_BACKUP_VOLUME", "").strip().rstrip("/")

# ---- Optional: Databricks Vector Search instead of the in-app index ----
VECTOR_SEARCH = _flag("VERITAS_VECTOR_SEARCH") and bool(DBX_VS_INDEX)

APP_NAME = "CAH VERITAS"
APP_TAGLINE = ("Commercial Analytics Hub — Validation Engine for Requirements, "
               "Integration & Testing of Analytical Solutions")


def llm_status() -> str:
    if LLM_PROVIDER == "databricks":
        if not (MODEL_FAST and MODEL_SMART):
            return ("Databricks Model Serving — no endpoint names set (VERITAS_MODEL_FAST / "
                    "VERITAS_MODEL_SMART); heuristic fallbacks active")
        return (f"Databricks Model Serving — fast={MODEL_FAST}, smart={MODEL_SMART}, "
                f"vision={MODEL_VISION}")
    if ANTHROPIC_API_KEY:
        return f"Anthropic — fast={MODEL_FAST}, smart={MODEL_SMART}, vision={MODEL_VISION}"
    return "heuristic mode (no API key) — deterministic fallbacks active"
