"""CAH VERITAS — FastAPI mediation layer.

Run from the project root:
    uvicorn backend.app.main:app --port 8000

Endpoints:
    POST /api/session                     new session
    GET  /api/state/{sid}                 full state: stage, messages, panels
    POST /api/message  {sid, prompt}      chat → stage machine / advisor Q&A
    POST /api/gate     {sid, action, comment}   accept | refine | confirm_execute
    POST /api/feedback {sid, stage, ref, sentiment, comment}   thumbs
    GET  /api/agent-stream/{run_id}       SSE live agent trace
    POST /api/ingest                      (re)ingest inputs/
    GET  /api/documents                   discovery grid: every input document
                                          with its chunk coverage + summary
    POST /api/documents/analyze {rel_path, doc_type}
                                          re-parse one source, verify coverage
    POST /api/documents/chunk {session_id, rel_paths?}
                                          (re-)chunk the given docs, or every
                                          incomplete one — SSE-attachable
    GET  /api/kb                          knowledge-base ledger
    GET  /api/config                      app name, runtime, llm status
    GET  /api/sessions                    recent sessions and how far each got
    POST /api/trace/retrace {session_id, req_no, note}
                                          re-run the design trace of one
                                          requirement — SSE-attachable
Static: serves frontend/dist when built (single-origin deploy — this is what
the Databricks App runs).
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import (chunker, config, db, discovery, ingest, kb, pipeline, products,
               sqlexec, stages, trace, volumes)
from .trace import bus_for

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("veritas.main")

app = FastAPI(title=config.APP_NAME, version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])

_RUNS: Dict[str, Dict[str, Any]] = {}   # run_id -> {sid, stage, task}
_ACTIVE_RUNS_BY_SESSION: Dict[str, str] = {}


def _seed_demo_mart() -> None:
    """VERITAS_SEED_DEMO_MART=1: create the synthetic analytics mart in the
    DuckDB file when it is missing (DuckDB SQL backend only)."""
    import importlib.util
    with db.connect() as con:
        have = con.execute("SELECT count(*) FROM information_schema.tables "
                           "WHERE table_name='mart_revenue_actual_vs_forecast'").fetchone()[0]
    if have:
        return
    spec = importlib.util.spec_from_file_location(
        "seed_mart_db", config.PROJECT_ROOT / "data" / "seed_mart_db.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.main(force=True, db_path=config.DB_PATH)


async def _backup_state() -> None:
    if config.STATE_BACKUP_VOLUME:
        await asyncio.to_thread(volumes.backup_state)


async def _sync_inputs_job(bus) -> Dict[str, Any]:
    await bus.emit("intake_librarian", "start", f"syncing inputs from {config.INPUTS_VOLUME}")
    loop = asyncio.get_running_loop()

    def progress(msg: str) -> None:
        asyncio.run_coroutine_threadsafe(bus.emit("intake_librarian", "info", msg), loop)
    try:
        res = await asyncio.to_thread(volumes.sync_inputs, progress)
        await bus.emit("intake_librarian", "finish",
                       f"{res.get('files', 0)} files · {res.get('downloaded', 0)} downloaded · "
                       f"{res.get('removed', 0)} removed · {res.get('unchanged', 0)} unchanged")
        return res
    except Exception as e:
        await bus.emit("intake_librarian", "error", f"Volume sync failed: {e}")
        return {"error": str(e)}


@app.on_event("startup")
async def _startup() -> None:
    if config.STATE_BACKUP_VOLUME:          # before the database is opened
        log.info("state backup: %s", await asyncio.to_thread(volumes.restore_state))
    db.init()
    if config.SEED_DEMO_MART and config.SQL_BACKEND == "duckdb":
        await asyncio.to_thread(_seed_demo_mart)
    if config.INPUTS_VOLUME and not config.INPUTS_VOLUME_MOUNTED and config.INPUTS_SYNC_ON_START:
        # first mirror of the inputs Volume, in the background
        run_id = str(uuid.uuid4())
        bus = bus_for(run_id)

        async def _job() -> None:
            try:
                await _sync_inputs_job(bus)
            finally:
                await bus.close()
                _SYNC_JOB["run_id"] = None
        _SYNC_JOB["run_id"] = run_id
        asyncio.get_running_loop().create_task(_job())
    log.info("platform: sql=%s inputs=%s (%s) state_backup=%s",
             sqlexec.backend_status(), config.INPUTS_SOURCE, config.INPUTS_DIR,
             config.STATE_BACKUP_VOLUME or "off")
    log.info("%s started — Phase 0 ingestion is deferred until the user initiates it; runtime=%s, llm=%s",
             config.APP_NAME, config.RUNTIME, config.llm_status())


# ------------------------------------------------------------------ schemas
class SessionOut(BaseModel):
    session_id: str


class MessageIn(BaseModel):
    session_id: str
    prompt: str = Field(..., min_length=1, max_length=20_000)


class GateIn(BaseModel):
    session_id: str
    action: str          # accept | refine | confirm_execute
    comment: str = ""


class FeedbackIn(BaseModel):
    session_id: str
    stage: str
    artifact_ref: str = ""
    sentiment: str       # positive | negative
    comment: str = ""


class Phase0In(BaseModel):
    session_id: str


class AnalyzeIn(BaseModel):
    rel_path: str
    doc_type: str


class RetraceIn(BaseModel):
    session_id: str
    req_no: str
    note: str = ""


class ChunkIn(BaseModel):
    session_id: Optional[str] = None
    rel_paths: Optional[List[str]] = None   # None = every incomplete document


# ------------------------------------------------------------------ helpers
def _welcome() -> str:
    return (f"**{config.APP_NAME}** — {config.APP_TAGLINE}.\n\n"
            "No corpus is ingested on startup. Start **Phase 0** to inventory the "
            "available BRD, design, code, source, and UAT inputs, then ingest only "
            "the available artifacts. After that, the workflow proceeds into Phase 1 "
            "requirements enumeration. At every stage you will **Accept** or "
            "**Refine** before I proceed; execution only happens after your explicit "
            "confirmation.")


def _launch_stage(sid: str, stage_code: str) -> str:
    """Spawn a stage run in the background; returns run_id for SSE attach."""
    existing = _ACTIVE_RUNS_BY_SESSION.get(sid)
    if existing and existing in _RUNS:
        return existing

    run_id = str(uuid.uuid4())
    bus = bus_for(run_id)

    async def _job() -> None:
        try:
            payload = await pipeline.run_stage(sid, stage_code, bus)
            summary = pipeline.stage_summary_md(stage_code, payload)
            db.add_message(sid, "assistant", summary)
            await bus.emit("veritas_advisor", "info", "stage summary posted")
            await _backup_state()
        finally:
            await bus.close()
            _RUNS.pop(run_id, None)
            _ACTIVE_RUNS_BY_SESSION.pop(sid, None)

    task = asyncio.get_running_loop().create_task(_job())
    _RUNS[run_id] = {"sid": sid, "stage": stage_code, "task": task}
    _ACTIVE_RUNS_BY_SESSION[sid] = run_id
    return run_id


def _state(sid: str) -> Dict[str, Any]:
    sess = db.get_session(sid)
    if not sess:
        raise HTTPException(404, "unknown session")
    return {
        "session": sess,
        "messages": db.get_messages(sid),
        "requirements": db.get_requirements(sid),
        "design_trace": db.get_design_trace(sid),
        "test_cases": db.get_test_cases(sid),
        "executions": db.get_executions(sid),
        "feedback": db.get_feedback(sid),
        "corpus": db.corpus_inventory(),
        # a run still in progress, so a refreshed page re-attaches to its stream
        "active_run_id": _ACTIVE_RUNS_BY_SESSION.get(sid),
        "runs": db.get_runs(sid),
    }


# ---------------------------------------------------------------- endpoints
@app.get("/api/sessions")
async def sessions() -> Dict[str, Any]:
    return {"sessions": db.list_sessions()}


@app.post("/api/session", response_model=SessionOut)
async def create_session() -> SessionOut:
    sid = db.create_session()
    db.add_message(sid, "assistant", _welcome())
    return SessionOut(session_id=sid)


@app.get("/api/state/{sid}")
async def get_state(sid: str) -> Dict[str, Any]:
    return _state(sid)


@app.post("/api/message")
async def post_message(body: MessageIn) -> Dict[str, Any]:
    sid = body.session_id
    if not db.get_session(sid):
        raise HTTPException(404, "unknown session")
    db.add_message(sid, "user", body.prompt)

    routed = pipeline.interpret_message(sid, body.prompt)
    if routed.get("message"):
        db.add_message(sid, "assistant", routed["message"])
    run_id: Optional[str] = None
    if routed.get("run"):
        run_id = _launch_stage(sid, routed["run"])
    elif not routed.get("message"):
        answer = await pipeline.advisor_answer(sid, body.prompt)
        db.add_message(sid, "assistant", answer)
    return {"run_id": run_id, "state": _state(sid)}


@app.post("/api/gate")
async def post_gate(body: GateIn) -> Dict[str, Any]:
    res = pipeline.gate_action(body.session_id, body.action, body.comment)
    if res.get("error"):
        raise HTTPException(400, res["error"])
    if res.get("message"):
        db.add_message(body.session_id, "assistant", res["message"])
    run_id = None
    if res.get("run"):
        run_id = _launch_stage(body.session_id, res["run"])
    return {"run_id": run_id, "state": _state(body.session_id)}


@app.post("/api/feedback")
async def post_feedback(body: FeedbackIn) -> Dict[str, Any]:
    db.record_feedback(body.session_id, body.stage, body.artifact_ref,
                       body.sentiment, "thumbs", body.comment)
    # Positive feedback on a completed execution affirms the report into the KB.
    if body.sentiment == "positive" and body.stage == "S4":
        path = kb.write_for_stage(body.session_id, "S4")
        db.add_message(body.session_id, "assistant",
                       f"Execution report affirmed — written to the knowledge "
                       f"base (`{path}`).")
    return {"ok": True, "state": _state(body.session_id)}


@app.get("/api/agent-stream/{run_id}")
async def agent_stream(run_id: str) -> StreamingResponse:
    bus = bus_for(run_id)

    async def gen():
        last = 0
        while True:
            events = await bus.drain(after_seq=last, timeout=20.0)
            for e in events:
                last = max(last, e.seq)
                yield f"data: {json.dumps(e.as_dict(), default=str)}\n\n"
            if bus.done and not events:
                yield "data: {\"event\": \"done\"}\n\n"
                trace.drop(run_id)
                return
            if not events:
                yield ": keepalive\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.post("/api/phase0")
async def start_phase0(body: Phase0In) -> Dict[str, Any]:
    sid = body.session_id
    if not db.get_session(sid):
        raise HTTPException(404, "unknown session")
    if _ACTIVE_RUNS_BY_SESSION.get(sid):
        run_id = _ACTIVE_RUNS_BY_SESSION[sid]
        return {"run_id": run_id, "state": _state(sid)}
    db.set_stage(sid, "P0")
    run_id = _launch_stage(sid, "P0")
    return {"run_id": run_id, "state": _state(sid)}


@app.post("/api/ingest")
async def reingest() -> Dict[str, Any]:
    result = ingest.ingest_all(force=True)
    return {"ingested": result, "corpus": db.corpus_inventory(),
            "index": chunker.index_stats()}


@app.get("/api/documents")
async def documents() -> Dict[str, Any]:
    rows = await asyncio.to_thread(discovery.inventory)
    return {"documents": rows, "summary": discovery.summarize(rows)}


@app.post("/api/documents/analyze")
async def analyze_document(body: AnalyzeIn) -> Dict[str, Any]:
    try:
        return await asyncio.to_thread(discovery.analyze, body.rel_path, body.doc_type)
    except FileNotFoundError:
        raise HTTPException(404, f"{body.rel_path} is no longer in inputs/")
    except ValueError as e:
        raise HTTPException(400, str(e))


_CHUNK_JOB: Dict[str, Optional[str]] = {"run_id": None}
_SYNC_JOB: Dict[str, Optional[str]] = {"run_id": None}


@app.post("/api/inputs/sync")
async def sync_inputs() -> Dict[str, Any]:
    """Mirror the inputs Volume now (incremental) — SSE-attachable."""
    if not config.INPUTS_VOLUME:
        raise HTTPException(400, "no inputs Volume configured (VERITAS_INPUTS_VOLUME)")
    if config.INPUTS_VOLUME_MOUNTED:
        return {"run_id": None, "detail": "the Volume is mounted and read in place"}
    if _SYNC_JOB["run_id"]:
        return {"run_id": _SYNC_JOB["run_id"], "already_running": True}
    run_id = str(uuid.uuid4())
    bus = bus_for(run_id)

    async def _job() -> None:
        try:
            await _sync_inputs_job(bus)
        finally:
            await bus.close()
            _SYNC_JOB["run_id"] = None
    _SYNC_JOB["run_id"] = run_id
    asyncio.get_running_loop().create_task(_job())
    return {"run_id": run_id}


@app.post("/api/documents/chunk")
async def chunk_documents(body: ChunkIn) -> Dict[str, Any]:
    """Chunk documents in the background without clearing the corpus. One job
    at a time; a stage run on the same session takes precedence."""
    if _CHUNK_JOB["run_id"]:
        return {"run_id": _CHUNK_JOB["run_id"], "queued": 0, "already_running": True}
    if body.session_id and _ACTIVE_RUNS_BY_SESSION.get(body.session_id):
        raise HTTPException(409, "a stage is running for this session — wait for it to finish")
    try:
        items = await asyncio.to_thread(discovery.targets, body.rel_paths)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not items:
        return {"run_id": None, "queued": 0}

    run_id = str(uuid.uuid4())
    bus = bus_for(run_id)
    _CHUNK_JOB["run_id"] = run_id

    async def _job() -> None:
        try:
            res = await discovery.chunk_documents(items, bus)
            if body.session_id and db.get_session(body.session_id):
                db.add_message(body.session_id, "assistant",
                               f"Chunked **{res['documents']}** document(s) "
                               f"(**{res['chunks']}** chunks)"
                               + (f"; {len(res['failed'])} produced no content"
                                  if res["failed"] else "")
                               + ". See the Document Discovery tab.")
        except Exception as e:                              # pragma: no cover
            log.exception("chunk job failed")
            await bus.emit("corpus_chunker", "error", str(e))
        finally:
            await _backup_state()
            _CHUNK_JOB["run_id"] = None
            await bus.close()

    asyncio.get_running_loop().create_task(_job())
    return {"run_id": run_id, "queued": len(items)}


@app.post("/api/trace/retrace")
async def retrace(body: RetraceIn) -> Dict[str, Any]:
    """Re-trace one requirement's design (review of the Design Trace tab)."""
    sid = body.session_id
    sess = db.get_session(sid)
    if not sess:
        raise HTTPException(404, "unknown session")
    if not any(t["req_no"] == body.req_no for t in db.get_design_trace(sid)):
        raise HTTPException(400, f"{body.req_no} has no design trace yet")
    if _ACTIVE_RUNS_BY_SESSION.get(sid):
        raise HTTPException(409, "a run is in progress for this session")
    run_id = str(uuid.uuid4())
    bus = bus_for(run_id)

    async def _job() -> None:
        try:
            t = await stages.retrace_one(sid, body.req_no, body.note, bus)
            db.add_message(sid, "assistant",
                           f"Re-traced **{body.req_no}**: {len(t['specs'])} design specs, "
                           f"{len(t['sttm'])} STTM rows — **{t['trace_status']}**.")
        except Exception as e:                              # pragma: no cover
            log.exception("retrace failed")
            await bus.emit("design_navigator", "error", str(e))
        finally:
            await bus.close()
            _RUNS.pop(run_id, None)
            _ACTIVE_RUNS_BY_SESSION.pop(sid, None)

    task = asyncio.get_running_loop().create_task(_job())
    _RUNS[run_id] = {"sid": sid, "stage": "S2_retrace", "task": task}
    _ACTIVE_RUNS_BY_SESSION[sid] = run_id
    return {"run_id": run_id}


@app.post("/api/llm/check")
async def llm_check() -> Dict[str, Any]:
    """Call each configured model once (fast, smart, and vision with a tiny
    image) — to confirm the serving endpoints / API key after deploying."""
    import time as _time
    from . import figures, llm

    async def probe(tier: str, model: str, image: bool) -> Dict[str, Any]:
        t0 = _time.time()
        if image:
            png = figures._png(96, 96, 3, bytes([200, 16, 46]) * 96 * 96)
            out = await llm.vision("What single colour fills this image? One word.", png,
                                   "image/png", model=model, max_tokens=20, timeout=120.0)
        else:
            out = await llm.complete("Reply with the single word: ready", model=model,
                                     max_tokens=20, timeout=120.0)
        return {"tier": tier, "model": model, "ok": bool(out), "reply": (out or "")[:60],
                "ms": int((_time.time() - t0) * 1000)}

    if not llm.available():
        return {"provider": config.LLM_PROVIDER, "status": config.llm_status(), "results": []}
    checks = [probe("fast", config.MODEL_FAST, False), probe("smart", config.MODEL_SMART, False)]
    if llm.vision_available():
        checks.append(probe("vision", config.MODEL_VISION, True))
    return {"provider": config.LLM_PROVIDER, "status": config.llm_status(),
            "results": await asyncio.gather(*checks)}


@app.get("/api/kb")
async def kb_ledger() -> Dict[str, Any]:
    return {"entries": db.get_kb()}


@app.get("/api/schema")
async def schema() -> Dict[str, Any]:
    return {"tables": sqlexec.schema()}


@app.get("/api/config")
async def get_config() -> Dict[str, Any]:
    return {"app": config.APP_NAME, "tagline": config.APP_TAGLINE,
            "runtime": config.RUNTIME, "llm": config.llm_status(),
            "platform": {"sql": sqlexec.backend_status(), "inputs": volumes.status(),
                         "llm": {"provider": config.LLM_PROVIDER, "fast": config.MODEL_FAST,
                                 "smart": config.MODEL_SMART, "vision": config.MODEL_VISION},
                         "inputs_sync_run_id": _SYNC_JOB["run_id"],
                         "vector_search": config.VECTOR_SEARCH,
                         "state_db": str(config.DB_PATH)},
            "products": products.catalog_summary(),
            "markets": products.MARKETS,
            "index": chunker.index_stats()}


# ------------------------------------------------------ static frontend
_DIST = config.PROJECT_ROOT / "frontend" / "dist"
if _DIST.is_dir():  # single-origin deploy (Databricks App)
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(_DIST / "index.html")
