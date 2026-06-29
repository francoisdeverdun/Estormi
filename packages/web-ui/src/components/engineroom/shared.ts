/**
 * Shared scaffolding for the engine-room popover and its sub-panels.
 *
 * These constants and the per-engine launch map used to live inline in
 * EngineRoomPopover.tsx; they were extracted so QueueRow / EnginesGrid /
 * EngineLogModal can each live in their own file under `components/engineroom/`
 * and import the common pieces from here.
 */
import type { EngineKind, QueueSource } from '../../state/SystemStatus'
import { runPipeline } from '../../api/pipeline'
import { runKnowledge } from '../../api/knowledge'

export const QUEUE_SOURCE_LABEL: Record<QueueSource, string> = {
  manual: 'manual',
  schedule: 'schedule',
  backlog: 'backlog',
}

// Per-engine launch — enqueue (or start immediately when nothing is running)
// through the typed API clients, the single source for each engine's
// endpoint. Backed by `estormi_server/api/{pipeline,knowledge}.py`. Partial by
// design: `distill` is a valid EngineKind (it shows in the badge/queue) but is
// launched from the DistillationCard, not the engine grid — so it has no entry
// here and EnginesGrid renders no tile for it.
export const RUN_ENGINE: Partial<Record<EngineKind, () => Promise<unknown>>> = {
  ingestion: () => runPipeline(),
  briefing: () => runKnowledge(),
}
