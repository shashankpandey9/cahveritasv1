import React, { useEffect, useRef, useState } from 'react'

/** Chat + the stage-gate controls (Phase 0's gate lives in DiscoveryStatus).
 *  Gates mirror the backend stage machine:
 *  *_review  -> Accept / Refine (with critique)
 *  S4_confirm -> Execute / Refine
 *  S0        -> Begin Review
 */
export default function ChatArea({ messages, stage, running, onSend, onGate }) {
  const [text, setText] = useState('')
  const [note, setNote] = useState('')
  const [refining, setRefining] = useState(false)
  const endRef = useRef(null)

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }) },
    [messages, running])
  useEffect(() => { setRefining(false); setNote('') }, [stage])

  const submit = (e) => {
    e.preventDefault()
    if (!text.trim() || running) return
    onSend(text.trim()); setText('')
  }

  const gateHint = {
    S1_review: 'Gate 1 — review the numbered requirements (right panel). Accept to trace the design, or refine with your critique.',
    S2_review: 'Gate 2 — verify the requirement→design→code→data trace. Accept to generate UAT, or refine.',
    S3_review: 'Gate 3 — human review of the generated test cases. Accept to arm execution, or refine.',
    S4_confirm: 'Confirmation — execute the accepted test cases against the analytics store?',
  }[stage]

  const md = (s) => s
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')

  return (
    <>
      <div className="chat">
        {messages.map((m) => (
          <div key={m.id} className={'msg ' + m.role}
            dangerouslySetInnerHTML={{ __html: md(m.content) }} />
        ))}
        {running && <div className="msg assistant">⏳ The agent network is working — watch the live trace below…</div>}
        <div ref={endRef} />
      </div>

      {(gateHint || stage === 'S0') && !running && (
        <div className="gatebar">
          {gateHint && <div className="hint">{gateHint}</div>}
          {stage === 'S0' && (
            <button className="btn primary" onClick={() => onSend('Review the materials')}>
              Begin Review
            </button>
          )}
          {stage?.endsWith('_review') && stage !== 'P0_review' && !refining && (
            <>
              <button className="btn primary" onClick={() => onGate('accept', '')}>Accept</button>
              <button className="btn secondary" onClick={() => setRefining(true)}>Refine…</button>
            </>
          )}
          {stage === 'S4_confirm' && !refining && (
            <>
              <button className="btn primary" onClick={() => onGate('confirm_execute', '')}>
                Execute Test Cases
              </button>
              <button className="btn secondary" onClick={() => setRefining(true)}>Refine…</button>
            </>
          )}
          {refining && (
            <div className="refinebox">
              <input autoFocus placeholder="What should change? (fed to the agents)"
                value={note} onChange={(e) => setNote(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && note.trim() && onGate('refine', note.trim())} />
              <button className="btn primary" disabled={!note.trim()}
                onClick={() => onGate('refine', note.trim())}>Send refinement</button>
              <button className="btn ghost" onClick={() => setRefining(false)}>cancel</button>
            </div>
          )}
        </div>
      )}

      <div className="routeBanner">
        Agent routing enabled: free prompts can route to the intake librarian, corpus chunker, stage gates, or the advisor. Examples: “fill missing BRD gaps”, “continue phase 0”, “proceed to phase 1”.
      </div>
      <form className="composer" onSubmit={submit}>
        <input
          placeholder={running ? 'Agents running…' : 'Ask VERITAS or route to agents: "fill missing BRD gaps", "continue phase 0", "accept"'}
          value={text}
          onChange={(e) => setText(e.target.value)}
          disabled={running}
          title="Free prompts may route to the intake librarian, corpus chunker, or stage logic."
        />
        <button className="btn primary" disabled={running || !text.trim()}>Send</button>
      </form>
    </>
  )
}
