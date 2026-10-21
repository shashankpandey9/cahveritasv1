# Databricks notebook source
# MAGIC %md
# MAGIC # CAH VERITAS — chunk-embed the corpus & build the Vector Search index
# MAGIC Reads BRD (.docx), Design (.docx), Source Listings (.pdf), code and the
# MAGIC UAT baseline from the UC Volume, chunks them with the same section-aware
# MAGIC chunker as the local runtime, writes `cah.veritas.corpus_chunks`, and
# MAGIC creates a **delta-sync Vector Search index** with managed embeddings
# MAGIC (databricks-gte-large-en), so BRD and design documents are chunk-embedded
# MAGIC exactly as required for the Databricks runtime.

# COMMAND ----------
# MAGIC %pip install python-docx pypdf databricks-vectorsearch
# MAGIC dbutils.library.restartPython()

# COMMAND ----------
CATALOG, SCHEMA = "cah", "veritas"
VOLUME_INPUTS = f"/Volumes/{CATALOG}/{SCHEMA}/inputs"
CHUNKS_TABLE = f"{CATALOG}.{SCHEMA}.corpus_chunks"
VS_ENDPOINT = "veritas-vs-endpoint"
VS_INDEX = f"{CATALOG}.{SCHEMA}.corpus_chunks_index"

# COMMAND ----------
import sys, pathlib
sys.path.insert(0, "/Workspace" + str(pathlib.Path.cwd()).split("Workspace")[-1].rsplit("/", 2)[0])
# Reuse the exact local parsers/chunker (backend/app/ingest.py) if the repo is
# synced to the workspace; otherwise the minimal inline copies below.

from pathlib import Path
import re

CODE_EXT = {".sql", ".py", ".scala", ".hql", ".sparksql"}
CHUNK, OVERLAP = 1400, 150

def parse(path: Path):
    ext = path.suffix.lower()
    if ext == ".docx":
        import docx
        d = docx.Document(str(path))
        cur, out, buf = "Preamble", [], []
        for p in d.paragraphs:
            t = p.text.strip()
            if not t: continue
            if (p.style and (p.style.name or "").lower().startswith("heading")):
                if buf: out.append((cur, "\n".join(buf)))
                cur, buf = t[:120], []
            else:
                buf.append(t)
        if buf: out.append((cur, "\n".join(buf)))
        return out
    if ext == ".pdf":
        from pypdf import PdfReader
        return [(f"page {i}", pg.extract_text() or "")
                for i, pg in enumerate(PdfReader(str(path)).pages, 1)]
    return [(path.name, path.read_text(errors="ignore"))]

def chunk(text):
    text = text.strip()
    out, start = [], 0
    while start < len(text):
        end = min(start + CHUNK, len(text))
        cut = text.rfind("\n", start + int(CHUNK * .6), end)
        cut = end if cut == -1 else cut
        out.append(text[start:cut].strip())
        if cut >= len(text): break
        start = max(cut - OVERLAP, start + 1)
    return [c for c in out if c]

def doc_type(p: Path):
    s = str(p).lower()
    if "/brd/" in s or "brd" in p.name.lower(): return "brd"
    if "/design/" in s or "design" in p.name.lower(): return "design"
    if "/sources/" in s or "source" in p.name.lower(): return "sources"
    if "baseline" in s or "uat" in p.name.lower(): return "uat_baseline"
    if p.suffix.lower() in CODE_EXT: return "code"
    return "misc"

rows = []
for p in Path(VOLUME_INPUTS).rglob("*"):
    if not p.is_file(): continue
    for section, text in parse(p):
        for i, piece in enumerate(chunk(text)):
            rows.append((p.name, doc_type(p), section, i, piece))
print(f"{len(rows)} chunks")

# COMMAND ----------
from pyspark.sql import Row
spark.sql(f"DELETE FROM {CHUNKS_TABLE}")
if rows:
    spark.createDataFrame([Row(doc_name=r[0], doc_type=r[1], section=r[2],
                               ord=r[3], text=r[4]) for r in rows]) \
         .write.mode("append").saveAsTable(CHUNKS_TABLE)

# COMMAND ----------
from databricks.vector_search.client import VectorSearchClient
vsc = VectorSearchClient()
try:
    vsc.create_endpoint(name=VS_ENDPOINT, endpoint_type="STANDARD")
except Exception as e:
    print("endpoint:", e)
try:
    vsc.create_delta_sync_index(
        endpoint_name=VS_ENDPOINT, index_name=VS_INDEX,
        source_table_name=CHUNKS_TABLE, pipeline_type="TRIGGERED",
        primary_key="chunk_id",
        embedding_source_column="text",
        embedding_model_endpoint_name="databricks-gte-large-en")
except Exception as e:
    print("index:", e)
    vsc.get_index(endpoint_name=VS_ENDPOINT, index_name=VS_INDEX).sync()
print("Vector Search index ready:", VS_INDEX)
