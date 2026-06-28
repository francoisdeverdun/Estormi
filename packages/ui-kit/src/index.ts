/**
 * @estormi/ui-kit
 *
 * Estormi — Ars Memoriae — design system.
 * Ink-and-gold, single dark theme. Cinzel/Inter/EB Garamond/JetBrains Mono.
 *
 * Always import tokens.css once at app start so the CSS custom properties
 * resolve. See ``packages/web-ui/src/main.tsx``. The SPA renders entirely
 * from those ``var(--…)`` custom properties — there is no JS token API.
 */

// Marks (small SVG primitives)
export { Fleuron, Diamond } from './components/marks'
export type { FleuronProps, DiamondProps } from './components/marks'

// Brand logo mark (the blocked illuminated initial — icon, masthead and
// in-content lettrine; the retired bracket-frame IlluminatedCap folded into it)
export { EstormiLogoMark } from './components/LogoMark'
export type { EstormiLogoMarkProps } from './components/LogoMark'

// Brand masthead (mark + wordmark + garlanded tagline + illuminated rule)
export { EstormiMasthead, IlluminatedRule } from './components/Masthead'
export type { EstormiMastheadProps, IlluminatedRuleProps } from './components/Masthead'

// Layout primitives
export { GildedPanel } from './components/GildedPanel'
export type { GildedPanelProps } from './components/GildedPanel'
export { SectionHeader } from './components/SectionHeader'
export type { SectionHeaderProps } from './components/SectionHeader'

// Actions
export { PrimaryAction, GhostAction } from './components/buttons'
export type { PrimaryActionProps, GhostActionProps } from './components/buttons'
export { GoldToggle } from './components/GoldToggle'
export type { GoldToggleProps } from './components/GoldToggle'
export { Switch } from './components/Switch'
export type { SwitchProps } from './components/Switch'

// Form fields (themed select / input / textarea + label wrapper)
export { TextInput, Textarea, Select, Field } from './components/fields'
export type {
  TextInputProps,
  TextareaProps,
  SelectProps,
  FieldProps,
} from './components/fields'

// States
export { EmptyState, LoadingState, ErrorState } from './components/states'
export type {
  EmptyStateProps,
  LoadingStateProps,
  ErrorStateProps,
} from './components/states'
