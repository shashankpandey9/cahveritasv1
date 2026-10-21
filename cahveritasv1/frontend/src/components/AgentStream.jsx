import React, { useEffect, useRef } from 'react'

/** Live AAOSA agent trace with per-agent timings (SSE-fed). */
export default function AgentStream({ events, running }) {
  const ref = useRef(null)
  useEffect(() => { ref.current && (ref.current.scrollTop = ref.current.scrollHeight) },
    [events])
  if (!events.length && !running) return null
  return (
    <div className="agentstream" ref={ref}>
      {events.map((e) => (
        <div className={'ev' + (e.event === 'error' ? ' err' : '')} key={e.seq}>
          <span className="agent">{e.agent}</span>
          <span>{e.event === 'start' ? '▶' : e.event === 'finish' ? '✔' : '·'} {e.detail}</span>
          {e.elapsed_ms != null && <span className="ms">{e.elapsed_ms} ms</span>}
        </div>
      ))}
      {running && <div className="ev"><span className="agent">veritas_advisor</span>
        <span>network running…</span></div>}
    </div>
  )
}
