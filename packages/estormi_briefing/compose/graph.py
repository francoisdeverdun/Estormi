"""Deterministic correlation graph for the briefing — the *reduce* step.

The briefing's headline value is correlation: connecting events that touch the
same real-world thing across sources into one thread, instead of listing them
apart (see the user's "briefing correlation priority"). The old pipeline left
that judgement to the LLM — it was handed loose, semantically-matched candidate
rows and a prompt rule begging it not to fuse unrelated facts (the
``cross_source_fusion`` failure mode). Prompt discipline is the wrong tool for a
correctness invariant: it is untestable and only probabilistically obeyed.

This module pulls correlation *out of the prompt and into code*. It models the
day as a graph:

  * nodes  = **facts** — one normalised unit per calendar event, reminder,
             WhatsApp tail, mail/note/document chunk, with its provenance kept.
  * edges  = a shared **anchor** — the same known person (or the same explicit
             place) referenced by both facts within a short date window.
  * threads = the connected components of that graph. The heaviest component
             (most distinct sources, then most facts) is *the thread of the
             day*.

The anchor rule is the same one the prompt hardened in
``knowledge_day_vision.j2`` — "same person AND (same date / nearby) [AND place]"
— but enforced structurally: two facts that share no anchor have *no edge*, so
the rewriter is handed pre-formed clusters it physically cannot fuse across. A
coincidental shared word (a month, a common noun) never forms an edge because
people are drawn from a **curated lexicon** (real WhatsApp contacts + the
partner + the profile), not from arbitrary tokens.

Pure and side-effect-free: no I/O, no LLM, no DB. Everything here is a
deterministic function of its inputs, so the correlation logic is unit-testable
in a way a prompt never was. The orchestrator (``run_briefing``) adapts the
already-fetched data into :func:`collect_facts`, calls :func:`build_threads`,
and renders the result into the vision prompt via :func:`render_threads`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from estormi_briefing.day.day import LOCAL_TZ, _parse_iso_datetime

__all__ = [
    "Fact",
    "Thread",
    "build_lexicon",
    "build_topic_terms",
    "collect_facts",
    "build_threads",
    "render_threads",
]

# Month names (FR + EN, accented and bare) that must NEVER form a topic anchor.
# This is the load-bearing guard against the original cross-source fusion bug:
# a note about "trip in August" and an unrelated "deadline in August" share
# the word "August" — and that coincidence must never become a link. Months are
# excluded outright so a shared date-word can't bind two facts; real temporal
# proximity is handled separately by the date window.
_MONTHS = frozenset(
    {
        "janvier",
        "février",
        "fevrier",
        "mars",
        "avril",
        "mai",
        "juin",
        "juillet",
        "août",
        "aout",
        "septembre",
        "octobre",
        "novembre",
        "décembre",
        "decembre",
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    }
)

# Generic connectors and meeting/work filler (FR + EN, length ≥ 4) that carry no
# subject of their own — anchoring on them would link unrelated commitments
# ("réunion" ↔ "réunion"). Short words (les/des/une/the/and…) are already dropped
# by the length floor, so this only needs the long generic words.
_STOPWORDS = frozenset(
    {
        "avec",
        "pour",
        "dans",
        "sans",
        "sous",
        "votre",
        "notre",
        "leur",
        "cette",
        "vous",
        "nous",
        "elle",
        "être",
        "etre",
        "fait",
        "faire",
        "plus",
        "tout",
        "tous",
        "toute",
        "mais",
        "donc",
        "alors",
        "chez",
        "elles",
        "leurs",
        "demain",
        "aujourd",
        "hier",
        "matin",
        "soir",
        "jour",
        "semaine",
        "réunion",
        "reunion",
        "point",
        "daily",
        "slot",
        "reserved",
        "work",
        "team",
        "with",
        "from",
        "this",
        "that",
        "your",
        "will",
        "have",
        "been",
        "today",
        "week",
        "call",
        "sync",
        "meeting",
    }
)

# Minimum length for a topic term. Below this, tokens are too generic ("v2",
# "aws" survive only as proper salient acronyms — see the floor of 3 for the
# token regex match below) to anchor reliably.
_MIN_TOPIC_LEN = 4

# Sources whose facts may carry TOPIC anchors. Topic matching is reliable only
# for the user's actual commitments and conversations — a calendar title, a
# reminder, a WhatsApp tail are about one thing. Mail and notes are the opposite:
# a newsletter inbox mentions every keyword under the sun ("Data Engineer",
# a contact name, "taxes" promos), so a shared topic word there is almost always
# coincidence, not correlation — exactly the fusion the briefing must avoid. Mail
# and notes still JOIN a thread, but only through a reliable person/place anchor,
# never a bare topic word.
_TOPIC_SOURCES = frozenset({"calendar", "gcal", "reminder", "reminders", "whatsapp"})


# ── Normalisation helpers ───────────────────────────────────────────────────────

# A "word" for name matching: unicode letters only, so "Sam" matches inside
# "lunch with Sam tomorrow" but not inside a longer word that merely contains
# those letters. Matching is done on a token set, not substrings, precisely so a
# name can never coincidentally fire on a longer word that merely contains it.
_TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def _norm(s: str) -> str:
    """Lowercase + collapse whitespace. The canonical form for anchor keys."""
    return " ".join((s or "").lower().split())


def _tokens(text: str) -> set[str]:
    """Lowercased letter-token set of ``text`` — the unit name matching works on."""
    return {t.lower() for t in _TOKEN_RE.findall(text or "")}


def _split_names(label: str) -> list[str]:
    """Split a conversation label into individual participant names.

    WhatsApp labels arrive as "Sam", "Sam, Marc" or "Alice & Bob" — one fact's
    label can name several people. Group/JID noise is filtered upstream by
    ``_conversation_label``; here we only break a multi-name label apart.
    """
    parts = re.split(r"\s*(?:,|&|\+|/| et | and )\s*", label or "")
    return [p.strip() for p in parts if p.strip()]


# A bare calendar date: exactly ``YYYY-MM-DD`` with no time component. Such a
# value localises to itself and must not be shifted by any UTC offset.
_BARE_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _local_date(value: str) -> str:
    """Best-effort *local* ISO date (``YYYY-MM-DD``) from a stored timestamp.

    Facts carry heterogeneous date strings (an ISO datetime ``date_ts``, a bare
    date, or nothing). We only need day granularity for the window check, but we
    must take the date in the *local* timezone: a UTC-stored
    ``2026-06-09T23:00:00+00:00`` is already 2026-06-10 in +02:00, so slicing the
    first 10 chars off the raw string would land the fact on the wrong day and
    flip the ±window edge test near local midnight. Parse + ``astimezone`` first,
    consistent with the rest of the pipeline (``day``). A bare date with
    no time component carries no offset and localises to itself. Falls back to
    "" when unparseable.
    """
    raw = (value or "").strip()
    if not raw:
        return ""
    # A bare ``YYYY-MM-DD`` is already a *local* calendar date: it carries no
    # time-of-day and no offset, so it must be returned as-is. Routing it
    # through the parser would stamp it as UTC midnight, and ``astimezone`` to a
    # zone west of UTC would then roll it back to the previous day. Only
    # timestamps that carry a time component need the parse + ``astimezone``.
    if _BARE_DATE_RE.fullmatch(raw):
        try:
            return date.fromisoformat(raw).isoformat()
        except ValueError:
            return ""
    dt = _parse_iso_datetime(raw)
    if dt is None:
        return ""
    return dt.astimezone(LOCAL_TZ).date().isoformat()


def _date_distance(a: str, b: str) -> int | None:
    """Whole-day distance between two ISO dates, or ``None`` if either is empty."""
    if not a or not b:
        return None
    try:
        da = date.fromisoformat(a)
        db = date.fromisoformat(b)
    except ValueError:
        return None
    return abs((da - db).days)


# ── Fact + Thread models ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Fact:
    """One normalised unit of the day, with its provenance kept for citations.

    ``people`` and ``place`` are the *anchors* the graph links on. Provenance
    (``source`` + ``fact_id``) is what lets a downstream verifier check that a
    narrated link corresponds to a real cluster rather than an invented one.
    """

    fact_id: str
    source: str  # calendar | reminder | whatsapp | mail | notes | documents | ...
    title: str
    text: str
    iso_date: str  # local YYYY-MM-DD, or "" when undated
    when_label: str  # human label already computed upstream ("Mon 2 Jun"), for display
    people: frozenset[str] = frozenset()
    place: str = ""
    topics: frozenset[str] = frozenset()  # salient subject terms (e.g. "taxes")

    def anchors(self) -> set[str]:
        """The non-date anchor keys this fact exposes for edge formation.

        Three anchor kinds, all curated so a coincidental shared word can never
        form one: a known **person**, an explicit **place**, or a salient
        **topic** term harvested from the day's real commitments (calendar /
        reminder titles + the structured extractor) — see :func:`build_topic_terms`.
        """
        keys = {f"person:{p}" for p in self.people}
        keys |= {f"topic:{t}" for t in self.topics}
        if self.place:
            keys.add(f"place:{self.place}")
        return keys


@dataclass
class Thread:
    """A connected component of the anchor graph — one correlated thread."""

    facts: list[Fact] = field(default_factory=list)
    anchors: set[str] = field(default_factory=set)

    @property
    def sources(self) -> set[str]:
        return {f.source for f in self.facts}

    @property
    def is_cross_source(self) -> bool:
        """A thread is only *correlation* if it joins ≥2 distinct sources."""
        return len(self.sources) >= 2

    def score(self) -> tuple[int, int, str]:
        """Rank key — distinct sources first, then size, then recency.

        Cross-source breadth is the headline value, so it dominates; ties break
        toward more facts, then the most recent date. Returned as a tuple so
        ``sorted(..., reverse=True)`` orders threads strongest-first.
        """
        latest = max((f.iso_date for f in self.facts if f.iso_date), default="")
        return (len(self.sources), len(self.facts), latest)


# ── Lexicon ─────────────────────────────────────────────────────────────────────


# Sentinel conversation labels that look like a name (letters, no digits) but
# name no one. ``estormi_briefing.compose.prompts._conversation_label`` returns
# "unknown conversation" for every WhatsApp chat it cannot resolve to a real
# contact. Left unguarded it would pass ``_is_real_name``, enter the curated
# lexicon, and — seeded on every unresolved chat's fact — act as one shared
# person-anchor that fuses all those unrelated chats into a single bogus thread.
_PLACEHOLDER_LABELS = frozenset({"unknown conversation"})


def _is_real_name(name: str) -> bool:
    """True when ``name`` looks like a person, not a phone number or JID noise.

    A real name is alphabetic: it has at least one letter and NO digits (and so
    no masked-number labels like ``+33∙∙∙∙∙15`` an unresolved WhatsApp contact
    leaves behind — those must never become an anchor, let alone surface in a
    thread's label). The ``unknown conversation`` placeholder is alphabetic but
    names no one, so it is rejected too — otherwise it becomes a shared anchor
    that fuses every unresolved chat into one thread.
    """
    if _norm(name) in _PLACEHOLDER_LABELS:
        return False
    return len(name) >= 2 and any(c.isalpha() for c in name) and not any(c.isdigit() for c in name)


def build_lexicon(
    wa_labels: list[str],
    partner_name: str = "",
    extra: list[str] | None = None,
    exclude: set[str] | None = None,
) -> set[str]:
    """Build the curated set of known person-names used for anchor matching.

    The lexicon is deliberately *closed*: only people the user actually knows —
    WhatsApp contacts, their partner, and any names passed explicitly — can ever
    form a person-anchor. This is what makes a shared name meaningful instead of
    coincidental: a calendar event and a message link on "Sam" only because
    Sam is a real contact, never because two notes happen to share a stray
    capitalised word.

    ``exclude`` (typically the user's own display names) is dropped: the user
    appears in nearly every fact, so anchoring on their name would fuse
    unrelated commitments (a work event "with Alex" and a newsletter
    addressed to Alex). Non-name noise — masked phone numbers, JIDs — is
    filtered by :func:`_is_real_name`.
    """
    drop = {_norm(x) for x in (exclude or set())}
    names: set[str] = set()
    for label in wa_labels:
        names.update(_split_names(label))
    if partner_name:
        names.update(_split_names(partner_name))
    for name in extra or []:
        names.update(_split_names(name))
    return {n for n in (_norm(x) for x in names) if _is_real_name(n) and n not in drop}


def _match_people(text: str, lexicon: list[tuple[str, frozenset[str]]]) -> frozenset[str]:
    """Return the lexicon names whose tokens all appear in ``text``.

    A multi-word name ("jean dupont") matches only when every one of its tokens
    is present, so a shared first name alone won't bind two unrelated people who
    happen to share it with someone in the lexicon. ``lexicon`` is pre-split
    into ``(name, token-set)`` pairs by the caller so the split runs once per
    name, not once per fact.
    """
    toks = _tokens(text)
    if not toks:
        return frozenset()
    return frozenset(name for name, name_toks in lexicon if toks >= name_toks)


def build_topic_terms(
    titles: list[str], extra_texts: list[str] | None = None, exclude: set[str] | None = None
) -> set[str]:
    """Build the curated set of salient *subject* terms anchors may link on.

    The recall fix: calendar titles name the *topic* ("Tax return"), not
    the contact, and many real correlations are project/place threads, not person
    threads — so person-only anchors miss them. Topic terms are drawn ONLY from
    inherently-meaningful sources — the day's calendar/reminder titles and the
    structured extractor's commitments — never from arbitrary note prose. That
    provenance is what preserves precision: a long note's stray word can't anchor
    unless it is also a real commitment term. Months, generic filler, short
    tokens, and any known person name (``exclude``) are dropped.
    """
    drop = {_norm(x) for x in (exclude or set())}
    terms: set[str] = set()
    for blob in list(titles) + list(extra_texts or []):
        for tok in _TOKEN_RE.findall(blob or ""):
            low = tok.lower()
            if (
                len(low) >= _MIN_TOPIC_LEN
                and low not in _MONTHS
                and low not in _STOPWORDS
                and low not in drop
            ):
                terms.add(low)
    return terms


def _match_topics(text: str, topic_terms: set[str]) -> frozenset[str]:
    """Return the salient terms from ``topic_terms`` present in ``text``."""
    if not topic_terms:
        return frozenset()
    toks = _tokens(text)
    return frozenset(t for t in topic_terms if t in toks)


# ── Fact collection ─────────────────────────────────────────────────────────────


def collect_facts(
    *,
    day: str,
    calendar: list[dict],
    reminders: list[dict],
    wa_items: list[dict],
    context_rows: list[dict],
    lexicon: set[str],
    topic_terms: set[str] | None = None,
) -> list[Fact]:
    """Adapt the already-fetched briefing data into a flat list of :class:`Fact`.

    Inputs are the same dicts the vision prompt already renders — this never
    fetches anything, it only reshapes. Person-anchors are matched against
    ``lexicon`` for every fact; topic-anchors against ``topic_terms`` (see
    :func:`build_topic_terms`); a WhatsApp item additionally seeds its own
    conversation participants (its label *is* identity, no matching needed).
    Calendar/reminder facts are dated to ``day`` (they are today's actions);
    context/WhatsApp facts carry their own stored date.
    """
    terms = topic_terms or set()
    # Pre-split each lexicon name into its token set once, rather than per fact.
    lexicon_split = [(name, frozenset(name.split())) for name in lexicon]
    facts: list[Fact] = []
    n = 0

    def _add(
        source: str,
        title: str,
        text: str,
        iso_date: str,
        when_label: str,
        people: frozenset[str],
        place: str = "",
    ) -> None:
        nonlocal n
        blob = f"{title} {text}"
        matched = people | _match_people(blob, lexicon_split)
        topics = _match_topics(blob, terms) if source in _TOPIC_SOURCES else frozenset()
        facts.append(
            Fact(
                fact_id=f"{source[:2]}{n}",
                source=source,
                title=(title or "").strip(),
                text=" ".join((text or "").split())[:400],
                iso_date=iso_date,
                when_label=when_label,
                people=matched,
                place=_norm(place),
                topics=topics,
            )
        )
        n += 1

    for ev in calendar:
        _add(
            "calendar",
            ev.get("title") or "(untitled)",
            "",
            _local_date(ev.get("date_ts") or "") or day,
            ev.get("when") or "All day",
            frozenset(),
            ev.get("location") or "",
        )
    for r in reminders:
        _add(
            "reminder",
            r.get("title") or "(untitled)",
            "",
            _local_date(r.get("date_ts") or "") or day,
            r.get("when") or "",
            frozenset(),
        )
    for wa in wa_items:
        label = wa.get("label") or ""
        _add(
            "whatsapp",
            label,
            wa.get("text") or "",
            _local_date(wa.get("date") or ""),
            wa.get("when_label") or "",
            frozenset(p for p in (_norm(x) for x in _split_names(label)) if _is_real_name(p)),
        )
    for c in context_rows:
        _add(
            c.get("source") or "context",
            c.get("title") or "",
            c.get("text") or "",
            _local_date(c.get("date") or c.get("date_ts") or ""),
            c.get("when_label") or "",
            frozenset(),
            c.get("place") or "",
        )
    return facts


# ── Graph construction ──────────────────────────────────────────────────────────


def _linked(a: Fact, b: Fact, window_days: int) -> bool:
    """True when two facts share an anchor close enough in time to be one thread.

    The edge rule, conservative by design:
      * they must share at least one anchor (a known person, or an explicit
        place) — a shared *date alone* is never a link (that is exactly the
        "coincidental August" fusion the briefing must avoid);
      * if both are dated, those dates must lie within ``window_days`` of each
        other — a message about today's dinner links to the dinner, but a
        same-person exchange three weeks apart does not;
      * an undated fact (a note with no timestamp) may still link on the shared
        anchor alone — we don't have a date to reject it on.
    """
    if not (a.anchors() & b.anchors()):
        return False
    dist = _date_distance(a.iso_date, b.iso_date)
    return dist is None or dist <= window_days


def build_threads(facts: list[Fact], *, window_days: int = 3) -> list[Thread]:
    """Cluster facts into threads via union-find over the anchor edges.

    Returns the connected components as :class:`Thread`s, strongest-first.
    Singleton facts (nothing shares their anchor) are dropped: a thread is by
    definition a *correlation*, and a lone fact is just an item the rewriter
    already sees in its own CALENDAR/CONTEXT block.
    """
    parent = list(range(len(facts)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    # O(n²) pairwise edge test. ``facts`` is one day's worth (calendar +
    # reminders + chats + context chunks ≈ a few dozen), so the quadratic is a
    # non-issue in practice and buys the simplest correct clustering. If the
    # window ever widens enough to push n into the hundreds, bucket facts by
    # date first to prune obviously-distant pairs before calling ``_linked``.
    for i in range(len(facts)):
        for j in range(i + 1, len(facts)):
            if _linked(facts[i], facts[j], window_days):
                union(i, j)

    groups: dict[int, Thread] = {}
    for idx, fact in enumerate(facts):
        root = find(idx)
        thread = groups.setdefault(root, Thread())
        thread.facts.append(fact)
        thread.anchors |= fact.anchors()

    threads = [t for t in groups.values() if len(t.facts) >= 2]
    threads.sort(key=lambda t: t.score(), reverse=True)
    return threads


# ── Prompt rendering ────────────────────────────────────────────────────────────


def _anchor_label(anchors: set[str]) -> str:
    """Human phrase for the shared anchor(s) — e.g. "Sam" or "taxes · Paris"."""
    people = sorted(a.split(":", 1)[1] for a in anchors if a.startswith("person:"))
    places = sorted(a.split(":", 1)[1] for a in anchors if a.startswith("place:"))
    topics = sorted(a.split(":", 1)[1] for a in anchors if a.startswith("topic:"))
    parts = [p.title() for p in people] + [p.title() for p in places] + topics
    if not parts:
        # No recognised prefix (malformed/unknown anchor kind). Fall back to the
        # raw anchor values rather than an empty label, which would render the
        # thread's link invisible to the rewriter.
        parts = sorted(a.split(":", 1)[-1] for a in anchors)
    return " · ".join(parts[:4])


def render_threads(
    threads: list[Thread],
    *,
    cross_source_only: bool = True,
    limit: int = 6,
    max_rows: int = 6,
) -> list[dict]:
    """Render threads into prompt-ready dicts for the THREADS block.

    By default only cross-source threads are surfaced — a single-source cluster
    (two chunks of the same note) is not the correlation the block is meant to
    showcase, and surfacing it invites the very same-origin fusion
    ``knowledge_day_vision.j2`` warns against. The first row is flagged
    ``dominant`` so the prompt can lead the briefing with the thread of the day.
    """
    rows: list[dict] = []
    pool = [t for t in threads if t.is_cross_source] if cross_source_only else list(threads)
    for i, thread in enumerate(pool[:limit]):
        rows.append(
            {
                "dominant": i == 0,
                "anchor": _anchor_label(thread.anchors),
                "sources": sorted(thread.sources),
                "rows": [
                    {
                        "source": f.source,
                        "when_label": f.when_label,
                        "date": f.iso_date,
                        "title": f.title,
                        "text": f.text[:300],
                    }
                    for f in thread.facts[:max_rows]
                ],
            }
        )
    return rows
