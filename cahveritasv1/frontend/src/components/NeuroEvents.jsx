import React, { useEffect, useMemo, useRef, useState } from 'react'

/* Neuro Events — the AAOSA agent network, laid out by phase so the graph
   reads like the delivery flow: Phase 0 ingestion → 1 requirements →
   2 design & environment trace → 3 UAT generation → 4 execution.
   Window modes: normal (docked, resizable) · maximized · minimized ·
   fullscreen. The graph zooms (buttons, mouse wheel) and pans (drag). */

const PHASES = [
  { id: 'P0', label: 'Phase 0 · Ingestion',
    agents: ['intake_librarian', 'office_structure_extractor', 'figure_extractor',
      'image_triage', 'vision_transcriber', 'corpus_chunker'] },
  { id: 'S1', label: '1 · Requirements',
    agents: ['requirements_enumerator', 'requirements_rag_analyst', 'product_steward'] },
  { id: 'S2', label: '2 · Design & Environment Trace',
    agents: ['sttm_mapper', 'design_navigator', 'data_environment_scout', 'trace_auditor'] },
  { id: 'S3', label: '3 · UAT Case Generation',
    agents: ['uat_test_designer', 'text2sql_engineer', 'coverage_referee'] },
  { id: 'S4', label: '4 · Agentic Test Execution',
    agents: ['test_executor', 'verdict_explainer', 'kb_curator'] },
]
const STAGE_PHASE = { S0: -1, P0: 0, P0_review: 0, S1: 1, S1_review: 1, S2: 2, S2_review: 2,
  S3: 3, S3_review: 3, S4_confirm: 3, S4: 4, done: 5 }

const W = 1520, COL = 300, X0 = 160, NODE_W = 236, NODE_H = 50, Y_ADVISOR = 92, Y0 = 250, DY = 78
const H = Y0 + DY * 5 + 90

function layout() {
  const nodes = [{ id: 'veritas_advisor', x: W / 2, y: Y_ADVISOR, phase: -1 }]
  PHASES.forEach((p, pi) => p.agents.forEach((a, i) => {
    nodes.push({ id: a, x: X0 + pi * COL, y: Y0 + i * DY, phase: pi })
  }))
  const edges = []
  PHASES.forEach((p, pi) => {
    edges.push(['veritas_advisor', p.agents[0], 'orchestrates'])
    p.agents.slice(1).forEach((a, i) => edges.push([p.agents[i], a, 'flow']))
    if (pi < PHASES.length - 1) edges.push([p.agents[p.agents.length - 1], PHASES[pi + 1].agents[0], 'handoff'])
  })
  return { nodes, edges, byId: Object.fromEntries(nodes.map((n) => [n.id, n])) }
}
const LAYOUT = layout()

function wrapLabel(id) {
  const words = id.split('_')
  const lines = ['']
  for (const w of words) {
    const cur = lines[lines.length - 1]
    if (cur && (cur + ' ' + w).length > 22) lines.push(w)
    else lines[lines.length - 1] = cur ? cur + ' ' + w : w
  }
  return lines.slice(0, 2)
}

function agentStatus(events) {
  const st = {}
  for (const e of events) {
    if (!e?.agent) continue
    if (e.event === 'start') st[e.agent] = 'live'
    else if (e.event === 'finish') st[e.agent] = 'done'
    else if (e.event === 'error') st[e.agent] = 'error'
    else if (!st[e.agent]) st[e.agent] = 'seen'
  }
  return st
}

const FILL = { live: '#C8102E', done: '#1E7A46', error: '#7d0f24', seen: '#B98C5A', idle: '#FFFFFF' }

function useLocal(key, initial) {
  const [v, setV] = useState(() => {
    try { const s = localStorage.getItem(key); return s ? JSON.parse(s) : initial } catch { return initial }
  })
  useEffect(() => { try { localStorage.setItem(key, JSON.stringify(v)) } catch { /* private mode */ } }, [key, v])
  return [v, setV]
}

export default function NeuroEvents({ open, onClose, events, running, runs = [], stage }) {
  const [mode, setMode] = useLocal('veritas.neuro.mode', 'normal')     // normal|maximized|minimized|fullscreen
  const [side, setSide] = useLocal('veritas.neuro.side', 'right')
  const [width, setWidth] = useLocal('veritas.neuro.width', 520)
  const [view, setView] = useState({ k: 1, x: 0, y: 0 })
  const [runSel, setRunSel] = useState('live')
  const drag = useRef(null)
  const resize = useRef(null)
  const svgRef = useRef(null)

  // Esc leaves fullscreen
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape' && mode === 'fullscreen') setMode('normal') }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [mode, setMode])

  // panel resize (normal mode)
  useEffect(() => {
    const move = (e) => {
      if (!resize.current) return
      const dx = e.clientX - resize.current.x0
      setWidth(Math.max(320, Math.min(window.innerWidth - 80,
        resize.current.w0 + (side === 'right' ? -dx : dx))))
    }
    const up = () => { resize.current = null }
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
    return () => { window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up) }
  }, [side, setWidth])

  const pastRuns = runs.filter((r) => r.events?.length)
  const shown = runSel === 'live' ? events : (pastRuns.find((r) => r.run_id === runSel)?.events || [])
  const status = useMemo(() => agentStatus(shown), [shown])
  const phaseNow = STAGE_PHASE[stage] ?? -1
  const last = shown[shown.length - 1]

  const zoomBy = (f, cx = W / 2, cy = H / 2) => setView((v) => {
    const k = Math.max(0.4, Math.min(3.5, v.k * f))
    // keep the point under the cursor fixed
    return { k, x: cx - (cx - v.x) * (k / v.k), y: cy - (cy - v.y) * (k / v.k) }
  })
  const toSvg = (e) => {
    const r = svgRef.current.getBoundingClientRect()
    const scale = Math.max(W / r.width, H / r.height)
    return { x: (e.clientX - r.left) * scale, y: (e.clientY - r.top) * scale, scale }
  }
  const onWheel = (e) => {
    e.preventDefault()
    const p = toSvg(e)
    zoomBy(e.deltaY < 0 ? 1.15 : 1 / 1.15, p.x, p.y)
  }
  // React's onWheel is passive; attach a non-passive listener so the page does not scroll
  useEffect(() => {
    const el = svgRef.current
    if (!el) return undefined
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  })
  const onDown = (e) => { const p = toSvg(e); drag.current = { x: p.x, y: p.y, v: view } }
  const onMove = (e) => {
    if (!drag.current) return
    const p = toSvg(e)
    setView({ ...drag.current.v, x: drag.current.v.x + (p.x - drag.current.x), y: drag.current.v.y + (p.y - drag.current.y) })
  }
  const onUp = () => { drag.current = null }

  if (!open) return null

  const panelStyle = mode === 'normal' ? { width } : undefined
  const btn = (label, title, onClick, cls = '') =>
    <button className={'ghostSquare ' + cls} title={title} aria-label={title} onClick={onClick}>{label}</button>

  return (
    <div className={`neuroPanel ${mode} ${side}`} style={panelStyle}>
      {mode === 'normal' && (
        <div className="neuroResizeHandle" title="Drag to resize"
          onMouseDown={(e) => { resize.current = { x0: e.clientX, w0: width }; e.preventDefault() }} />
      )}
      <div className="neuroHeader">
        <span className="neuroHeaderTitle">Neuro Events</span>
        {running && <span className="liveDot" title="A run is in progress">live</span>}
        {mode !== 'minimized' && (
          <select className="runSelect" value={runSel} onChange={(e) => setRunSel(e.target.value)}
            title="Show the live run, or the agent activity of an earlier run">
            <option value="live">{running ? 'Live run' : 'Latest activity'}</option>
            {pastRuns.slice().reverse().map((r) => (
              <option key={r.run_id} value={r.run_id}>
                {r.stage} · {r.status} · {(r.finished_at || r.started_at || '').slice(5, 16)}
              </option>
            ))}
          </select>
        )}
        <span className="spacer" />
        <div className="neuroHeaderActions">
          {(mode === 'normal' || mode === 'maximized') &&
            btn(side === 'right' ? '↤' : '↦', 'Dock on the other side', () => setSide(side === 'right' ? 'left' : 'right'))}
          {mode !== 'minimized' && btn('—', 'Minimize', () => setMode('minimized'))}
          {mode === 'maximized' || mode === 'minimized'
            ? btn('❐', 'Restore to docked size', () => setMode('normal'))
            : null}
          {mode !== 'maximized' && mode !== 'fullscreen' && btn('▢', 'Maximize', () => setMode('maximized'))}
          {mode === 'fullscreen'
            ? btn('⤡', 'Exit full screen (Esc)', () => setMode('normal'), 'wide')
            : btn('⛶', 'Full screen', () => setMode('fullscreen'))}
          {btn('×', 'Close Neuro Events', onClose, 'danger')}
        </div>
      </div>

      {mode !== 'minimized' && (
        <div className="neuroBody">
          <div className="neuroGraphWrap">
            <div className="zoomBar">
              {btn('+', 'Zoom in', () => zoomBy(1.25))}
              {btn('−', 'Zoom out', () => zoomBy(1 / 1.25))}
              {btn('⤢', 'Fit to view', () => setView({ k: 1, x: 0, y: 0 }), 'wide')}
              <span className="zoomPct">{Math.round(view.k * 100)}%</span>
            </div>
            <svg ref={svgRef} viewBox={`0 0 ${W} ${H}`} className="neuroGraph" preserveAspectRatio="xMidYMid meet"
              onMouseDown={onDown} onMouseMove={onMove} onMouseUp={onUp} onMouseLeave={onUp}>
              <defs>
                <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                  <path d="M 0 0 L 10 5 L 0 10 z" fill="#b9b2ab" />
                </marker>
              </defs>
              <g transform={`translate(${view.x} ${view.y}) scale(${view.k})`}>
                {PHASES.map((p, pi) => {
                  const cls = pi < phaseNow ? 'done' : pi === phaseNow ? 'active' : 'pending'
                  return (
                    <g key={p.id} className={'phaseCol ' + cls}>
                      <rect x={X0 + pi * COL - COL / 2 + 8} y={150} width={COL - 16} height={H - 170} rx={14} className="phaseLane" />
                      <rect x={X0 + pi * COL - COL / 2 + 8} y={150} width={COL - 16} height={62} rx={14} className="phaseHead" />
                      <text x={X0 + pi * COL} y={178} textAnchor="middle" className="phaseTitle">{p.label}</text>
                      <text x={X0 + pi * COL} y={200} textAnchor="middle" className="phaseSub">
                        {cls === 'done' ? 'complete' : cls === 'active' ? 'current phase' : 'pending'}
                      </text>
                    </g>
                  )
                })}
                {LAYOUT.edges.map(([a, b, kind]) => {
                  const A = LAYOUT.byId[a], B = LAYOUT.byId[b]
                  const lit = status[a] === 'done' || status[b] === 'done' || status[b] === 'live'
                  let d
                  if (kind === 'flow') d = `M ${A.x} ${A.y + NODE_H / 2} L ${B.x} ${B.y - NODE_H / 2 - 2}`
                  else if (kind === 'orchestrates') d = `M ${A.x} ${A.y + NODE_H / 2} C ${A.x} ${A.y + 80}, ${B.x} ${B.y - 110}, ${B.x} ${B.y - NODE_H / 2 - 2}`
                  else d = `M ${A.x + NODE_W / 2} ${A.y} C ${A.x + NODE_W / 2 + 40} ${A.y}, ${B.x - NODE_W / 2 - 40} ${B.y}, ${B.x - NODE_W / 2 - 2} ${B.y}`
                  return <path key={a + b} d={d} fill="none" markerEnd="url(#arrow)"
                    className={'edge ' + kind + (lit ? ' lit' : '')} />
                })}
                {LAYOUT.nodes.map((n) => {
                  const s = status[n.id] || 'idle'
                  const lines = wrapLabel(n.id)
                  return (
                    <g key={n.id} className={'agentNode ' + s} transform={`translate(${n.x - NODE_W / 2} ${n.y - NODE_H / 2})`}>
                      <title>{n.id} — {s}</title>
                      <rect width={NODE_W} height={NODE_H} rx={12} fill={FILL[s]}
                        stroke={s === 'idle' ? '#d6d0ca' : FILL[s]} strokeWidth={2} />
                      {lines.map((ln, i) => (
                        <text key={i} x={NODE_W / 2} y={NODE_H / 2 + (i - (lines.length - 1) / 2) * 17 + 5}
                          textAnchor="middle" className="agentLabel" fill={s === 'idle' ? '#3a3a3c' : '#fff'}>{ln}</text>
                      ))}
                    </g>
                  )
                })}
              </g>
            </svg>
            <div className="legend">
              <span><i className="lg live" />running</span><span><i className="lg done" />finished</span>
              <span><i className="lg seen" />reported</span><span><i className="lg error" />error</span>
              <span><i className="lg idle" />not used in this run</span>
              <span className="muted">wheel to zoom · drag to pan</span>
            </div>
          </div>

          <div className="neuroLog">
            <div className="neuroStateLabel">
              {runSel === 'live' ? (running ? 'Live trace' : 'Latest trace') : 'Run trace'} · {shown.length} events
            </div>
            {last && <div className="neuroLastEvent"><strong>{running && runSel === 'live' ? 'Now' : 'Last'}:</strong> {last.agent} · {last.detail || last.event}</div>}
            <div className="neuroEvents">
              {shown.slice().reverse().slice(0, 400).map((e, i) => (
                <div key={(e.seq ?? i) + ':' + i} className={'nev ' + (e.event || '')}>
                  <span className="agent">{e.agent}</span>
                  <span className="det">{e.event === 'start' ? '▶ ' : e.event === 'finish' ? '✔ ' : e.event === 'error' ? '✖ ' : '· '}{e.detail}</span>
                  {e.elapsed_ms != null && <span className="ms">{e.elapsed_ms} ms</span>}
                </div>
              ))}
              {!shown.length && <div className="muted">No agent activity yet for this session.</div>}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
