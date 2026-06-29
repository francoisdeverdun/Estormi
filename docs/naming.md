<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../assets/brand/estormi-wordmark-dark.svg">
    <img src="../assets/brand/estormi-wordmark-light.svg" alt="Estormi" width="220">
  </picture>
</p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../assets/brand/estormi-divider.svg">
    <img src="../assets/brand/estormi-divider-light.svg" alt="" width="420">
  </picture>
</p>

# Product Naming

Public product name: **Estormi**. Public tagline: ***Ars Memoriae***.

## Surfaces and identifiers

| Surface (dir) | User-facing name | Bundle ID | Status |
|---------------|------------------|-----------|--------|
| `apps/estormi-macos/` | **Estormi** | `app.estormi.local` | Active |
| `apps/estormi-ios/` | **Estormi** | `app.estormi.ios` | Active |
| `apps/estormi-cloud/` | *(internal helper — `EstormiCloud`)* | `app.estormi.doorbell` | Active |

Both shipping surfaces are presented to users as **Estormi**; the platform
suffix lives only in technical docs and bundle identifiers. `estormi-cloud` is
a faceless contributor-side CloudKit-doorbell helper, never branded to users.

## Branding guidelines

**Estormi** is always written in title case. Never all-caps (ESTORMI), never lowercase (estormi) in user-facing copy.

**Ars Memoriae** is always italicised in prose: *Ars Memoriae*. In code/config it appears without italics.

## Color palette

The canonical palette lives in [`design-system.md`](design-system.md), backed
by [`packages/ui-kit/src/tokens.css`](../packages/ui-kit/src/tokens.css).
The runtime ships a single dark theme — there is no separate light-mode token
set today.

## Typography

Typography is defined in [`design-system.md`](design-system.md).

## Design motifs

Estormi draws visual inspiration from medieval illuminated manuscripts,
read through a modern-minimal lens — geometry and air, not crowded ornament:
- **Blocked illuminated initial** — the one lettrine device (`EstormiLogoMark`):
  a gold-gradient Cinzel majuscule on a burgundy rounded ground inside a gold
  keyline. The brand `E` in the masthead/app icon; the section's own initial
  on page titles.
- **Illuminated rule** — a hairline fading at both ends with a centred
  burgundy lozenge, flanking quatrefoils and outboard pips (`IlluminatedRule`).
- **Gilded panels** — rounded charbon cards inside a single gilt hairline
  (`GildedPanel`; 12 px panel radius, 4 px tight radius for chips/tiles).

These motifs are implemented in `packages/ui-kit/src/components/` (web) and `apps/estormi-ios/Sources/Design/` (native iOS).

## URL prefixes

The briefing READ endpoints live under `/api/briefings` (read / delete /
reset, in `packages/estormi_server/api/knowledge.py`). The engine-**control** routes
live under `/api/knowledge/*` — the same Briefing engine; `knowledge` is its
internal label (also used by the `knowledge_*` settings keys and
`knowledge.log`).

