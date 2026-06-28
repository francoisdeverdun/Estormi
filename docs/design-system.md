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

# Design system — Ars Memoriae

Estormi runs on a single dark theme inspired by illuminated medieval
manuscripts: **ink + gold**. The design system lives in
[`packages/ui-kit`](../packages/ui-kit/README.md) and is the single source of
truth for colour, typography, ornaments, and primitive components.

## Palette

| Token              | Hex        | Use                                   |
| ------------------ | ---------- | ------------------------------------- |
| `--encre`          | `#0D1117`  | App background                        |
| `--charbon`        | `#1A1F29`  | Card surface                          |
| `--charbon-3`      | `#2E3441`  | Disabled / inert surface              |
| `--or-ancien`      | `#C8A96B`  | Canonical gold                        |
| `--or-clair`       | `#DCBA8A`  | Hover / active gold                   |
| `--or-sombre`      | `#8A7142`  | Pressed / dim gold                    |
| `--parchemin`      | `#F5F1E8`  | Primary text on ink                   |
| `--parchemin-os`   | `#FAF8F4`  | Brightest text                        |
| `--ink-dim`        | `α 0.62`   | Secondary text                        |
| `--ink-dimmer`     | `α 0.38`   | Tertiary text                         |
| `--pourpre`        | `#B82E2E`  | Running / busy state · destructive accent |
| `--pourpre-clair`  | `#B83A57`  | Running / destructive hover (GoldToggle on) |
| `--pourpre-fonce`  | `#6A1818`  | Pressed pourpre                       |
| `--enluminure`     | `#1E3A5F`  | Info state (iOS)                      |
| `--enluminure-clair` | `#4264BA` | Info accent (web SPA)                |
| `--vert-sauge`     | `#6B8A5F`  | Healthy / success                     |
| `--gilt-line`      | `α 0.22`   | Subtle border                         |
| `--gilt-line-strong` | `α 0.45` | Standard border                       |

CSS source: [`packages/ui-kit/src/tokens.css`](../packages/ui-kit/src/tokens.css).
There is no JS token API — SVG fills that can't reach a CSS variable use the
literal hex value inline.

## Typography

| Stack          | Family                              | Used for                          |
| -------------- | ----------------------------------- | --------------------------------- |
| display        | Cinzel + Cinzel Decorative          | titles, eyebrows, buttons         |
| body           | EB Garamond                         | long-form copy (briefing)         |
| ui             | Inter                               | UI controls, search inputs        |
| mono           | JetBrains Mono                      | numbers, timestamps, logs         |

Fonts are vendored under
[`assets/fonts/`](../assets/fonts/) and served by
FastAPI at `/fonts/*.woff2` (mount registered in
[`packages/estormi_server/server/static.py`](../packages/estormi_server/server/static.py)). Only
the Latin subset is shipped — the app is English + French only. To rebuild the
vendor bundle, run `python3 scripts/vendor_fonts.py`; the script writes
`fonts.css` next to the binaries with one `@font-face` rule per declared
weight (variable fonts dedupe to a single shared binary). See
`assets/fonts/SOURCE.md` for the per-family licence and
version notes.

## Primitives (ui-kit exports)

- **Marks** — `Fleuron`, `Diamond`
- **Brand** — `EstormiLogoMark` (blocked burgundy initial — logo, masthead and in-content lettrine), `EstormiMasthead`, `IlluminatedRule`
- **Layout** — `GildedPanel`, `SectionHeader`
- **Actions** — `PrimaryAction` (the one filled-gold hero per surface), `GhostAction` (gilt outline; `tone="danger"` for destructive — there is no filled red button)
- **Controls** — `Switch` (a labelled `GoldToggle`), `GoldToggle` (gilded on/off switch)
- **Inputs** — `TextInput`, `Textarea`, `Select`, `Field` (own the gilt-line chrome; never style a raw `<input>`/`<select>`/`<textarea>` inline)
- **States** — `EmptyState`, `LoadingState`, `ErrorState`

Core primitives ship with a Vitest in `packages/ui-kit/src/__tests__/`.

## Surface

The SPA in `packages/web-ui/` is a single compact one-pager
(`App.tsx` → `OnePagerTopBar` + `CardinalSection` + `ParametersSection` +
app-level modals; `App.tsx` mounts `BriefingModal` and `CharacterModal`, and its
`ModalId` is `'briefing' | 'character' | null`). It composes from these primitives — no hash
routes, no multi-page shell. See [`.claude/skills/web-ui/SKILL.md`](../.claude/skills/web-ui/SKILL.md)
for the live layout.

## How to add a new primitive

Every new component must:

1. Live in [`packages/ui-kit/src/components/<Name>.tsx`](../packages/ui-kit/src/components/).
2. Reference colours via `var(--...)` from `tokens.css` (not hard-coded hex).
3. Export from [`packages/ui-kit/src/index.ts`](../packages/ui-kit/src/index.ts).
4. Ship with a Vitest in `packages/ui-kit/src/__tests__/`.
5. Be referenced from at least one page — otherwise it's premature.

## How to add a new token

1. Declare it in [`tokens.css`](../packages/ui-kit/src/tokens.css) under the
   appropriate group.
2. Reference it via `var(--…)`. There is no JS token API; SVG fills that
   cannot reach a CSS variable use the literal hex value inline.

## Theme

The app is dark-only by design contract. The CSS root sets
`data-theme="dark"` and there is no light theme variant. If a future spec
needs light theme, add a sibling block under
`html[data-theme='light']` in `tokens.css` (matching the existing
`html[data-theme='dark']` block) — do not invent a second tokens file.
