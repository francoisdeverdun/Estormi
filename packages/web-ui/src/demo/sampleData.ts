/**
 * Fictitious sample dataset for demo mode.
 *
 * All names and events are fictional. The data mirrors the shapes the SPA
 * consumes from the real API so every panel renders with plausible content.
 */

import type { Overview } from '../api/overview'
import type { PipelineData } from '../api/pipeline'
import type { BriefingSummary, Briefing } from '../api/knowledge'

// ── Overview (settings/overview) ────────────────────────────────────────────

export const demoOverview: Overview = {
  data_dir: '/Users/demo/Estormi-data',
  settings: {
    briefing_language: 'fr',
    briefing_cron: '0 6 * * *',
  },
  storage: {
    db_bytes: 42_000_000,
    qdrant_bytes: 18_500_000,
    staging_bytes: 0,
    whatsapp_cache_bytes: 3_200_000,
    total_chunks: 4_312,
  },
  sources: {
    counts: {
      notes: 620,
      mail: 1_540,
      calendar: 380,
      reminders: 95,
      imessage: 870,
      whatsapp: 507,
      documents: 210,
      gcal: 90,
    },
    watermarks: {
      notes: '2026-06-24T22:00:00',
      mail: '2026-06-24T22:00:00',
      calendar: '2026-06-24T22:00:00',
      reminders: '2026-06-24T22:00:00',
      imessage: '2026-06-24T22:00:00',
      whatsapp: '2026-06-24T22:00:00',
      documents: '2026-06-24T22:00:00',
      gcal: '2026-06-24T22:00:00',
    },
  },
  model: {
    name: 'ministral-3-14b',
    loaded: true,
    exists: true,
    size_bytes: 8_200_000_000,
    tier: 'ministral-3-14b',
  },
  pipeline: {
    next_run_at: '2026-06-25T06:00:00',
    last_run_started: '2026-06-24T06:02:11',
    overall_status: 'ok',
    last_run_failed_stages: [],
  },
  whatsapp: { connected: true, paired: true, session_state: 'ready' },
  permissions: { imessage_fda: true },
}

// ── Pipeline ────────────────────────────────────────────────────────────────

export const demoPipeline: PipelineData = {
  is_running: false,
  overall_status: 'ok',
  last_run_started: '2026-06-24T06:02:11',
  last_run_ended: '2026-06-24T06:14:38',
  last_run_duration_s: 747,
  last_run_duration: '12m 27s',
  last_run_ago: 'il y a 23 h',
  last_run_failed_stages: [],
  last_run_chunks_added: 42,
  last_run_chunks_by_source: { notes: 8, mail: 14, calendar: 3, imessage: 12, whatsapp: 5 },
  mean_duration_s: 690,
  mean_duration: '11m 30s',
  next_run_at: '2026-06-25T06:00:00',
  run_count: 18,
  errors: [],
  stages: [
    { name: 'notes', status: 'ok', duration_s: 45, duration: '45s' },
    { name: 'mail', status: 'ok', duration_s: 120, duration: '2m 00s' },
    { name: 'calendar', status: 'ok', duration_s: 12, duration: '12s' },
    { name: 'reminders', status: 'ok', duration_s: 8, duration: '8s' },
    { name: 'imessage', status: 'ok', duration_s: 90, duration: '1m 30s' },
    { name: 'whatsapp', status: 'ok', duration_s: 180, duration: '3m 00s' },
    { name: 'documents', status: 'ok', duration_s: 210, duration: '3m 30s' },
    { name: 'gcal', status: 'ok', duration_s: 15, duration: '15s' },
  ],
  history: [],
}

// ── Briefings ───────────────────────────────────────────────────────────────

const DEMO_DATE = '2026-06-24'

export const demoBriefingList: { items: BriefingSummary[] } = {
  items: [
    { date: '2026-06-24', title: 'Mercredi 24 juin 2026' },
    { date: '2026-06-23', title: 'Mardi 23 juin 2026' },
    { date: '2026-06-22', title: 'Lundi 22 juin 2026' },
  ],
}

export const demoBriefing: Briefing = {
  date: DEMO_DATE,
  title: 'Mercredi 24 juin 2026',
  htmlBody: `
<section class="briefing-lede">
  <h2>Ta journée en un coup d'œil</h2>
  <p>Après une nuit de 7 h 20 de sommeil (score de récupération 82 %),
  ta journée s'annonce chargée mais maîtrisée. Le point avec
  <strong>Camille Renoir</strong> sur le projet Ariane est calé à 10 h,
  et le déjeuner avec <strong>Marc Lefèvre</strong> au Comptoir du
  Panthéon à midi. Ce soir, cours de céramique à 19 h.</p>
</section>

<section class="briefing-readiness">
  <h3>État de forme</h3>
  <p>Récupération WHOOP : <strong>82 %</strong> (vert). Variabilité
  cardiaque à 48 ms, fréquence de repos à 52 bpm. Bonne base pour une
  journée active — pas besoin de lever le pied.</p>
</section>

<section class="briefing-myday">
  <h3>Fil de la journée</h3>
  <ul>
    <li><strong>10 h 00</strong> — Point projet Ariane avec Camille Renoir
    (visio). Tu avais noté hier soir dans tes Notes un point à soulever
    sur le calendrier de livraison Q3.</li>
    <li><strong>12 h 00</strong> — Déjeuner avec Marc Lefèvre, Comptoir du
    Panthéon. Il t'avait envoyé un message WhatsApp dimanche pour
    confirmer.</li>
    <li><strong>14 h 30</strong> — Rappel : envoyer le devis à
    <strong>Sophie Marchand</strong> (mail en attente depuis vendredi).</li>
    <li><strong>19 h 00</strong> — Cours de céramique, atelier Terre &amp;
    Feu. Pense à prendre le tablier.</li>
  </ul>
</section>

<section class="briefing-world">
  <h3>Le monde autour</h3>
  <p>Les négociations climatiques de Bonn ont abouti à un accord-cadre
  sur les crédits carbone — premier consensus depuis la COP de Dubaï.
  En France, la réforme du marché locatif entre en vigueur le 1er juillet.
  <span class="src">[Le Monde, Reuters]</span></p>
</section>
`.trim(),
  fields: {
    objective:
      "Boucler le devis Marchand et préparer les points Ariane pour la visio de 10 h.",
    readiness:
      "Récupération 82 % — journée verte, pas de restriction.",
    myDay:
      "10 h point Ariane avec Camille, midi déjeuner Marc au Comptoir du Panthéon, 14 h 30 devis Sophie, 19 h céramique.",
  },
}

// ── Jobs state (engine room) ────────────────────────────────────────────────

export const demoJobsState = {
  running: null,
  queue: [],
  history: [],
}

export const demoJobsSchedule = {
  ingestion: { next_run_at: '2026-06-25T06:00:00' },
  briefing: { next_run_at: '2026-06-25T06:15:00' },
}

// ── Settings ────────────────────────────────────────────────────────────────

export const demoSettings: Record<string, string> = {
  briefing_language: 'fr',
  briefing_cron: '0 6 * * *',
  knowledge_enabled: 'true',
  pipeline_cron: '0 6 * * *',
}

// ── Distill status ──────────────────────────────────────────────────────────

export const demoDistillStatus = {
  workspace: null,
  tooling: { installed: false },
  references: { total: 0, models: {} },
  last_run: null,
}

// ── Model catalog ───────────────────────────────────────────────────────────

export const demoModelCatalog = {
  models: [
    {
      tier: 'ministral-3-14b',
      label: 'Ministral 3 — 14B',
      installed: true,
      size_bytes: 8_200_000_000,
      selected: true,
    },
    {
      tier: 'gemma-4-12b',
      label: 'Gemma 4 — 12B',
      installed: true,
      size_bytes: 7_100_000_000,
      selected: false,
    },
  ],
}

// ── Timeseries ──────────────────────────────────────────────────────────────

export const demoTimeseries = {
  days: [
    '2026-06-18',
    '2026-06-19',
    '2026-06-20',
    '2026-06-21',
    '2026-06-22',
    '2026-06-23',
    '2026-06-24',
  ],
  sources: ['notes', 'mail', 'calendar', 'imessage', 'whatsapp'],
  series: [
    { day: '2026-06-18', by_source: { notes: 80, mail: 200, calendar: 50, imessage: 110, whatsapp: 60 } },
    { day: '2026-06-19', by_source: { notes: 160, mail: 400, calendar: 100, imessage: 220, whatsapp: 120 } },
    { day: '2026-06-20', by_source: { notes: 240, mail: 600, calendar: 150, imessage: 330, whatsapp: 190 } },
    { day: '2026-06-21', by_source: { notes: 320, mail: 810, calendar: 200, imessage: 440, whatsapp: 260 } },
    { day: '2026-06-22', by_source: { notes: 410, mail: 1020, calendar: 260, imessage: 560, whatsapp: 340 } },
    { day: '2026-06-23', by_source: { notes: 510, mail: 1250, calendar: 320, imessage: 700, whatsapp: 420 } },
    { day: '2026-06-24', by_source: { notes: 620, mail: 1540, calendar: 380, imessage: 870, whatsapp: 507 } },
  ],
}
