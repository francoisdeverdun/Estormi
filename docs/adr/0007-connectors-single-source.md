# 7. Connectors are the single source of ingestion logic

- Status: Accepted

## Context

Source adapters (mail, notes, calendar, iMessage, WhatsApp, …) could be
duplicated across the surfaces that need them, which drifts.

## Decision

All source adapters live in `packages/connectors/` (one `ConnectorSpec` each,
registered uniquely in the `ConnectorRegistry`) and are shared across surfaces.
The per-source scripts they drive live under `packages/estormi_ingestion/`.

## Consequences

No per-app duplication of ingestion logic — a contract test enforces unique
specs, and the layering contracts (see
[0009](0009-layering-dag-import-linter.md)) forbid FastAPI in `memory_core` and
keep connector logic out of the apps. The trade-off: Estormi is macOS + iOS only
and connectors carry no runtime/host tag; re-adding another host would be a
deliberate change.
