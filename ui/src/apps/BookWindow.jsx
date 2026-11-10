/* BookWindow — your book (Phase 13): choose what goes in (everything read, a page and what
 * branched from it, or since a date), the title and By line, and which extras to include; a
 * preview count (debounced); Make the book, with progress while the server writes the EPUB and
 * the HTML; then the two downloads. Earlier books are listed below with their downloads and a
 * Delete that asks first, inside the window.
 *
 * One book is made at a time (the server answers 409 otherwise). A book still being made when
 * the window opens is picked up and followed. Polling stops when the window closes. The timed
 * parts (the debounced preview, following a book) live in bookJobs.js. */
import { useEffect, useMemo, useRef, useState } from 'react'
import * as api from '../api.js'
import { Button, Input } from '../kit/parts.jsx'
import {
  INCLUDE_KEYS, INCLUDE_LABELS, MAX_BY, MAX_TITLE, allIncluded, bookDate, bookErrorText, defaultTitle,
  isActive, pickablePages, previewText, progressText, scopeBody, sinceSeconds, sizesText,
} from './book.js'
import { createFollower, createPreviewer } from './bookJobs.js'
import './apps.css'

const PAGE_LIST_MAX = 200
const SCOPES = [
  ['all', 'Everything I’ve read'],
  ['branch', 'A page and what branched from it'],
  ['since', 'Since a date'],
]

/* the two downloads of a finished book; `named` puts the book's title in their labels */
export function Downloads({ book, named = false }) {
  if (!book || book.status !== 'done') return null
  const of = named ? ` of “${book.title || 'Untitled'}”` : ''
  return (
    <span className="bkw-dl">
      <a className="bkw-link" href={api.bookFileUrl(book.id, 'epub')} download aria-label={`Download the EPUB${of}`}>Download EPUB</a>
      <a className="bkw-link" href={api.bookFileUrl(book.id, 'html')} download aria-label={`Download the HTML${of}`}>Download HTML</a>
    </span>
  )
}

/* one earlier book: title, date, sizes or state, the downloads, and Delete with its confirm */
export function BookRow({ book, confirming, deleting, onAsk, onCancel, onDelete }) {
  const state = book.status === 'done' ? sizesText(book) : progressText(book)
  const name = `“${book.title || 'Untitled'}”`
  return (
    <li className="bkw-book">
      <div className="bkw-book-top">
        <span className="bkw-book-title">{book.title || 'Untitled'}</span>
        <span className="bkw-book-date">{bookDate(book.created_at)}</span>
      </div>
      <div className={'bkw-book-state' + (book.status === 'failed' ? ' bkw-err' : '')}>{state}</div>
      <div className="bkw-book-actions">
        <Downloads book={book} named />
        {confirming ? (
          <span className="bkw-confirm" role="group" aria-label={`Confirm deleting ${name}`}>
            <span>Delete this book?</span>
            <Button size="sm" variant="danger" disabled={deleting} onClick={onDelete} aria-label={`Delete ${name}`}>Delete</Button>
            <Button size="sm" variant="ghost" disabled={deleting} onClick={onCancel} aria-label={`Keep ${name}`}>Keep</Button>
          </span>
        ) : (
          <Button size="sm" variant="ghost" onClick={onAsk} aria-label={`Delete ${name}`}>Delete</Button>
        )}
      </div>
    </li>
  )
}

export default function BookWindow() {
  const [kind, setKind] = useState('all')
  const [query, setQuery] = useState('')
  const [nodeId, setNodeId] = useState('')
  const [since, setSince] = useState('')
  const [title, setTitle] = useState('')
  const [by, setBy] = useState('')
  const [include, setInclude] = useState(allIncluded)
  const [pageData, setPageData] = useState(null)
  const [pagesErr, setPagesErr] = useState('')
  const [preview, setPreview] = useState(null)
  const [previewErr, setPreviewErr] = useState('')
  const [previewing, setPreviewing] = useState(false)
  const [available, setAvailable] = useState({ ok: true, reason: '' })
  const [list, setList] = useState(null)
  const [listErr, setListErr] = useState('')
  const [job, setJob] = useState(null)
  const [pollErr, setPollErr] = useState('')
  const [starting, setStarting] = useState(false)
  const [err, setErr] = useState(null)          // {text, status}: the last Make error
  const [confirmId, setConfirmId] = useState('')
  const [deleting, setDeleting] = useState(false)
  const alive = useRef(true)
  const previewer = useRef(null)
  const follower = useRef(null)

  // the preview and the books: made once, stopped when the window closes
  useEffect(() => {
    alive.current = true
    previewer.current = createPreviewer({
      fetchPreview: api.bookPreview,
      onPending: () => { setPreview(null); setPreviewErr(''); setPreviewing(true) },
      onResult: ({ preview: p, error }) => {
        setPreviewing(false)
        if (error) setPreviewErr(bookErrorText(error)); else { setPreview(p); setPreviewErr('') }
      },
    })
    follower.current = createFollower({
      api,
      onBooks: (rows) => { setList(rows); setListErr('') },
      onJob: (b) => { setJob(b); setPollErr('') },
      onEnded: () => setErr(e => (e && e.status === 409 ? null : e)),   // the book the 409 named is over
      onError: (where, e) => (where === 'list' ? setListErr(bookErrorText(e)) : setPollErr(bookErrorText(e))),
    })
    follower.current.load()
    api.bookPages().then(d => { if (alive.current) setPageData(d) })
      .catch(e => { if (alive.current) setPagesErr(bookErrorText(e)) })
    api.status().then(s => { if (alive.current && s && s.book_available) setAvailable(s.book_available) }).catch(() => {})
    return () => { alive.current = false; previewer.current.stop(); follower.current.stop() }
  }, [])

  const state = { kind, nodeId, since, include, title, by }
  const body = scopeBody(state)
  const previewKey = body ? JSON.stringify({ scope: body.scope, include: body.include }) : ''

  // the preview count, 400 ms after the last change
  useEffect(() => {
    if (!previewer.current) return
    if (!previewKey) { setPreview(null); setPreviewErr(''); setPreviewing(false) }
    previewer.current.request(previewKey ? JSON.parse(previewKey) : null)
  }, [previewKey])

  const pages = useMemo(() => pickablePages(pageData, query), [pageData, query])
  const allPages = useMemo(() => pickablePages(pageData), [pageData])
  const picked = allPages.find(p => p.id === nodeId)
  const scope = body ? body.scope : { kind }
  const fallbackTitle = (preview && preview.title) || (kind === 'branch' ? (picked ? defaultTitle(scope, picked.title) : '')
    : kind === 'since' ? (sinceSeconds(since) !== null ? defaultTitle(scope) : '')
    : defaultTitle(scope))

  const running = starting || isActive(job)
  const canMake = !!body && !running && available.ok && !(preview && !preview.chapters)

  const make = async () => {
    if (!body) return
    setStarting(true); setErr(null); setJob(null)
    try {
      await follower.current.make(body)
    } catch (e) {
      if (alive.current) setErr({ text: bookErrorText(e), status: e && e.status })
    } finally {
      if (alive.current) setStarting(false)
    }
  }

  const remove = async (id) => {
    setDeleting(true)
    try {
      await api.bookDelete(id)
      if (!alive.current) return
      if (follower.current.id === id) { follower.current.drop(id); setJob(null) }
      setConfirmId('')
      setList(l => (l || []).filter(b => b.id !== id))
      follower.current.load()
    } catch (e) {
      if (alive.current) setListErr(bookErrorText(e))
    } finally {
      if (alive.current) setDeleting(false)
    }
  }

  const toggle = (k) => setInclude(inc => ({ ...inc, [k]: !inc[k] }))
  const previewLine = !body
    ? (kind === 'branch' ? 'Choose a page.' : 'Choose a date.')
    : previewErr || (previewing || !preview ? 'Counting…' : previewText(preview))

  return (
    <div className="bkw">
      <section className="bkw-sec">
        <fieldset className="bkw-scopes">
          <legend className="bkw-h">What goes in</legend>
          {SCOPES.map(([k, label]) => (
            <label key={k} className="bkw-radio">
              <input type="radio" name="bkw-scope" value={k} checked={kind === k} onChange={() => setKind(k)} />
              {label}
            </label>
          ))}
        </fieldset>
        {kind === 'branch' ? (
          <div className="bkw-picker">
            <Input type="search" placeholder="Find a page" aria-label="Find a page" value={query}
                   onChange={(e) => setQuery(e.target.value)} />
            {pagesErr ? <div className="bkw-err">{pagesErr}</div> : null}
            {!pageData && !pagesErr ? <div className="bkw-note">Loading the pages…</div> : null}
            {pageData && !pages.length ? <div className="bkw-note">{query ? 'No page matches.' : 'No pages read yet.'}</div> : null}
            {pages.length ? (
              <ul className="bkw-pages" aria-label="Pages">
                {pages.slice(0, PAGE_LIST_MAX).map(p => (
                  <li key={p.id}>
                    <button type="button" className={'bkw-page' + (p.id === nodeId ? ' bkw-page-sel' : '')}
                            aria-pressed={p.id === nodeId} onClick={() => setNodeId(p.id)}>
                      {p.title}{p.note ? <span className="bkw-page-note"> ({p.note})</span> : null}
                    </button>
                  </li>
                ))}
              </ul>
            ) : null}
            {pages.length > PAGE_LIST_MAX
              ? <div className="bkw-note">{pages.length - PAGE_LIST_MAX} more: narrow the search.</div> : null}
            {picked ? <div className="bkw-note">From: {picked.title}</div> : null}
          </div>
        ) : null}
        {kind === 'since' ? (
          <label className="bkw-field">
            <span>Pages first opened on or after</span>
            <Input type="date" min="1970-01-02" max="2099-12-31" value={since} onChange={(e) => setSince(e.target.value)} />
          </label>
        ) : null}
      </section>

      <section className="bkw-sec">
        <label className="bkw-field">
          <span>Title</span>
          <Input value={title} maxLength={MAX_TITLE} placeholder={fallbackTitle} onChange={(e) => setTitle(e.target.value)} />
        </label>
        <label className="bkw-field">
          <span>By (optional)</span>
          <Input value={by} maxLength={MAX_BY} onChange={(e) => setBy(e.target.value)} />
        </label>
        <div className="bkw-includes" role="group" aria-label="Include">
          {INCLUDE_KEYS.map(k => (
            <label key={k} className="bkw-check">
              <input type="checkbox" checked={include[k] !== false} onChange={() => toggle(k)} />
              {INCLUDE_LABELS[k]}
            </label>
          ))}
        </div>
      </section>

      <section className="bkw-sec">
        <div className={'bkw-preview' + (previewErr ? ' bkw-err' : '')} role="status">{previewLine}</div>
        <div className="bkw-make">
          <Button variant="primary" disabled={!canMake} onClick={make}>Make the book</Button>
          {job ? (
            <span className={'bkw-progress' + (job.status === 'failed' ? ' bkw-err' : '')} role="status">
              {isActive(job) ? <span className="ui-spin" aria-hidden="true" /> : null} {progressText(job)}
            </span>
          ) : null}
        </div>
        {!available.ok ? <div className="bkw-err">{available.reason || 'A book cannot be made now.'}</div> : null}
        {job && job.status === 'done' ? <div className="bkw-result"><Downloads book={job} /></div> : null}
        {err ? <div className="bkw-err" role="alert">{err.text}</div> : null}
        {pollErr ? <div className="bkw-err">{pollErr}</div> : null}
      </section>

      <section className="bkw-sec">
        <h3 className="bkw-h">Earlier books</h3>
        {listErr ? <div className="bkw-err">{listErr}</div> : null}
        {list && !list.length ? <div className="bkw-note">No books yet.</div> : null}
        {list && list.length ? (
          <ul className="bkw-books">
            {list.map(b => (
              <BookRow key={b.id} book={job && b.id === job.id ? { ...b, ...job } : b} confirming={confirmId === b.id}
                       deleting={deleting} onAsk={() => setConfirmId(b.id)} onCancel={() => setConfirmId('')}
                       onDelete={() => remove(b.id)} />
            ))}
          </ul>
        ) : null}
      </section>
    </div>
  )
}
