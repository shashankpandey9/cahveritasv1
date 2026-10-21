import React, { useState } from 'react'
import { sendFeedback } from '../api.js'

/** 👍/👎 + comment, recorded per artifact. Positive S4 feedback also affirms
 * the execution report into the markdown KB (backend behavior). */
export default function FeedbackBar({ sid, stage, artifactRef, onRefresh }) {
  const [sel, setSel] = useState(null)
  const [comment, setComment] = useState('')
  const [sent, setSent] = useState(false)

  const submit = async (sentiment) => {
    setSel(sentiment)
    await sendFeedback(sid, stage, artifactRef, sentiment, comment)
    setSent(true)
    onRefresh && onRefresh()
  }

  if (sent) return <div className="fb"><span className="sent">✓ feedback recorded{sel === 'negative' ? ' — use Refine to re-run with your critique' : ''}</span></div>
  return (
    <div className="fb">
      <button className={sel === 'positive' ? 'on' : ''} title="Affirm"
        onClick={() => submit('positive')}>👍</button>
      <button className={sel === 'negative' ? 'on' : ''} title="Flag"
        onClick={() => submit('negative')}>👎</button>
      <input placeholder="optional comment…" value={comment}
        onChange={(e) => setComment(e.target.value)} />
    </div>
  )
}
