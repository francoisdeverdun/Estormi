/**
 * Regression tests for ``sanitizeBriefingHtml`` — the defence-in-depth pass
 * that runs over any briefing body headed for ``dangerouslySetInnerHTML``.
 *
 * The hole it closes is the raw-HTML edit fallback in ``BriefingModal``: a
 * user-typed draft round-trips straight back into ``htmlBody`` and is stored
 * verbatim, so without this pass an edited briefing could re-render arbitrary
 * markup. Each documented vector is asserted neutralised here, and benign
 * markup (a styled objective paragraph, an ordinary https link) is asserted to
 * survive untouched so the pass is not over-stripping.
 *
 * These run under happy-dom (``vitest.config.ts`` → environment: 'happy-dom'),
 * so ``document``/``DOMParser`` exist and the sanitiser takes its real DOM
 * path, not the SSR textual fallback. We re-parse the returned string into a
 * fresh document and assert on the resulting DOM, not on the raw string — the
 * browser is what ultimately interprets the output.
 */
import { describe, expect, it } from 'vitest'
import { sanitizeBriefingHtml } from '../sections/BriefingModal'

/** Re-parse the sanitiser's output so assertions run against a real DOM. */
function parse(html: string): Document {
  return new DOMParser().parseFromString(sanitizeBriefingHtml(html), 'text/html')
}

describe('sanitizeBriefingHtml — dangerous elements removed', () => {
  it.each([
    ['script', '<p>before</p><script>alert(1)</script><p>after</p>'],
    ['style', '<p>before</p><style>body{display:none}</style><p>after</p>'],
    ['iframe', '<p>before</p><iframe srcdoc="<p>nested</p>"></iframe><p>after</p>'],
    ['object', '<p>before</p><object data="evil.swf"></object><p>after</p>'],
    ['embed', '<p>before</p><embed src="evil.swf"><p>after</p>'],
  ])('strips <%s> elements while keeping surrounding prose', (tag, html) => {
    const doc = parse(html)
    expect(doc.querySelector(tag)).toBeNull()
    // The benign siblings are untouched — only the dangerous node is excised.
    expect(doc.querySelectorAll('p')).toHaveLength(2)
    expect(doc.body.textContent).toContain('before')
    expect(doc.body.textContent).toContain('after')
  })

  it('removes all five dangerous element types in one pass', () => {
    const doc = parse(
      '<p>keep</p><script></script><style></style>' +
        '<iframe></iframe><object></object><embed>',
    )
    for (const tag of ['script', 'style', 'iframe', 'object', 'embed']) {
      expect(doc.querySelector(tag)).toBeNull()
    }
    expect(doc.querySelector('p')?.textContent).toBe('keep')
  })
})

describe('sanitizeBriefingHtml — on* event-handler attributes stripped', () => {
  it.each([
    ['onerror', '<img src="x" onerror="alert(1)">'],
    ['onclick', '<button onclick="alert(1)">go</button>'],
    ['onload', '<svg onload="alert(1)"></svg>'],
    ['onmouseover', '<p onmouseover="alert(1)">hover</p>'],
  ])('removes the %s handler attribute', (attr, html) => {
    const doc = parse(html)
    const el = doc.querySelector(`[${attr}]`)
    expect(el).toBeNull()
    // The element itself survives — only its handler attribute is dropped.
    expect(doc.body.children.length).toBeGreaterThan(0)
  })

  it('keeps the host element but drops its handler', () => {
    const doc = parse('<img src="https://host.test/p.png" onerror="alert(1)">')
    const img = doc.querySelector('img')
    expect(img).not.toBeNull()
    expect(img!.hasAttribute('onerror')).toBe(false)
    // A safe https src is preserved on the same element.
    expect(img!.getAttribute('src')).toBe('https://host.test/p.png')
  })
})

describe('sanitizeBriefingHtml — srcdoc stripped', () => {
  it('removes the srcdoc attribute that would smuggle a nested document', () => {
    const doc = parse(
      '<iframe-x srcdoc="<script>alert(1)</script>">x</iframe-x>',
    )
    expect(doc.querySelector('[srcdoc]')).toBeNull()
  })
})

describe('sanitizeBriefingHtml — executable URL schemes cleared on URL_ATTRS', () => {
  // Each row is one URL-bearing attribute the sanitiser guards. The custom
  // element keeps the attribute parsing uniform across attribute names that
  // a standard element would not normally accept.
  it.each([
    ['href', '<a href="javascript:alert(1)">x</a>', 'a'],
    ['src', '<img src="javascript:alert(1)">', 'img'],
    ['formaction', '<button formaction="javascript:alert(1)">x</button>', 'button'],
    ['action', '<form action="javascript:alert(1)"></form>', 'form'],
  ])('clears javascript: in %s', (attr, html, sel) => {
    const doc = parse(html)
    const el = doc.querySelector(sel)
    expect(el).not.toBeNull()
    expect(el!.hasAttribute(attr)).toBe(false)
  })

  it.each([
    ['href', '<a href="data:text/html,<script>alert(1)</script>">x</a>', 'a'],
    ['src', '<img src="data:text/html,evil">', 'img'],
  ])('clears data: in %s', (attr, html, sel) => {
    const doc = parse(html)
    const el = doc.querySelector(sel)
    expect(el).not.toBeNull()
    expect(el!.hasAttribute(attr)).toBe(false)
  })

  // The scheme guard is an allowlist (http/https/mailto/tel), so it neutralises
  // any other scheme — not just the two we used to name explicitly. vbscript:
  // is the classic one a deny-list misses.
  it.each([
    ['href', '<a href="vbscript:msgbox(1)">x</a>', 'a'],
    ['href', '<a href="VBScript:msgbox(1)">x</a>', 'a'],
  ])('clears vbscript: in %s', (attr, html, sel) => {
    const doc = parse(html)
    const el = doc.querySelector(sel)
    expect(el).not.toBeNull()
    expect(el!.hasAttribute(attr)).toBe(false)
  })

  it('clears a javascript: xlink:href on an SVG link', () => {
    const doc = parse(
      '<svg><a xlink:href="javascript:alert(1)"><text>x</text></a></svg>',
    )
    const a = doc.querySelector('svg a')
    expect(a).not.toBeNull()
    // The attribute is matched case-insensitively by lowercased name; assert
    // neither casing survives.
    expect(a!.hasAttribute('xlink:href')).toBe(false)
    expect(a!.getAttributeNames().some((n) => n.toLowerCase() === 'xlink:href')).toBe(
      false,
    )
  })

  it('clears a control-char-smuggled javascript: scheme on href', () => {
    // A leading  would slip a "javascript:" past a naive
    // startsWith() check — browsers ignore such C0 junk when resolving the
    // URL, so the sanitiser must strip the control bytes before comparing.
    const doc = parse('<a href="javascript:alert(1)">x</a>')
    const a = doc.querySelector('a')
    expect(a).not.toBeNull()
    expect(a!.hasAttribute('href')).toBe(false)
  })

  it('clears a <button formaction="javascript:..."> submit hijack', () => {
    const doc = parse(
      '<form action="https://host.test/post">' +
        '<button formaction="javascript:alert(1)">submit</button>' +
        '</form>',
    )
    const button = doc.querySelector('button')
    expect(button).not.toBeNull()
    expect(button!.hasAttribute('formaction')).toBe(false)
    // The benign form action on the parent is left intact.
    expect(doc.querySelector('form')!.getAttribute('action')).toBe(
      'https://host.test/post',
    )
  })
})

describe('sanitizeBriefingHtml — benign markup is not over-stripped', () => {
  it('keeps a styled objective paragraph verbatim', () => {
    const doc = parse('<p class="briefing-objective">The day’s through-line.</p>')
    const p = doc.querySelector('p.briefing-objective')
    expect(p).not.toBeNull()
    expect(p!.getAttribute('class')).toBe('briefing-objective')
    expect(p!.textContent).toBe('The day’s through-line.')
  })

  it('keeps an ordinary https link with its href intact', () => {
    const doc = parse('<a href="https://estormi.app/docs">Read more</a>')
    const a = doc.querySelector('a')
    expect(a).not.toBeNull()
    expect(a!.getAttribute('href')).toBe('https://estormi.app/docs')
    expect(a!.textContent).toBe('Read more')
  })

  it('keeps a relative/anchor href and a mailto link', () => {
    const doc = parse(
      '<a href="#section">jump</a><a href="mailto:hi@estormi.app">mail</a>',
    )
    const anchors = doc.querySelectorAll('a')
    expect(anchors).toHaveLength(2)
    expect(anchors[0].getAttribute('href')).toBe('#section')
    expect(anchors[1].getAttribute('href')).toBe('mailto:hi@estormi.app')
  })
})
