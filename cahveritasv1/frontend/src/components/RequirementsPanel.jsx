import React, { useEffect, useMemo, useRef, useState } from 'react'
import FeedbackBar from './FeedbackBar.jsx'

/* The register is large by design — a real BRD yields ~30 requirements and
   well over 100 sub-requirements. The panel therefore leads with counts and a
   filter, keeps sub-requirements collapsed under their parent, and puts the
   verbatim extracted text one click away on every record. */

const KIND_LABEL = { requirement: 'requirement', note: 'note', context: 'context' }

function ProductMeta({ meta }) {
  if (!meta || !meta.primary) {
    return <div className="kv"><b>Gilead product:</b> <span className="muted">
      portfolio-wide / not product specific</span></div>
  }
  const p = meta.primary
  const markets = (meta.markets || []).map(m => m.code).join(', ')
  return (
    <div className="prodmeta">
      <div className="kv"><b>Gilead product:</b></div>
      <table className="metatable"><tbody>
        <tr><th>Branded name</th><td>{p.branded_name === 'TBD'
          ? <span className="tbd">TBD in source</span> : p.branded_name}</td></tr>
        <tr><th>Abbreviated / shorthand</th><td className="mono">{p.abbrev}</td></tr>
        <tr><th>Molecule</th><td>{p.molecule}{p.molecule_class
          ? <span className="muted"> · {p.molecule_class}</span> : null}</td></tr>
        <tr><th>Full product name</th><td>{p.full_product_name}</td></tr>
        {p.therapeutic_area
          ? <tr><th>Therapeutic area</th><td>{p.therapeutic_area}</td></tr> : null}
        {markets ? <tr><th>Market baskets</th><td className="mono">{markets}</td></tr> : null}
        {meta.inherited_from
          ? <tr><th>Attribution</th><td className="muted">
              inherited from {meta.inherited_from}</td></tr> : null}
      </tbody></table>
      {(meta.products || []).length > 1 && (
        <div className="kv"><b>Also referenced:</b>{' '}
          {meta.products.filter(x => x.key !== p.key)
            .map(x => x.branded_name === 'TBD' ? x.abbrev : x.branded_name).join(', ')}
        </div>
      )}
      {p.note ? <div className="kv note"><b>Note:</b> {p.note}</div> : null}
    </div>
  )
}

function RawText({ text }) {
  const [open, setOpen] = useState(false)
  if (!text) return null
  return (
    <div className="rawblock">
      <button className="linkbtn" onClick={() => setOpen(!open)}>
        {open ? '▾' : '▸'} Raw extracted text
      </button>
      {open && <pre className="raw">{text}</pre>}
    </div>
  )
}

function SubRequirement({ sid, r, onRefresh }) {
  return (
    <div className={'subcard kind-' + (r.req_kind || 'requirement')}>
      <div className="head">
        <span className="no sub">{r.req_no}</span>
        <span className="ttl">{r.title}</span>
        <span className={'badge tag ' + (r.req_kind || 'requirement')}>
          {KIND_LABEL[r.req_kind] || r.req_kind}</span>
        {r.brand && r.brand !== 'ALL' && <span className="badge brand">{r.brand}</span>}
        {r.phase === 'Phase 2' && <span className="badge gray">Phase 2</span>}
        {r.open_item && <span className="badge UNTRACED">OPEN / TBD</span>}
      </div>
      <RawText text={r.raw_text} />
      <div className="kv"><b>Plain language:</b> {r.plain_language}</div>
      <div className="kv"><b>Business rule:</b> {r.business_rule}</div>
      {r.business_description
        ? <div className="kv"><b>Business description:</b> {r.business_description}</div>
        : null}
      <div className="kv src"><b>Source:</b> <span className="mono">{r.source_ref}</span></div>
      <FeedbackBar sid={sid} stage="S1" artifactRef={r.req_no} onRefresh={onRefresh} />
    </div>
  )
}

function Requirement({ sid, r, subs, onRefresh, defaultOpen, cardRef, highlight, trace, nCases,
  onOpenDesign, onOpenTests }) {
  const [open, setOpen] = useState(!!defaultOpen)
  useEffect(() => { if (defaultOpen) setOpen(true) }, [defaultOpen])
  return (
    <div className={'card req' + (highlight ? ' focused' : '')} ref={cardRef}>
      <div className="head">
        <span className="no">{r.req_no}</span>
        <span className="ttl">{r.title}</span>
        {r.section_no && <span className="badge gray mono">§{r.section_no}</span>}
        <span className="badge brand">{r.brand}</span>
        {r.phase === 'Phase 2' && <span className="badge EXTENDED">Phase 2</span>}
        {r.open_item && <span className="badge UNTRACED">OPEN / TBD</span>}
        <span className="badge NOVEL" title="how this record was described">
          {r.enrichment === 'rag' ? 'RAG' : 'deterministic'}</span>
        <span className="badge gray">{r.status}</span>
        {trace && <button className="linkbtn inline" onClick={() => onOpenDesign(r.req_no)}
          title="Open this requirement's design trace">design: {trace.trace_status} · {(trace.specs || []).length} specs →</button>}
        {nCases > 0 && <button className="linkbtn inline" onClick={() => onOpenTests(r.req_no)}
          title="Open this requirement's UAT cases">{nCases} test cases →</button>}
      </div>
      <div className="kv section"><span className="mono">{r.section_path}</span></div>
      <ProductMeta meta={r.product_meta} />
      <div className="kv"><b>Plain language:</b> {r.plain_language}</div>
      <div className="kv"><b>Business description:</b> {r.business_description}</div>
      <div className="kv"><b>Business rule:</b> {r.business_rule}</div>
      <div className="kv"><b>Design features required:</b>{' '}
        {(r.features || []).join(', ') || '—'}</div>
      {(r.data_objects || []).length
        ? <div className="kv"><b>Data objects touched:</b>{' '}
            {r.data_objects.join(', ')}</div>
        : null}
      <div className="kv src"><b>Source:</b> <span className="mono">{r.source_ref}</span></div>
      <RawText text={r.raw_text} />
      <FeedbackBar sid={sid} stage="S1" artifactRef={r.req_no} onRefresh={onRefresh} />

      {subs.length > 0 && (
        <div className="subs">
          <button className="linkbtn strong" onClick={() => setOpen(!open)}>
            {open ? '▾' : '▸'} {subs.length} sub-requirement{subs.length === 1 ? '' : 's'}
            {' '}({r.req_no}.i – {r.req_no}.{subs[subs.length - 1].req_no.split('.').pop()})
          </button>
          {open && subs.map(s => (
            <SubRequirement key={s.req_no} sid={sid} r={s} onRefresh={onRefresh} />
          ))}
        </div>
      )}
    </div>
  )
}

export default function RequirementsPanel({ sid, items, onRefresh, focus, traces, cases, onOpenDesign, onOpenTests }) {
  const [q, setQ] = useState('')
  const [only, setOnly] = useState('all')
  const [expandAll, setExpandAll] = useState(false)
  const refs = useRef({})
  const focusParent = focus?.id ? focus.id.split('.')[0] : null
  const traceBy = useMemo(() => Object.fromEntries((traces || []).map((t) => [t.req_no, t])), [traces])
  const casesBy = useMemo(() => {
    const m = {}
    for (const c of cases || []) m[c.req_no] = (m[c.req_no] || 0) + 1
    return m
  }, [cases])

  // arriving from the design or UAT tab: scroll to the requirement
  useEffect(() => {
    if (!focusParent) return
    setQ(''); setOnly('all')
    setTimeout(() => refs.current[focusParent]?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 80)
  }, [focus])  // eslint-disable-line react-hooks/exhaustive-deps

  const { tops, subsOf, stats } = useMemo(() => {
    const list = items || []
    const subsOf = {}
    list.forEach(r => {
      if (r.parent_req) (subsOf[r.parent_req] = subsOf[r.parent_req] || []).push(r)
    })
    const tops = list.filter(r => !r.parent_req)
    const byProduct = {}
    list.forEach(r => { byProduct[r.brand || 'ALL'] = (byProduct[r.brand || 'ALL'] || 0) + 1 })
    return {
      tops, subsOf,
      stats: {
        total: list.length,
        requirements: tops.length,
        subs: list.length - tops.length,
        open: list.filter(r => r.open_item).length,
        phase2: list.filter(r => r.phase === 'Phase 2').length,
        rag: list.filter(r => r.enrichment === 'rag').length,
        byProduct,
      },
    }
  }, [items])

  if (!items?.length) {
    return <div className="panel"><div className="empty">
      The numbered requirements register appears here after Stage 1.
      Press <b>Begin Review</b>.</div></div>
  }

  const needle = q.trim().toLowerCase()
  const matches = (r) => {
    if (only === 'open' && !r.open_item) return false
    if (only === 'phase2' && r.phase !== 'Phase 2') return false
    if (!needle) return true
    return [r.req_no, r.title, r.raw_text, r.plain_language, r.business_rule,
      r.brand, r.section_path].some(v => (v || '').toLowerCase().includes(needle))
  }
  const visible = tops.filter(r => matches(r) || (subsOf[r.req_no] || []).some(matches))

  return (
    <div className="panel">
      <h3>Requirements Register — identifier · extracted text · plain-language
        summary · business rule · Gilead product metadata</h3>

      <div className="statbar">
        <span><b>{stats.requirements}</b> requirements</span>
        <span><b>{stats.subs}</b> sub-requirements</span>
        <span><b>{stats.total}</b> records</span>
        <span className="warn"><b>{stats.open}</b> open / TBD</span>
        <span><b>{stats.phase2}</b> Phase 2</span>
        <span className="muted"><b>{stats.rag}</b> RAG-enriched</span>
      </div>
      <div className="statbar products">
        {Object.entries(stats.byProduct).sort((a, b) => b[1] - a[1]).map(([k, v]) => (
          <span key={k} className="badge brand">{k} · {v}</span>
        ))}
      </div>

      <div className="filterbar">
        <input className="search" value={q} onChange={e => setQ(e.target.value)}
          placeholder="Filter by id, text, product, section…" />
        {['all', 'open', 'phase2'].map(k => (
          <button key={k} className={'chip' + (only === k ? ' on' : '')}
            onClick={() => setOnly(k)}>
            {k === 'all' ? 'All' : k === 'open' ? 'Open / TBD' : 'Phase 2'}
          </button>
        ))}
        <button className="chip" onClick={() => setExpandAll(!expandAll)}>
          {expandAll ? 'Collapse all' : 'Expand all'}
        </button>
        <span className="muted count">
          showing {visible.length} of {tops.length}
        </span>
      </div>

      {visible.map(r => (
        <Requirement
          /* expandAll is part of the key so toggling it remounts the rows and
             their local open state picks up the new default */
          key={r.req_no + ':' + expandAll}
          sid={sid}
          r={r}
          subs={(subsOf[r.req_no] || []).filter(s => !needle || matches(r) || matches(s))}
          onRefresh={onRefresh}
          defaultOpen={expandAll || (!!needle && (subsOf[r.req_no] || []).some(matches))
            || (focusParent === r.req_no && focus.id !== r.req_no)}
          cardRef={(el) => { refs.current[r.req_no] = el }}
          highlight={focusParent === r.req_no}
          trace={traceBy[r.req_no]}
          nCases={casesBy[r.req_no] || 0}
          onOpenDesign={onOpenDesign}
          onOpenTests={onOpenTests}
        />
      ))}
    </div>
  )
}
