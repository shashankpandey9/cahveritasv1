import React, { useCallback, useEffect, useRef, useState } from 'react'
import {
  analyzeDocument, chunkDocuments, createSession, gate, getConfig, getDocuments, getSessions,
  getState, openAgentStream, reingest, retraceRequirement, sendMessage, startPhase0, syncInputs,
} from './api.js'
import GileadBrand from './components/GileadBrand.jsx'
import StageRail from './components/StageRail.jsx'
import ChatArea from './components/ChatArea.jsx'
import AgentStream from './components/AgentStream.jsx'
import RequirementsPanel from './components/RequirementsPanel.jsx'
import DesignTracePanel from './components/DesignTracePanel.jsx'
import TestCasesPanel from './components/TestCasesPanel.jsx'
import ExecutionPanel from './components/ExecutionPanel.jsx'
import DocumentDiscoveryPanel from './components/DocumentDiscoveryPanel.jsx'
import DiscoveryStatus from './components/DiscoveryStatus.jsx'
import NeuroEvents from './components/NeuroEvents.jsx'

const TABS = [
  { id: 'docs', label: '0 · Document Discovery' },
  { id: 'req', label: '1 · Requirements' },
  { id: 'design', label: '2 · Design Trace' },
  { id: 'uat', label: '3 · UAT Cases' },
  { id: 'exec', label: '4 · Execution' },
]
const STAGE_TAB = { S0: 'docs', P0: 'docs', P0_review: 'docs', S1: 'req', S1_review: 'req', S2: 'design', S2_review: 'design',
  S3: 'uat', S3_review: 'uat', S4_confirm: 'uat', S4: 'exec', done: 'exec' }
const STAGE_LABEL = { S0: 'not started', P0: 'Phase 0 running', P0_review: 'Phase 0 review', S1: 'Requirements running',
  S1_review: 'Requirements review', S2: 'Design trace running', S2_review: 'Design trace review', S3: 'UAT generation running',
  S3_review: 'UAT review', S4_confirm: 'Execution confirmation', S4: 'Executing', done: 'Complete' }

// Browser storage is a convenience only (the database holds the session):
// it may be unavailable, so every access is guarded.
const store = {
  get: (k) => { try { return localStorage.getItem(k) } catch { return null } },
  set: (k, v) => { try { localStorage.setItem(k, v) } catch { /* private mode */ } },
}
const SID_KEY = 'veritas.sid'

/** The session to reopen: the one this browser used last, else the most
 *  progressed recent session, else a new one. */
async function restoreSession() {
  const saved = store.get(SID_KEY)
  if (saved) {
    const st = await getState(saved).catch(() => null)
    if (st?.session) return st
  }
  const { sessions = [] } = await getSessions().catch(() => ({}))
  const pick = sessions.find((s) => s.requirements > 0) || sessions.find((s) => s.stage !== 'S0')
  if (pick) {
    const st = await getState(pick.session_id).catch(() => null)
    if (st?.session) return st
  }
  const { session_id } = await createSession()
  return getState(session_id)
}

function lastRunEvents(st) {
  const runs = (st?.runs || []).filter((r) => r.events?.length)
  return runs.length ? runs[runs.length - 1].events : []
}

/** Where the app is reading and writing: analytics store for the UAT SQL,
 *  inputs source, and the LLM in use. */
function PlatformBadge({ p, llm }) {
  const sql = p.sql?.backend === 'databricks'
    ? `SQL: Databricks ${p.sql.catalog}.${(p.sql.schemas || []).join(',')}`
    : 'SQL: DuckDB'
  const inputs = p.inputs?.source === 'volume' ? `Inputs: ${p.inputs.volume}` : 'Inputs: app folder'
  const problems = [p.sql?.last_error, ...(p.sql?.missing_settings || []).map((m) => `${m} not set`),
    p.inputs?.last_error].filter(Boolean)
  return (
    <div className={'runtime' + (problems.length ? ' warnRuntime' : '')}
      title={[llm, sql, inputs, `State: ${p.state_db}`, p.inputs?.state_backup_volume ? `State backup: ${p.inputs.state_backup_volume}` : 'State backup: off',
        ...problems].join('\n')}>
      {sql} · {inputs}{problems.length ? ' · ⚠' : ''}
    </div>
  )
}

export default function App() {
  const [sid, setSid] = useState(null)
  const [cfg, setCfg] = useState(null)
  const [state, setState] = useState(null)
  const [tab, setTabState] = useState('docs')
  const [focus, setFocus] = useState(null)        // {tab, id} — cross-tab navigation target
  const [sessions, setSessions] = useState([])
  const [docs, setDocs] = useState(null)
  const [docsLoading, setDocsLoading] = useState(false)
  const [docsError, setDocsError] = useState(null)
  const [events, setEvents] = useState([])
  const [running, setRunning] = useState(false)
  const [showProceedModal, setShowProceedModal] = useState(false)
  const [bootError, setBootError] = useState(null)
  const [banner, setBanner] = useState(null)
  const [neuroOpen, setNeuroOpen] = useState(() => store.get('veritas.neuro.open') === '1')
  const esRef = useRef(null)
  const stageRef = useRef(null)

  const setTab = useCallback((t, s = sid) => {
    setTabState(t)
    if (s) store.set('veritas.tab.' + s, t)
  }, [sid])

  useEffect(() => { store.set('veritas.neuro.open', neuroOpen ? '1' : '0') }, [neuroOpen])

  const loadDocs = useCallback(async () => {
    setDocsLoading(true)
    try {
      setDocs(await getDocuments())
      setDocsError(null)
    } catch (error) {
      setDocsError('Unable to load the document inventory.')
    } finally {
      setDocsLoading(false)
    }
  }, [])

  const loadSessions = useCallback(() => {
    getSessions().then((r) => setSessions(r.sessions || [])).catch(() => {})
  }, [])

  /** Apply a state snapshot. The tab follows the stage only when the stage
   *  changes — a refresh never moves the reviewer off the tab they chose. */
  const applyState = useCallback((next) => {
    if (!next?.session) return
    setState(next)
    const stage = next.session.stage
    if (stageRef.current !== null && stageRef.current !== stage && STAGE_TAB[stage]) {
      setTab(STAGE_TAB[stage], next.session.session_id)
    }
    stageRef.current = stage
  }, [setTab])

  const refresh = useCallback(async (s) => {
    try {
      const next = s || (sid && await getState(sid))
      applyState(next)
      return next
    } catch (error) {
      setBanner(error?.message === '404' ? 'Session could not be restored. Please reload the app.'
        : 'Unable to refresh the workspace state.')
      return null
    }
  }, [sid, applyState])

  const attachRun = useCallback((runId) => {
    if (!runId) return
    setEvents([])
    setNeuroOpen(true)
    setRunning(true)
    esRef.current && esRef.current.close()
    esRef.current = openAgentStream(runId,
      (ev) => setEvents((prev) => [...prev, ev]),
      async () => { setRunning(false); await refresh(); loadDocs(); loadSessions() })
  }, [refresh, loadDocs, loadSessions])

  const openSession = useCallback(async (st) => {
    const id = st.session.session_id
    store.set(SID_KEY, id)
    setSid(id)
    stageRef.current = st.session.stage
    setState(st)
    setTabState(store.get('veritas.tab.' + id) || STAGE_TAB[st.session.stage] || 'docs')
    setRunning(false)
    esRef.current && esRef.current.close()
    if (st.active_run_id) {
      // a run was in progress when the page was refreshed: re-attach to its
      // stream, which replays the run's events from the start
      setEvents([])
      setRunning(true)
      esRef.current = openAgentStream(st.active_run_id,
        (ev) => setEvents((prev) => [...prev, ev]),
        async () => {
          setRunning(false)
          const fresh = await getState(id).catch(() => null)
          if (fresh) applyState(fresh)
          loadDocs(); loadSessions()
        })
    } else {
      setEvents(lastRunEvents(st))
    }
  }, [applyState, loadDocs, loadSessions])

  useEffect(() => {
    (async () => {
      try {
        const conf = await getConfig()
        setCfg(conf)
        await openSession(await restoreSession())
        loadDocs()
        loadSessions()
        // the inputs Volume is being mirrored at start-up: show it, then refresh the inventory
        if (conf?.platform?.inputs_sync_run_id) {
          openAgentStream(conf.platform.inputs_sync_run_id, () => {}, () => { loadDocs(); getConfig().then(setCfg).catch(() => {}) })
        }
      } catch (error) {
        setBootError('Unable to reach the CAH VERITAS backend. Start the FastAPI service on localhost:8000 and refresh this page.')
      }
    })()
    return () => esRef.current && esRef.current.close()
  }, [])  // eslint-disable-line react-hooks/exhaustive-deps

  const onSwitchSession = async (value) => {
    try {
      const st = value === '__new__'
        ? await getState((await createSession()).session_id)
        : await getState(value)
      await openSession(st)
      loadSessions()
      setBanner(null)
    } catch (error) {
      setBanner('That session could not be opened.')
    }
  }

  const act = (fn, failMsg) => async (...args) => {
    try {
      const res = await fn(...args)
      if (res?.state) await refresh(res.state)
      if (res?.run_id) attachRun(res.run_id)
      setBanner(null)
      return res
    } catch (error) {
      setBanner(error?.message === '409' ? 'Another run is in progress for this session — wait for it to finish.' : failMsg)
      return null
    }
  }
  const onSend = act((text) => sendMessage(sid, text), 'The message could not be sent. Check that the backend is running and retry.')
  const onPhase0 = act(() => startPhase0(sid), 'Phase 0 could not start. Ensure the backend is reachable and try again.')
  const onGate = act((action, comment) => gate(sid, action, comment), 'The review gate could not be submitted. Please retry once the backend is available.')
  const onRetrace = act((reqNo, note) => retraceRequirement(sid, reqNo, note), 'The re-trace could not start.')

  const onChunk = async (relPaths) => {
    try {
      const res = await chunkDocuments(sid, relPaths)
      if (res.run_id) attachRun(res.run_id)
      else { setBanner('Nothing to chunk — every selected document is already complete.'); loadDocs() }
    } catch (error) {
      setBanner(error?.message === '409'
        ? 'A stage is still running — chunk once it finishes.'
        : 'Chunking could not start. Check that the backend is running and retry.')
    }
  }

  const onAnalyze = async (row) => {
    const res = await analyzeDocument(row.rel_path, row.doc_type)
    loadDocs()
    return res
  }

  const onSyncInputs = async () => {
    try {
      const res = await syncInputs()
      if (res.run_id) attachRun(res.run_id)
      else setBanner(res.detail || 'Nothing to sync.')
    } catch (error) {
      setBanner("The Volume sync could not start — check VERITAS_INPUTS_VOLUME and the app's READ VOLUME grant.")
    }
  }

  const onReingest = async () => {
    await reingest().catch(() => null)
    await refresh()
    loadDocs()
  }

  const goTo = (t, id) => { setTab(t); setFocus(id ? { tab: t, id, at: Date.now() } : null) }

  const onProceedConfirm = async () => {
    setShowProceedModal(false)
    await onGate('accept', 'User accepted the Phase 0 audit and wants to proceed to Phase 1.')
  }

  const docSummary = docs?.summary
  const proceedSummary = docSummary ? [
    `${docSummary.discovered} documents discovered`,
    `${docSummary.complete} fully chunked`,
    `${docSummary.partial} partial`,
    `${docSummary.not_chunked} not chunked`,
    `${docSummary.chunks} chunks indexed`,
  ].join(' · ') : 'Document inventory not loaded yet.'

  if (bootError) {
    return (
      <div className="app appError">
        <div className="errorCard">
          <div className="errorTitle">CAH VERITAS is not ready</div>
          <div className="errorText">{bootError}</div>
          <button className="btn primary" onClick={() => window.location.reload()}>Reload</button>
        </div>
      </div>
    )
  }

  if (!state) return <div className="app"><div className="empty">Restoring your CAH VERITAS session…</div></div>
  const stage = state.session.stage
  const focusFor = (t) => (focus && focus.tab === t ? focus : null)

  return (
    <div className="app">
      {banner && <div className="errorBanner" onClick={() => setBanner(null)} title="Dismiss">{banner}</div>}
      <div className="titlebar">
        <GileadBrand />
        <div>
          <div className="appname">CAH <span>VERITAS</span></div>
          <div className="tagline">Commercial Analytics Hub — Validation Engine for Requirements,
            Integration &amp; Testing of Analytical Solutions</div>
        </div>
        <div className="spacer" />
        <div className="titleActions">
          {cfg?.platform && <PlatformBadge p={cfg.platform} llm={cfg.llm} />}
          <select className="sessionSelect" value={sid || ''} onChange={(e) => onSwitchSession(e.target.value)}
            title="Your review sessions — work is saved; pick one to reopen it">
            {sessions.filter((s) => s.stage !== 'S0' || s.requirements > 0 || s.session_id === sid).map((s) => (
              <option key={s.session_id} value={s.session_id}>
                {s.session_id.slice(0, 8)} · {STAGE_LABEL[s.stage] || s.stage}
                {s.requirements ? ` · ${s.requirements} req` : ''}{s.design_trace ? ` · ${s.design_trace} traced` : ''}
                {s.test_cases ? ` · ${s.test_cases} cases` : ''} · {(s.last_activity || '').slice(5, 16)}
              </option>
            ))}
            {!sessions.some((s) => s.session_id === sid) && sid && <option value={sid}>{sid.slice(0, 8)} · current</option>}
            <option value="__new__">+ New review session</option>
          </select>
          <button className={'btn neuroToggle' + (neuroOpen ? ' on' : '')} onClick={() => setNeuroOpen((v) => !v)}
            title={neuroOpen ? 'Close the Neuro Events view' : 'Open the Neuro Events view (agent network by phase)'}>
            Neuro Events
          </button>
          <button className="btn" onClick={onPhase0} title="Start explicit Phase 0 ingestion">Phase 0</button>
          <button className="btn ghost" onClick={onReingest} title="Rebuild the whole corpus from inputs/ (clears existing chunks)">Re-ingest</button>
        </div>
      </div>

      <StageRail stage={stage} />

      <div className="body">
        <div className="left">
          <DiscoveryStatus
            stage={stage}
            docs={docs}
            docsLoading={docsLoading}
            running={running}
            onStart={onPhase0}
            onReview={() => setTab('docs')}
            onAccept={() => onGate('accept', 'Phase 0 reviewed and accepted.')}
            onRefine={(note) => onGate('refine', note)}
            onProceed={() => setShowProceedModal(true)}
            onChunkRemaining={() => onChunk(null)}
          />
          <ChatArea
            messages={state.messages}
            stage={stage}
            running={running}
            onSend={onSend}
            onGate={onGate}
          />
          <AgentStream events={events} running={running} />
        </div>

        <div className="right">
          <div className="tabs">
            {TABS.map(t => (
              <button key={t.id} className={'tab' + (tab === t.id ? ' on' : '')}
                onClick={() => setTab(t.id)}>{t.label}</button>
            ))}
          </div>
          {tab === 'docs' && (
            <DocumentDiscoveryPanel
              docs={docs}
              loading={docsLoading}
              error={docsError}
              busy={running}
              onReload={loadDocs}
              onAnalyze={onAnalyze}
              onChunk={onChunk}
              inputs={cfg?.platform?.inputs}
              onSyncInputs={onSyncInputs}
            />
          )}
          {tab === 'req' && <RequirementsPanel sid={sid} items={state.requirements} onRefresh={refresh}
            focus={focusFor('req')} traces={state.design_trace} cases={state.test_cases}
            onOpenDesign={(r) => goTo('design', r)} onOpenTests={(r) => goTo('uat', r)} />}
          {tab === 'design' && <DesignTracePanel sid={sid} items={state.design_trace} requirements={state.requirements}
            cases={state.test_cases} stage={stage} running={running} onRefresh={refresh} focus={focusFor('design')}
            onRetrace={onRetrace} onOpenReq={(r) => goTo('req', r)} onOpenTests={(r) => goTo('uat', r)} />}
          {tab === 'uat' && <TestCasesPanel sid={sid} items={state.test_cases} requirements={state.requirements}
            onRefresh={refresh} focus={focusFor('uat')}
            onOpenReq={(r) => goTo('req', r)} onOpenDesign={(r) => goTo('design', r)} />}
          {tab === 'exec' && <ExecutionPanel sid={sid} items={state.executions} cases={state.test_cases} onRefresh={refresh} />}
        </div>
      </div>

      <NeuroEvents
        open={neuroOpen}
        onClose={() => setNeuroOpen(false)}
        events={events}
        running={running}
        runs={state.runs}
        stage={stage}
      />

      {showProceedModal && (
        <div className="modalBackdrop" onClick={() => setShowProceedModal(false)}>
          <div className="modalCard" onClick={(e) => e.stopPropagation()}>
            <div className="modalTitle">Proceed to Phase 1</div>
            <div className="modalBody">
              <p>Current corpus summary:</p>
              <div className="modalSummary">{proceedSummary}</div>
              <div className="modalList">
                <strong>By document type:</strong>
                <ul>
                  {Object.entries(docSummary?.by_type || {}).map(([t, b]) => (
                    <li key={t}>{t}: {b.complete} of {b.discovered} fully chunked
                      {b.partial ? ` · ${b.partial} partial` : ''}{b.not_chunked ? ` · ${b.not_chunked} not chunked` : ''}
                      {b.unsupported ? ` · ${b.unsupported} unsupported` : ''}</li>
                  ))}
                </ul>
              </div>
              <p className="warningText">
                {!docSummary?.required_ready
                  ? 'The requirements document is not fully chunked — Phase 1 cannot start until it is.'
                  : (docSummary.partial || docSummary.not_chunked)
                    ? 'The requirements document is complete, but some other documents are not fully chunked. Proceeding keeps them out of retrieval until they are chunked.'
                    : 'Every ingestible document is fully chunked.'}
              </p>
            </div>
            <div className="modalActions">
              <button className="btn secondary" onClick={() => setShowProceedModal(false)}>Keep reviewing</button>
              <button className="btn primary" disabled={!docSummary?.required_ready} onClick={onProceedConfirm}>Continue to Phase 1</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
