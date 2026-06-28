/**
 * BriefingModal — the deep-dive modal triggered from CardinalSection.
 *
 * The briefing modal is an `ExtModalShell` around the briefing archive: a
 * date strip + the selected day's composed scroll, with a reset action.
 *
 *   - BriefingModal → listBriefings + getBriefing (today open by default)
 */
import { useEffect, useRef, useState } from 'react'
import {
  EmptyState,
  ErrorState,
  EstormiMasthead,
  Field,
  GhostAction,
  LoadingState,
  Textarea,
} from '@estormi/ui-kit'
import { ExtModalShell } from '../components/ExtModalShell'
import { ResetButton } from '../components/ResetButton'
import {
  listBriefings,
  getBriefing,
  editBriefing,
  editBriefingFields,
  deleteBriefing,
  resetBriefings,
  type BriefingSummary,
  type Briefing,
  type BriefingFields,
} from '../api/knowledge'

/* ─────────────────── Briefing ─────────────────── */

// Defence-in-depth sanitiser for any HTML headed for `dangerouslySetInnerHTML`.
// The server composes `htmlBody` with every tag under its own control and
// escapes all source content (see the trust-boundary note at the render site),
// so a server-composed body passes through untouched. The hole is the raw-HTML
// edit fallback (`saveEdit` below): it round-trips a user-typed draft straight
// back into `htmlBody`, and the PUT endpoint stores that verbatim — so without
// this pass an edited briefing could re-render arbitrary markup. We have no
// HTML-sanitiser dependency (and won't add one for this), so neutralise with
// the browser's own parser: drop script/style/iframe/object/embed elements,
// on*-handler + `srcdoc` attributes, and `javascript:`/`data:` URLs on every
// URL-bearing attribute. Runs only in the DOM; SSR/tests without a document
// fall back to stripping the dangerous tags textually.
const DANGEROUS_TAGS = 'script, style, iframe, object, embed'
// Attributes that can carry an executable URL scheme.
const URL_ATTRS = new Set(['href', 'src', 'xlink:href', 'formaction', 'action'])

export function sanitizeBriefingHtml(html: string): string {
  if (typeof document === 'undefined') {
    return html.replace(/<\/?(?:script|style|iframe|object|embed)\b[^>]*>/gi, '')
  }
  const doc = new DOMParser().parseFromString(html, 'text/html')
  doc.querySelectorAll(DANGEROUS_TAGS).forEach((el) => el.remove())
  doc.querySelectorAll('*').forEach((el) => {
    for (const attr of [...el.attributes]) {
      const name = attr.name.toLowerCase()
      // Strip control chars (and whitespace) anywhere in the value, not just
      // runs of ASCII space: a leading \x01 etc. would otherwise smuggle a
      // `\x01javascript:` scheme past the startsWith() check (browsers ignore
      // such leading junk when resolving the URL).
      // eslint-disable-next-line no-control-regex -- intentional: strip C0 control bytes so they can't smuggle a URL scheme
      const value = attr.value.replace(/[\u0000-\u0020]+/g, '').toLowerCase()
      if (
        name.startsWith('on') ||
        name === 'srcdoc' ||
        (URL_ATTRS.has(name) &&
          (value.startsWith('javascript:') || value.startsWith('data:')))
      ) {
        el.removeAttribute(attr.name)
      }
    }
  })
  return doc.body.innerHTML
}

// The editable prose sections, in reading order. Only sections the briefing
// actually carries (non-empty `fields[key]`, i.e. it has the zone markers) are
// shown — so the form never offers a field the server can't splice back in.
const FIELD_DEFS: {
  key: keyof BriefingFields
  label: string
  hint: string
  minHeight: string
}[] = [
  { key: 'objective', label: 'Objective', hint: 'The day’s through-line (subtitle).', minHeight: '4.5em' },
  { key: 'readiness', label: 'Readiness', hint: 'The health steer in the gilt card.', minHeight: '6em' },
  { key: 'myDay', label: 'My day', hint: 'The narrative prose. Start with a letter — the drop cap gilds it.', minHeight: '26vh' },
]

export function BriefingModal({ onClose }: { onClose: () => void }) {
  const [archive, setArchive] = useState<BriefingSummary[]>([])
  const [selectedDate, setSelectedDate] = useState<string | null>(null)
  // The date whose body we are currently fetching. selectDate/refresh set it
  // synchronously so an out-of-order getBriefing() resolution for a day the
  // user already navigated away from can't overwrite the day now showing.
  const selectedDateRef = useRef<string | null>(null)
  const [body, setBody] = useState<Briefing | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [deletingDate, setDeletingDate] = useState<string | null>(null)
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  // Edit mode: the user can correct a briefing in place; the save feeds the
  // local quill's training set (see editBriefing). Two shapes: `fieldDraft` is
  // the structured per-section form (preferred, when the briefing carries
  // `fields`); `draft` is the raw-HTML textarea fallback for older briefings.
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [fieldDraft, setFieldDraft] = useState<BriefingFields | null>(null)
  const [saving, setSaving] = useState(false)

  const refresh = async () => {
    setLoading(true)
    setError(null)
    try {
      const r = await listBriefings()
      const items = (r.items ?? []).slice(0, 90)
      setArchive(items)
      if (items.length > 0) {
        const newest = items[0]!.date
        // Latest by default — only override when the user has explicitly
        // selected a date AND it still exists in the list.
        const stillThere = selectedDate && items.some((x) => x.date === selectedDate)
        const target = stillThere ? selectedDate! : newest
        selectedDateRef.current = target
        setSelectedDate(target)
        try {
          const next = await getBriefing(target)
          if (selectedDateRef.current === target) setBody(next)
        } catch (e) {
          if (selectedDateRef.current === target) {
            setError(e instanceof Error ? e.message : String(e))
          }
        }
      } else {
        setSelectedDate(null)
        setBody(null)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  // Mount-only: load the archive once when the modal opens. `refresh` closes
  // over `selectedDate` (null at mount) and is also driven by the delete/reset
  // handlers; re-running it on every `selectedDate` change would refetch the
  // whole archive each time the user picks a date — selectDate already loads
  // the chosen day. So this effect intentionally fires once.
  useEffect(() => {
    void refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const selectDate = async (date: string) => {
    selectedDateRef.current = date
    setSelectedDate(date)
    setEditing(false)
    setFieldDraft(null)
    setBody(null)
    try {
      const next = await getBriefing(date)
      // Drop a slow response for a day the user already navigated away from.
      if (selectedDateRef.current === date) setBody(next)
    } catch (e) {
      if (selectedDateRef.current === date) {
        setError(e instanceof Error ? e.message : String(e))
      }
    }
  }

  const startEdit = () => {
    if (!body) return
    // Prefer the structured form when the briefing carries editable sections;
    // otherwise fall back to raw-HTML editing.
    const present = FIELD_DEFS.filter(
      (f) => (body.fields?.[f.key] ?? '').trim().length > 0,
    )
    if (present.length > 0) {
      const fd: BriefingFields = {}
      for (const f of present) fd[f.key] = body.fields![f.key] ?? ''
      setFieldDraft(fd)
      setDraft('')
    } else {
      setFieldDraft(null)
      setDraft(body.htmlBody ?? '')
    }
    setEditing(true)
  }

  const cancelEdit = () => {
    setEditing(false)
    setFieldDraft(null)
  }

  const saveEdit = async () => {
    if (!selectedDate) return
    setSaving(true)
    try {
      if (fieldDraft) {
        // Send only the sections the user actually changed — an untouched
        // section keeps its original server-side HTML (no re-render). This
        // matters for backfilled briefings, whose field text is a best-effort
        // recovery from the rendered HTML; re-rendering it could drift.
        const orig = body?.fields ?? {}
        const changed: BriefingFields = {}
        for (const k of Object.keys(fieldDraft) as (keyof BriefingFields)[]) {
          if ((fieldDraft[k] ?? '') !== (orig[k] ?? '')) changed[k] = fieldDraft[k]
        }
        if (Object.keys(changed).length > 0) {
          await editBriefingFields(selectedDate, changed)
          // The server spliced the edits into htmlBody; refetch so the rendered
          // scroll shows the result (we don't reassemble HTML on the client).
          setBody(await getBriefing(selectedDate))
        }
      } else {
        await editBriefing(selectedDate, draft)
        setBody((b) => (b ? { ...b, htmlBody: draft } : b))
      }
      setEditing(false)
      setFieldDraft(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = async (date: string) => {
    setDeletingDate(date)
    setConfirmDelete(null)
    try {
      await deleteBriefing(date)
      // After delete: drop from local state and pick the next briefing
      // (newer-then-older preferred) so the body view never lingers on a
      // briefing that no longer exists.
      const next = archive.filter((b) => b.date !== date)
      setArchive(next)
      if (selectedDate === date) {
        if (next.length > 0) {
          await selectDate(next[0]!.date)
        } else {
          setSelectedDate(null)
          setBody(null)
        }
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setDeletingDate(null)
    }
  }

  return (
    <ExtModalShell
      onClose={onClose}
      accent="var(--vert-sauge)"
      ariaLabel="Briefing"
          >
      {/* Crown the briefing — the marquee surface — with the canonical
          masthead, mirroring the iOS Briefings page. The illuminated rule at
          its foot doubles as the header divider, so no separate drop-cap title
          is needed. A slim eyebrow + reset action sits beneath it. */}
      <div
        style={{
          padding: '20px 20px 0',
          display: 'flex',
          flexDirection: 'column',
          gap: 12,
          marginBottom: 14,
        }}
      >
        <EstormiMasthead markSize={44} />
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: 8,
          }}
        >
          <span
            style={{
              fontFamily: 'var(--font-display)',
              fontSize: 11,
              letterSpacing: '0.28em',
              color: 'var(--or-ancien)',
              textTransform: 'uppercase',
            }}
          >
            {"Diurnale"}
          </span>
          <ResetButton
            label={"Reset briefings"}
            confirmTitle={"Reset briefings?"}
            confirmBody={"Removes every composed briefing — chunks, vault files, run history. Sources and schedule are kept. Cannot be undone."}
            onReset={resetBriefings}
            onDone={() => void refresh()}
          />
        </div>
      </div>
      <div style={{ padding: '0 20px 20px' }}>
      {loading && archive.length === 0 ? (
        <LoadingState label={"Loading…"} />
      ) : error ? (
        <ErrorState
          message={"Could not load briefings"}
          detail={error}
          onRetry={refresh}
        />
      ) : archive.length === 0 ? (
        <EmptyState
          title={"No briefings yet"}
          body={"Run the briefing engine to compose today’s scroll."}
        />
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {/* Horizontal date strip — newest first, scrolls horizontally
              when the archive grows past the modal width. Each chip is
              clickable; the active chip has a sage left border. */}
          <div
            style={{
              display: 'flex',
              gap: 6,
              overflowX: 'auto',
              paddingBottom: 6,
              borderBottom: '1px solid var(--gilt-line)',
            }}
            aria-label={"Archive"}
          >
            {archive.map((b) => {
              const active = b.date === selectedDate
              return (
                <button
                  key={b.date}
                  type="button"
                  disabled={editing || saving}
                  onClick={() => void selectDate(b.date)}
                  style={{
                    flexShrink: 0,
                    padding: '6px 10px',
                    // Mirrors the iOS BriefingDateStrip chip: rounded, with a
                    // gold fill + frame on the open day.
                    background: active
                      ? 'color-mix(in srgb, var(--or-ancien) 18%, transparent)'
                      : 'transparent',
                    border: active
                      ? '1px solid var(--or-ancien)'
                      : '1px solid var(--gilt-line)',
                    borderRadius: 'var(--radius-tight)',
                    cursor: 'pointer',
                    color: active ? 'var(--parchemin-os)' : 'var(--ink-dim)',
                    fontFamily: 'var(--font-display)',
                    fontSize: 10,
                    letterSpacing: '0.16em',
                    textTransform: 'uppercase',
                    whiteSpace: 'nowrap',
                  }}
                  aria-pressed={active}
                  aria-label={`${b.date} · ${b.title || 'untitled'}`}
                >
                  {b.date}
                </button>
              )
            })}
          </div>

          {/* Selected briefing — title + meta + delete button + body */}
          {selectedDate && (
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                gap: 8,
              }}
            >
              <div style={{ minWidth: 0 }}>
                <div
                  style={{
                    fontFamily: 'var(--font-display)',
                    fontSize: 9,
                    letterSpacing: '0.28em',
                    color: 'var(--or-ancien)',
                    textTransform: 'uppercase',
                  }}
                >
                  {selectedDate}
                </div>
                <div
                  style={{
                    fontFamily: 'var(--font-body)',
                    fontSize: 18,
                    color: 'var(--parchemin)',
                    marginTop: 2,
                    whiteSpace: 'nowrap',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                  }}
                >
                  {archive.find((b) => b.date === selectedDate)?.title || '—'}
                </div>
              </div>
              {confirmDelete === selectedDate ? (
                <div style={{ display: 'flex', gap: 6, flexShrink: 0 }}>
                  <GhostAction
                    label={"Cancel"}
                    size="sm"
                    onClick={() => setConfirmDelete(null)}
                    disabled={deletingDate === selectedDate}
                  />
                  <GhostAction
                    label={
                      deletingDate === selectedDate
                        ? "Deleting…"
                        : "Confirm"
                    }
                    size="sm"
                    onClick={() => void handleDelete(selectedDate)}
                    disabled={deletingDate === selectedDate}
                  />
                </div>
              ) : (
                <GhostAction
                  label={"Delete"}
                  size="sm"
                  tone="danger"
                  onClick={() => setConfirmDelete(selectedDate)}
                />
              )}
            </div>
          )}

          {body && !editing && (
            <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
              <GhostAction label={"Edit"} size="sm" onClick={startEdit} />
            </div>
          )}
          {/* Body */}
          <div
            style={{
              maxHeight: '55vh',
              overflowY: 'auto',
              paddingRight: 6,
              borderTop: '1px solid var(--gilt-line)',
              paddingTop: 10,
            }}
          >
            {!body ? (
              <LoadingState label={"Loading…"} />
            ) : editing ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                {fieldDraft ? (
                  // Structured editor: one plain-text field per editable prose
                  // section (objective / readiness / my-day). No raw HTML — on
                  // save the server re-renders each section and splices it back
                  // between its zone markers, so the drop cap, derived timeline,
                  // and World blocks are left untouched.
                  FIELD_DEFS.filter((f) => f.key in fieldDraft).map((f) => (
                    <Field key={f.key} label={f.label} hint={f.hint}>
                      <Textarea
                        value={fieldDraft[f.key] ?? ''}
                        onChange={(e) =>
                          setFieldDraft((fd) =>
                            fd ? { ...fd, [f.key]: e.target.value } : fd,
                          )
                        }
                        aria-label={f.label}
                        style={{
                          minHeight: f.minHeight,
                          fontFamily: 'var(--font-body)',
                        }}
                      />
                    </Field>
                  ))
                ) : (
                  <Textarea
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                    spellCheck={false}
                    aria-label={"Edit briefing"}
                    style={{
                      minHeight: '40vh',
                      fontFamily: 'var(--font-mono)',
                    }}
                  />
                )}
                {/* Pinned action bar — the fields can outgrow the editor, so
                    Cancel/Save stay stuck to the bottom of the scroll area
                    (content scrolls under them) instead of falling below the
                    fold. Background matches the modal panel (--charbon) so the
                    scrolling prose is cleanly occluded. */}
                <div
                  style={{
                    display: 'flex',
                    gap: 8,
                    justifyContent: 'flex-end',
                    position: 'sticky',
                    bottom: 0,
                    background: 'var(--charbon)',
                    borderTop: '1px solid var(--gilt-line)',
                    paddingTop: 10,
                    paddingBottom: 4,
                  }}
                >
                  <GhostAction
                    label={"Cancel"}
                    size="sm"
                    onClick={cancelEdit}
                    disabled={saving}
                  />
                  <GhostAction
                    label={saving ? "Saving…" : "Save"}
                    size="sm"
                    onClick={() => void saveEdit()}
                    disabled={saving}
                  />
                </div>
              </div>
            ) : (
              // Trust boundary: `htmlBody` is assembled server-side by
              // estormi_briefing/compose/build_daily_note.py, which HTML-escapes all
              // source content (`_esc`) and renders the LLM day-vision from a
              // plain-text marker format (`_render_vision_html` — Python owns
              // every tag; any HTML the model emits is stripped). The second
              // layer is the server-stamped CSP on the SPA document
              // (`_SPA_CSP` in estormi_server/server/static.py:
              // `script-src 'self'`, `img-src 'self' data:`), which blocks any
              // inline <script> or remote image beacon a crafted body could
              // smuggle. The third layer is the client `sanitizeBriefingHtml`
              // pass below — it costs nothing for server-composed bodies (all
              // tags already safe) but closes the raw-HTML edit fallback, which
              // round-trips a user draft back into `htmlBody`.
              // Font/colour/heading/drop-cap styling lives in the shared
              // @estormi/ui-kit/briefing.css (same file the iOS WKWebView loads)
              // so both surfaces render identically. Only the reading font-size
              // is set per-surface here (desktop modal vs phone).
              <div
                className="briefing-body"
                style={{ fontSize: 16 }}
                // eslint-disable-next-line no-restricted-syntax -- sanitised above by `sanitizeBriefingHtml`; server-composed bodies are also CSP-gated (see trust-boundary note)
                dangerouslySetInnerHTML={{ __html: sanitizeBriefingHtml(body.htmlBody ?? '') }}
              />
            )}
          </div>
        </div>
      )}
      </div>
    </ExtModalShell>
  )
}
