# Vendored license-text overlay

`make bundle` collects each bundled Python wheel's license text from its
`*.dist-info` directory into `Estormi.app/Contents/Resources/THIRD-PARTY-LICENSES/`.
A few wheels ship **no** license file (only a `METADATA` declaration) — for those
the bundle copies `METADATA` as a fallback, but where the license itself
*requires the verbatim text accompany the binary* (notably LGPL/GPL), the text
must be supplied explicitly.

Each subdirectory here is named after a bundled distribution (e.g. `odfpy/`) and
its contents are overlaid onto that package's `THIRD-PARTY-LICENSES/<dist>-<ver>/`
directory at bundle time (matched by name prefix). Add a directory here whenever
a copyleft dependency's wheel omits its license text.

| Package | Why vendored | Text |
|---|---|---|
| `odfpy` | Wheel ships no `LICENSE`; the imported `odf` library is LGPL-2.1-or-later | `COPYING.LESSER` (verbatim GNU LGPL v2.1) |

The invariant — every copyleft dependency that omits its wheel text has an
overlay here — is pinned by `tests/contract/test_bundle_license_collection.py`.
