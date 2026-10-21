import React, { useEffect, useState } from 'react'

/* Always-visible Phase 0 status: what has been discovered, what of it is
   chunked, whether the requirements document is complete — and the phase
   actions (start, review, accept, refine, proceed) for the current stage. */

const TYPE_LABEL = { brd: 'BRD', design: 'Design', sources: 'Sources', code: 'Code', uat_baseline: 'UAT baseline' }
const TYPE_ORDER = ['brd', 'design', 'sources', 'code', 'uat_baseline']

function stepIndex(stage, summary) {
  if (stage === 'S0') return summary?.chunks ? 1 : 0
  if (stage === 'P0') return 1
  if (stage === 'P0_review') return 2
  return 4
}

export default function DiscoveryStatus({ stage, docs, docsLoading, running, onStart, onReview, onAccept, onRefine, onProceed, onChunkRemaining }) {
  const summary = docs?.summary
  const inPhase0 = stage === 'S0' || stage === 'P0' || stage === 'P0_review'
  const [open, setOpen] = useState(inPhase0)
  const [refining, setRefining] = useState(false)
  const [note, setNote] = useState('')
  useEffect(() => { setOpen(inPhase0); setRefining(false); setNote('') }, [stage])  // eslint-disable-line react-hooks/exhaustive-deps

  const step = stepIndex(stage, summary)
  const steps = ['Discover', 'Chunk', 'Review', 'Accept']
  const requiredReady = !!summary?.required_ready
  const incomplete = (summary?.partial || 0) + (summary?.not_chunked || 0)

  const headline = !summary
    ? (docsLoading ? 'Discovering documents…' : 'Document status unavailable')
    : `${summary.discovered} discovered · ${summary.complete} fully chunked · ${incomplete} not complete`

  return (
    <div className="discovery">
      <div className="discoveryHead" onClick={() => setOpen(!open)} title={open ? 'Collapse' : 'Expand'}>
        <div>
          <div className="phase0header">Phase 0 · Document discovery</div>
          <div className="discoveryHeadline">{headline}</div>
        </div>
        <span className={'auditBadge ' + (requiredReady ? 'ready' : 'review')}>
          {stage === 'P0' || running ? 'running' : inPhase0 ? (requiredReady ? 'BRD ready' : 'needs review') : 'accepted'}
        </span>
        <span className="caret">{open ? '▾' : '▸'}</span>
      </div>

      {open && (
        <>
          <div className="discoverySteps">
            {steps.map((s, i) => (
              <div key={s} className={'dstep' + (i < step ? ' done' : i === step ? ' active' : '')}>
                <span className="phase0dot">{i < step ? '✓' : i + 1}</span>{s}
              </div>
            ))}
          </div>

          {summary && (
            <>
              <table className="typeTable">
                <thead>
                  <tr><th>Type</th><th>Found</th><th>Chunked</th><th>Partial</th><th>Not chunked</th><th>Unsupported</th></tr>
                </thead>
                <tbody>
                  {TYPE_ORDER.filter((t) => summary.by_type?.[t]).map((t) => {
                    const b = summary.by_type[t]
                    return (
                      <tr key={t}>
                        <td>{TYPE_LABEL[t]}</td>
                        <td>{b.discovered}</td>
                        <td className={b.complete ? 'ok' : ''}>{b.complete}</td>
                        <td className={b.partial ? 'warn' : ''}>{b.partial}</td>
                        <td className={b.not_chunked ? 'warn' : ''}>{b.not_chunked}</td>
                        <td className="muted">{b.unsupported}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>

              <div className={'requiredBar ' + (requiredReady ? 'ok' : 'bad')}>
                {summary.required?.length
                  ? summary.required.map((r) => (
                    <div key={r.rel_path}>
                      {r.complete ? '✓' : '✗'} <b>Requirements document</b> {r.complete ? 'fully chunked' : 'not fully chunked'}
                      {r.verified ? ' · verified' : ''} — <span className="muted">{r.name}</span>
                    </div>))
                  : <div>✗ <b>No requirements document</b> found under inputs/brd/.</div>}
              </div>
            </>
          )}

          <div className="discoveryActions">
            {stage === 'S0' && (
              <button className="btn primary small" disabled={running} onClick={onStart}>Start Phase 0 discovery</button>
            )}
            {stage === 'P0' && <span className="muted">Discovery and chunking in progress — watch the live trace below.</span>}
            {stage === 'P0_review' && !refining && (
              <>
                <button className="btn secondary small" onClick={onReview}>Review documents</button>
                <button className="btn primary small" disabled={running || !requiredReady} onClick={onAccept}
                  title={requiredReady ? 'Accept the Phase 0 corpus' : 'The requirements document must be fully chunked first'}>
                  Accept Phase 0
                </button>
                <button className="btn secondary small" disabled={running} onClick={() => setRefining(true)}>Refine…</button>
                <button className="btn primary small" disabled={running || !requiredReady} onClick={onProceed}
                  title={requiredReady ? 'Review the corpus summary and continue to Phase 1' : 'The requirements document must be fully chunked first'}>
                  Proceed to next phase
                </button>
              </>
            )}
            {stage === 'P0_review' && refining && (
              <div className="refinebox">
                <input autoFocus placeholder="What should Phase 0 do differently? (e.g. chunk the design folder)"
                  value={note} onChange={(e) => setNote(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && note.trim() && onRefine(note.trim())} />
                <button className="btn primary small" disabled={!note.trim()} onClick={() => onRefine(note.trim())}>Send</button>
                <button className="btn ghost small" onClick={() => setRefining(false)}>cancel</button>
              </div>
            )}
            {!inPhase0 && <span className="muted">Phase 0 accepted.</span>}
            {incomplete > 0 && stage !== 'P0' && (
              <button className="btn ghost small" disabled={running} onClick={onChunkRemaining}
                title="Chunk every document that is not fully chunked, keeping the existing corpus">
                Chunk {incomplete} remaining
              </button>
            )}
          </div>
          {stage === 'P0_review' && !requiredReady && (
            <div className="discoveryHint">Phase 1 needs the requirements document fully chunked. Analyze it in the
              Document Discovery tab and chunk it if anything is missing.</div>
          )}
        </>
      )}
    </div>
  )
}
