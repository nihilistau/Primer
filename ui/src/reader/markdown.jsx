/* markdown — a page's Markdown as React elements, from marked's lexer tokens. No HTML is
 * ever produced or injected: a raw HTML token is shown as the text it is. Highlighted
 * phrases become buttons; each phrase is marked at its first occurrence on the page. */
import { Fragment } from 'react'
import { marked } from 'marked'
import { splitByPhrases } from './highlight.js'

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

export function Markdown({ source, phrases = [], onPhrase }) {
  const seen = new Set()
  const text = (s, key) => splitByPhrases(s, phrases, seen).map((part, i) =>
    typeof part === 'string' ? part : <Phrase key={key + '.' + i} {...part} onPhrase={onPhrase} />)

  const inline = (tokens = [], key = '') => tokens.map((t, i) => {
    const k = key + i
    switch (t.type) {
      case 'strong': return <strong key={k}>{inline(t.tokens, k + '.')}</strong>
      case 'em': return <em key={k}>{inline(t.tokens, k + '.')}</em>
      case 'del': return <del key={k}>{inline(t.tokens, k + '.')}</del>
      case 'codespan': return <code key={k}>{t.text}</code>
      case 'br': return <br key={k} />
      case 'link': return <span key={k}>{inline(t.tokens, k + '.')}</span>
      case 'image': return <span key={k}>{t.text}</span>
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
      case 'hr': return <hr key={k} />
      case 'code': return <pre key={k}><code>{t.text}</code></pre>
      case 'html': return <p key={k}>{t.raw}</p>
      case 'space': return null
      default: return t.raw ? <p key={k}>{t.raw}</p> : null
    }
  })

  return <div className="rd-md">{block(marked.lexer(source || ''))}</div>
}
