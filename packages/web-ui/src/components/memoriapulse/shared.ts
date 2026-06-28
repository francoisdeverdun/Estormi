/**
 * Shared types + colour palette for the MemoriaPulse stacked-area strip.
 * Extracted from MemoriaPulse.tsx so the chart's renderer (StackedArea) and
 * hover breakdown (HoverTooltip) can share one source of truth.
 */

export interface TimeseriesResponse {
  days: string[]
  sources: string[]
  series: Array<{ day: string; total: number; by_source: Record<string, number> }>
}

/**
 * Data-viz source palette — twelve muted tones in the Estormi gold/sage/lapis
 * family (not the chart-library rainbow). The order is stable so a given
 * source keeps its colour across refreshes; sources past the twelfth wrap.
 *
 * This palette deliberately lives in JS rather than as `var(--token)`: the
 * colours are applied to SVG *presentation attributes* (`<path stroke>`,
 * `<line stroke>`), where CSS custom properties do not resolve — `var()`
 * works only on CSS properties, not SVG attributes. They are also string-
 * concatenated with an alpha suffix (`${colour}55`) for glow shadows. Keeping
 * one named array is the single source of truth; these tones have no chrome-
 * token equivalent (the names are bespoke viz hues, not the palette tokens).
 */
const VIZ_SOURCE_PALETTE = [
  '#C8A467', // soft gold
  '#A88A4F', // old gold
  '#6B8A5F', // sage
  '#7D8FB3', // pale blue
  '#B05A6E', // pale purpure
  '#D9B978', // light enlumine
  '#8AA0A7', // slate
  '#9C7B5C', // tobacco
  '#5F7C8A', // night blue
  '#A8765F', // brick
  '#7A8B6F', // moss
  '#6E6A8A', // soft violet
]

export const colourFor = (i: number) =>
  VIZ_SOURCE_PALETTE[i % VIZ_SOURCE_PALETTE.length]
