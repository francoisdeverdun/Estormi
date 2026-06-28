// Flat ESLint config for the Estormi one-pager SPA (@estormi/web-ui).
//
// React 18/19 + Vite + TypeScript (strict). Scope is `src/` only — config,
// build output and tests' tooling are not linted here.
//
// Policy: real bugs stay `error` (rules-of-hooks, no-undef, etc.); the
// rules that surface pre-existing debt without being clear-cut bugs
// (exhaustive-deps, jsx-a11y) start at `warn` so CI stays green while the
// linter is present. Tighten to `error` once the warnings are burned down.
import js from '@eslint/js'
import tseslint from 'typescript-eslint'
import reactHooks from 'eslint-plugin-react-hooks'
import jsxA11y from 'eslint-plugin-jsx-a11y'
import globals from 'globals'

export default tseslint.config(
  { ignores: ['dist', 'coverage', 'playwright-report', 'test-results'] },
  {
    files: ['src/**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      ...tseslint.configs.recommended,
      reactHooks.configs['recommended-latest'],
      jsxA11y.flatConfigs.recommended,
    ],
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: 'module',
      globals: {
        ...globals.browser,
        ...globals.es2023,
      },
      parserOptions: {
        ecmaFeatures: { jsx: true },
      },
    },
    rules: {
      // Pre-existing debt: keep visible but non-blocking for now.
      'react-hooks/exhaustive-deps': 'warn',
      // XSS guardrail: flag any new `dangerouslySetInnerHTML` (raw-HTML)
      // usage. The only legitimate site is the briefing render in
      // BriefingModal.tsx, which sanitises first and carries a scoped
      // eslint-disable. `eslint-plugin-react`'s `react/no-danger` is not
      // installed (and we won't add a plugin for one rule), so target the
      // JSX attribute directly. New raw-HTML must be sanitised and disabled
      // with a documented reason.
      'no-restricted-syntax': [
        'error',
        {
          selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']",
          message:
            'Avoid dangerouslySetInnerHTML (XSS risk). Sanitise the HTML first, then add a scoped eslint-disable explaining why it is safe.',
        },
      ],
    },
  },
  // jsx-a11y is relevant (modals, polling UI) but should not block CI on
  // the existing backlog — downgrade its whole rule set to `warn`.
  {
    files: ['src/**/*.{ts,tsx}'],
    rules: Object.fromEntries(
      // Downgrade only the rules recommended *enables* — preserve the ones it
      // ships as `off` (e.g. the deprecated `label-has-for`, superseded by
      // `label-has-associated-control`). Blindly forcing every key to `warn`
      // resurrected that dead rule and flagged valid nested `<label>`s.
      Object.entries(jsxA11y.flatConfigs.recommended.rules)
        .filter(([, level]) => level !== 'off' && level !== 0)
        .map(([rule]) => [rule, 'warn']),
    ),
  },
  // Our @estormi/ui-kit form primitives (TextInput / Select / Textarea) render
  // a real <input>/<select>/<textarea> under the hood, so a <label> wrapping
  // one IS properly associated — tell jsx-a11y to treat them as controls
  // instead of warning that the label has no control.
  {
    files: ['src/**/*.{ts,tsx}'],
    rules: {
      'jsx-a11y/label-has-associated-control': [
        'warn',
        { controlComponents: ['TextInput', 'Select', 'Textarea'] },
      ],
    },
  },
  // Test fixtures render throwaway markup (e.g. a bare clickable <div> to
  // assert click-propagation) that has no place being held to shipped-UI a11y
  // rules — turn the whole jsx-a11y set off for tests.
  {
    files: ['src/**/__tests__/**/*.{ts,tsx}', 'src/**/*.test.{ts,tsx}'],
    rules: Object.fromEntries(
      Object.keys(jsxA11y.flatConfigs.recommended.rules).map((rule) => [
        rule,
        'off',
      ]),
    ),
  },
)
