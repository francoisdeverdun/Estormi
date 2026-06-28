/**
 * BriefingBuildControls — how the daily briefing is *composed*.
 *
 * Estormi has two separate processes: ingestion of sources (the External
 * knowledge feed list, configured in that source's manage modal) and the
 * *build* of the briefing from what's already in memory. These controls drive
 * the build — schedule, narration voice, and home location — so they live in
 * Officina (the MaintenanceCard), not next to the source list.
 *
 * The reasoning backend is no longer a choice: the briefing always runs through
 * the two bundled local quills (two-quills routing). The narration voice stays
 * selectable. The "About you" profile moved up to the Summarium page (the
 * Character modal).
 *
 * Backed by these settings keys:
 *   - briefing_schedule_cron   — cron, or "manual"
 *   - briefing_tts_model / briefing_tts_voice — narration model + voice
 *   - briefing_home_location   — home city for keyless weather
 */
import { GhostAction, Select, Switch, TextInput } from '@estormi/ui-kit'
import { useEffect, useState } from 'react'
import { getTtsCatalog, type TtsModel } from '../../api/tts'
import { useSettings } from '../../hooks/useSettings'
import { formatRelative, nextCronTime } from '../../lib/cron'

/** Default briefing cron when the setting is unset (07:00 daily) — mirrors the
 *  server's ENGINE_SCHEDULE_DEFAULTS and DistillationCard's DEFAULT_CRON. */
const DEFAULT_BRIEFING_CRON = '0 7 * * *'

/** Voxtral preset prefixes (`<prefix>_<gender>`) → display language and the
 *  briefing-language code they speak. The style prefixes (neutral/casual/
 *  cheerful) are the English narrators. */
const VOICE_PREFIX: Record<string, { label: string; lang: string }> = {
  neutral: { label: 'English', lang: 'en' },
  casual: { label: 'English · casual', lang: 'en' },
  cheerful: { label: 'English · cheerful', lang: 'en' },
  fr: { label: 'French', lang: 'fr' },
  es: { label: 'Spanish', lang: 'es' },
  de: { label: 'German', lang: 'de' },
  it: { label: 'Italian', lang: 'it' },
  pt: { label: 'Portuguese', lang: 'pt' },
  nl: { label: 'Dutch', lang: 'nl' },
  ar: { label: 'Arabic', lang: 'ar' },
  hi: { label: 'Hindi', lang: 'hi' },
}

/** "fr_female" → "French · female". */
function voiceLabel(key: string): string {
  const i = key.lastIndexOf('_')
  if (i < 0) return key
  const prefix = VOICE_PREFIX[key.slice(0, i)]
  return prefix ? `${prefix.label} · ${key.slice(i + 1)}` : key
}

/** Briefing-language code a preset speaks ('' when unknown). */
function voiceLang(key: string): string {
  const i = key.lastIndexOf('_')
  return i < 0 ? '' : (VOICE_PREFIX[key.slice(0, i)]?.lang ?? '')
}

export function BriefingBuildControls() {
  const { settings, save: saveSettings } = useSettings()
  const [error, setError] = useState<string | null>(null)

  // Schedule (cron, or "manual"). Local draft kept off the settings snapshot
  // so typing doesn't race a /api/settings round-trip per keystroke.
  const briefingCron = (settings ?? {})['briefing_schedule_cron'] || DEFAULT_BRIEFING_CRON
  const [cronDraft, setCronDraft] = useState(briefingCron)
  const [cronDirty, setCronDirty] = useState(false)
  useEffect(() => {
    if (!cronDirty) setCronDraft(briefingCron)
  }, [briefingCron, cronDirty])
  const cronEnabled = briefingCron !== 'manual'
  const commitCron = async (next: string) => {
    try {
      await saveSettings({ briefing_schedule_cron: next })
      setCronDirty(false)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  // TTS backend — narration model + narrator voice. The model catalog and
  // presets come from /api/tts/catalog so the lists never drift from the
  // backend; the choices persist via briefing_tts_model / briefing_tts_voice.
  const ttsVoice = (settings ?? {})['briefing_tts_voice'] || ''
  const [voiceOpts, setVoiceOpts] = useState<string[]>([])
  const [ttsModels, setTtsModels] = useState<TtsModel[]>([])
  const [ttsModelCurrent, setTtsModelCurrent] = useState<string>('')
  useEffect(() => {
    let alive = true
    void (async () => {
      try {
        const r = await getTtsCatalog()
        if (!alive) return
        setVoiceOpts(r.voices ?? [])
        setTtsModels(r.models ?? [])
        setTtsModelCurrent(r.selected ?? '')
      } catch {
        /* selectors fall back to Auto-only; narration still works */
      }
    })()
    return () => {
      alive = false
    }
  }, [])
  // French-only briefing: show the French voice presets (plus the already-chosen
  // voice, so a manual off-language pick stays visible instead of vanishing).
  const voiceChoices = voiceOpts
    .filter((v) => voiceLang(v) === 'fr' || v === ttsVoice)
    .map((v) => ({ value: v, label: voiceLabel(v) }))
    .sort((a, b) => a.label.localeCompare(b.label))
  if (ttsVoice && !voiceChoices.some((v) => v.value === ttsVoice)) {
    voiceChoices.unshift({ value: ttsVoice, label: voiceLabel(ttsVoice) })
  }
  const commitVoice = async (next: string) => {
    try {
      await saveSettings({ briefing_tts_voice: next })
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }
  const commitTtsModel = async (next: string) => {
    if (!next) return
    setTtsModelCurrent(next)
    try {
      await saveSettings({ briefing_tts_model: next })
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  // Home location feeds keyless weather (Open-Meteo) for the day-vision. Local
  // draft, committed on blur/Enter, so typing doesn't round-trip per keystroke.
  const homeLoc = (settings ?? {})['briefing_home_location'] || ''
  const [homeDraft, setHomeDraft] = useState(homeLoc)
  const [homeDirty, setHomeDirty] = useState(false)
  useEffect(() => {
    if (!homeDirty) setHomeDraft(homeLoc)
  }, [homeLoc, homeDirty])
  const commitHome = async (next: string) => {
    try {
      await saveSettings({ briefing_home_location: next })
      setHomeDirty(false)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const rowStyle: React.CSSProperties = {
    display: 'flex',
    flexWrap: 'wrap',
    alignItems: 'center',
    gap: 8,
    marginBottom: 8,
    fontFamily: 'var(--font-mono)',
    fontSize: 11,
    color: 'var(--ink-dim)',
  }

  return (
    <div>
      {/* Schedule */}
      <div style={rowStyle}>
        <Switch
          checked={cronEnabled}
          onChange={(on) => {
            if (on) {
              const next = cronDraft && cronDraft !== 'manual' ? cronDraft : DEFAULT_BRIEFING_CRON
              setCronDraft(next)
              void commitCron(next)
            } else {
              void commitCron('manual')
            }
          }}
          label="⏱"
          ariaLabel="Enable scheduled briefings"
          title="Enable scheduled briefings"
          dimWhenOff
        />
        <TextInput
          type="text"
          value={cronDraft === 'manual' ? '' : cronDraft}
          placeholder={DEFAULT_BRIEFING_CRON}
          disabled={!cronEnabled}
          onChange={(e) => {
            setCronDraft(e.target.value)
            setCronDirty(true)
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') void commitCron(cronDraft.trim() || 'manual')
            if (e.key === 'Escape') {
              setCronDraft(briefingCron)
              setCronDirty(false)
            }
          }}
          spellCheck={false}
          aria-label="Briefing cron schedule"
          style={{ flex: '0 1 130px', minWidth: 110 }}
        />
        {cronDirty ? (
          <GhostAction
            label="Save"
            size="sm"
            onClick={() => void commitCron(cronDraft.trim() || 'manual')}
          />
        ) : (
          <span title={cronEnabled ? 'Next scheduled briefing' : 'Scheduled briefings disabled'}>
            {cronEnabled
              ? `· next in ${formatRelative(nextCronTime(briefingCron))}`
              : '· manual only'}
          </span>
        )}
      </div>

      {/* TTS backend — narration model + narrator voice */}
      <div style={rowStyle}>
        <label
          style={{ display: 'flex', alignItems: 'center', gap: 6 }}
          title="TTS model the briefing narration is synthesized with"
        >
          🎙
          <Select
            value={ttsModelCurrent}
            onChange={(e) => void commitTtsModel(e.target.value)}
            aria-label="TTS model"
          >
            {ttsModels.length === 0 ? (
              <option value="">(no voice model)</option>
            ) : (
              ttsModels.map((m) => (
                <option key={m.key} value={m.key}>
                  {m.label}
                </option>
              ))
            )}
          </Select>
        </label>
        <Select
          value={ttsVoice}
          onChange={(e) => void commitVoice(e.target.value)}
          aria-label="Narrator voice"
          title="Narrator voice — Auto picks the French voice matching the briefing"
          style={{ flex: '1 1 160px', minWidth: 140 }}
        >
          <option value="">Auto · match language</option>
          {voiceChoices.map((v) => (
            <option key={v.value} value={v.value}>
              {v.label}
            </option>
          ))}
        </Select>
      </div>

      {/* Weather — home location feeds the keyless Open-Meteo forecast */}
      <div style={rowStyle}>
        <label
          style={{ display: 'flex', alignItems: 'center', gap: 6, flex: '1 1 200px' }}
          title="Home location — used for the day's weather forecast"
        >
          📍
          <TextInput
            type="text"
            value={homeDraft}
            placeholder="Paris, France"
            onChange={(e) => {
              setHomeDraft(e.target.value)
              setHomeDirty(true)
            }}
            onBlur={() => {
              if (homeDirty) void commitHome(homeDraft.trim())
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') void commitHome(homeDraft.trim())
            }}
            aria-label="Home location"
            spellCheck={false}
            style={{ flex: 1, minWidth: 140 }}
          />
        </label>
      </div>

      {error && (
        <div
          role="alert"
          style={{
            marginTop: 4,
            color: 'var(--rouge-clair)',
            fontFamily: 'var(--font-mono)',
            fontSize: 11,
          }}
        >
          {error}
        </div>
      )}
    </div>
  )
}
