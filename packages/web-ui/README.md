# @estormi/web-ui

Compact one-pager React + Vite SPA for Estormi. Served by FastAPI at `/app/`
and bundled into the macOS Tauri shell.

## Development

```bash
pnpm --filter @estormi/web-ui dev     # Vite dev server with HMR
pnpm --filter @estormi/web-ui build   # production build → dist/
pnpm --filter @estormi/web-ui test    # Vitest unit + interaction tests
```

## Demo mode

Run the SPA with fictitious French data and no backend:

```bash
VITE_DEMO_MODE=true pnpm --filter @estormi/web-ui dev
```

A banner "Mode demo — donnees fictives" appears at the top. All API calls
are intercepted client-side and return sample data from
`src/demo/sampleData.ts`. See `.env.example`.

## Design system

UI primitives and design tokens live in the sibling
[`@estormi/ui-kit`](../ui-kit/) package. See its README for the token
contract (colours, typography, form controls).
