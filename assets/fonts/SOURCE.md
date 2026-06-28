# Vendored fonts

Estormi is an offline-first local app. Loading webfonts from a CDN on
every launch is both a privacy regression and a reliability one — the
wordmark renders in a fallback serif until the network comes back. To
fix that we vendor the five design-system families locally and serve
them ourselves under FastAPI's `/fonts/*` mount (see
`packages/estormi_server/server/static.py`).

## Pipeline

`scripts/vendor_fonts.py` is the (re)build tool. It is stdlib-only — no
new package dependency. Run it whenever the design system's weight
selection changes:

```
python3 scripts/vendor_fonts.py
```

The script

1. fetches `https://fonts.googleapis.com/css2?…` once per family,
2. extracts the `/* latin */` `@font-face` blocks (we ship Latin only),
3. downloads the referenced `.woff2` binaries,
4. **deduplicates** binaries when multiple weights share a URL — this
   is the common case for modern Google fonts which now ship as
   *variable* fonts (one binary, full weight axis), and
5. writes `fonts.css` next to the binaries with one `@font-face` block
   per declared weight, all pointing at the deduplicated filenames.

`scripts/vendor_fonts.py --check` is a network-free sanity check used
by tests and CI to assert that every required variant has a file on
disk.

## Families & weights kept

| Family             | Style  | Weights              | File                                  |
| ------------------ | ------ | -------------------- | ------------------------------------- |
| Cinzel             | normal | 400, 500, 600, 700, 800, 900 | `cinzel-variable.woff2`       |
| Cinzel Decorative  | normal | 400                  | `cinzel-decorative-400.woff2`         |
| Cinzel Decorative  | normal | 700                  | `cinzel-decorative-700.woff2`         |
| Cinzel Decorative  | normal | 900                  | `cinzel-decorative-900.woff2`         |
| Inter              | normal | 300, 400, 500, 600, 700      | `inter-variable.woff2`        |
| EB Garamond        | normal | 400, 500, 600        | `eb-garamond-variable.woff2`          |
| EB Garamond        | italic | 400, 500             | `eb-garamond-variable-italic.woff2`   |
| JetBrains Mono     | normal | 400, 500             | `jetbrains-mono-variable.woff2`       |

No weights were dropped. The total bundle is well under the ~600 KB
guardrail (see "Budget" below) because most of these families now ship
as variable fonts — a single binary covers every weight in its axis.

Only the **Latin subset** (`U+0000-00FF` plus a small punctuation set)
is shipped. Cyrillic, Greek, and Vietnamese subsets are intentionally
excluded — the app is English + French only and the extra subsets
would add hundreds of KB without value.

## Budget

After vendoring (May 2026):

```
cinzel-variable.woff2              25 KB
cinzel-decorative-400.woff2        14 KB
cinzel-decorative-700.woff2        15 KB
cinzel-decorative-900.woff2        14 KB
inter-variable.woff2               47 KB
eb-garamond-variable.woff2         40 KB
eb-garamond-variable-italic.woff2  41 KB
jetbrains-mono-variable.woff2      31 KB
-------------------------------------
Total                            ~228 KB
```

`fonts.css` adds another ~7 KB of text. Grand total ≈ 235 KB, comfortably
under the 600 KB budget.

## Source URLs

These are the URLs the script hit at vendoring time. Re-running the
script will re-resolve them via Google's CSS endpoint, which may rev
the version path (`/v26/…`) without changing the bytes — the user-agent
header in `vendor_fonts.py` pins the modern CSS variant so the URLs
stay woff2-only.

- `https://fonts.googleapis.com/css2?family=Cinzel:wght@400;500;600;700;800;900&display=swap`
- `https://fonts.googleapis.com/css2?family=Cinzel+Decorative:wght@400;700;900&display=swap`
- `https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap`
- `https://fonts.googleapis.com/css2?family=EB+Garamond:ital,wght@0,400;0,500;0,600;1,400;1,500&display=swap`
- `https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500&display=swap`

## Licences

All five families ship under the **SIL Open Font License v1.1**. The
combined copyright + licence text is in `OFL.txt` alongside the binaries.
Per the OFL we must include the copyright notice — that's exactly what
`OFL.txt` does, listed once per font family followed by the canonical
licence body.

Upstream copies (kept here for traceability — not fetched at runtime):

| Family            | Upstream OFL                                                                  |
| ----------------- | ----------------------------------------------------------------------------- |
| Cinzel            | `https://raw.githubusercontent.com/google/fonts/main/ofl/cinzel/OFL.txt`            |
| Cinzel Decorative | `https://raw.githubusercontent.com/google/fonts/main/ofl/cinzeldecorative/OFL.txt`  |
| Inter             | `https://raw.githubusercontent.com/google/fonts/main/ofl/inter/OFL.txt`             |
| EB Garamond       | `https://raw.githubusercontent.com/google/fonts/main/ofl/ebgaramond/OFL.txt`        |
| JetBrains Mono    | `https://raw.githubusercontent.com/google/fonts/main/ofl/jetbrainsmono/OFL.txt`     |
