import React, { useEffect, useMemo, useRef, useState } from 'react'
import FeedbackBar from './FeedbackBar.jsx'

/* Stage 2 review surface: per requirement, the design specs that satisfy
   each sub-requirement (with evidence), the STTM rows that implement it
   (catalog ids + citations), and the environment it touches. Every
   requirement can be re-traced with a reviewer note, and links back to the
   requirement and forward to its test cases. */

const txt = (v) => (typeof v === 'string' ? v : v && typeof v === 'object'
  ? (v.entity ? `${v.entity}${(v.columns || []).length ? ` (${v.columns.join(', ')})` : ''}`
    : v.left ? `${v.left} ⋈ ${v.right}${(v.keys || []).length ? ` on ${v.keys.join(', ')}` : ''}${v.type ? ` [${v.type}]` : ''}`
      : v.target_column ? `${v.target_column} = ${v.expression || ''}${v.note ? ` — ${v.note}` : ''}`
        : JSON.stringify(v)) : String(v ?? ''))

function List({ label, items }) {
  if (!items?.length) return null
  return (
    <div className="kv"><b>{label}:</b>
      <ul className="tightList">{items.map((x, i) => <li key={i}>{txt(x)}</li>)}</ul>
    </div>
  )
}

function SpecTable({ specs, onOpenReq }) {
  if (!specs?.length) return <div className="muted">No design specs recorded.</div>
  return (
    <table className="specTable">
      <thead><tr><th>Spec</th><th>Sub-req</th><th>Feature</th><th>Specification</th><th>Evidence</th><th>Status</th></tr></thead>
      <tbody>
        {specs.map((s) => (
          <tr key={s.id} className={'sp-' + s.status}>
            <td className="mono">{s.id}</td>
            <td><button className="linkbtn inline" onClick={() => onOpenReq(s.sub_req)} title="Open this requirement">{s.sub_req}</button></td>
            <td>{s.feature}</td>
            <td>{s.specification}</td>
            <td className="evid">{s.evidence}</td>
            <td><span className={'badge sp ' + s.status}>{s.status}</span></td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function SttmTable({ rows }) {
  if (!rows?.length) return <div className="muted">No source-to-target mapping linked to this requirement.</div>
  return (
    <table className="specTable">
      <thead><tr><th>STTM</th><th>Source</th><th /><th>Target</th><th>Transformation / rule</th><th>Where stated</th></tr></thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={(r.id || 'd') + i}>
            <td className="mono">{r.id || <span className="badge gray">derived</span>}</td>
            <td className="mono">{r.source}</td>
            <td>→</td>
            <td className="mono">{r.target}</td>
            <td>{r.transformation}{r.data_type ? <span className="muted"> · {r.data_type}</span> : null}
              {r.description ? <div className="muted">{r.description}</div> : null}</td>
            <td className="evid">{r.citation}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function TraceCard({ sid, t, req, nCases, open, onToggle, cardRef, running, onRetrace, onOpenReq, onOpenTests, onRefresh }) {
  const [note, setNote] = useState('')
  const [asking, setAsking] = useState(false)
  const m = t.meta || {}
  const specCounts = (t.specs || []).reduce((a, s) => { a[s.status] = (a[s.status] || 0) + 1; return a }, {})
  return (
    <div className={'card trace ' + (open ? 'open' : '')} ref={cardRef}>
      <div className="head clickable" onClick={onToggle}>
        <span className="caret">{open ? '▾' : '▸'}</span>
        <button className="linkbtn inline no" onClick={(e) => { e.stopPropagation(); onOpenReq(t.req_no) }} title="Open the requirement">{t.req_no}</button>
        <span className="ttl">{req?.title || ''}</span>
        <span className={'badge ' + t.trace_status}>{t.trace_status}</span>
        {m.method === 'heuristic' && <span className="badge PARTIAL" title="The design model was unavailable — candidate evidence only">heuristic</span>}
        {!m.method && <span className="badge UNTRACED" title="Traced by an earlier version without design specs — Re-trace it, or Refine to re-run Stage 2">legacy — re-run</span>}
        <span className="badge gray" title="Sub-requirements with a fully specified design">{m.subs_specified ?? '?'}/{m.subs_total ?? '?'} sub-reqs specified</span>
        <span className="badge gray">{(t.specs || []).length} specs{specCounts.gap ? ` · ${specCounts.gap} gap` : ''}</span>
        <span className="badge gray">{(t.sttm || []).length} STTM</span>
        <button className="linkbtn inline" onClick={(e) => { e.stopPropagation(); onOpenTests(t.req_no) }}
          title="Open this requirement's UAT cases">{nCases ? `${nCases} test cases →` : 'tests →'}</button>
      </div>
      {open && (
        <div className="traceBody">
          {(t.design_features || []).length > 0 && <List label="Design features" items={t.design_features} />}
          <h4>Design specifications — one or more per sub-requirement</h4>
          <SpecTable specs={t.specs} onOpenReq={onOpenReq} />
          <h4>Source-to-target mapping (STTM)</h4>
          <SttmTable rows={t.sttm} />
          <h4>Environment</h4>
          <div className="envGrid">
            <List label="Source entities & columns" items={t.source_entities} />
            <List label="Joins" items={t.joins} />
            <List label="Filters" items={t.filters} />
            <List label="Inclusions" items={t.inclusions} />
            <List label="Exclusions" items={t.exclusions} />
            <List label="Transformations" items={t.transformations} />
            <List label="Code files" items={t.code_files} />
            <List label="Target tables / marts / reports" items={t.target_marts} />
            <List label="Output columns" items={t.output_columns} />
          </div>
          {t.notes && <div className="kv note"><b>Gaps & notes:</b> <span className="prewrap">{t.notes}</span></div>}
          {(m.retrieved || []).length > 0 && (
            <details className="retrieved"><summary>{m.retrieved.length} corpus sections consulted</summary>
              <ul className="tightList">{m.retrieved.map((c) => <li key={c} className="mono">{c}</li>)}</ul>
            </details>
          )}
          <div className="traceActions">
            {!asking ? (
              <button className="btn secondary small" disabled={running} onClick={() => setAsking(true)}
                title="Run the design agents again for this requirement only">Re-trace {t.req_no}…</button>
            ) : (
              <div className="refinebox">
                <input autoFocus placeholder={`Optional note for the design agents about ${t.req_no}`}
                  value={note} onChange={(e) => setNote(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') { onRetrace(t.req_no, note.trim()); setAsking(false); setNote('') } }} />
                <button className="btn primary small" disabled={running}
                  onClick={() => { onRetrace(t.req_no, note.trim()); setAsking(false); setNote('') }}>Re-trace</button>
                <button className="btn ghost small" onClick={() => setAsking(false)}>cancel</button>
              </div>
            )}
          </div>
          <FeedbackBar sid={sid} stage="S2" artifactRef={t.req_no} onRefresh={onRefresh} />
        </div>
      )}
    </div>
  )
}

export default function DesignTracePanel({ sid, items, requirements, cases, stage, running, onRefresh, focus,
  onRetrace, onOpenReq, onOpenTests }) {
  const [q, setQ] = useState('')
  const [only, setOnly] = useState('all')
  const [openSet, setOpenSet] = useState({})
  const refs = useRef({})

  const reqBy = useMemo(() => Object.fromEntries((requirements || []).map((r) => [r.req_no, r])), [requirements])
  const casesBy = useMemo(() => {
    const m = {}
    for (const c of cases || []) m[c.req_no] = (m[c.req_no] || 0) + 1
    return m
  }, [cases])

  // navigate here from another tab: open and scroll to the requirement
  useEffect(() => {
    if (!focus?.id) return
    const parent = focus.id.split('.').slice(0, 1)[0]
    setOpenSet((s) => ({ ...s, [parent]: true }))
    setTimeout(() => refs.current[parent]?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 60)
  }, [focus])

  const stats = useMemo(() => {
    const s = { TRACED: 0, PARTIAL: 0, UNTRACED: 0, specs: 0, specified: 0, gap: 0, sttm: 0, heuristic: 0, subs: 0, subsSpec: 0 }
    for (const t of items || []) {
      s[t.trace_status] = (s[t.trace_status] || 0) + 1
      s.specs += (t.specs || []).length
      s.specified += (t.specs || []).filter((x) => x.status === 'specified').length
      s.gap += (t.specs || []).filter((x) => x.status === 'gap').length
      s.sttm += (t.sttm || []).length
      s.heuristic += t.meta?.method === 'heuristic' ? 1 : 0
      s.subs += t.meta?.subs_total || 0
      s.subsSpec += t.meta?.subs_specified || 0
    }
    return s
  }, [items])

  if (!items?.length) {
    return <div className="panel"><div className="empty">
      The design trace appears here after Stage 2 — design specs per sub-requirement, STTM rows,
      entities, joins, filters and code, each linked to its requirement.</div></div>
  }

  const needle = q.trim().toLowerCase()
  const visible = items.filter((t) => {
    if (only !== 'all' && t.trace_status !== only && !(only === 'gaps' && (t.specs || []).some((s) => s.status === 'gap'))) return false
    if (!needle) return true
    return JSON.stringify(t).toLowerCase().includes(needle) || (reqBy[t.req_no]?.title || '').toLowerCase().includes(needle)
  })

  const allOpen = visible.length > 0 && visible.every((t) => openSet[t.req_no])

  return (
    <div className="panel">
      <h3>Design & Environment Trace — design specs per sub-requirement · STTM · entities · code</h3>
      {items.some((t) => !t.meta?.method) && (
        <div className="reviewHint warnHint">These traces were produced by the earlier Stage 2, which had no design specs and
          fell back to keyword heuristics. Re-run the stage with <b>Refine…</b> (left panel, e.g. "re-trace with design specs")
          or use <b>Re-trace</b> on individual requirements.</div>
      )}
      {stage === 'S2_review' && (
        <div className="reviewHint">Review each requirement's specs and STTM, give feedback or Re-trace any one,
          then <b>Accept</b> (left panel) to generate UAT cases — or <b>Refine</b> to re-run the whole stage.</div>
      )}
      <div className="statbar">
        <span><b>{items.length}</b> requirements</span>
        <span><b>{stats.TRACED}</b> traced</span>
        <span className={stats.PARTIAL ? 'warn' : ''}><b>{stats.PARTIAL}</b> partial</span>
        <span className={stats.UNTRACED ? 'warn' : ''}><b>{stats.UNTRACED}</b> untraced</span>
        <span><b>{stats.specs}</b> design specs ({stats.specified} specified · {stats.gap} gap)</span>
        <span><b>{stats.subsSpec}/{stats.subs}</b> sub-reqs specified</span>
        <span><b>{stats.sttm}</b> STTM rows</span>
        {stats.heuristic > 0 && <span className="warn"><b>{stats.heuristic}</b> heuristic — re-trace these</span>}
      </div>
      <div className="filterbar">
        <input className="search" value={q} onChange={(e) => setQ(e.target.value)}
          placeholder="Search specs, tables, columns, STTM, code…" />
        {[['all', 'All'], ['TRACED', 'Traced'], ['PARTIAL', 'Partial'], ['UNTRACED', 'Untraced'], ['gaps', 'Has gaps']].map(([k, l]) => (
          <button key={k} className={'chip' + (only === k ? ' on' : '')} onClick={() => setOnly(k)}>{l}</button>
        ))}
        <button className="chip" onClick={() => setOpenSet(allOpen ? {} : Object.fromEntries(visible.map((t) => [t.req_no, true])))}>
          {allOpen ? 'Collapse all' : 'Expand all'}
        </button>
        <span className="muted count">showing {visible.length} of {items.length}</span>
      </div>
      {visible.map((t) => (
        <TraceCard key={t.req_no} sid={sid} t={t} req={reqBy[t.req_no]} nCases={casesBy[t.req_no] || 0}
          open={!!openSet[t.req_no]} onToggle={() => setOpenSet((s) => ({ ...s, [t.req_no]: !s[t.req_no] }))}
          cardRef={(el) => { refs.current[t.req_no] = el }} running={running}
          onRetrace={onRetrace} onOpenReq={onOpenReq} onOpenTests={onOpenTests} onRefresh={onRefresh} />
      ))}
    </div>
  )
}
