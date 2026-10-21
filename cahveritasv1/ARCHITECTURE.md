# CAH VERITAS — Architecture

```
                             ┌────────────────────────────────────────────┐
  inputs/                    │            React UI (Gilead-branded)       │
  brd/*.docx  design/*.docx  │  StageRail · Chat+Gates · AgentStream      │
  sources/*.pdf  code/**     │  Requirements / DesignTrace / UAT / Exec   │
  uat_baseline/*             │  FeedbackBar (👍/👎 + comment everywhere)   │
      │                      └───────────────┬────────────────────────────┘
      ▼ ingest (once, persisted)             │ REST + SSE (/api/*)
┌───────────────┐            ┌───────────────▼────────────────────────────┐
│ docparse.py   │            │  FastAPI (backend/app/main.py)             │
│  docx (OOXML, │            │  sessions · stage machine · gates ·        │
│  stdlib) pdf  │            │  feedback · KB · agent-stream SSE          │
│  md code →    │            └───────┬───────────────────────┬────────────┘
│  BLOCK stream │                    │                       │
└──────┬────────┘                    │                       │
       ▼                             │                       │
┌───────────────┐                    │                       │
│ chunker.py    │  block-atomic,     │                       │
│ (corpus_      │  heading-prefixed, │                       │
│  chunker)     │  span-addressable  │                       │
│  → ingest.py  │──chunks+blocks──►  │                       │
└───────────────┘                    │                       │
                          ┌──────────▼──────────┐   ┌────────▼─────────┐
                          │ pipeline.py         │   │ trace.py         │
                          │ AAOSA driver:       │   │ per-run bus with │
                          │ Determine (fast,    │   │ agent timings    │
                          │ concurrent) →       │   └──────────────────┘
                          │ Fulfill (stages)    │
                          └──────────┬──────────┘
        ┌────────────────────────────┼──────────────────────────────┐
        ▼                            ▼                              ▼
┌──────────────┐            ┌──────────────────┐          ┌──────────────────┐
│ stages.py    │            │ llm.py           │          │ sqlexec.py       │
│ S1 reqs ─────┼─► reqenum  │ tiering fast/    │          │ schema probe +   │
│ S2 trace     │──retrieval─│ smart · prompt   │          │ read-only SQL    │
│ S3 UAT gen   │  (embeddings.py: local index  │          │ DuckDB ⇄ DBX SQL │
│ S4 execute   │   or Databricks Vector Search)│          └──────────────────┘
└──────┬───────┘            └──────────────────┘
       │
       │  reqenum.py — Stage 1's two agents
       │  ┌────────────────────────────────────────────────────────────┐
       │  │ enumerate_structural()   deterministic, LLM-free traversal │
       │  │   sections → R1..Rn · blocks → R#.i, R#.ii  (count final)  │
       │  │ enrich_register()        RAG per group, batched + salvaged │
       │  │   + products.py lexicon: branded·molecule·abbrev·full name │
       │  └────────────────────────────────────────────────────────────┘
       │ accept + 👍
       ▼
┌──────────────┐   ledger    ┌──────────────────┐
│ kb.py → kb/  │────────────►│ db.py (DuckDB)   │  sessions · runs · reqs ·
│ *.md         │             │                  │  trace · cases · executions ·
└──────────────┘             └──────────────────┘  feedback · kb · corpus
```

## Stage machine
`S0 → S1 → S1_review → S2 → S2_review → S3 → S3_review → S4_confirm → S4 → done`
Gates accept `accept | refine(note) | confirm_execute`; refine re-runs the
stage with the critique injected into every agent prompt (`_refines()`).

## AAOSA
Registry `registries/cah_veritas.hocon` declares the network; `pipeline.py`
executes the protocol (Determine polls concurrently on the fast tier with a
120-token budget; Fulfill lands in `stages.py` where retrieval-grounded work
happens). Installing `neuro-san` upgrades to full DirectAgentSession serving
with zero code changes elsewhere.

## Latency levers (measured intent)
tiering · per-call token budgets · prompt-cached corpus/schema prefix ·
bounded async fan-out (per-REQ, per-TC) · ingest-once persistence ·
top-k vector prefilter · SSE + live agent timings.
