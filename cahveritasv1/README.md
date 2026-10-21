# CAH VERITAS v2 — Gilead Commercial Analytics Hub - Validation Engine for Requirements, Integration, and Testing for Analytical Solutions. 

**CAH VERITAS** comprises of 1) ReactJS frontend, 2) FASTAPI Mediation API, 3) Autonomous Agent of Software Architecture (AAOSA) Agent Network, 4) Coded Tools & MCPs, 5) backend Operational Tables, Context Layer Views, Vector Indexes, and 6) a RESTFUL Client Utility. 

As shipped, the solution may locally and with an instantiable **DuckDB**.  The solution also supports **Databricks Integration** to run the entire solution in Databricks as a **Databricks App**, and with separate configuration switches that one can enable inside 'app.yaml': 

| Concern | Default | Databricks option |
|---|---|---|
| App state: sessions, requirements, traces, cases, corpus, figure cache | DuckDB file `data/veritas.duckdb` | Stays DuckDB. Optionally back it up to a Volume so it survives restarts (`VERITAS_STATE_BACKUP_VOLUME`). |
| Analytics store the UAT test SQL runs against | DuckDB (`VERITAS_SQL_BACKEND=duckdb`) | Unity Catalog through a **SQL warehouse** (`VERITAS_SQL_BACKEND=databricks` + `DATABRICKS_SQL_WAREHOUSE_ID`, `DATABRICKS_CATALOG`, `DATABRICKS_SCHEMA`) |
| Inputs (BRD, design, sources, code, UAT baseline) | The app's `inputs/` folder, deployed **empty** | A **Unity Catalog Volume** folder (`VERITAS_INPUTS_VOLUME=/Volumes/<catalog>/<schema>/<volume>/<folder>`) |
| LLM (Claude) | **Databricks Model Serving**: one endpoint per tier, named in `VERITAS_MODEL_FAST` / `_SMART` / `_VISION` | Or the Anthropic API directly (`VERITAS_LLM_PROVIDER=anthropic` + `ANTHROPIC_API_KEY`) |
| Retrieval | In-app vector index (numpy) | Or Databricks Vector Search (`VERITAS_VECTOR_SEARCH=1`) |

## Layout

```
app.yaml            Databricks App spec: command + env (all options documented inline)
run_app.py          entry point: uvicorn on DATABRICKS_APP_PORT
requirements.txt    installed by Databricks Apps from the app root
backend/app/        FastAPI app, stages, agents' grounded work
  dbx.py            Databricks auth (SDK unified auth: app service principal / token / profile)
  sqlexec.py        analytics store: duckdb | databricks SQL warehouse
  volumes.py        inputs Volume mirror (Files API) + optional state backup
coded_tools/, registries/   AAOSA agent network (neuro-san registry + coded tools)
frontend/dist/      prebuilt UI served by FastAPI (frontend/src for rebuilding)
inputs/             empty folder skeleton: brd/ design/ sources/ code/ uat_baseline/ samples/
data/               DuckDB state is created here at first start (not deployed)
databricks/         UC setup SQL and the ingest/index notebook (for Vector Search)
```

`.gitignore` excludes input files, DuckDB files, `kb/` artifacts, `.env`,
`node_modules/` and caches. `databricks sync` honours it, so a deployment never
carries inputs or state, even if some exist in your local copy.

## Deploy

```bash
# 1. create the app (once)
databricks apps create cah-veritas2

# 2. sync this folder to a workspace directory
databricks sync . /Workspace/Users/<you>/cah-veritas2

# 3. deploy from that directory
databricks apps deploy cah-veritas2 \
  --source-code-path /Workspace/Users/<you>/cah-veritas2
```

The app starts with an empty corpus. Either add documents to `inputs/` (and
sync again), or point it at a Volume (below). Then run **Phase 0** in the UI.

## Turning on the Databricks integrations

Each block below is written out, commented, in `app.yaml`. Uncomment it and
redeploy. Grants go to the **app's service principal**; its name is shown on
the app's page in the workspace.

### Inputs from a Unity Catalog Volume

Lay the Volume folder out like `inputs/`:

```
/Volumes/<catalog>/<schema>/<volume>/inputs/
    brd/  design/  sources/  code/  uat_baseline/
```

```yaml
- name: VERITAS_INPUTS_VOLUME
  value: "/Volumes/<catalog>/<schema>/<volume>/inputs"
```

```sql
GRANT USE CATALOG ON CATALOG <catalog> TO `<app service principal>`;
GRANT USE SCHEMA  ON SCHEMA  <catalog>.<schema> TO `<app service principal>`;
GRANT READ VOLUME ON VOLUME  <catalog>.<schema>.<volume> TO `<app service principal>`;
```

A Databricks App does not mount `/Volumes`. The app therefore walks the Volume
folder tree through the Files API and mirrors it into `data/volume_inputs/`:
- New or changed files (compared by size and modification time) are downloaded.
- Files deleted from the Volume are removed from the mirror.
- Unchanged files are skipped.

The mirror is refreshed at start-up (`VERITAS_INPUTS_SYNC_ON_START=1`), at the
start of every Phase 0, and when you press **Sync from Volume** in the
Document Discovery tab. Discovery, Analyze, chunking and figure transcription
then work on the mirror. Where `/Volumes` *is* mounted (a cluster or
notebook), the folder is read in place and nothing is copied.

### UAT SQL on Unity Catalog through a SQL warehouse

1. Add a **SQL warehouse** resource to the app, with key `sql-warehouse` and
   permission *Can use*.
2. In `app.yaml`, set `VERITAS_SQL_BACKEND` to `databricks` and uncomment the
   block below it:

```yaml
- name: VERITAS_SQL_BACKEND
  value: "databricks"
- name: DATABRICKS_SQL_WAREHOUSE_ID
  valueFrom: sql-warehouse
- name: DATABRICKS_CATALOG
  value: "<catalog>"
- name: DATABRICKS_SCHEMA            # one schema, or several comma-separated
  value: "<schema>"
```

```sql
GRANT USE CATALOG ON CATALOG <catalog> TO `<app service principal>`;
GRANT USE SCHEMA, SELECT ON SCHEMA <catalog>.<schema> TO `<app service principal>`;
```

With this on:
- **Schema:** the schema(s) are read from `information_schema` (cached for 10
  minutes) and given to the SQL-writing agents as fully qualified
  `catalog.schema.table` names, together with the Databricks SQL dialect.
- **Execution:** test cases run on the warehouse with the same guard as
  before: only `SELECT`/`WITH` statements, and a row cap.
- **Failures:** if the warehouse can't be reached, the title bar shows ⚠ with
  the reason. Tests never silently fall back to the DuckDB tables.

With the DuckDB backend, set `VERITAS_SEED_DEMO_MART=1` to create a synthetic
demo mart, so Stages 3–4 have tables to query.

### Keep the DuckDB state across restarts (optional)

An app's local disk is ephemeral: the DuckDB state lasts while the app runs
and is lost on restart or redeploy.

```yaml
- name: VERITAS_STATE_BACKUP_VOLUME
  value: "/Volumes/<catalog>/<schema>/<volume>/veritas_state"
```

Grant the service principal `READ VOLUME` and `WRITE VOLUME` on that Volume.

- **At start-up:** the DuckDB file is restored from that folder, but only
  when there is no local state yet.
- **After each stage run or chunking job:** the file is checkpointed and
  copied back.

### LLM: Claude through Databricks Model Serving

The app calls the Claude models through serving endpoints; each tier names one:

| Setting | Used for | Needs image input |
|---|---|---|
| `VERITAS_MODEL_FAST` | AAOSA routing polls, figure triage | yes |
| `VERITAS_MODEL_SMART` | requirements, design trace, UAT generation, explanations | no |
| `VERITAS_MODEL_VISION` | figure transcription (defaults to the smart endpoint) | yes |

1. For each endpoint, add a **Serving endpoint** resource to the app with
   permission *Can query*, using the keys `llm-fast`, `llm-smart` and
   `llm-vision`. `app.yaml` reads the endpoint names from those resources
   (`valueFrom`). You can instead put the names directly under `value:`.
2. Deploy, then confirm with `POST /api/llm/check`. It calls each endpoint
   once (the vision tier with a tiny image) and reports `ok`, the reply and
   the latency for each.

- **Request format:** requests use the endpoints' chat format (system and user
  messages; images as `image_url` data URLs) and authenticate as the app's
  service principal. No Anthropic key or outbound internet access is needed.
- **Prompt caching:** Anthropic prompt-cache markers are not sent on this path.
- **One endpoint for everything:** set only `VERITAS_MODEL_SMART` (or
  `DATABRICKS_SERVING_ENDPOINT`) and it is used for every tier.
- **No endpoints configured:** the app runs its deterministic heuristic mode,
  and figures stay pending.
- **Anthropic API instead:** set `VERITAS_LLM_PROVIDER=anthropic` and
  `ANTHROPIC_API_KEY`. The model settings are then Anthropic model ids.

## Checking what is active

The title bar shows the analytics store and the inputs source (the LLM endpoints are in its tooltip) (for example
`SQL: Databricks cah.veritas · Inputs: /Volumes/…`); hover over it for the
state DB, backup and any error. `GET /api/config` returns the same under
`platform`.

## Run locally

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # defaults: DuckDB everywhere, inputs/ folder
python run_app.py           # http://localhost:8000 (UI + API)
python -m pytest backend/tests -q
```

To rebuild the UI after changing `frontend/src`:
`cd frontend && npm install && npm run build`. Commit `frontend/dist`, since
the app serves the prebuilt bundle and Databricks does not build it.
