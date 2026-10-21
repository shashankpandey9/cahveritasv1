import React, { useEffect, useMemo, useRef, useState } from 'react'
import FeedbackBar from './FeedbackBar.jsx'

/* Stage 3 review surface: the enumerated UAT cases grouped by requirement,
   each naming the sub-requirement and design spec it verifies, with its
   SQL, expectation and relevance self-check, and per-case feedback. */

function Case({ sid, c, onOpenReq, onRefresh }) {
  const [sqlOpen, setSqlOpen] = useState(false)
  return (
    <div className={'caseRow' + (c.parent_tc ? ' sub' : '')}>
      <div className="head">
        <span className="no">{c.tc_no}</span>
        <span className="ttl">{c.name}</span>
        <span className="badge gray">{c.category}</span>
        <span className={'badge ' + c.coverage} title={c.coverage_ref}>{c.coverage}</span>
        {c.sub_req && <button className="linkbtn inline" onClick={() => onOpenReq(c.sub_req)}>{c.sub_req}</button>}
        {c.spec_ref && <span className="badge brand" title="Design spec this case verifies">{c.spec_ref}</span>}
        <span className="badge gray">{c.status}</span>
      </div>
      <div className="kv"><b>Description:</b> {c.description}</div>
      <div className="kv"><b>Expectation:</b> {c.expectation}</div>
      <div className="kv"><b>Relevance self-check:</b> {c.relevance_check}</div>
      <button className="linkbtn" onClick={() => setSqlOpen(!sqlOpen)}>{sqlOpen ? '▾' : '▸'} SQL</button>
      {sqlOpen && <div className="sqlblock">{c.sql}</div>}
      <FeedbackBar sid={sid} stage="S3" artifactRef={c.tc_no} onRefresh={onRefresh} />
    </div>
  )
}

export default function TestCasesPanel({ sid, items, requirements, onRefresh, focus, onOpenReq, onOpenDesign }) {
  const [q, setQ] = useState('')
  const [cov, setCov] = useState('all')
  const [openSet, setOpenSet] = useState({})
  const refs = useRef({})

  const reqBy = useMemo(() => Object.fromEntries((requirements || []).map((r) => [r.req_no, r])), [requirements])

  useEffect(() => {
    if (!focus?.id) return
    const parent = focus.id.split('.')[0]
    setOpenSet((s) => ({ ...s, [parent]: true }))
    setTimeout(() => refs.current[parent]?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 60)
  }, [focus])

  if (!items?.length) return <div className="panel"><div className="empty">
    Generated UAT test cases (with sub-cases, SQL, and coverage against your team's
    baseline) appear here after Stage 3, grouped by requirement.</div></div>

  const counts = { MATCH: 0, EXTENDED: 0, NOVEL: 0 }
  items.forEach((c) => { counts[c.coverage] = (counts[c.coverage] || 0) + 1 })
  const needle = q.trim().toLowerCase()
  const shown = items.filter((c) => (cov === 'all' || c.coverage === cov)
    && (!needle || [c.tc_no, c.name, c.description, c.sql, c.req_no, c.sub_req, c.spec_ref, c.category]
      .some((v) => (v || '').toLowerCase().includes(needle))))
  const groups = []
  const byReq = {}
  for (const c of shown) {
    if (!byReq[c.req_no]) { byReq[c.req_no] = []; groups.push(c.req_no) }
    byReq[c.req_no].push(c)
  }
  const allOpen = groups.length > 0 && groups.every((g) => openSet[g])

  return (
    <div className="panel">
      <h3>UAT Test Cases — grouped by requirement, each linked to the sub-requirement and design spec it verifies</h3>
      <div className="scorebar">
        <div className="score"><div className="big">{items.length}</div><div className="cap">cases</div></div>
        <div className="score"><div className="big">{groups.length}</div><div className="cap">requirements covered</div></div>
        <div className="score"><div className="big">{counts.MATCH}</div><div className="cap">match team</div></div>
        <div className="score"><div className="big">{counts.EXTENDED}</div><div className="cap">extended</div></div>
        <div className="score"><div className="big">{counts.NOVEL}</div><div className="cap">novel — beyond team</div></div>
      </div>
      <div className="filterbar">
        <input className="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search cases, SQL, requirement, spec…" />
        {['all', 'MATCH', 'EXTENDED', 'NOVEL'].map((k) => (
          <button key={k} className={'chip' + (cov === k ? ' on' : '')} onClick={() => setCov(k)}>{k === 'all' ? 'All' : k}</button>
        ))}
        <button className="chip" onClick={() => setOpenSet(allOpen ? {} : Object.fromEntries(groups.map((g) => [g, true])))}>
          {allOpen ? 'Collapse all' : 'Expand all'}
        </button>
        <span className="muted count">{shown.length} of {items.length} cases</span>
      </div>
      {groups.map((g) => (
        <div key={g} className={'card trace ' + (openSet[g] ? 'open' : '')} ref={(el) => { refs.current[g] = el }}>
          <div className="head clickable" onClick={() => setOpenSet((s) => ({ ...s, [g]: !s[g] }))}>
            <span className="caret">{openSet[g] ? '▾' : '▸'}</span>
            <button className="linkbtn inline no" onClick={(e) => { e.stopPropagation(); onOpenReq(g) }}>{g}</button>
            <span className="ttl">{reqBy[g]?.title || ''}</span>
            <span className="badge gray">{byReq[g].length} cases</span>
            <button className="linkbtn inline" onClick={(e) => { e.stopPropagation(); onOpenDesign(g) }}>design trace →</button>
          </div>
          {openSet[g] && byReq[g].map((c) => (
            <Case key={c.tc_no} sid={sid} c={c} onOpenReq={onOpenReq} onRefresh={onRefresh} />
          ))}
        </div>
      ))}
    </div>
  )
}
