"""Databricks integration seams, exercised without a workspace.

A fake Files API client stands in for databricks.sdk's WorkspaceClient.files
(list_directory_contents / download / upload), so the Volume mirror and the
state backup run end to end on local temp folders.
"""
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_TMP = Path(tempfile.mkdtemp())
os.environ["VERITAS_DB_PATH"] = str(_TMP / "state" / "veritas.duckdb")

from backend.app import config, db, sqlexec, volumes  # noqa: E402

config.DB_PATH = Path(os.environ["VERITAS_DB_PATH"])


class FakeFiles:
    """In-memory stand-in for WorkspaceClient.files over a /Volumes tree."""

    def __init__(self, files):
        self.files = dict(files)          # "/Volumes/c/s/v/inputs/brd/a.docx" -> (bytes, mtime)
        self.downloads = 0
        self.uploads = {}

    def list_directory_contents(self, directory_path):
        d = directory_path.rstrip("/") + "/"
        seen_dirs = set()
        for path in sorted(self.files):
            if not path.startswith(d):
                continue
            rest = path[len(d):]
            if "/" in rest:
                sub = d + rest.split("/")[0]
                if sub not in seen_dirs:
                    seen_dirs.add(sub)
                    yield SimpleNamespace(path=sub, is_directory=True, file_size=None, last_modified=None)
            else:
                data, mtime = self.files[path]
                yield SimpleNamespace(path=path, is_directory=False, file_size=len(data), last_modified=mtime)

    def download(self, file_path):
        if file_path not in self.files:
            raise FileNotFoundError(file_path)
        self.downloads += 1
        return SimpleNamespace(contents=io.BytesIO(self.files[file_path][0]))

    def upload(self, file_path, contents, overwrite=False):
        self.uploads[file_path] = contents.read()


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


class VolumeMirrorTest(unittest.TestCase):
    ROOT = "/Volumes/cah/veritas/raw/inputs"

    def test_sync_is_incremental_and_tracks_deletions(self):
        fake = FakeFiles({
            f"{self.ROOT}/brd/BRD.docx": (b"brd-v1", 100),
            f"{self.ROOT}/design/Claim.xlsx": (b"xlsx", 100),
            f"{self.ROOT}/code/dags/a/job.py": (b"print(1)", 100),
            f"{self.ROOT}/code/.hidden": (b"x", 100),
        })
        client = SimpleNamespace(files=fake)
        cache = _TMP / "mirror"

        r1 = volumes.sync_inputs(client=client, volume=self.ROOT, cache=cache)
        self.assertEqual((r1["files"], r1["downloaded"], r1["unchanged"]), (3, 3, 0))
        self.assertEqual((cache / "code/dags/a/job.py").read_bytes(), b"print(1)")
        self.assertFalse((cache / "code/.hidden").exists())

        r2 = volumes.sync_inputs(client=client, volume=self.ROOT, cache=cache)
        self.assertEqual((r2["downloaded"], r2["unchanged"]), (0, 3))      # nothing re-downloaded

        fake.files[f"{self.ROOT}/brd/BRD.docx"] = (b"brd-v2-longer", 200)     # changed
        del fake.files[f"{self.ROOT}/design/Claim.xlsx"]                      # deleted
        r3 = volumes.sync_inputs(client=client, volume=self.ROOT, cache=cache)
        self.assertEqual((r3["downloaded"], r3["removed"]), (1, 1))
        self.assertEqual((cache / "brd/BRD.docx").read_bytes(), b"brd-v2-longer")
        self.assertFalse((cache / "design/Claim.xlsx").exists())


class StateBackupTest(unittest.TestCase):
    def test_backup_then_restore(self):
        folder = "/Volumes/cah/veritas/state"
        fake = FakeFiles({})
        client = SimpleNamespace(files=fake)
        old = config.STATE_BACKUP_VOLUME
        config.STATE_BACKUP_VOLUME = folder
        try:
            db.init()
            sid = db.create_session()
            self.assertIsNotNone(volumes.backup_state(client=client))
            name = Path(config.DB_PATH).name
            uploaded = fake.uploads[f"{folder}/{name}"]
            self.assertGreater(len(uploaded), 0)

            Path(config.DB_PATH).unlink()                          # a fresh app container
            fake.files[f"{folder}/{name}"] = (uploaded, 1)
            self.assertIn("restored", volumes.restore_state(client=client))
            self.assertIsNotNone(db.get_session(sid))
        finally:
            config.STATE_BACKUP_VOLUME = old


class SqlBackendTest(unittest.TestCase):
    def test_duckdb_is_the_default(self):
        self.assertEqual(sqlexec.backend_status()["backend"], "duckdb")
        self.assertIn("DuckDB", sqlexec.dialect())

    def test_databricks_failure_is_reported_not_replaced_by_duckdb(self):
        old = (config.SQL_BACKEND, config.DBX_WAREHOUSE, config.DBX_CATALOG,
               config.DBX_SCHEMA, config.DBX_SCHEMAS)
        config.SQL_BACKEND, config.DBX_WAREHOUSE = "databricks", ""
        config.DBX_CATALOG, config.DBX_SCHEMA, config.DBX_SCHEMAS = "cah", "", []
        sqlexec._DBX.update(schema=None, at=0.0, error=None)
        try:
            st = sqlexec.backend_status()
            self.assertEqual(st["backend"], "databricks")
            self.assertIn("DATABRICKS_SQL_WAREHOUSE_ID", st["missing_settings"])
            self.assertEqual(sqlexec.schema(), [])                 # no silent DuckDB fallback
            self.assertIsNotNone(sqlexec._DBX["error"])
            self.assertIn("Databricks SQL", sqlexec.schema_text())
            res = sqlexec.execute("SELECT 1")
            self.assertIsNotNone(res["error"])
        finally:
            (config.SQL_BACKEND, config.DBX_WAREHOUSE, config.DBX_CATALOG,
             config.DBX_SCHEMA, config.DBX_SCHEMAS) = old
            sqlexec._DBX.update(schema=None, at=0.0, error=None)


if __name__ == "__main__":
    unittest.main()
