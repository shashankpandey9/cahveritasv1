import React from 'react'

const STAGES = [
  { code: 'P0', label: 'Phase 0 · Ingestion' },
  { code: 'S1', label: 'Requirements Discovery' },
  { code: 'S2', label: 'Design & Environment Trace' },
  { code: 'S3', label: 'UAT Case Generation' },
  { code: 'S4', label: 'Agentic Execution' },
]
const ORDER = ['S0', 'P0', 'S1', 'S1_review', 'S2', 'S2_review', 'S3', 'S3_review',
  'S4_confirm', 'S4', 'done']

export default function StageRail({ stage }) {
  const pos = ORDER.indexOf(stage)
  return (
    <div className="stagerail">
      {STAGES.map((s, i) => {
        const startPos = ORDER.indexOf(s.code)
        const donePos = ORDER.indexOf(STAGES[i + 1] ? STAGES[i + 1].code : 'done')
        const cls = pos >= donePos ? 'done' : (pos >= startPos ? 'active' : '')
        return (
          <div key={s.code} className={'stage ' + cls}>
            <div className="num">{cls === 'done' ? '✓' : i + 1}</div>
            <div className="lbl">{s.label}</div>
          </div>
        )
      })}
    </div>
  )
}
