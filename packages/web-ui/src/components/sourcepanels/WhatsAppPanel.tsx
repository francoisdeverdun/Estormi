/**
 * WhatsAppPanel — the WhatsApp source pairing + chat-tagging block, extracted
 * from SourceManageModal.tsx.
 *
 * Shows the QR pairing affordance until the sidecar reports a paired session,
 * then a paginated list of chats/groups each with an include toggle and a
 * life-context tag (ChatRow). Disconnect clears the pairing.
 */
import { useCallback, useEffect, useState } from 'react'
import { GhostAction } from '@estormi/ui-kit'
import { ChatRow, DangerButton, Eyebrow } from './shared'
import { SourceList } from './SourceList'
import type { SourceConnection } from '../SourceRow'
import {
  getWhatsAppChats,
  patchWhatsAppChat,
  resetWhatsAppPairing,
  whatsappQrUrl,
  type WhatsAppChat,
} from '../../api/sources_ext'

export function WhatsAppPanel({
  connection,
  onChanged,
}: {
  connection: SourceConnection
  onChanged?: () => void
}) {
  const paired = connection === 'connected'
  const [qrSrc, setQrSrc] = useState(whatsappQrUrl())
  const [chats, setChats] = useState<WhatsAppChat[] | null>(null)
  const [loadErr, setLoadErr] = useState<string | null>(null)
  const [confirmDisc, setConfirmDisc] = useState(false)
  const [disconnecting, setDisconnecting] = useState(false)
  // Paginate the chat list — a paired account can carry hundreds of chats,
  // and rendering them all turns the modal into an endless scroll.
  const [page, setPage] = useState(0)

  const refresh = useCallback(async () => {
    try {
      const list = await getWhatsAppChats()
      setChats(list)
      setLoadErr(null)
    } catch (e) {
      setLoadErr(e instanceof Error ? e.message : String(e))
    }
  }, [])

  // Disconnect lives here, in the Pairing section, as well as in the modal's
  // Danger zone — the danger-zone button sits *below* the full chat list (can
  // be hundreds of rows), so the "use Disconnect below" hint above pointed at
  // a control the user had to scroll past every chat to reach. Forgetting the
  // session here lets the QR re-pair flow take over in place.
  const onDisconnect = useCallback(async () => {
    setDisconnecting(true)
    try {
      await resetWhatsAppPairing()
      setConfirmDisc(false)
      setChats(null)
      setQrSrc(whatsappQrUrl())
      onChanged?.()
      await refresh()
    } catch (e) {
      setLoadErr(e instanceof Error ? e.message : String(e))
    } finally {
      setDisconnecting(false)
    }
  }, [onChanged, refresh])

  // Poll while the modal is open so a fresh QR pairing populates the chats
  // list in place — the sidecar syncs chats a few seconds after the scan,
  // and the user shouldn't have to close and reopen the modal to see them.
  // The list also reflects auto-tag results that the nightly WhatsApp
  // ingestion run produces in the background.
  useEffect(() => {
    void refresh()
    const id = setInterval(() => void refresh(), 4000)
    return () => clearInterval(id)
  }, [refresh])

  const setKind = async (chatId: string, kind: string) => {
    try {
      await patchWhatsAppChat(chatId, kind)
      await refresh()
      onChanged?.()
    } catch (e) {
      setLoadErr(e instanceof Error ? e.message : String(e))
    }
  }

  const onCount = chats?.filter((c) => c.group_type !== 'noise').length ?? 0
  const totCount = chats?.length ?? 0

  // 4s polling re-sets `chats` but leaves `page` untouched, so clamp here
  // rather than reset on refresh — the page stays put while you tag, and
  // only snaps back into range if the list shrinks under you.
  const PAGE_SIZE = 50
  const pageCount = Math.max(1, Math.ceil(totCount / PAGE_SIZE))
  const safePage = Math.min(page, pageCount - 1)
  const pageChats = chats?.slice(safePage * PAGE_SIZE, safePage * PAGE_SIZE + PAGE_SIZE) ?? []
  const rangeStart = totCount === 0 ? 0 : safePage * PAGE_SIZE + 1
  const rangeEnd = Math.min(totCount, safePage * PAGE_SIZE + PAGE_SIZE)

  return (
    <>
      <Eyebrow text={"Pairing"} />
      {paired ? (
        <div
          style={{
            padding: 16,
            marginBottom: 18,
            background: 'var(--encre)',
            border: '1px solid var(--gilt-line)',
            fontFamily: 'var(--font-ui)',
            fontSize: 14,
            color: 'var(--ink-dim)',
            lineHeight: 1.5,
          }}
        >
          <div
            style={{
              fontFamily: 'var(--font-display)',
              fontSize: 13,
              letterSpacing: '0.2em',
              color: 'var(--or-clair)',
              textTransform: 'uppercase',
              fontWeight: 700,
              marginBottom: 6,
            }}
          >
            Paired
          </div>
          Linked to your phone. WhatsApp messages sync during the nightly run.
          To re-pair from scratch — the only way to re-pull message history
          after a data reset — disconnect and scan a fresh QR.
          <div style={{ display: 'flex', gap: 6, marginTop: 12 }}>
            <DangerButton
              label={"Disconnect"}
              disabled={disconnecting}
              onClick={() => setConfirmDisc(true)}
            />
          </div>
          {confirmDisc && (
            <div
              role="alertdialog"
              aria-label={"Confirm disconnect"}
              style={{
                marginTop: 12,
                padding: 10,
                border: '1px solid var(--rouge-clair)',
                background: 'var(--overlay-pourpre)',
                color: 'var(--parchemin)',
              }}
            >
              <div style={{ marginBottom: 8 }}>
                Disconnect WhatsApp? This wipes the paired session, the chat
                list, and the ingested message history (and its memory) for a
                clean slate. You'll scan a new QR, and the next run re-pulls
                history from scratch.
              </div>
              <div style={{ display: 'flex', gap: 6 }}>
                <GhostAction
                  label={"Cancel"}
                  size="sm"
                  onClick={() => setConfirmDisc(false)}
                />
                <DangerButton
                  label={disconnecting ? 'Disconnecting…' : 'Confirm'}
                  disabled={disconnecting}
                  onClick={() => void onDisconnect()}
                />
              </div>
            </div>
          )}
        </div>
      ) : (
        <div
          style={{
            padding: 16,
            marginBottom: 18,
            background: 'var(--encre)',
            border: '1px solid var(--gilt-line)',
            display: 'flex',
            gap: 16,
            alignItems: 'center',
          }}
        >
          <img
            // `qrSrc` is a cache-busted URL (`/api/whatsapp/qr.png?ts=…`). The
            // `key` forces React to remount the <img> on each refresh — that
            // clears any imperative `visibility: hidden` we set on a previous
            // load failure, so a successful refetch is actually visible again.
            key={qrSrc}
            src={qrSrc}
            alt="WhatsApp pairing QR"
            width={120}
            height={120}
            onLoad={(e) => {
              ;(e.currentTarget as HTMLImageElement).style.visibility = 'visible'
            }}
            onError={(e) => {
              // QR endpoint returns 503 when no pairing in progress — hide the
              // broken-image glyph; the "Refresh QR" button below stays usable.
              ;(e.currentTarget as HTMLImageElement).style.visibility = 'hidden'
            }}
            style={{
              width: 120,
              height: 120,
              border: '1px solid var(--gilt-line)',
              background: 'var(--charbon)',
              objectFit: 'contain',
            }}
          />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div
              style={{
                fontFamily: 'var(--font-display)',
                fontSize: 13,
                letterSpacing: '0.2em',
                color: 'var(--or-clair)',
                textTransform: 'uppercase',
                fontWeight: 700,
                marginBottom: 6,
              }}
            >
              Awaiting scan
            </div>
            <div
              style={{
                fontFamily: 'var(--font-ui)',
                fontSize: 14,
                color: 'var(--ink-dim)',
                lineHeight: 1.5,
                marginBottom: 10,
              }}
            >
              On your phone: WhatsApp → Settings → Linked Devices → Link a Device, then scan
              the QR.
            </div>
            <GhostAction
              label={"Refresh QR"}
              size="sm"
              onClick={() => setQrSrc(whatsappQrUrl())}
            />
          </div>
        </div>
      )}

      <SourceList
        title={`Chats & groups (${onCount} / ${totCount})`}
        items={chats === null ? null : pageChats}
        error={loadErr}
        loadingLabel="Loading chats…"
        emptyLabel="No chats yet — pair a device above."
        renderRow={(c) => (
          <ChatRow
            key={c.chat_id}
            name={c.chat_name}
            kind={c.group_type}
            chatKind={c.chat_kind}
            onKind={(k) => void setKind(c.chat_id, k)}
          />
        )}
        footer={
          pageCount > 1 ? (
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                gap: 10,
                marginTop: 8,
              }}
            >
              <GhostAction
                label={"‹ Prev"}
                size="sm"
                disabled={safePage === 0}
                onClick={() => setPage((p) => Math.max(0, p - 1))}
              />
              <span
                style={{
                  fontFamily: 'var(--font-mono)',
                  fontSize: 12,
                  color: 'var(--ink-dim)',
                }}
              >
                {rangeStart}–{rangeEnd} of {totCount}
              </span>
              <GhostAction
                label={"Next ›"}
                size="sm"
                disabled={safePage >= pageCount - 1}
                onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))}
              />
            </div>
          ) : null
        }
      />
    </>
  )
}
