import React from 'react'
import FeedbackBar from './FeedbackBar.jsx'

export default function ExecutionPanel({ sid, items, cases, onRefresh }) {
  if (!items?.length) return <div className="panel"><div className="empty">
    PASS / FAIL verdicts with explainability appear here after Stage 4
    (execution requires your explicit confirmation at Gate 3).</div></div>
  const s = { PASS: 0, FAIL: 0, ERROR: 0 }
  items.forEach((e) => { s[e.verdict] = (s[e.verdict] || 0) + 1 })
  const nameOf = (tc) => cases?.find((c) => c.tc_no === tc)?.name || ''
  return (
    <div className="panel">
      <div className="scorebar">
        <div className="score"><div className="big" style={{ color: 'var(--green)' }}>{s.PASS}</div><div className="cap">pass</div></div>
        <div className="score"><div className="big" style={{ color: 'var(--gilead-red)' }}>{s.FAIL}</div><div className="cap">fail</div></div>
        <div className="score"><div className="big">{s.ERROR}</div><div className="cap">error</div></div>
        <div className="score"><div className="big">{items.length}</div><div className="cap">executed</div></div>
      </div>
      {items.map((e, i) => (
        <div className="card" key={e.tc_no + i}>
          <div className="head">
            <span className="no">{e.tc_no}</span>
            <span className="ttl">{nameOf(e.tc_no)}</span>
            <span className={'badge ' + e.verdict}>{e.verdict}</span>
            <span className="badge gray">{e.row_count} rows · {e.elapsed_ms} ms</span>
          </div>
          <div className="kv"><b>Explainability:</b> {e.explanation}</div>
          {e.verdict === 'FAIL' && <div className="kv"><b>Likely defect locus:</b> {e.defect_locus}</div>}
          {e.sample_rows?.length > 0 &&
            <div className="sqlblock">{JSON.stringify(e.sample_rows, null, 1)}</div>}
          <FeedbackBar sid={sid} stage="S4" artifactRef={e.tc_no} onRefresh={onRefresh} />
        </div>
      ))}
    </div>
  )
}
