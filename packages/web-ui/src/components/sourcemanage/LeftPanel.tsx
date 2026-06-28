/**
 * LeftPanel — routes a source to its connection / pairing / paths panel inside
 * SourceManageModal. Extracted from SourceManageModal.tsx.
 */
import type { SourceRowDescriptor, SourceConnection } from '../SourceRow'
import { KnowledgeSourcesPanel } from '../briefing/KnowledgeSourcesPanel'
import { WhatsAppPanel } from '../sourcepanels/WhatsAppPanel'
import { WhoopPanel } from '../sourcepanels/WhoopPanel'
import { GoogleCalendarPanel } from '../sourcepanels/GoogleCalendarPanel'
import { PathPanel } from '../sourcepanels/PathPanel'
import { ConnectionPanel } from '../sourcepanels/ConnectionPanel'

export function LeftPanel({
  desc,
  connection,
  fdaGranted,
  onChanged,
}: {
  desc: SourceRowDescriptor
  connection: SourceConnection
  /** iMessage only: Full Disk Access confirmed granted by the Tauri shell. */
  fdaGranted?: boolean
  onChanged?: () => void
}) {
  if (desc.key === 'whatsapp')
    return <WhatsAppPanel connection={connection} onChanged={onChanged} />
  // External knowledge: the manage body IS the briefing-sources panel
  // (YouTube/RSS feeds + the briefing schedule, language, and LLM backend
  // that compose them). The panel draws no frame/title — this modal supplies it.
  if (desc.key === 'knowledge') return <KnowledgeSourcesPanel />
  // Google Calendar needs its own OAuth flow + per-calendar picker.
  if (desc.key === 'gcal') return <GoogleCalendarPanel onChanged={onChanged} />
  if (desc.key === 'whoop') return <WhoopPanel onChanged={onChanged} />
  if (desc.key === 'documents') return <PathPanel desc={desc} onChanged={onChanged} />
  return (
    <ConnectionPanel
      desc={desc}
      connection={connection}
      fdaGranted={fdaGranted}
      onChanged={onChanged}
    />
  )
}
