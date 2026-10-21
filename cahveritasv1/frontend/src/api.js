// Thin API client. Same-origin in prod; Vite proxies /api in dev.
const j = (r) => { if (!r.ok) throw new Error(`${r.status}`); return r.json() }

export const createSession = () =>
  fetch('/api/session', { method: 'POST' }).then(j)

export const getState = (sid) => fetch(`/api/state/${sid}`).then(j)

export const sendMessage = (sid, prompt) =>
  fetch('/api/message', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sid, prompt })
  }).then(j)

export const startPhase0 = (sid) =>
  fetch('/api/phase0', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sid })
  }).then(j)

export const gate = (sid, action, comment = '') =>
  fetch('/api/gate', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sid, action, comment })
  }).then(j)

export const sendFeedback = (sid, stage, ref, sentiment, comment = '') =>
  fetch('/api/feedback', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sid, stage, artifact_ref: ref, sentiment, comment })
  }).then(j)

export const getConfig = () => fetch('/api/config').then(j)

// Mirror the inputs Unity Catalog Volume now (Databricks) — returns a run to attach to
export const syncInputs = () => fetch('/api/inputs/sync', { method: 'POST' }).then(j)
export const getKb = () => fetch('/api/kb').then(j)

export const openAgentStream = (runId, onEvent, onDone) => {
  const es = new EventSource(`/api/agent-stream/${runId}`)
  es.onmessage = (m) => {
    try {
      const d = JSON.parse(m.data)
      if (d.event === 'done') { es.close(); onDone && onDone() }
      else onEvent(d)
    } catch { /* keepalive */ }
  }
  es.onerror = () => { es.close(); onDone && onDone() }
  return es
}

export const reingest = () => fetch('/api/ingest', { method: 'POST' }).then(j)

// Document discovery (Phase 0): the inventory grid and its coverage checks.
export const getDocuments = () => fetch('/api/documents').then(j)

export const analyzeDocument = (relPath, docType) =>
  fetch('/api/documents/analyze', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rel_path: relPath, doc_type: docType })
  }).then(j)

// relPaths omitted → every document that is not fully chunked
export const chunkDocuments = (sid, relPaths = null) =>
  fetch('/api/documents/chunk', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sid, rel_paths: relPaths })
  }).then(j)

// Sessions: restore the last session after a refresh instead of starting over.
export const getSessions = () => fetch('/api/sessions').then(j)

// Re-run the design trace of one requirement (Design Trace review).
export const retraceRequirement = (sid, reqNo, note = '') =>
  fetch('/api/trace/retrace', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sid, req_no: reqNo, note })
  }).then(j)
