# 1. Local-first, loopback-only

- Status: Accepted

## Context

Estormi indexes a person's most private data: mail, messages, calendar, notes,
chats.

## Decision

Everything — SQLite, the Qdrant index, embeddings, the optional local LLM — runs
in-process on the user's Mac. The FastAPI server binds `127.0.0.1:8000` by
default. Remote/LAN access is opt-in and gated by a bearer token, enforced by
the `security_boundary` middleware.

## Consequences

Privacy is the product, not a feature: if the data never leaves the machine
there is no server to breach, no account to compromise, and no cloud trust to
extend. The system is single-user and zero-ops. The cost: no multi-device sync
server and no web access from elsewhere — the iOS companion is fed through an
iCloud Drive folder (see [0006](0006-icloud-vault-for-ios.md)) rather than a
live API.
