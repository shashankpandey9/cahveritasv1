import React, { useMemo, useState } from 'react'

/* Phase 0 inventory: every document found under inputs/, whether it has been
   chunked, and how much is left. "Complete" starts as a quick check against
   what is stored; Analyze re-parses the source and verifies it block by
   block, and that verdict sticks until the document is re-chunked. */

const TYPE_LABEL = { brd: 'BRD', design: 'Design', sources: 'Sources', code: 'Code', uat_baseline: 'UAT baseline' }
const TYPES = ['all', 'brd', 'design', 'sources', 'code', 'uat_baseline']
const STATUS_FILTERS = [
  { id: 'all', label: 'All' },
  { id: 'incomplete', label: 'Not complete' },
  { id: 'complete', label: 'Complete' },
  { id: 'unsupported', label: 'Unsupported' },
]
const PAGE = 100

const COLUMNS = [
  { id: 'doc_id', label: 'Document ID' },
  { id: 'name', label: 'Document name' },
  { id: 'doc_type', label: 'Type' },
  { id: 'pages', label: 'Pages', num: true },
  { id: 'sections', label: 'Sections', num: true },
  { id: 'chunks', label: 'Chunks', num: true },
  { id: 'figures', label: 'Figures', num: true },
  { id: 'complete', label: 'Complete' },
  { id: 'remaining_sections', label: 'Remaining sections', num: true },
  { id: 'remaining_chunks', label: 'Remaining chunks', num: true },
]

const isIncomplete = (r) => r.status === 'partial' || r.status === 'not_chunked'

function remaining(r, field) {
  if (r.status === 'unsupported') return <span className="muted">—</span>
  const v = r[field]
  if (v == null) {
    return r.status === 'not_chunked'
      ? <span className="muted" title="Nothing chunked yet — Analyze for the exact count">all</span>
      : <span className="muted" title="Analyze for the exact count">?</span>
  }
  return <span className={v > 0 ? 'remain' : ''}>{v}</span>
}

const figureSortKey = (r) => (r.figures ? r.figures.pending * 1000 + r.figures.total : -1)

function FiguresCell({ r }) {
  const f = r.figures
  if (!f) return <span className="muted">—</span>
  if (!f.total) return <span className="muted">0</span>
  return (
    <span title={`${f.captured} transcribed · ${f.skipped} skipped (icons, decorative, page furniture, unsupported) · ${f.pending} pending`}>
      {f.captured}/{f.total}
      {f.pending > 0 && <span className="remain"> · {f.pending} pending</span>}
    </span>
  )
}

const VERDICT_LABEL = {
  transcribed: 'transcribed', pending: 'pending — needs vision', failed: 'failed — will retry',
  icon: 'skipped: icon', page_furniture: 'skipped: repeated page header/footer',
  decorative: 'skipped: decorative', unsupported_format: 'skipped: format not readable (EMF/TIFF…)',
  too_large: 'skipped: over size limit',
}

function CompleteCell({ r }) {
  if (r.status === 'unsupported') {
    return <span className="badge gray" title="File type is not ingested (e.g. .xml, .yml, .json)">unsupported</span>
  }
  if (r.status === 'removed') return <span className="badge gray" title="Stored, but no longer in inputs/">removed</span>
  return (
    <span className="completeCell">
      <span className={'yn ' + (r.complete ? 'y' : 'n')}>{r.complete ? 'Y' : 'N'}</span>
      <span className="muted how" title={r.verified ? `Analyzed ${r.analyzed_at}` : 'From stored chunk spans — Analyze to verify against the source'}>
        {r.verified ? 'verified' : 'quick check'}
      </span>
    </span>
  )
}

function AnalysisDetail({ a, colSpan }) {
  return (
    <tr className="detailRow">
      <td colSpan={colSpan}>
        {a.error ? <div className="verdict bad">Analyze failed: {a.error}</div> : (
          <>
            <div className={'verdict ' + (a.complete ? 'good' : 'bad')}>{a.verdict}</div>
            <div className="detailStats mono">
              <span>content blocks: {a.source_blocks}</span>
              <span>stored blocks: {a.stored_blocks}</span>
              <span>coverage: {a.coverage_pct}%</span>
              <span>sections: {a.expected_sections - a.remaining_sections}/{a.expected_sections}</span>
              <span>chunks: {a.stored_chunks}/{a.expected_chunks}</span>
              <span>pages: {a.pages}{a.pages_estimated ? ' (est.)' : ''}</span>
              {a.figures?.total > 0 && <span>figures: {a.figures.captured} transcribed / {a.figures.skipped} skipped / {a.figures.pending} pending</span>}
              {a.changed_since_ingest && <span className="remain">source changed since ingest</span>}
            </div>
            {a.figure_list?.length > 0 && (
              <div className="detailSections">
                <b>Embedded figures:</b>
                <ul>
                  {a.figure_list.map((f, i) => (
                    <li key={f.name + i}>
                      <span className="mono">{f.name}</span>{f.size ? ` (${f.size})` : ''} · {f.location} —{' '}
                      <span className={f.verdict === 'transcribed' ? 'ok' : (f.verdict === 'pending' || f.verdict === 'failed') ? 'remain' : 'muted'}>
                        {VERDICT_LABEL[f.verdict] || f.verdict}
                      </span>
                      {f.title ? <> · <i>{f.title}</i></> : null}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {a.remaining_section_names?.length > 0 && (
              <div className="detailSections">
                <b>Sections not fully chunked:</b>
                <ul>{a.remaining_section_names.map((s) => <li key={s}>{s}</li>)}</ul>
                {a.remaining_sections > a.remaining_section_names.length &&
                  <div className="muted">…and {a.remaining_sections - a.remaining_section_names.length} more</div>}
              </div>
            )}
          </>
        )}
      </td>
    </tr>
  )
}

export default function DocumentDiscoveryPanel({ docs, loading, error, busy, onReload, onAnalyze, onChunk, inputs, onSyncInputs }) {
  const [q, setQ] = useState('')
  const [type, setType] = useState('all')
  const [status, setStatus] = useState('all')
  const [sort, setSort] = useState({ col: null, dir: 1 })
  const [page, setPage] = useState(0)
  const [analyzing, setAnalyzing] = useState({})   // rel_path -> true
  const [details, setDetails] = useState({})       // rel_path -> analysis result

  const rows = docs?.documents || []
  const summary = docs?.summary

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase()
    let out = rows.filter((r) => {
      if (type !== 'all' && r.doc_type !== type) return false
      if (status === 'complete' && !r.complete) return false
      if (status === 'incomplete' && !isIncomplete(r)) return false
      if (status === 'unsupported' && r.status !== 'unsupported') return false
      if (!needle) return true
      return [r.name, r.rel_path, r.doc_id, r.doc_type].some((v) => (v || '').toLowerCase().includes(needle))
    })
    if (sort.col) {
      const col = COLUMNS.find((c) => c.id === sort.col)
      out = [...out].sort((a, b) => {
        let x = a[sort.col], y = b[sort.col]
        if (sort.col === 'figures') { x = figureSortKey(a); y = figureSortKey(b) }
        if (col?.num) { x = x ?? -1; y = y ?? -1; return (x - y) * sort.dir }
        return String(x ?? '').localeCompare(String(y ?? '')) * sort.dir
      })
    }
    return out
  }, [rows, q, type, status, sort])

  const pages = Math.max(1, Math.ceil(filtered.length / PAGE))
  const curPage = Math.min(page, pages - 1)
  const visible = filtered.slice(curPage * PAGE, (curPage + 1) * PAGE)
  const incompleteCount = rows.filter((r) => isIncomplete(r) && r.on_disk).length

  const setFilter = (fn) => (v) => { fn(v); setPage(0) }
  const toggleSort = (col) => setSort((s) => ({ col, dir: s.col === col ? -s.dir : 1 }))

  const analyze = async (r) => {
    setAnalyzing((m) => ({ ...m, [r.rel_path]: true }))
    try {
      const res = await onAnalyze(r)
      setDetails((m) => ({ ...m, [r.rel_path]: res }))
    } catch (e) {
      setDetails((m) => ({ ...m, [r.rel_path]: { error: e?.message || 'request failed' } }))
    } finally {
      setAnalyzing((m) => { const n = { ...m }; delete n[r.rel_path]; return n })
    }
  }
  const toggleDetail = (r) => setDetails((m) => { const n = { ...m }; delete n[r.rel_path]; return n })

  if (!docs && loading) return <div className="panel"><div className="empty">Discovering documents in inputs/…</div></div>
  if (!docs && error) return <div className="panel"><div className="empty">{error} <button className="linkbtn" onClick={onReload}>Retry</button></div></div>

  return (
    <div className="panel docpanel">
      <h3>Document Discovery — every input document, its chunks, and what is left to chunk</h3>
      {inputs && (
        <div className="muted inputsSource">
          Inputs: {inputs.source === 'volume'
            ? <>Unity Catalog Volume <span className="mono">{inputs.volume}</span>
              {inputs.mounted ? ' (read in place)' : ` → mirrored to ${inputs.local_dir}`}
              {inputs.last_result ? ` · last sync ${inputs.last_sync}: ${inputs.last_result.files} files` : ''}
              {inputs.last_error ? <span className="remain"> · {inputs.last_error}</span> : null}</>
            : <>app folder <span className="mono">{inputs.local_dir}</span></>}
        </div>
      )}

      {summary && (
        <>
          <div className="statbar">
            <span><b>{summary.discovered}</b> discovered</span>
            <span><b>{summary.supported}</b> ingestible</span>
            <span><b>{summary.complete}</b> fully chunked</span>
            <span className={summary.partial ? 'warn' : ''}><b>{summary.partial}</b> partial</span>
            <span className={summary.not_chunked ? 'warn' : ''}><b>{summary.not_chunked}</b> not chunked</span>
            <span className="muted"><b>{summary.unsupported}</b> unsupported</span>
            <span className="muted"><b>{summary.chunks}</b> chunks</span>
            <span className={summary.figures_pending ? 'warn' : 'muted'}><b>{summary.figures_captured}</b> figures transcribed
              {summary.figures_pending ? <> · <b>{summary.figures_pending}</b> pending</> : null}</span>
          </div>
          <div className={'requiredBar ' + (summary.required_ready ? 'ok' : 'bad')}>
            {summary.required?.length
              ? summary.required.map((r) => (
                <span key={r.rel_path}>
                  <b>Requirements document:</b> {r.name} — {r.complete ? 'fully chunked' : 'NOT fully chunked'}
                  {r.verified ? ' (verified)' : ' (quick check — Analyze to verify)'}
                </span>))
              : <span><b>No requirements document found</b> — add the BRD under inputs/brd/.</span>}
          </div>
        </>
      )}

      <div className="filterbar">
        <input className="search" value={q} onChange={(e) => setFilter(setQ)(e.target.value)}
          placeholder="Search by document ID, name or path…" />
        <button className="chip" onClick={onReload} disabled={loading}>{loading ? 'Refreshing…' : 'Refresh'}</button>
        {inputs?.source === 'volume' && !inputs.mounted && (
          <button className="chip" disabled={busy} onClick={onSyncInputs}
            title={`Mirror ${inputs.volume} into the app (only new or changed files are downloaded)`
              + (inputs.last_sync ? ` — last synced ${inputs.last_sync}` : '')}>
            Sync from Volume
          </button>
        )}
        <button className="chip primaryChip" disabled={busy || !incompleteCount}
          onClick={() => onChunk(null)}
          title="Chunk every document that is not fully chunked; the rest of the corpus is kept">
          Chunk all remaining ({incompleteCount})
        </button>
      </div>
      <div className="filterbar">
        {TYPES.map((t) => (
          <button key={t} className={'chip' + (type === t ? ' on' : '')} onClick={() => setFilter(setType)(t)}>
            {t === 'all' ? 'All types' : TYPE_LABEL[t]}
            {t !== 'all' && summary?.by_type?.[t] ? ` · ${summary.by_type[t].discovered}` : ''}
          </button>
        ))}
        <span className="sep" />
        {STATUS_FILTERS.map((s) => (
          <button key={s.id} className={'chip' + (status === s.id ? ' on' : '')} onClick={() => setFilter(setStatus)(s.id)}>
            {s.label}
          </button>
        ))}
      </div>

      <div className="gridWrap">
        <table className="docgrid">
          <thead>
            <tr>
              {COLUMNS.map((c) => (
                <th key={c.id} className={c.num ? 'num' : ''} onClick={() => toggleSort(c.id)}>
                  {c.label}{sort.col === c.id ? (sort.dir > 0 ? ' ▲' : ' ▼') : ''}
                </th>
              ))}
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {visible.length === 0 && (
              <tr><td colSpan={COLUMNS.length + 1} className="muted emptyCell">No documents match.</td></tr>
            )}
            {visible.map((r) => (
              <React.Fragment key={r.key}>
                <tr className={'st-' + r.status + (r.doc_type === 'brd' ? ' required' : '')}>
                  <td className="mono" title={r.doc_id || 'not chunked yet'}>{r.doc_id ? r.doc_id.slice(0, 8) : '—'}</td>
                  <td className="docname">
                    <div className="nm">{r.name}</div>
                    <div className="path mono" title={r.rel_path}>{r.rel_path}</div>
                  </td>
                  <td><span className="badge tag">{TYPE_LABEL[r.doc_type] || r.doc_type}</span></td>
                  <td className="num" title={r.pages_estimated ? 'Estimated from text length' : ''}>
                    {r.pages ?? '—'}{r.pages != null && r.pages_estimated ? '*' : ''}
                  </td>
                  <td className="num">{r.sections ?? '—'}</td>
                  <td className="num">{r.chunks}</td>
                  <td className="num"><FiguresCell r={r} /></td>
                  <td><CompleteCell r={r} /></td>
                  <td className="num">{remaining(r, 'remaining_sections')}</td>
                  <td className="num">{remaining(r, 'remaining_chunks')}</td>
                  <td className="actions">
                    {r.on_disk && r.supported && (
                      <button className="rowbtn" disabled={!!analyzing[r.rel_path]} onClick={() => analyze(r)}
                        title="Re-parse the source and check that every part of it is chunked">
                        {analyzing[r.rel_path] ? 'Analyzing…' : 'Analyze'}
                      </button>
                    )}
                    {r.on_disk && r.supported && !r.complete && (
                      <button className="rowbtn strong" disabled={busy} onClick={() => onChunk([r.rel_path])}
                        title="Chunk this document now (replaces any partial chunks)">
                        {r.status === 'partial' ? 'Re-chunk' : 'Chunk'}
                      </button>
                    )}
                    {details[r.rel_path] && (
                      <button className="rowbtn ghost" onClick={() => toggleDetail(r)} title="Hide analysis">×</button>
                    )}
                  </td>
                </tr>
                {details[r.rel_path] && <AnalysisDetail a={details[r.rel_path]} colSpan={COLUMNS.length + 1} />}
              </React.Fragment>
            ))}
          </tbody>
        </table>
      </div>

      <div className="pager">
        <span className="muted">
          {filtered.length ? `${curPage * PAGE + 1}–${Math.min(filtered.length, (curPage + 1) * PAGE)} of ${filtered.length}` : '0'}
          {filtered.length !== rows.length ? ` (filtered from ${rows.length})` : ''}
          {' · '}* pages estimated from text length
        </span>
        <span className="spacer" />
        <button className="chip" disabled={curPage === 0} onClick={() => setPage(curPage - 1)}>‹ Prev</button>
        <span className="muted">page {curPage + 1} / {pages}</span>
        <button className="chip" disabled={curPage >= pages - 1} onClick={() => setPage(curPage + 1)}>Next ›</button>
      </div>
    </div>
  )
}
