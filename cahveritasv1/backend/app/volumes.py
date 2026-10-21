"""CAH VERITAS — Unity Catalog Volume access: inputs mirror + state backup.

Inputs (VERITAS_INPUTS_VOLUME)
    The Volume folder holding brd/, design/, sources/, code/, uat_baseline/.
    Where /Volumes is mounted (clusters, notebooks) discovery reads it in
    place. A Databricks App has no such mount, so the folder tree is
    enumerated through the Files API and mirrored into VERITAS_INPUTS_CACHE:
    new or changed files (by size and modification time) are downloaded,
    files deleted from the Volume are removed from the mirror, and unchanged
    files are skipped — a re-sync of an unchanged Volume downloads nothing.
    Discovery, chunking and figure extraction then work on the mirror as on
    any local folder.

State backup (VERITAS_STATE_BACKUP_VOLUME)
    Optional. A Databricks App's disk is ephemeral, so the DuckDB state file
    can be restored from a Volume folder at start-up and copied back after
    each stage run or chunking job.
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import config

log = logging.getLogger("veritas.volumes")

_MANIFEST = ".volume_manifest.json"
_STATUS: Dict[str, Any] = {"last_sync": None, "last_result": None, "last_error": None,
                           "state_restored": None, "last_backup": None}

Progress = Optional[Callable[[str], None]]


def _client():
    from . import dbx
    return dbx.workspace_client()


def list_files(root: str, client: Any = None) -> List[Dict[str, Any]]:
    """Every file under a Volume folder: [{path, rel, size, modified}]."""
    client = client or _client()
    out: List[Dict[str, Any]] = []
    stack = [root.rstrip("/")]
    while stack:
        d = stack.pop()
        for e in client.files.list_directory_contents(d):
            if e.is_directory:
                stack.append(e.path.rstrip("/"))
                continue
            rel = e.path[len(root.rstrip("/")) + 1:]
            if Path(rel).name.startswith("."):
                continue
            out.append({"path": e.path, "rel": rel, "size": e.file_size,
                        "modified": e.last_modified})
    out.sort(key=lambda f: f["rel"])
    return out


def sync_inputs(progress: Progress = None, client: Any = None,
                volume: Optional[str] = None, cache: Optional[Path] = None) -> Dict[str, Any]:
    """Mirror the inputs Volume into the local cache (incremental)."""
    volume = (volume or config.INPUTS_VOLUME).rstrip("/")
    cache = Path(cache or config.INPUTS_CACHE)
    if not volume:
        return {"skipped": "no inputs Volume configured"}
    if config.INPUTS_VOLUME_MOUNTED and cache == config.INPUTS_CACHE and client is None:
        return {"skipped": "Volume is mounted — read in place", "path": volume}
    t0 = time.time()
    try:
        client = client or _client()
        files = list_files(volume, client)
        cache.mkdir(parents=True, exist_ok=True)
        mpath = cache / _MANIFEST
        try:
            manifest = json.loads(mpath.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {}
        downloaded = unchanged = 0
        for i, f in enumerate(files, 1):
            key = [f["size"], f["modified"]]
            target = cache / f["rel"]
            if manifest.get(f["rel"]) == key and target.exists():
                unchanged += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            resp = client.files.download(f["path"])
            with open(target, "wb") as out:
                shutil.copyfileobj(resp.contents, out)
            manifest[f["rel"]] = key
            downloaded += 1
            if progress and (downloaded <= 20 or downloaded % 50 == 0):
                progress(f"{i}/{len(files)} · {f['rel']}")
        current = {f["rel"] for f in files}
        removed = 0
        for rel in list(manifest):
            if rel not in current:
                (cache / rel).unlink(missing_ok=True)
                del manifest[rel]
                removed += 1
        mpath.write_text(json.dumps(manifest), encoding="utf-8")
        result = {"volume": volume, "files": len(files), "downloaded": downloaded,
                  "unchanged": unchanged, "removed": removed,
                  "seconds": round(time.time() - t0, 1)}
        _STATUS.update(last_sync=time.strftime("%Y-%m-%d %H:%M:%S"), last_result=result,
                       last_error=None)
        return result
    except Exception as e:
        _STATUS["last_error"] = f"{type(e).__name__}: {e}"
        log.exception("inputs Volume sync failed")
        raise


def restore_state(client: Any = None) -> Optional[str]:
    """Copy the DuckDB state from the backup Volume, if one is configured and
    no local state exists yet. Returns what happened."""
    folder = config.STATE_BACKUP_VOLUME
    if not folder:
        return None
    local = Path(config.DB_PATH)
    if local.exists():
        _STATUS["state_restored"] = "local state present — not restored"
        return _STATUS["state_restored"]
    try:
        client = client or _client()
        resp = client.files.download(f"{folder}/{local.name}")
        local.parent.mkdir(parents=True, exist_ok=True)
        with open(local, "wb") as out:
            shutil.copyfileobj(resp.contents, out)
        _STATUS["state_restored"] = f"restored from {folder}/{local.name}"
    except Exception as e:                                   # no backup yet, or no access
        _STATUS["state_restored"] = f"not restored ({type(e).__name__})"
    return _STATUS["state_restored"]


def backup_state(client: Any = None) -> Optional[str]:
    """Checkpoint the DuckDB file and upload it to the backup Volume."""
    folder = config.STATE_BACKUP_VOLUME
    if not folder:
        return None
    from . import db
    local = Path(config.DB_PATH)
    try:
        with db.connect() as con:
            con.execute("CHECKPOINT")
        client = client or _client()
        with open(local, "rb") as f:                       # streamed, not read into memory
            client.files.upload(f"{folder}/{local.name}", f, overwrite=True)
        _STATUS["last_backup"] = time.strftime("%Y-%m-%d %H:%M:%S")
        return _STATUS["last_backup"]
    except Exception as e:
        _STATUS["last_error"] = f"state backup: {type(e).__name__}: {e}"
        log.warning("state backup to %s failed: %s", folder, e)
        return None


def status() -> Dict[str, Any]:
    return {"source": config.INPUTS_SOURCE, "volume": config.INPUTS_VOLUME or None,
            "mounted": config.INPUTS_VOLUME_MOUNTED, "local_dir": str(config.INPUTS_DIR),
            "state_backup_volume": config.STATE_BACKUP_VOLUME or None, **_STATUS}
