# 6. iCloud Drive vault for the iOS companion

- Status: Accepted

## Context

The native iOS companion needs the daily briefings and engine history, but the
phone should not talk to a server (see [0001](0001-local-first-loopback-only.md)).

## Decision

The Mac writes the daily briefings (`briefings/<date>.json`) and an
engine-history log as JSON into a user-picked iCloud Drive folder; the SwiftUI
app reads that folder. The phone never talks to the FastAPI server. (CloudKit is
used only for the optional push *doorbell*, never for the briefing data itself.)

## Consequences

No paid Apple Developer account is required for the data path, no CloudKit
container, no server exposed to the network, and no auth to manage — the phone is
a read-only viewer of files the Mac already produces, and sync is Apple's
problem. The trade-off: the companion is read-only and only as fresh as the last
iCloud sync; it cannot trigger ingestion or search live. The vault payload is a
cross-surface contract, so its schema is pinned by
`tests/contract/test_briefing_payload_schema.py`.
