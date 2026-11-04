/* markdown — a page's Markdown as React elements, from marked's lexer tokens. No HTML is
 * ever produced or injected: a raw HTML token is shown as the text it is. Highlighted
 * phrases become buttons; each phrase is marked at its first occurrence on the page. */
import { Fragment } from 'react'
import { marked } from 'marked'
import { splitByPhrases } from './highlight.js'
import { splitCitations, safeHref } from './citations.js'

function Phrase({ phrase, text, onPhrase }) {
  const pick = (e, newWindow) => onPhrase && onPhrase({ phrase, rect: e.currentTarget.getBoundingClientRect(), newWindow })
  return (
    <span className="rd-phrase" role="button" tabIndex={0}
          onClick={e => { e.preventDefault(); pick(e, e.ctrlKey || e.metaKey) }}
          onMouseDown={e => { if (e.button === 1) e.preventDefault() }}
          onAuxClick={e => { if (e.button === 1) { e.preventDefault(); pick(e, true) } }}
          onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(e, false) } }}>
      {text}
    </span>
  )
}

/* [n] -> a superscript link to source n; a source with no href is just the number. */
function Cite({ source, n, onSource }) {
  return (
    <sup className="rd-cite">
      {source && safeHref(source.href)
        ? <a href={safeHref(source.href)} target="_blank" rel="noopener noreferrer" title={source.title}
             onClick={onSource ? e => onSource(e, source) : undefined}>{n}</a>
        : n}
    </sup>
  )
}

/* With an onLink, [text](url) is a link for http(s) and kiwix: addresses only, and an image is
 * shown only when it is one of the Primer's own proxied pictures; anything else is its text. */
const LINK_OK = /^(https?:\/\/|kiwix:)/i
const PICTURE = '/v1/browse/image?'

/* A plain click follows a link in this window; ctrl, cmd or the middle button asks for a new one. */
export function followLink(e, url, onLink) {
  e.preventDefault()
  onLink(url, { newWindow: !!(e.ctrlKey || e.metaKey || e.button === 1) })
}

export function Markdown({ source, phrases = [], sources = [], onPhrase, onLink, onSource }) {
  const seen = new Set()
  const phrased = (s, key) => splitByPhrases(s, phrases, seen).map((part, i) =>
    typeof part === 'string' ? part : <Phrase key={key + '.' + i} {...part} onPhrase={onPhrase} />)
  // citations are split first, then phrases are matched within each stretch of plain text
  const text = (s, key) => sources.length
    ? splitCitations(s, sources.length).map((part, i) => typeof part === 'string'
        ? <Fragment key={key + '.c' + i}>{phrased(part, key + '.c' + i)}</Fragment>
        : <Cite key={key + '.c' + i} n={part.n} onSource={onSource} source={sources.find(x => x.n === part.n)} />)
    : phrased(s, key)

  const inline = (tokens = [], key = '') => tokens.map((t, i) => {
    const k = key + i
    switch (t.type) {
      case 'strong': return <strong key={k}>{inline(t.tokens, k + '.')}</strong>
      case 'em': return <em key={k}>{inline(t.tokens, k + '.')}</em>
      case 'del': return <del key={k}>{inline(t.tokens, k + '.')}</del>
      case 'codespan': return <code key={k}>{t.text}</code>
      case 'br': return <br key={k} />
      case 'link':
        if (onLink && typeof t.href === 'string' && LINK_OK.test(t.href)) {
          return <a key={k} href={t.href} className="rd-link" title={t.href}
                    onClick={e => followLink(e, t.href, onLink)}
                    onAuxClick={e => { if (e.button === 1) followLink(e, t.href, onLink) }}>{inline(t.tokens, k + '.')}</a>
        }
        return <span key={k}>{inline(t.tokens, k + '.')}</span>
      case 'image':
        if (onLink && typeof t.href === 'string' && t.href.startsWith(PICTURE)) {
          return <img key={k} className="rd-pic" src={t.href} alt={t.text || ''} loading="lazy" />
        }
        return <span key={k}>{t.text}</span>
      case 'escape': return <span key={k}>{t.text}</span>
      case 'html': return <span key={k}>{t.raw}</span>
      default:
        if (t.tokens) return <Fragment key={k}>{inline(t.tokens, k + '.')}</Fragment>
        return <Fragment key={k}>{text(t.text ?? t.raw ?? '', k)}</Fragment>
    }
  })

  const block = (tokens = [], key = '') => tokens.map((t, i) => {
    const k = key + i
    switch (t.type) {
      case 'heading': {
        const H = 'h' + Math.min(Math.max(t.depth, 1), 6)
        return <H key={k}>{inline(t.tokens, k + '.')}</H>
      }
      case 'paragraph': return <p key={k}>{inline(t.tokens, k + '.')}</p>
      case 'text': return <p key={k}>{t.tokens ? inline(t.tokens, k + '.') : text(t.text, k)}</p>
      case 'blockquote': return <blockquote key={k}>{block(t.tokens, k + '.')}</blockquote>
      case 'list': {
        const L = t.ordered ? 'ol' : 'ul'
        return <L key={k}>{t.items.map((it, j) => <li key={k + '.' + j}>{block(it.tokens, k + '.' + j + '.')}</li>)}</L>
      }
      case 'table': {
        const al = (i) => (t.align && t.align[i]) || undefined
        const cell = (c, i, j, Tag) => <Tag key={k + '.' + j + '.' + i} style={al(i) ? { textAlign: al(i) } : undefined}>{inline(c.tokens, k + '.' + j + '.' + i + '.')}</Tag>
        return (
          <div key={k} className="rd-table"><table>
            <thead><tr>{t.header.map((c, i) => cell(c, i, 'h', 'th'))}</tr></thead>
            <tbody>{t.rows.map((r, j) => <tr key={k + '.r' + j}>{r.map((c, i) => cell(c, i, j, 'td'))}</tr>)}</tbody>
          </table></div>
        )
      }
      case 'hr': return <hr key={k} />
      case 'code': return <pre key={k}><code>{t.text}</code></pre>
      case 'html': return <p key={k}>{t.raw}</p>
      case 'space': return null
      default: return t.raw ? <p key={k}>{t.raw}</p> : null
    }
  })

  return <div className="rd-md">{block(marked.lexer(source || ''))}</div>
}
