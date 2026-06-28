"""WhatsApp service layer: SQL + business logic behind ``api/whatsapp_settings``.

The router under :mod:`estormi_server.api.whatsapp_settings` owns HTTP concerns
only (route decorators, rate limiting, request/response shaping). Everything
that touches SQLite, the loopback sidecar, macOS Contacts, or the local LLM
auto-tagger lives here so it is unit-testable without an ASGI client and the
router stays a thin parse -> call -> respond shell.

Sidecar passthroughs (``status`` / ``qr`` / ``reset``) stay in the router: they
are pure HTTP proxying with no business logic worth lifting out, and the reset
flow's filesystem/sidecar choreography is request-scoped.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass

import httpx
import structlog

from estormi_server.integrations.whatsapp_sidecar import SIDECAR_URL, sidecar_headers
from estormi_server.server.sources import WA_STAGING_PATH
from memory_core.labels import WA_AUTOTAG_CHOICES, WA_GROUP_TYPES, is_opaque_label

log = structlog.get_logger()


# Semantic life-context labels a chat may carry (the calendar vocabulary minus
# the self/partner labels). The PATCH endpoint validates against this set before
# persisting. Canonical source: memory_core.labels.
_WA_GROUP_TYPES = WA_GROUP_TYPES


def _format_phone_jid(chat_id: str) -> str:
    """Render a ``<digits>@s.whatsapp.net`` JID as an E.164 phone number.

    Falls back to the raw JID if the userpart is empty or not numeric — a
    safety net for ``@lid`` / ``@g.us`` JIDs that the caller forgets to gate.
    """
    user = chat_id.split("@", 1)[0]
    if not user.isdigit():
        return chat_id
    return "+" + user


def _is_masked_pushname(name: str) -> bool:
    """``True`` if ``name`` is WhatsApp's own ``+33∙∙∙∙∙∙∙11``-style mask.

    The library forwards this string verbatim in ``info.push_name`` for any
    contact the user hasn't saved on their phone. It looks like a phone
    number from a distance but conveys no signal, so we treat it as a "no
    name" marker and fall back to the full formatted JID.
    """
    return "∙" in name  # U+2219 BULLET OPERATOR


def _wa_display_name(
    chat_id: str,
    chat_name: str | None,
    contacts: dict[str, str] | None = None,
    statuses: dict[str, str] | None = None,
) -> str:
    """Best human-readable label for a WhatsApp chat.

    Order of preference:
      1. the Mac address-book name — what the user actually calls the person;
      2. the stored WhatsApp name (group subject or contact's push_name) —
         unless it's WhatsApp's own ``+33∙∙∙∙∙∙∙11`` mask, which carries no
         information and looks broken in the UI;
      3. the contact's ``About`` text via the sidecar's ``get_user_info``
         pass — last-resort label only. The About field is often a quote or
         "Available", not a name, so we use it strictly when nothing better
         exists. The user has accepted that it may be wrong but it's the
         only signal we have left.
      4. the formatted phone number for DM JIDs, e.g. ``+33612345678``;
      5. the raw JID, when nothing else is known.

    ``contacts`` is the cached macOS Contacts index (see ``macos_contacts``).
    It only helps phone-number DMs: ``@lid`` and ``@g.us`` JIDs carry no phone
    number, so they are never matched against the address book.
    """
    is_dm = chat_id.endswith("@s.whatsapp.net")

    if contacts and is_dm:
        from estormi_server.integrations import macos_contacts  # noqa: PLC0415

        resolved = macos_contacts.name_for_phone(chat_id, contacts)
        if resolved:
            return resolved

    n = (chat_name or "").strip()
    if n and n != chat_id and not _is_masked_pushname(n):
        return n

    if statuses:
        status = (statuses.get(chat_id) or "").strip()
        if status and not _is_masked_pushname(status):
            return status

    if is_dm:
        return _format_phone_jid(chat_id)
    return chat_id


async def enrich_chat_names_from_sidecar(db) -> dict[str, str]:
    """Query the sidecar for current chat names and back-fill any empty rows in whatsapp_chats.

    Returns a ``{chat_id: about_text}`` map for DMs the sidecar enriched with
    the contact's ``About`` text via ``get_user_info`` — the caller hands it
    to ``_wa_display_name`` as a last-resort label below every other source.
    An empty dict means no enrichment (sidecar unreachable or no DMs needed
    the fallback).

    Uses a single ``executemany`` so the upserts hit SQLite as one round
    trip instead of one statement per chat; the sidecar fetch is async too
    so a slow sidecar can't block the event loop.
    """
    from estormi_server.storage.tools import get_write_lock  # noqa: PLC0415
    from estormi_server.storage.writers import _chat_kind_from_jid  # noqa: PLC0415

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{SIDECAR_URL}/api/whatsapp/chats", headers=sidecar_headers())
            if r.status_code != 200:
                return {}
            sidecar_chats = r.json()
    except Exception:
        return {}  # best-effort: chat-name enrichment skipped if sidecar unreachable

    rows: list[tuple[str, str, str]] = []
    statuses: dict[str, str] = {}
    for chat in sidecar_chats:
        jid = (chat.get("id") or "").strip()
        name = (chat.get("name") or chat.get("subject") or chat.get("pushname") or "").strip()
        status = (chat.get("status") or "").strip()
        if jid and status:
            statuses[jid] = status
        if not jid or not name or name == jid or _is_masked_pushname(name):
            # WhatsApp's ``+33∙∙∙∙∙∙∙11`` mask carries no usable signal — the
            # display layer falls back to a formatted phone number instead, so
            # there's no point persisting the mask in SQLite where it'd also
            # bleed into the auto-tagger's LLM prompt.
            continue
        rows.append((jid, name, _chat_kind_from_jid(jid)))

    # Both the masked-name clear and the upsert share one locked execute→commit
    # span: the single shared connection serialises all writers, so a bare
    # commit here would otherwise flush a concurrent ingest's in-flight INSERT
    # (see tools._write_lock). The clear is committed even when there are no
    # rows to upsert, so it never lingers as a pending write.
    async with get_write_lock():
        # Retroactively clear any masked names left in the DB from prior runs so
        # the auto-tagger and the chat list don't keep hitting stale junk.
        await db.execute("UPDATE whatsapp_chats SET chat_name = '' WHERE chat_name LIKE '%∙%'")
        if rows:
            # Seed the structural chat_kind (dm/group/broadcast) from the JID. The
            # semantic group_type keeps its table default ('unknown') until the
            # user or the auto-tagger sets it — the two axes are stored separately.
            await db.executemany(
                """
                INSERT INTO whatsapp_chats (chat_id, chat_name, chat_kind)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    chat_name = excluded.chat_name,
                    chat_kind = COALESCE(excluded.chat_kind, chat_kind),
                    last_seen  = datetime('now')
                WHERE excluded.chat_name != ''
                """,
                rows,
            )
        await db.commit()
    return statuses


async def list_chats(db) -> list[dict]:
    """Resolve display names for every stored chat, persist resolved Contacts names, and return rows.

    Enriches from the sidecar first, then resolves each row's best label
    (macOS Contacts > stored name > About status > phone > raw JID). Only the
    macOS-Contacts name is persisted back — the About/status text feeds the
    display label but must not cement a status message as the contact's
    identity for downstream readers.
    """
    from estormi_server.integrations import macos_contacts  # noqa: PLC0415

    statuses = await enrich_chat_names_from_sidecar(db)
    rows = await db.execute_fetchall(
        "SELECT chat_id, chat_name, group_type, chat_kind FROM whatsapp_chats"
    )
    # The Mac address book is the best source of human names for phone-number
    # DMs. Resolved per request off an index that macos_contacts caches, so the
    # Settings UI poll does not re-scan Contacts every few seconds.
    contacts = await asyncio.to_thread(macos_contacts.phone_name_index)

    chats = []
    persist: list[tuple[str, str]] = []
    for chat_id, stored_name, group_type, chat_kind in rows:
        label = _wa_display_name(chat_id, stored_name, contacts, statuses)
        contact_name = (
            macos_contacts.name_for_phone(chat_id, contacts)
            if contacts and chat_id.endswith("@s.whatsapp.net")
            else None
        )
        if contact_name and contact_name != stored_name:
            persist.append((contact_name, chat_id))
        chats.append(
            {
                "chat_id": chat_id,
                "chat_name": label,
                "group_type": group_type,
                "chat_kind": chat_kind,
            }
        )

    if persist:
        from estormi_server.storage.tools import get_write_lock  # noqa: PLC0415

        try:
            async with get_write_lock():
                await db.executemany(
                    "UPDATE whatsapp_chats SET chat_name = ? WHERE chat_id = ?", persist
                )
                await db.commit()
        except Exception:
            log.exception("whatsapp_chats.persist_resolved_names_failed")

    # Sort on the resolved label, not the stored column it may have replaced.
    chats.sort(key=lambda c: c["chat_name"].casefold())
    return chats


async def _resolve_names_for(db, chat_ids: list[str]) -> dict[str, str]:
    """Resolve ``chat_id → best human name``, independent of whatsapp_chats rows.

    macOS Contacts for phone-number DMs; otherwise the stored chat_name /
    @lid push_name already in ``whatsapp_chats``. Unlike ``list_chats``
    this resolves ids that may not yet be rows, so a name is available even
    before the sidecar has listed the chat. Shared by ``resolve-names``
    (pre-ingest) and ``backfill-titles`` (post-ingest).
    """
    from estormi_server.integrations import macos_contacts  # noqa: PLC0415

    ids = [c for c in {(i or "").strip() for i in chat_ids} if c]
    if not ids:
        return {}
    contacts = await asyncio.to_thread(macos_contacts.phone_name_index)
    rows = await db.execute_fetchall("SELECT chat_id, chat_name FROM whatsapp_chats")
    stored = {cid: (nm or "") for cid, nm in rows}
    out: dict[str, str] = {}
    for cid in ids:
        if contacts and cid.endswith("@s.whatsapp.net"):
            nm = macos_contacts.name_for_phone(cid, contacts)
            if nm:
                out[cid] = nm
                continue
        nm = stored.get(cid, "")
        if nm and nm != cid and not _is_masked_pushname(nm) and not is_opaque_label(nm):
            out[cid] = nm
    return out


async def resolve_and_persist_names(db, chat_ids: list[str]) -> dict[str, str]:
    """Resolve names for ``chat_ids`` and upsert them into ``whatsapp_chats``.

    Returns the resolved ``{chat_id: name}`` map. Persisting keeps every later
    reader (the chat list, the briefing) consistent even for a brand-new DM
    whose row the sidecar enrichment hasn't created yet.
    """
    from estormi_server.storage.tools import get_write_lock  # noqa: PLC0415
    from estormi_server.storage.writers import _chat_kind_from_jid  # noqa: PLC0415

    names = await _resolve_names_for(db, chat_ids)
    if names:
        rows = [(cid, nm, _chat_kind_from_jid(cid)) for cid, nm in names.items()]
        try:
            async with get_write_lock():
                await db.executemany(
                    "INSERT INTO whatsapp_chats (chat_id, chat_name, chat_kind) "
                    "VALUES (?, ?, ?) ON CONFLICT(chat_id) DO UPDATE SET "
                    "chat_name = excluded.chat_name, "
                    "chat_kind = COALESCE(excluded.chat_kind, chat_kind), "
                    "last_seen = datetime('now') WHERE excluded.chat_name != ''",
                    rows,
                )
                await db.commit()
        except Exception:
            log.exception("whatsapp_resolve_names.persist_failed")
    return names


async def backfill_titles(db) -> int:
    """Re-title WhatsApp chunks left with a raw-JID title by an ingest-time race.

    A chunk's title is baked in when its window is first ingested; if the chat's
    name resolved only later (the metadata lagged behind ingestion), the chunk
    keeps a ``WhatsApp — <raw JID>`` title forever and the briefing renders it
    as "a contact". This finds those chunks and rewrites the title once a real
    name is available. Idempotent: a chunk already carrying a real name is not
    matched, so steady-state runs update nothing. Returns the number updated.
    """
    rows = await db.execute_fetchall(
        "SELECT id, chat_id_raw, title FROM chunks "
        "WHERE source = 'whatsapp' AND chat_id_raw IS NOT NULL AND ("
        "  title LIKE '%@s.whatsapp.net' OR title LIKE '%@lid' OR title LIKE '%@g.us'"
        "  OR title GLOB 'WhatsApp — [+0-9]*')"
    )
    todo: list[tuple[str, str]] = []
    chat_ids: set[str] = set()
    for chunk_id, chat_id_raw, title in rows:
        label = (title or "").removeprefix("WhatsApp — ").strip()
        if chat_id_raw and is_opaque_label(label):
            todo.append((chunk_id, chat_id_raw))
            chat_ids.add(chat_id_raw)
    if not todo:
        return 0

    names = await _resolve_names_for(db, list(chat_ids))
    updates = [
        (f"WhatsApp — {names[craw]}", chunk_id) for chunk_id, craw in todo if names.get(craw)
    ]
    if updates:
        from estormi_server.storage.tools import get_write_lock  # noqa: PLC0415

        async with get_write_lock():
            await db.executemany("UPDATE chunks SET title = ? WHERE id = ?", updates)
            await db.commit()
    return len(updates)


async def set_chat_group_type(db, chat_id: str, group_type: str) -> int:
    """Persist a chat's semantic group_type and re-tag its already-ingested chunks.

    Returns the number of chunks re-tagged. Caller validates ``group_type``
    against ``_WA_GROUP_TYPES`` before calling (it raises 422 from the router).
    """
    # Deferred: chunk_admin reaches tools, which pulls in Qdrant/embedder — kept lazy.
    from estormi_server.storage.chunk_admin import retag_chunks  # noqa: PLC0415
    from estormi_server.storage.tools import get_write_lock  # noqa: PLC0415

    # The bare UPDATE→commit span takes the write lock so a concurrent ingest's
    # in-flight INSERT isn't flushed by our commit. retag_chunks acquires the
    # lock itself (non-reentrant), so it stays OUTSIDE the block.
    async with get_write_lock():
        await db.execute(
            "UPDATE whatsapp_chats SET group_type = ? WHERE chat_id = ?",
            (group_type, chat_id),
        )
        await db.commit()
    # Re-tag chunks already ingested from this chat so the change is not
    # stuck behind the next ingestion run.
    retag = await retag_chunks("whatsapp", chat_id, group_type)
    return retag["retagged"]


async def wipe_whatsapp_log(db) -> int:
    """Wipe the durable WhatsApp message log + everything derived from it.

    Drops the derived chunks/vectors, the raw ``whatsapp_messages`` log, its
    watermarks, the per-source run-history rows, and the staging hop. Returns
    the number of chunks deleted. Shared by the per-source "Reset log" action
    (``/api/sources/whatsapp/log/reset``) and the Disconnect flow, so the
    two stay in lock-step. The chat list (``whatsapp_chats``) is handled by the
    caller — Disconnect clears it; "Reset log" intentionally keeps it.
    """
    import shutil  # noqa: PLC0415

    from estormi_server.storage.tools import get_write_lock  # noqa: PLC0415
    from estormi_server.storage.writers import delete_by_source  # noqa: PLC0415

    # delete_by_source acquires the write lock itself, so it stays outside the
    # block below; the bare DELETE→commit span must take the lock so a
    # concurrent ingest's in-flight INSERT isn't flushed by our commit.
    result = await delete_by_source("whatsapp")
    async with get_write_lock():
        await db.execute(
            "DELETE FROM ingestion_watermarks WHERE source IN ('whatsapp', 'whatsapp_log')"
        )
        await db.execute("DELETE FROM whatsapp_messages")
        await db.execute("DELETE FROM dag_stages WHERE stage_name = 'whatsapp'")
        await db.commit()
    if WA_STAGING_PATH.exists():
        shutil.rmtree(WA_STAGING_PATH, ignore_errors=True)
    return int(result.get("deleted", 0))


# ── Auto-tag (LLM-classified group_type) ─────────────────────────────────────
#
# A user with hundreds of chats cannot reasonably hand-tag every row. The
# auto-tagger samples a handful of message chunks per chat and asks the local
# LLM to pick one of the allowed `_WA_GROUP_TYPES` labels. It always
# commits a guess — the user can still correct any row from the modal.
#
# Tags that the LLM is allowed to choose. "unknown" is intentionally excluded
# (the user asked the model to always commit) and "couple" is excluded because
# guessing romantic context from a chat snippet is fragile. ORDERED — rendered
# verbatim into the auto-tag prompt; canonical source: memory_core.labels.
_WA_AUTOTAG_CHOICES: tuple[str, ...] = WA_AUTOTAG_CHOICES
_WA_AUTOTAG_SAMPLE_CHUNKS = 8
_WA_AUTOTAG_TEXT_TRIM = 800

_AUTOTAG_PROMPT = """You are tagging a WhatsApp conversation with the life context it belongs to.

Pick the SINGLE best label for this chat from:
- work — professional colleagues, clients, job-related coordination
- family — parents, siblings, children, in-laws, extended family
- friends — personal friends, social outings, hobbies, casual chat
- organisation — clubs, associations, parent-teacher, building syndic, alumni, religious or civic group
- charity — non-profits, fundraising, volunteering, NGO coordination
- sport — sport teams, training partners, fitness groups, race coordination
- noise — automated alerts, marketing broadcasts, OTP/2FA, delivery notifications, spam

Rules:
- Output ONE word from the list — nothing else. No prose, no punctuation.
- If the chat is clearly mixed, pick the dominant context.
- If the chat is automated/transactional or otherwise low-signal, choose "noise".

Chat name: {name}

Sample messages from this chat:
---
{sample}
---

Label:"""


# Module-level state for the background auto-tag job. There is only ever one
# at a time — a second start request joins the running job instead of spawning
# a competing one (the local LLM is single-threaded; parallel calls would
# serialise behind the same model and not finish any faster).
@dataclass
class _AutotagState:
    running: bool = False
    started_at: float = 0.0
    finished_at: float = 0.0
    total: int = 0
    done: int = 0
    tagged: int = 0
    skipped_no_text: int = 0
    errors: int = 0
    last_chat: str = ""
    last_label: str = ""
    error_message: str = ""

    def reset_counters(self) -> None:
        """Zero the per-run progress fields, leaving the run-lifecycle flags."""
        self.total = 0
        self.done = 0
        self.tagged = 0
        self.skipped_no_text = 0
        self.errors = 0


_autotag_state = _AutotagState()
_autotag_lock = asyncio.Lock()


async def _sample_chat_text(db, chat_id: str) -> str:
    """Return a short, representative slice of the chat's already-ingested text.

    Sampling is from SQLite ``chunks`` (cheap) joined to Qdrant payloads (where
    the text lives). Pulls the most recent ``_WA_AUTOTAG_SAMPLE_CHUNKS`` chunks
    because the *most recent* messages are the best signal for what the chat
    is currently about — old chats sometimes drift across contexts.
    """
    from estormi_server.services.chunks import retrieve_chunk_texts  # noqa: PLC0415

    cursor = await db.execute(
        """
        SELECT id FROM chunks
        WHERE source = 'whatsapp' AND chat_id_raw = ?
        ORDER BY date_ts DESC
        LIMIT ?
        """,
        (chat_id, _WA_AUTOTAG_SAMPLE_CHUNKS),
    )
    ids = [row[0] for row in await cursor.fetchall()]
    await cursor.close()
    if not ids:
        return ""
    texts_by_id = await retrieve_chunk_texts(ids)
    parts: list[str] = []
    for cid in ids:
        text = texts_by_id.get(cid)
        if not text:
            continue
        parts.append(text[:_WA_AUTOTAG_TEXT_TRIM])
    return "\n\n---\n\n".join(parts)


async def _classify_chat(name: str, sample: str) -> str | None:
    """Call the local LLM and return one ``_WA_AUTOTAG_CHOICES`` value.

    Returns ``None`` when the model output cannot be matched to a known label
    — the caller then leaves the chat's tag untouched rather than committing
    a guess we cannot verify.
    """
    from memory_core.llm_local import chat_completion  # noqa: PLC0415

    prompt = _AUTOTAG_PROMPT.format(name=name or "(no name)", sample=sample[:4000])
    messages = [
        {
            "role": "system",
            "content": "Reply with exactly one of: "
            + ", ".join(_WA_AUTOTAG_CHOICES)
            + ". No other words.",
        },
        {"role": "user", "content": prompt},
    ]
    try:
        raw = await chat_completion(messages, max_tokens=8, temperature=0.0, timeout=60.0)
    except Exception as e:
        log.warning("whatsapp_autotag.llm_failed", error=str(e))
        return None
    token = (raw or "").strip().strip(".").strip('"').strip("'").lower()
    # Model sometimes wraps in extra prose — keep the first alphabetic run.
    head = "".join(ch for ch in token if ch.isalpha() or ch == "-")
    if head in _WA_AUTOTAG_CHOICES:
        return head
    # A common LLM quirk: it answers "organization" (en-US) when the prompt
    # offered "organisation" — accept the alias rather than dropping the call.
    if head == "organization":
        return "organisation"
    return None


async def run_autotag(only_unknown: bool) -> None:
    """Background worker: classify every eligible chat and persist the guess.

    Auto-tag intentionally runs in-process, OUTSIDE the engine queue/mutex in
    ``server.jobs`` — it is a lightweight side-task, not an engine. Its primary
    trigger is the WhatsApp ingestion stage itself (``ingest_conversations``
    fires it fire-and-forget at the end of its run), which executes *inside* the
    nightly ingestion engine — so it must NOT gate on engine state, or the
    nightly pass would skip forever. It only ever uses the local chat LLM
    concurrently with the lightweight fastembed ingestion path, never with the
    briefing engine (which has no auto-tag trigger), so co-residence is bounded.
    """
    from estormi_server.storage.chunk_admin import retag_chunks  # noqa: PLC0415
    from estormi_server.storage.tools import get_write_lock, sqlite_conn  # noqa: PLC0415

    state = _autotag_state

    try:
        db = sqlite_conn()
        # Make sure newly-paired chats are visible to the run.
        await enrich_chat_names_from_sidecar(db)
        where = "WHERE group_type IN ('unknown', '')" if only_unknown else ""
        cursor = await db.execute(
            f"SELECT chat_id, chat_name FROM whatsapp_chats {where} ORDER BY chat_id"
        )
        rows = list(await cursor.fetchall())
        await cursor.close()

        state.reset_counters()
        state.total = len(rows)

        for chat_id, chat_name in rows:
            try:
                sample = await _sample_chat_text(db, chat_id)
                if not sample:
                    state.skipped_no_text += 1
                    continue
                label = await _classify_chat(chat_name or chat_id, sample)
                if label is None:
                    state.errors += 1
                    continue
                async with get_write_lock():
                    await db.execute(
                        "UPDATE whatsapp_chats SET group_type = ? WHERE chat_id = ?",
                        (label, chat_id),
                    )
                    await db.commit()
                await retag_chunks("whatsapp", chat_id, label)
                state.tagged += 1
                state.last_chat = chat_name or chat_id
                state.last_label = label
            except Exception as e:
                log.warning(
                    "whatsapp_autotag.chat_failed", chat_id=chat_id, error=str(e), exc_info=True
                )
                state.errors += 1
            finally:
                state.done += 1
    except Exception as e:
        log.error("whatsapp_autotag.run_failed", error=str(e), exc_info=True)
        state.error_message = str(e)
    finally:
        state.running = False
        state.finished_at = time.time()


def autotag_status_payload() -> dict:
    return asdict(_autotag_state)


def begin_autotag_run() -> None:
    """Mark the auto-tag state as freshly started (caller holds ``_autotag_lock``)."""
    state = _autotag_state
    state.reset_counters()
    state.running = True
    state.started_at = time.time()
    state.finished_at = 0.0
    state.last_chat = ""
    state.last_label = ""
    state.error_message = ""
