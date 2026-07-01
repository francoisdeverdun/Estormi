"""Deterministic structure lint for the day-vision output.

The day-vision prompt mandates a strict plain-text shape (labelled
``READINESS:``/``OBJECTIVE:``/``AROUND:`` lines, prose-only MY DAY, sourced
bullets only under AROUND, output language) that ``build_daily_note`` parses
with exact-label regexes. Cloud models hold the shape from instructions; a
local 14B drifts — and every drift either mangles the rendered note or loses
a section silently.

This linter re-checks those invariants in code and returns issues in the same
``{"type", "excerpt"}`` shape as the LLM critic (``prompts.critique_briefing``),
so the orchestrator can merge both into one repair pass. It costs no LLM call,
never raises, and only flags what it can prove from the text alone — the
judgment calls (fusion, misattribution) stay with the LLM critic.
"""

from __future__ import annotations

import re

# Localised label variants a non-English run drifts into. The renderer only
# parses the English labels, so a French "OBJECTIF :" line would leak into the
# body as prose — flag it for repair instead.
_LABEL_VARIANTS = re.compile(
    r"(?mi)^[*#>\s-]*(OBJECTIF|AUTOUR|FORME DU JOUR|PR[EÉ]PARATION|READINESS DU JOUR)\s*\**\s*:"
)
_OBJECTIVE_RE = re.compile(r"(?mi)^[*#>\s-]*OBJECTIVE\s*\**\s*:")
_AROUND_RE = re.compile(r"(?mi)^[*#>\s-]*AROUND\s*\**\s*:")
_BULLET_RE = re.compile(r"^\s*[-•]\s+")
# Tolerate trailing punctuation after the marker — "… [src: gcal · 12 Jun]."
# is sourced, not a violation.
_SRC_RE = re.compile(r"\[src:[^\]]+\]\s*[.…)]?\s*$")
# A ``[src: …]`` marker with substantive text still running after it on the same
# line — the model buried the attribution mid-sentence instead of closing on it
# ("… la revue [src: agenda · 12 juin] débouche sur le point leadership"). Only
# a real word after the bracket counts; trailing punctuation/quotes/whitespace
# are a legitimate close.
_SRC_MID_SENTENCE_RE = re.compile(r"\[src:[^\]]+\][»”\")\s.,;:…]*[0-9A-Za-zÀ-ÿ]")
# The structure labels are the prompt's scaffolding — "MY DAY" names a section
# in the instructions, it must never appear in the output itself.
_MY_DAY_LABEL_RE = re.compile(r"(?mi)^[*#>\s-]*MY DAY\b|\*\*MY DAY\*\*")
# A heading-style line: short, ends on a bare colon (nothing after it). The
# briefing's prose never ends a line that way; local models drift into
# "To prepare:" / "Suivi technique :" rubric lines.
_HEADING_LINE_RE = re.compile(r"^\s*\**[^:\n]{2,60}\s*:\s*\**\s*$")
_ALLOWED_LABELS_RE = re.compile(
    r"^\s*[*#>\s-]*(READINESS|OBJECTIVE|AROUND)\s*\**\s*:", re.IGNORECASE
)

# Crude English-drift detector for non-English runs: function words that are
# unmistakably English and never French. Only egregious drift trips the check
# (several distinct markers), so borrowed words / proper nouns never flag.
_ENGLISH_MARKERS = re.compile(
    r"\b(the|and|with|your|today|meeting|before|because|should|between)\b", re.IGNORECASE
)

# Formal-address markers for French runs — the briefing tutoie. "rendez-vous"
# is excluded (its "vous" is not an address), and a single slip is enough to
# flag: mixed tu/vous reads worse than either alone.
_FRENCH_FORMAL_RE = re.compile(r"\b(votre|vos)\b|(?<!rendez-)\bvous\b", re.IGNORECASE)
# Vouvoiement also hides in the plural imperative, which carries no "vous"
# pronoun to catch ("Réservez l'énergie", "Planifiez la revue"). A blanket
# ``\w+ez\b`` would eat innocent words (assez, chez, nez, aptamerez… and every
# 2nd-person-plural non-imperative), so this is an explicit, curated list of
# the advice verbs the writer actually drifts into — the same verbs
# ``_repair_voice`` is told to singularise.
_FRENCH_IMPERATIVE_VOUS_RE = re.compile(
    r"(?i)\b(planifiez|v[ée]rifiez|pensez|r[ée]servez|contactez|notez|pr[ée]voyez|"
    r"pr[ée]parez|gardez|prenez|anticipez|priorisez|bloquez|appelez|relisez|"
    r"envoyez|confirmez|validez|organisez|profitez|[ée]vitez|assurez|veillez|"
    r"gérez|g[ée]rez|surveillez|programmez|planchez|traitez|terminez|finalisez)\b"
)

# Purple prose a small model slips into ("avant que l'après-midi ne scelle leur
# sort", "n'admettent aucun délai"). Specific phrasings only, to avoid flagging
# legitimate urgency; an advisory nudge into the same repair pass.
#
# The second alternative catches the personification tic — a day/meeting/review
# "scelle/trace/révèle/commande/tranche" its object ("la journée scelle la
# boucle", "la réunion tranche la trajectoire"). It excludes the literal deal-
# sealing sense ("sceller un accord/pacte/contrat/alliance"), which is plain
# language, not melodrama.
_MELODRAMA_VERB_RE = re.compile(
    r"(?i)\b(?:scelle|scellent|trace|tracent|r[ée]v[èe]le|r[ée]v[èe]lent|"
    r"commande|commandent|tranche|tranchent)\s+"
    r"(?!(?:un|une|le|la|les|l')\s*(?:accord|pacte|contrat|alliance|trait[ée])\b)"
    r"(?:le|la|les|leur|son|sa|ses|l'|un|une|ce|cette)\b"
)
_MELODRAMA_RE = re.compile(
    r"(?i)\bscell\w+\s+(?:le|la|leur|son|sa)\s+sort\b|n'admet\w*\s+aucun\s+d[ée]lai"
)

# MY DAY is "the heart of the briefing" (~150-200 words per the prompt); under
# this floor the model almost certainly truncated or skipped the synthesis.
#
# The floor stays at 60 rather than the 80 the review floated: MY DAY quality
# is the LLM critic's job, and a hard bump would flag genuinely-tight days into
# a needless repair pass. Self-repetition — the concrete 9.2 failure, where a
# small model re-verbalises the same content bigram after bigram — is caught by
# the (advisory) repeated-bigram check below instead.
_MY_DAY_MIN_WORDS = 60

# Function words carry no content, so a repeated ("de", "la") bigram means
# nothing — only content bigrams count toward self-repetition.
_STOPWORDS_FR = frozenset(
    """
    le la les un une des du de d au aux à a et ou où que qui quoi dont ce cet
    cette ces son sa ses leur leurs mon ma mes ton ta tes notre nos votre vos
    en dans sur sous par pour avec sans vers chez entre est sont être été fait
    faire il elle ils elles on tu te toi se sy ne pas plus tout tous toute
    toutes ça cela comme si mais donc car puis alors déjà encore aussi bien
    the and with your for this that from into onto their his her its
    """.split()
)
# A MY DAY paragraph that repeats several distinct content-word bigrams verbatim
# is re-verbalising itself, not synthesising. Advisory only; the threshold is
# high enough that ordinary prose (which rarely repeats a two-content-word span
# at all) never trips it.
_MY_DAY_REPEAT_BIGRAM_MIN = 3

# Numbered-list line ("1. …" / "2) …") — the bullet ban's favourite loophole.
_NUMBERED_LINE_RE = re.compile(r"^\s*\d+[.)]\s+")
# Coach-speak the writer prompt already bans but a 14B still emits — the
# deterministic counterpart of the prompt rule, feeding the same repair loop.
# Each phrase is advice-shaped filler, never a fact from the rows.
_COACH_SPEAK_RE = re.compile(
    r"(?i)(?:\bsera plus utile si\b|\bn'h[ée]site pas\b|\bpense [àa] \b|"
    r"\bveille [àa] \b|\bselon ce qui a [ée]t[ée] d[ée]cid[ée]\b|"
    r"\bpour gagner du temps\b|\bsans plus attendre\b|\bsans d[ée]lai\b|"
    r"\bmake sure to\b|\bdon'?t hesitate\b|\bbe sure to\b|\bstay tuned\b)"
)
# READINESS must be a steer, not a metric recital; count its numbers.
_READINESS_LINE_RE = re.compile(r"(?mi)^[*#>\s-]*READINESS\s*\**\s*:\s*(.*)$")
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
_READINESS_MAX_FIGURES = 2
# Clock times ("12:00", "12h", "12 h 30") are schedule anchors, not health
# metrics — the day-load adviser legitimately names a free slot in the steer,
# and counting its digits as "figures" would flag every concrete advice line.
_CLOCK_TOKEN_RE = re.compile(r"\b\d{1,2}\s*[h:]\s*\d{2}\b|\b\d{1,2}\s*h\b", re.IGNORECASE)


def _excerpt(line: str) -> str:
    return line.strip()[:200]


_WORD_RE = re.compile(r"[0-9A-Za-zÀ-ÿ']+")


def repeated_content_bigrams(text: str) -> int:
    """How many distinct content-word bigrams ``text`` repeats verbatim.

    A bigram counts only when BOTH its words are content words (stopwords carry
    no signal), and only distinct repeated bigrams are tallied — so a paragraph
    that says "revue partenaire … revue partenaire … revue partenaire" scores 1,
    not 2. Ordinary synthesising prose repeats a content bigram rarely; a small
    model re-verbalising itself repeats several. Advisory signal only.
    """
    words = [w.lower() for w in _WORD_RE.findall(text or "") if w.lower() not in _STOPWORDS_FR]
    seen: set[tuple[str, str]] = set()
    repeated: set[tuple[str, str]] = set()
    for a, b in zip(words, words[1:]):
        bigram = (a, b)
        if bigram in seen:
            repeated.add(bigram)
        else:
            seen.add(bigram)
    return len(repeated)


def readiness_line_span(text: str) -> tuple[int, int, str] | None:
    """Locate the READINESS line in ``text`` — ``(start, end, content)`` of the
    whole line, or ``None``. Shared with the surgical READINESS repair in
    ``day_vision`` so both sides agree on what "the READINESS line" is."""
    m = _READINESS_LINE_RE.search(text or "")
    if not m:
        return None
    return m.start(), m.end(), m.group(1)


def readiness_has_figure_dump(content: str) -> bool:
    """``True`` when a READINESS steer recites more figures than it should.

    Clock times are exempt: "ta séance passe sur le créneau 12:00–14:00" is
    concrete advice, not a metric recital."""
    return len(_NUMBER_RE.findall(_CLOCK_TOKEN_RE.sub(" ", content or ""))) > _READINESS_MAX_FIGURES


# ── lede lint ─────────────────────────────────────────────────────────────────
# The opening line is the briefing's most visible sentence and a small model's
# weakest: it defaults to a mission statement ("Concrétiser l'impact des
# décisions…") that carries zero information. These checks are the prefilter
# for the best-of-N lede tournament — a candidate that trips any of them never
# reaches the judge.

_LEDE_ABSTRACT_OPENER_RE = re.compile(
    r"(?i)^[\s«\"']*(concr[ée]tiser|concentrer|optimiser|garantir|maximiser|"
    r"assurer|aligner|mobiliser|structurer|renforcer|consolider|prioriser|"
    r"capitaliser|harmoniser|favoriser|valoriser|"
    r"ensuring|maximizing|maximising|optimizing|optimising|leveraging|"
    r"aligning|driving|prioritizing|prioritising|focusing)\b"
)
_LEDE_JARGON_RE = re.compile(
    r"(?i)\b(performance(?:s)? optimale(?:s)?|engagements? critiques?|"
    r"valeur ajout[ée]e|synergie|alignement|transitions? logistiques?|"
    r"l[ée]gitimit[ée]|efficience|optimal performance|critical commitments|"
    r"added value|synergy|logistical transitions)\b"
)
# A capitalised token past the first word — a name anchoring the lede in the
# user's actual day (sentence punctuation resets don't matter for a one-liner).
_LEDE_PROPER_RE = re.compile(r"(?<!^)(?<![.!?]\s)\b[A-ZÀ-Þ][A-Za-zà-ÿ'’\-]{2,}\b")
# The generic day-span opener: "Une journée de 9h à 18h…", "la journée est de
# 14h30 à 21h…". It brackets the day's hours but names no pivot, so it reads
# identically every day — the tic a small model drifts into when "a time
# counts as an anchor". Flag it so the best-of-N tournament keeps a lede that
# leads with the named event instead.
_LEDE_TIMESPAN_RE = re.compile(
    r"(?i)\b(journée|matin[ée]e|après-midi|soir[ée]e)\b.{0,24}?\b(?:de|est\s+de)\s+"
    r"\d{1,2}\s*h\d*\s*(?:à|–|-|au)\s*\d{1,2}\s*h"
)


def lede_issues(lede: str) -> list[str]:
    """Why this lede candidate is unusable (empty list = acceptable).

    A good lede names the day's pivot — a meeting, a person, a place — and reads
    as information, not a corporate mission statement nor a bare recital of the
    day's hours. A clock time situates; it does not anchor on its own.
    """
    text = (lede or "").strip()
    if not text:
        return ["empty"]
    issues: list[str] = []
    if _LEDE_ABSTRACT_OPENER_RE.match(text):
        issues.append("opens on an abstract infinitive/gerund — name the day's anchors instead")
    if _LEDE_JARGON_RE.search(text):
        issues.append("corporate jargon")
    if not _LEDE_PROPER_RE.search(text):
        issues.append(
            "no named anchor — name the day's pivot (a meeting, person or place), not just the hours"
        )
    if _LEDE_TIMESPAN_RE.search(text):
        issues.append("opens on a bare time-span — lead with the named pivot, not the day's hours")
    if _MELODRAMA_RE.search(text) or _MELODRAMA_VERB_RE.search(text):
        issues.append(
            "melodramatic personification (a day/meeting 'scelle/tranche/commande'…) — "
            "state what happens soberly, don't dramatise"
        )
    if len(text) > 240:
        issues.append("too long for an opening line")
    return issues


# Per-stage degeneration floor for the distillation eval. The distill gate
# regenerates held-out prompts for every briefing stage and counts how many
# come back un-degenerate. This dispatcher gives each non-lede stage the same
# kind of deterministic floor ``lede_issues`` gives the lede: it flags a
# fine-tune that collapsed into empties, coach-speak filler, English drift or
# list/heading rubric — NOT editorial quality (style is what the adapter is
# for). Returns the issues found (empty list == acceptable).
_STAGE_MAX_LEN = {"lede": 240, "readiness": 400, "writer": 1000, "impact": 320}
_STAGE_ENGLISH_DRIFT = 4  # distinct English function words; mirrors lint_vision's threshold


def stage_issues(stage: str, text: str) -> list[str]:
    """Why a generated ``stage`` output is degenerate (empty list = acceptable).

    ``stage`` is ``lede`` / ``readiness`` / ``writer`` / ``impact``; the lede
    keeps its richer anchor check (:func:`lede_issues`), the rest share a floor
    that only catches collapse, never taste. An unknown stage gets the floor.
    """
    if stage == "lede":
        return lede_issues(text)
    body = (text or "").strip()
    if not body:
        return ["empty"]
    issues: list[str] = []
    if len(body) > _STAGE_MAX_LEN.get(stage, 1000):
        issues.append("too long")
    if _COACH_SPEAK_RE.search(body):
        issues.append("coach-speak filler")
    if len({m.group(0).lower() for m in _ENGLISH_MARKERS.finditer(body)}) >= _STAGE_ENGLISH_DRIFT:
        issues.append("English drift")
    if any(_NUMBERED_LINE_RE.match(ln) or _BULLET_RE.match(ln) for ln in body.splitlines()):
        issues.append("list formatting where prose is required")
    if stage == "readiness" and readiness_has_figure_dump(body):
        issues.append("recites too many health figures")
    return issues


def lint_vision(text: str, language: str = "English") -> list[dict]:
    """Check ``text`` (raw day-vision output) against the structural contract.

    Returns a list of ``{"type", "excerpt"}`` issues (empty when clean) that
    :func:`estormi_briefing.compose.prompts.format_critic_feedback` can render into a
    repair directive alongside the LLM critic's findings.
    """
    issues: list[dict] = []
    body = (text or "").strip()
    if not body:
        return issues

    for m in _LABEL_VARIANTS.finditer(body):
        issues.append(
            {
                "type": "label_not_english",
                "excerpt": _excerpt(
                    m.group(0) + " — section labels must be exactly READINESS:/OBJECTIVE:/AROUND: "
                    "(English), whatever the output language"
                ),
            }
        )

    if _MY_DAY_LABEL_RE.search(body):
        issues.append(
            {
                "type": "rogue_my_day_label",
                "excerpt": '"MY DAY" appears in the output — it is a section name from the '
                "instructions, never a label to emit; remove it and start the prose directly",
            }
        )

    for line in body.splitlines():
        if _HEADING_LINE_RE.match(line) and not _ALLOWED_LABELS_RE.match(line):
            issues.append(
                {
                    "type": "rogue_heading",
                    "excerpt": _excerpt(line)
                    + " — no headings: the only labelled lines are READINESS:/OBJECTIVE:/AROUND:; "
                    "turn this into a normal sentence",
                }
            )
            break

    if not _OBJECTIVE_RE.search(body):
        issues.append(
            {
                "type": "missing_objective",
                "excerpt": "no `OBJECTIVE:` line — the through-line of the day is required",
            }
        )

    around_match = _AROUND_RE.search(body)
    if not around_match:
        issues.append(
            {
                "type": "missing_around",
                "excerpt": "no `AROUND:` line — the periphery section is required",
            }
        )
        my_day, around = body, ""
    else:
        my_day, around = body[: around_match.start()], body[around_match.end() :]

    for line in my_day.splitlines():
        if _BULLET_RE.match(line):
            issues.append(
                {
                    "type": "bullet_in_my_day",
                    "excerpt": _excerpt(line)
                    + " — MY DAY must be continuous prose; bullets belong only under AROUND:",
                }
            )
            break  # one example is enough for a repair directive

    for line in my_day.splitlines():
        if _NUMBERED_LINE_RE.match(line):
            issues.append(
                {
                    "type": "numbered_list_in_my_day",
                    "excerpt": _excerpt(line)
                    + " — no numbered lists: MY DAY is continuous prose; relate the items "
                    "in sentences instead",
                }
            )
            break

    for line in my_day.splitlines():
        m = _COACH_SPEAK_RE.search(line)
        if m:
            issues.append(
                {
                    "type": "coach_speak",
                    "excerpt": _excerpt(line)
                    + f" — « {m.group(0).strip()} » est du remplissage de coach, pas un "
                    "fait : énonce le fait et sa conséquence, sans conseil",
                }
            )
            break

    for line in my_day.splitlines():
        if _SRC_MID_SENTENCE_RE.search(line):
            issues.append(
                {
                    "type": "src_marker_mid_sentence",
                    "excerpt": _excerpt(line)
                    + " — the [src: …] attribution belongs at the END of its sentence; "
                    "don't bury it mid-sentence with more prose running after it",
                }
            )
            break

    span = readiness_line_span(body)
    if span and readiness_has_figure_dump(span[2]):
        issues.append(
            {
                "type": "readiness_figure_dump",
                "excerpt": _excerpt(span[2])
                + " — READINESS is a steer, not a metric recital: keep at most one or two "
                "figures and say what the day should look like",
            }
        )

    for line in around.splitlines():
        if _BULLET_RE.match(line) and not _SRC_RE.search(line.rstrip()):
            issues.append(
                {
                    "type": "unsourced_around_bullet",
                    "excerpt": _excerpt(line)
                    + " — every AROUND bullet must end with its [src: LABEL · WHEN]",
                }
            )
            break

    # Strip labelled lines before counting MY DAY's prose.
    prose_lines = [
        ln
        for ln in my_day.splitlines()
        if not re.match(r"^[*#>\s-]*(READINESS|OBJECTIVE)\s*\**\s*:", ln.strip(), re.IGNORECASE)
    ]
    prose_text = " ".join(prose_lines)
    if 0 < len(prose_text.split()) < _MY_DAY_MIN_WORDS:
        issues.append(
            {
                "type": "my_day_too_thin",
                "excerpt": "MY DAY is only a few sentences — develop the day's threads "
                "(~150-200 words of connected prose)",
            }
        )
    elif repeated_content_bigrams(prose_text) >= _MY_DAY_REPEAT_BIGRAM_MIN:
        issues.append(
            {
                "type": "my_day_self_repetition",
                "excerpt": "MY DAY re-verbalises itself — the same phrases recur verbatim; "
                "synthesise the day's threads once, don't restate them",
            }
        )

    if language.lower() != "english":
        markers = {m.group(0).lower() for m in _ENGLISH_MARKERS.finditer(body)}
        if len(markers) >= 4:
            issues.append(
                {
                    "type": "english_drift",
                    "excerpt": f"English words in a {language} briefing "
                    f"({', '.join(sorted(markers)[:5])}…) — write the ENTIRE briefing "
                    f"in {language}",
                }
            )

    if language.lower() == "french":
        mel = _MELODRAMA_RE.search(body) or _MELODRAMA_VERB_RE.search(body)
        if mel:
            issues.append(
                {
                    "type": "melodrama",
                    "excerpt": _excerpt(mel.group(0))
                    + " — registre trop dramatique : énonce le fait sobrement, sans "
                    "grandiloquence ni sur-dramatisation",
                }
            )
        for ln in body.splitlines():
            # Pull-quotes ("> …") are verbatim message text — a "vous" inside
            # one is the sender's wording, not the briefing's address.
            if ln.lstrip().startswith(">"):
                continue
            if _FRENCH_FORMAL_RE.search(ln) or _FRENCH_IMPERATIVE_VOUS_RE.search(ln):
                issues.append(
                    {
                        "type": "formal_address",
                        "excerpt": _excerpt(ln)
                        + " — tutoie l'utilisateur (« tu », « ton agenda »), jamais de "
                        "vouvoiement",
                    }
                )
                break

    return issues
