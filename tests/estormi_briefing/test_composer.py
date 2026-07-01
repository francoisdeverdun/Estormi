"""Tests for the plan-then-write composer."""

from __future__ import annotations

import json

import pytest

from estormi_briefing.compose.composer import (
    ComposerError,
    add_news_entries,
    build_registry,
    compose_vision,
    filter_around,
    paragraph_violations,
    plan_schema,
)

pytestmark = pytest.mark.unit

_DATE = "2026-06-11"


def _rows() -> dict:
    return {
        "calendar": [
            {"when": "10:00", "title": "Revue archi", "group_type": "work"},
            {"when": "15:00", "title": "Audit cloud (2/2)", "group_type": "work"},
            {"when": "17:00", "title": "Comité data", "group_type": "work"},
        ],
        "overdue": [],
        "today_rem": [{"when": "", "title": "réserver les billets du voyage"}],
        "threads": [
            {
                "anchor": "Camille",
                "rows": [
                    {
                        "source": "whatsapp",
                        "when_label": "2026-06-06 (Saturday)",
                        "title": "Les copains",
                        "text": "canapé chez la maman de Camille samedi midi",
                    }
                ],
            }
        ],
        "corr_blocks": [],
        "ctx_rows": [
            {
                "source": "mail",
                "when_label": "2026-03-19 (Thursday)",
                "title": "Firebase",
                "text": "plus de nouveaux workspaces après le 22 juin",
            }
        ],
        "wa_blocks": [{"label": "Marius [friends]", "texts": ["jeux de société samedi 18h30 ?"]}],
        "health_rows": [],
    }


# ── registry & schema ─────────────────────────────────────────────────────────


def test_registry_ids_kinds_and_today_marking():
    reg = build_registry(_rows(), _DATE)
    by_kind = {}
    for e in reg:
        by_kind.setdefault(e["kind"], []).append(e)
    assert [e["id"] for e in by_kind["A"]] == ["A1", "A2", "A3"]
    assert all(e["when"].startswith("aujourd'hui") for e in by_kind["A"])
    assert all(e["date"] == _DATE for e in by_kind["A"])
    assert by_kind["T"][0]["date"] == "2026-06-06"
    assert by_kind["W"][0]["label"] == "WhatsApp · Marius"


def test_registry_marks_cancelled_calendar_event_inline():
    """E5: a cancelled calendar event carries an inline "(ANNULÉ …)" note into
    its registry text so the plan, the writers and the lede all see it and it
    never anchors the day. A live event is untouched."""
    rows = _rows()
    rows["calendar"][0]["cancelled"] = True  # "Revue archi"
    reg = build_registry(rows, _DATE)
    a_entries = [e for e in reg if e["kind"] == "A"]
    cancelled = next(e for e in a_entries if "Revue archi" in e["text"])
    assert "ANNULÉ" in cancelled["text"]
    live = next(e for e in a_entries if "Comité data" in e["text"])
    assert "ANNULÉ" not in live["text"]


def test_plan_schema_locks_ids_to_enum():
    reg = build_registry(_rows(), _DATE)
    schema = plan_schema([e["id"] for e in reg])
    enum = schema["properties"]["around"]["items"]["properties"]["id"]["enum"]
    assert set(enum) == {e["id"] for e in reg}


def test_add_news_entries_appends_periphery_candidates():
    reg = build_registry(_rows(), _DATE)
    add_news_entries(reg, "- Sommet du G7 le 16 juin\n- Inflation US à 4,2 %\nprose ignorée")
    news = [e for e in reg if e["kind"] == "N"]
    assert len(news) == 2
    assert news[0]["text"].startswith("Sommet du G7")


# ── per-paragraph verification ────────────────────────────────────────────────


def test_paragraph_violations_catch_invented_hour_and_date():
    entries = [{"when": "aujourd'hui 10:00", "text": "[work] Revue archi", "kind": "A"}]
    ok = paragraph_violations("L'Revue archi occupe la matinée dès 10h.", entries, _DATE)
    assert ok == []
    bad_hour = paragraph_violations("La revue démarre à 9h45.", entries, _DATE)
    assert any("9:45" in v for v in bad_hour)
    bad_date = paragraph_violations("La revue est reportée au 16 juin.", entries, _DATE)
    assert bad_date


def test_paragraph_violations_allow_briefing_day():
    entries = [{"when": "", "text": "réserver les billets", "kind": "R"}]
    assert paragraph_violations("À régler ce 11 juin.", entries, _DATE) == []


# ── around filtering ──────────────────────────────────────────────────────────


def test_filter_around_drops_past_today_calendar_and_duplicates():
    reg = build_registry(_rows(), _DATE)
    by_id = {e["id"]: e for e in reg}
    # Add a past WhatsApp-derived entry.
    by_id["T1"]["date"] = "2026-06-01"
    plan_around = [
        {"id": "A1", "stake": "un événement du jour déguisé en périphérie"},
        {"id": "T1", "stake": "événement passé"},
        {"id": "C1", "stake": "échéance Firebase après le 22 juin"},
        {"id": "C1", "stake": "doublon du même fait"},
        {"id": "ZZ", "stake": "id inconnu"},
    ]
    kept = filter_around(plan_around, by_id, _DATE)
    assert [k["id"] for k in kept] == ["C1"]


def test_filter_around_dedups_same_chunk_under_two_anchor_prefixes():
    """The SAME chunk can orbit twice — once as a thread row "(fil: …)" and once
    as a linked chunk "(lié à: …)". The prefix is stripped before keying so the
    two prefixed copies collapse to one row (mirrors B4's HTML seen-set)."""
    body = "Les copains: canapé chez la maman de Camille samedi midi"
    by_id = {
        "T1": {
            "id": "T1",
            "kind": "T",
            "label": "whatsapp",
            "when": "",
            "date": "",
            "text": f"(fil:Camille) {body}",
        },
        "L1": {
            "id": "L1",
            "kind": "L",
            "label": "whatsapp",
            "when": "",
            "date": "",
            "text": f"(lié à: dîner) {body}",
        },
    }
    kept = filter_around(
        [{"id": "T1", "stake": "à préparer"}, {"id": "L1", "stake": "à préparer"}],
        by_id,
        _DATE,
    )
    assert [k["id"] for k in kept] == ["T1"]  # the L1 copy is a duplicate


def test_filter_around_replaces_stake_with_invented_deadline():
    reg = build_registry(_rows(), _DATE)
    by_id = {e["id"]: e for e in reg}
    kept = filter_around(
        [{"id": "C1", "stake": "Firebase à migrer avant 22h ce soir"}], by_id, _DATE
    )
    # "22h" exists nowhere in the row → the stake is swapped for the row text.
    assert kept[0]["stake"].startswith("Firebase: plus de nouveaux workspaces")


# ── end-to-end composition with a scripted LLM ────────────────────────────────


async def test_compose_vision_prose_carries_only_planned_threads():
    """The inversion: events the plan didn't select are NOT re-added as prose
    — coverage of the bare schedule lives in code (timeline strip + reminders
    line in the renderer), the prose carries only the planned insight."""
    reg_rows = _rows()
    calls: list[dict] = []

    async def fake_llm(prompt: str, **kw) -> str:
        calls.append({"prompt": prompt, **kw})
        if "json_schema" in kw:  # the plan call
            return json.dumps(
                {
                    "myday_threads": [{"ids": ["A1"], "angle": "la revue conditionne la suite"}],
                    "around": [
                        {"id": "C1", "stake": "Échéance Firebase le 22 juin"},
                        {"id": "A2", "stake": "tentative de mettre un événement du jour ici"},
                    ],
                }
            )
        if kw.get("max_tokens") == 120:  # lede candidate — trips the jargon lint
            return "Optimiser la performance globale des engagements du jour."
        return "Paragraphe factuel sans heure inventée."

    out = await compose_vision(_DATE, reg_rows, fake_llm, day_anchor="Nous sommes jeudi.")
    # A2/A3/R1 missing from the plan → exactly ONE writer call, no synthetics.
    writer_calls = [c for c in calls if "json_schema" not in c and c.get("max_tokens") == 220]
    assert len(writer_calls) == 1
    # Every lede candidate failed the concreteness lint → code-built fallback.
    assert "OBJECTIVE: La journée s'ouvre sur Revue archi à 10:00" in out
    assert "AROUND:" in out
    assert "Échéance Firebase le 22 juin [src: mail · 19 Mar]" in out
    # The A2 around item was filtered (today's calendar never orbits).
    assert "tentative de mettre" not in out


async def test_lede_candidate_kept_when_concrete_and_grounded():
    reg_rows = _rows()

    async def fake_llm(prompt: str, **kw) -> str:
        if "json_schema" in kw:
            return json.dumps(
                {
                    "myday_threads": [{"ids": ["A1"], "angle": "a"}],
                    "around": [],
                }
            )
        if kw.get("max_tokens") == 120:
            return "La Revue archi de 10h donne le ton avant le Comité data de 17h."
        return "Paragraphe factuel sans heure inventée."

    out = await compose_vision(_DATE, reg_rows, fake_llm)
    assert "OBJECTIVE: La Revue archi de 10h donne le ton" in out


async def test_compose_vision_regenerates_violating_paragraph_once():
    reg_rows = _rows()
    writer_outputs = iter(
        [
            "Réunion déplacée à 9h45 demain.",  # invents an hour → rejected
            "La session de 10:00 occupe la matinée.",  # clean retry
        ]
    )

    async def fake_llm(prompt: str, **kw) -> str:
        if "json_schema" in kw:
            return json.dumps(
                {
                    "objective": "x",
                    "myday_threads": [{"ids": ["A1", "A2", "A3", "R1"], "angle": "a"}],
                    "around": [],
                }
            )
        if kw.get("stage") == "writer":
            return next(writer_outputs)
        return "Texte sans information concrète."  # lede candidates → lint-rejected

    out = await compose_vision(_DATE, reg_rows, fake_llm)
    assert "9h45" not in out
    assert "10:00" in out


async def test_compose_vision_raises_on_empty_plan():
    async def fake_llm(prompt: str, **kw) -> str:
        if "json_schema" in kw:
            return json.dumps({"objective": "x", "myday_threads": [], "around": []})
        return "n/a"

    with pytest.raises(ComposerError):
        await compose_vision(_DATE, _rows(), fake_llm)


async def test_compose_vision_readiness_from_health_rows():
    rows = _rows()
    rows["health_rows"] = [{"when_label": "07:00", "text": "Recovery 66%, sommeil 7h48"}]

    async def fake_llm(prompt: str, **kw) -> str:
        if "json_schema" in kw:
            return json.dumps(
                {
                    "objective": "x",
                    "myday_threads": [{"ids": ["A1", "A2", "A3"], "angle": "a"}],
                    "around": [],
                }
            )
        if kw.get("gbnf_grammar", "").strip().startswith('root ::= "READINESS'):
            return "READINESS: Base correcte, garde l'après-midi léger."
        return "La session de 10:00 occupe la matinée."

    out = await compose_vision(_DATE, rows, fake_llm)
    assert out.startswith("READINESS: Base correcte")


async def test_thread_only_plan_yields_single_paragraph():
    """A plan that selects one T-row thread produces exactly one paragraph —
    no synthetic coverage threads exist any more (the timeline carries the
    bare schedule)."""
    rows = _rows()
    rows["threads"] = [
        {
            "anchor": "redshift",
            "rows": [
                {
                    "source": "calendar",
                    "when_label": "2026-06-11 (Thursday) 10:00",
                    "title": "Revue archi",
                    "text": "Revue archi session de deux heures",
                }
            ],
        }
    ]
    writer_calls = []

    async def fake_llm(prompt: str, **kw) -> str:
        if "json_schema" in kw:
            return json.dumps(
                {
                    "myday_threads": [{"ids": ["T1"], "angle": "a"}],
                    "around": [],
                }
            )
        if kw.get("max_tokens") == 220:
            writer_calls.append(prompt)
        return "Paragraphe factuel sans heure inventée."

    await compose_vision(_DATE, rows, fake_llm)
    assert len(writer_calls) == 1


async def test_readiness_figure_must_exist_in_health_rows():
    rows = _rows()
    rows["health_rows"] = [{"when_label": "07:00", "text": "Recovery 66%, sommeil 7h48"}]
    readiness_outputs = iter(
        [
            "READINESS: Récupération à 61%, garde la journée légère.",  # 61 invented
            "READINESS: Récupération à 66%, garde la journée légère.",
        ]
    )

    async def fake_llm(prompt: str, **kw) -> str:
        if "json_schema" in kw:
            return json.dumps(
                {
                    "objective": "x",
                    "myday_threads": [{"ids": ["A1", "A2", "A3"], "angle": "a"}],
                    "around": [],
                }
            )
        if kw.get("gbnf_grammar", "").strip().startswith('root ::= "READINESS'):
            return next(readiness_outputs)
        return "Paragraphe factuel sans heure inventée."

    out = await compose_vision(_DATE, rows, fake_llm)
    assert "66%" in out.splitlines()[0]
    assert "61%" not in out


def test_src_marker_humanises_dates_and_dedupes_labels():
    from estormi_briefing.compose.composer import _src_marker

    entries = [
        {"label": "agenda", "date": "2026-06-11", "when": "aujourd'hui 10:00"},
        {"label": "agenda", "date": "2026-06-11", "when": "aujourd'hui 15:00"},
        {"label": "whatsapp", "date": "2026-06-06", "when": "2026-06-06 (Saturday)"},
    ]
    assert _src_marker(entries) == "[src: agenda · 11 Jun + whatsapp · 6 Jun]"


async def test_due_today_reminders_never_orbit_nor_force_prose():
    """Due-today reminders are banned from AROUND (today's actions never
    orbit) — and since the inversion they are NOT forced into prose either:
    the renderer's code-built reminders line carries them."""
    rows = _rows()
    writer_prompts = []

    async def fake_llm(prompt: str, **kw) -> str:
        if "json_schema" in kw:
            return json.dumps(
                {
                    "myday_threads": [{"ids": ["A1", "A2", "A3"], "angle": "a"}],
                    # The plan ignores R1 in threads AND tries to park it in around.
                    "around": [{"id": "R1", "stake": "billets du voyage, sans urgence"}],
                }
            )
        if kw.get("max_tokens") == 220:
            writer_prompts.append(prompt)
        return "Paragraphe factuel sans heure inventée."

    out = await compose_vision(_DATE, rows, fake_llm)
    # R1 is not forced into any writer prompt…
    assert not any("réserver les billets" in p for p in writer_prompts)
    # …and its around placement was filtered out (today's actions never orbit).
    assert "sans urgence" not in out


async def test_cohesion_accepted_when_no_new_facts():
    from estormi_briefing.compose.composer import _cohere_paragraphs

    paras = [
        "La revue de 10:00 ouvre la journée. [src: agenda · 11 Jun]",
        "La revue de 15:00 enchaîne sur la synthèse de 17:00. [src: agenda · 11 Jun]",
    ]

    async def fake_llm(prompt: str, **kw) -> str:
        return (
            "La revue de 10:00 ouvre la journée et nourrit la suite. [src: agenda · 11 Jun]\n\n"
            "La revue de 15:00 enchaîne directement sur la synthèse de 17:00. [src: agenda · 11 Jun]"
        )

    out = await _cohere_paragraphs(fake_llm, paras, "")
    assert len(out) == 2
    assert "nourrit la suite" in out[0]


async def test_cohesion_rejected_on_new_hour_or_lost_marker():
    from estormi_briefing.compose.composer import _cohere_paragraphs

    paras = [
        "La revue de 10:00 ouvre la journée. [src: agenda · 11 Jun]",
        "La revue de 15:00 suit. [src: agenda · 11 Jun]",
    ]

    async def adds_hour(prompt: str, **kw) -> str:
        return (
            "La revue de 10:00 puis un point à 9h45. [src: agenda · 11 Jun]\n\n"
            "La revue de 15:00 suit. [src: agenda · 11 Jun]"
        )

    async def drops_marker(prompt: str, **kw) -> str:
        return (
            "La revue de 10:00 ouvre la journée.\n\nLa revue de 15:00 suit. [src: agenda · 11 Jun]"
        )

    assert await _cohere_paragraphs(adds_hour, paras, "") == paras
    assert await _cohere_paragraphs(drops_marker, paras, "") == paras


def test_thread_adjacencies_match_by_title():
    from estormi_briefing.compose.composer import _thread_adjacencies

    chained = [{"from": "Audit cloud (2/2)", "to": "Comité data", "at": "17:00", "gap_min": 0}]
    entries = [{"text": "[work] Audit cloud (2/2)"}]
    assert _thread_adjacencies(chained, entries) == chained
    assert _thread_adjacencies(chained, [{"text": "[work] autre chose"}]) == []


async def test_around_stake_filler_clause_stripped():
    rows = _rows()

    async def fake_llm(prompt: str, **kw) -> str:
        if "json_schema" in kw:
            return json.dumps(
                {
                    "objective": "x",
                    "myday_threads": [{"ids": ["A1", "A2", "A3", "R1"], "angle": "a"}],
                    "around": [
                        {
                            "id": "C1",
                            "stake": "Échéance Firebase le 22 juin, sans lien avec les priorités du jour",
                        }
                    ],
                }
            )
        return "Paragraphe factuel sans heure inventée."

    out = await compose_vision(_DATE, rows, fake_llm)
    assert "Échéance Firebase le 22 juin [src:" in out
    assert "sans lien avec" not in out


# ── review fixes ──────────────────────────────────────────────────────────────


def test_entry_is_past_keeps_next_year_deadline():
    from estormi_briefing.compose.composer import _entry_is_past

    # December briefing, December chunk, January deadline → NOT past.
    e = {"date": "2026-12-15", "text": "échéance le 5 janvier pour le dossier"}
    assert _entry_is_past(e, "2026-12-20") is False
    # Same chunk without any future date → past.
    assert _entry_is_past({"date": "2026-12-15", "text": "compte rendu"}, "2026-12-20") is True


async def test_news_rows_never_reach_my_day_threads():
    rows = _rows()
    writer_prompts = []

    async def fake_llm(prompt: str, **kw) -> str:
        if "json_schema" in kw:
            return json.dumps(
                {
                    "objective": "x",
                    # The plan tries to weave a news row into MY DAY.
                    "myday_threads": [{"ids": ["A1", "A2", "A3", "R1", "N1"], "angle": "a"}],
                    "around": [],
                }
            )
        writer_prompts.append(prompt)
        return "Paragraphe factuel sans heure inventée."

    await compose_vision(_DATE, rows, fake_llm, news_digest="- Sommet du G7 le 16 juin")
    assert all("Sommet du G7" not in p for p in writer_prompts)


def test_paragraph_violations_allow_tomorrow():
    entries = [{"when": "aujourd'hui 10:00", "text": "[work] demo", "kind": "A"}]
    # The day anchor names tomorrow — "demain, le 12 juin" is legitimate prose.
    assert paragraph_violations("On prépare demain, le 12 juin.", entries, _DATE) == []


def test_hours_regex_ignores_durations():
    from estormi_briefing.compose.composer import _hours_in

    assert _hours_in("une session de 2h puis pause") == set()
    assert _hours_in("rendez-vous à 14h30 et 9h45") == {"14:30", "9:45"}


async def test_cohesion_rejected_on_new_figure():
    from estormi_briefing.compose.composer import _cohere_paragraphs

    paras = [
        "La récupération est à 66%. [src: health · 11 Jun]",
        "La revue de 15:00 suit. [src: agenda · 11 Jun]",
    ]

    async def adds_figure(prompt: str, **kw) -> str:
        return (
            "La récupération est à 61%. [src: health · 11 Jun]\n\n"
            "La revue de 15:00 suit. [src: agenda · 11 Jun]"
        )

    assert await _cohere_paragraphs(adds_figure, paras, "") == paras


async def test_readiness_omitted_when_retry_still_phantom():
    rows = _rows()
    rows["health_rows"] = [{"when_label": "07:00", "text": "Recovery 66%"}]

    async def fake_llm(prompt: str, **kw) -> str:
        if "json_schema" in kw:
            return json.dumps(
                {
                    "objective": "x",
                    "myday_threads": [{"ids": ["A1", "A2", "A3", "R1"], "angle": "a"}],
                    "around": [],
                }
            )
        if kw.get("gbnf_grammar", "").strip().startswith('root ::= "READINESS'):
            return "READINESS: Récupération à 61%, journée légère."  # always phantom
        return "Paragraphe factuel sans heure inventée."

    out = await compose_vision(_DATE, rows, fake_llm)
    assert "READINESS" not in out  # omitted, not shipped wrong


# ── fusion guards, label hygiene, periphery decay ─────────────────────────────


def test_registry_normalises_labels_and_rejects_time_as_date():
    rows = _rows()
    rows["ctx_rows"].append(
        {
            "source": "gcal",
            "when_label": "2026-06-13 (Saturday) 18:30",
            "title": "Bar à jeux Riton",
            "text": "samedi soir avec la bande",
        }
    )
    rows["threads"][0]["rows"].append(
        {"source": "calendar", "when_label": "09:45", "title": "Daily : Data Lake", "text": "x"}
    )
    reg = build_registry(rows, _DATE)
    riton = next(e for e in reg if "Riton" in e["text"])
    assert riton["label"] == "agenda"  # gcal → agenda
    assert riton["date"] == "2026-06-13"
    daily = next(e for e in reg if "Data Lake" in e["text"])
    assert daily["label"] == "agenda"  # calendar → agenda
    assert daily["date"] == ""  # a bare clock time must not pose as a date


def test_src_marker_merges_normalised_labels_and_drops_time_dates():
    from estormi_briefing.compose.composer import _src_marker

    entries = [
        {"label": "agenda", "date": "2026-06-11"},
        {"label": "agenda", "date": ""},  # the ex-"calendar · 09:45" row
        {"label": "mail", "date": "2026-03-19"},
    ]
    assert _src_marker(entries) == "[src: agenda · 11 Jun + mail · 19 Mar]"


def test_registry_annotates_next_future_deadline():
    reg = build_registry(_rows(), _DATE)
    c1 = next(e for e in reg if e["kind"] == "C")
    # Chunk dated 19 Mar, text names "après le 22 juin" → the actionable date.
    assert c1["deadline_iso"] == "2026-06-22"
    assert c1["deadline"] == "22 Jun"
    a1 = next(e for e in reg if e["kind"] == "A")
    assert a1["deadline"] == ""  # no future date in the title → no annotation


def test_filter_around_drops_today_dated_calendar_chunk():
    rows = _rows()
    rows["ctx_rows"].append(
        {
            "source": "gcal",
            "when_label": f"{_DATE} (Thursday) 16:00",
            "title": "TCL Time",
            "text": "TCL Time de 16:00 à 18:00",
        }
    )
    reg = build_registry(rows, _DATE)
    by_id = {e["id"]: e for e in reg}
    tcl_id = next(e["id"] for e in reg if "TCL" in e["text"])
    assert filter_around([{"id": tcl_id, "stake": "TCL Time plus tard"}], by_id, _DATE) == []


def test_fact_fallback_humanises_timestamps_and_title_echo():
    from estormi_briefing.compose.composer import _fact_fallback

    entry = {
        "text": "(lié à: TCL) TCL Time: TCL Time "
        "2026-06-12T16:00:00+02:00 → 2026-06-12T18:00:00+02:00"
    }
    out = _fact_fallback(entry)
    assert "2026-06-12T16" not in out
    assert "(lié à" not in out
    assert out.startswith("TCL Time — 12 Jun 16h00")


def test_news_only_nouns_and_leak_detection():
    from estormi_briefing.compose.composer import _noun_leaks, news_only_nouns

    reg = build_registry(_rows(), _DATE)
    add_news_entries(
        reg,
        "- L'Iran bloque le détroit d'Ormuz, riposte de Donald Trump "
        "[SOURCE: hugo decrypte | 2026-06-12]",
    )
    nouns = news_only_nouns(reg, trusted_text="profil: tech lead chez Air Liquide")
    assert "ormuz" in nouns and "trump" in nouns
    assert "hugo" not in nouns  # [SOURCE: …] markers never count as content
    leaks = _noun_leaks(
        "intégrer les tensions autour d'Ormuz au point Data Lake", nouns, "Daily Data Lake"
    )
    assert leaks == ["ormuz"]
    assert _noun_leaks("le point Data Lake à 9h45", nouns, "") == []


async def test_compose_vision_neutralises_news_in_angle_objective_and_stake():
    rows = _rows()
    writer_prompts: list[str] = []

    async def fake_llm(prompt: str, **kw) -> str:
        if "json_schema" in kw:
            return json.dumps(
                {
                    "objective": "Aligner la journée sur les tensions à Ormuz.",
                    "myday_threads": [
                        {"ids": ["A1", "A2", "A3", "R1"], "angle": "intégrer Ormuz à la revue"}
                    ],
                    "around": [
                        {"id": "C1", "stake": "Firebase et l'accord de Trump sur les workspaces"}
                    ],
                }
            )
        if kw.get("max_tokens") == 220:
            writer_prompts.append(prompt)
        return "Paragraphe factuel sans heure inventée."

    out = await compose_vision(
        _DATE,
        rows,
        fake_llm,
        news_digest="- L'Iran bloque le détroit d'Ormuz, riposte de Donald Trump [SOURCE: x | 2026-06-11]",
    )
    # The news-bearing objective is not promoted (its fallback angle leaked too).
    assert "Ormuz" not in out and "Trump" not in out
    # The contaminated angle never reached a writer.
    assert all("Ormuz" not in p.split("ANGLE")[-1] for p in writer_prompts)
    # The stake fell back to the row's own fact.
    assert "Firebase: plus de nouveaux workspaces" in out


async def test_compose_vision_rejects_stake_naming_another_entry():
    rows = _rows()

    async def fake_llm(prompt: str, **kw) -> str:
        if "json_schema" in kw:
            return json.dumps(
                {
                    "objective": "x",
                    "myday_threads": [{"ids": ["A1", "A2", "A3", "R1"], "angle": "a"}],
                    "around": [
                        {
                            "id": "C1",
                            # Fabricated relation: the Firebase stake names the
                            # unrelated "Audit cloud" calendar event.
                            "stake": "échéance Firebase à confirmer après l'Audit cloud (2/2)",
                        }
                    ],
                }
            )
        return "Paragraphe factuel sans heure inventée."

    out = await compose_vision(_DATE, rows, fake_llm)
    assert "à confirmer après" not in out
    assert "Firebase: plus de nouveaux workspaces" in out


def test_decay_seen_around_retires_stale_periphery(monkeypatch, tmp_path):
    monkeypatch.setenv("ESTORMI_DATA_DIR", str(tmp_path))
    from estormi_briefing.compose.composer import _decay_seen_around

    far = {"entry": {"text": "Firebase migration mars 2027", "deadline_iso": "2027-03-22"}}
    near = {"entry": {"text": "Famileo à finaliser", "deadline_iso": "2026-06-14"}}
    # Two showings pass, the third (a later day) is retired…
    assert _decay_seen_around([far], "2026-06-10") == [far]
    assert _decay_seen_around([far], "2026-06-11") == [far]
    assert _decay_seen_around([far], "2026-06-12") == []
    # …but a same-day re-run reproduces the second showing (idempotent).
    assert _decay_seen_around([far], "2026-06-11") == [far]
    # An imminent deadline resurfaces regardless of showings.
    assert _decay_seen_around([near], "2026-06-10") == [near]
    assert _decay_seen_around([near], "2026-06-11") == [near]
    assert _decay_seen_around([near], "2026-06-12") == [near]


def test_stake_unsupported_words_radical_containment():
    from estormi_briefing.compose.composer import _stake_unsupported_words

    famileo = {
        "text": "Faire le Famileo (GM & BM)",
        "when": "",
        "deadline": "14 Jun",
        "label": "reminder",
    }
    # The residual confabulation class: invented purpose, generic vocabulary.
    bad = _stake_unsupported_words(
        "Le Famileo est prévu dimanche pour une mise à jour technique des systèmes domestiques",
        famileo,
    )
    assert "technique" in bad and "domestiques" in bad
    # A faithful restatement passes: row words, temporal lexicon, inflection.
    firebase = {
        "text": "Firebase: plus de nouveaux workspaces après le 22 juin",
        "when": "2026-03-19 (Thursday)",
        "deadline": "22 Jun",
        "label": "mail",
    }
    assert (
        _stake_unsupported_words(
            "Plus de nouveaux workspaces Firebase prévus après le 22 juin", firebase
        )
        == []
    )
    # Inflection tolerance: "réservations" traces back to "réserver".
    billets = {"text": "réserver les billets du voyage", "when": "", "deadline": "", "label": ""}
    assert _stake_unsupported_words("réservations des billets avant demain", billets) == []


async def test_compose_vision_replaces_editorialising_stake():
    rows = _rows()

    async def fake_llm(prompt: str, **kw) -> str:
        if "json_schema" in kw:
            return json.dumps(
                {
                    "objective": "x",
                    "myday_threads": [{"ids": ["A1", "A2", "A3", "R1"], "angle": "a"}],
                    "around": [
                        {
                            "id": "C1",
                            # No foreign noun, no cross-item title — pure
                            # invented purpose. Only the containment guard
                            # can catch it.
                            "stake": "Migration Firebase avec une liste de tâches à valider",
                        }
                    ],
                }
            )
        return "Paragraphe factuel sans heure inventée."

    out = await compose_vision(_DATE, rows, fake_llm)
    assert "liste de tâches" not in out
    assert "Firebase: plus de nouveaux workspaces" in out


def test_entry_is_past_explicit_year_never_resurrects():
    from estormi_briefing.compose.composer import _entry_is_past

    # A May mail about a May 2026 meeting is PAST on June 12 — the explicit
    # year must not be re-read as "28 mai 2027".
    e = {"date": "2026-05-10", "text": "Réunion des bénévoles du 28 mai 2026 à 19h"}
    assert _entry_is_past(e, "2026-06-12") is True
    # The year-less December→January case still survives.
    e2 = {"date": "2026-12-15", "text": "échéance le 5 janvier pour le dossier"}
    assert _entry_is_past(e2, "2026-12-20") is False


def test_fact_fallback_strips_reminder_and_mail_boilerplate():
    from estormi_briefing.compose.composer import _fact_fallback

    reminder = {
        "text": "Faire le Famileo (GM & BM): List: Reminders Title: Faire le Famileo (GM & BM) "
        "Due: 2026-06-13T22:00:00Z Status: pending"
    }
    out = _fact_fallback(reminder)
    assert "List:" not in out and "Status" not in out
    assert out.startswith("Faire le Famileo (GM & BM) — échéance 13 Jun 22h00")
    # Long text cuts at a word boundary, never mid-word.
    long = {"text": "Réunion " + "particulièrement " * 12 + "longue"}
    cut = _fact_fallback(long)
    assert len(cut) <= 141 and not cut.rstrip("…").endswith("particulièrem")


def test_fact_fallback_surfaces_deadline_segment_for_mail():
    from estormi_briefing.compose.composer import _fact_fallback

    # A past meeting's mail whose body buries a real future date — the
    # fallback must lead with the subject and surface THAT segment.
    e = {
        "label": "mail",
        "title": "Réunion des bénévoles du 28 mai 2026 à 19h",
        "deadline_iso": "2026-06-14",
        "text": "Réunion des bénévoles du 28 mai 2026 à 19h: partage d'expériences – 40 min "
        "Point planning – 5 min Pique-nique : 14 juin 2026 Ateliers – 10 min",
    }
    out = _fact_fallback(e)
    assert "Pique-nique : 14 juin 2026" in out
    assert "40 min" not in out
    # A mail with no future deadline renders its subject alone — no body tail.
    e2 = {
        "label": "mail",
        "title": "[Action Advised] Migrate Firebase Studio projects by Mar 22, 2027",
        "deadline_iso": "",
        "text": "[Action Advised] Migrate Firebase Studio projects by Mar 22, 2027: "
        "From: Firebase Subject: whatever MY CONSOLE Hi Alex, You're receiving this",
    }
    out2 = _fact_fallback(e2)
    assert out2 == "Migrate Firebase Studio projects by Mar 22, 2027"


def test_fact_fallback_collapses_title_echo_with_trailing_space():
    from estormi_briefing.compose.composer import _fact_fallback

    e = {"text": "Bar à jeux Riton : Bar à jeux Riton 2026-06-13T18:30:00+02:00"}
    assert _fact_fallback(e).startswith("Bar à jeux Riton — 13 Jun 18h30")


async def test_correlated_rows_ride_with_their_anchor_event():
    """The L-rows retrieved FOR an event must reach the writer of whichever
    thread carries that event, even when the plan dropped them — a writer can
    only state the link it was shown."""
    rows = _rows()
    rows["corr_blocks"] = [
        {
            "event": "Audit cloud (2/2)",
            "when_label": "2026-06-11 (Thursday) 15:00",
            "rows": [
                {
                    "source": "whatsapp",
                    "when_label": "2026-06-08 (Monday)",
                    "title": "Ops",
                    "text": "le créneau audit cloud de jeudi portera sur les coûts réseau",
                }
            ],
        }
    ]
    writer_prompts: list[str] = []

    async def fake_llm(prompt: str, **kw) -> str:
        if "json_schema" in kw:
            return json.dumps(
                {
                    "objective": "x",
                    # The plan selects the calendar rows but DROPS the L-row.
                    "myday_threads": [{"ids": ["A1", "A2", "A3", "R1"], "angle": "a"}],
                    "around": [],
                }
            )
        if kw.get("max_tokens") == 220:
            writer_prompts.append(prompt)
        return "Paragraphe factuel sans heure inventée."

    await compose_vision(_DATE, rows, fake_llm)
    # The correlated WhatsApp fact reached the writer of A2's thread.
    assert any("coûts réseau" in p for p in writer_prompts)


def test_paragraph_violations_catch_spelt_out_hours():
    """'À midi trente' for a 13:30 quiz shipped in production — word-hours
    must verify like numeric ones."""
    entries = [{"when": "aujourd'hui 13:30", "text": "[work] Quiz cybersécurité", "kind": "A"}]
    bad = paragraph_violations("Le quiz démarre à midi trente.", entries, _DATE)
    assert any("12:30" in v for v in bad)
    # The correct spelt-out form is accepted…
    entries14 = [{"when": "aujourd'hui 14:00", "text": "[work] Rétro", "kind": "A"}]
    assert paragraph_violations("La rétro à quatorze heures suit.", entries14, _DATE) == []
    # …idiomatic bare "midi" and durations never trip the check.
    assert paragraph_violations("Garde le créneau du midi léger.", entries14, _DATE) == []
    assert paragraph_violations("Une session de deux heures pour la rétro.", entries14, _DATE) == []


def test_fact_fallback_strips_copyright_footer():
    from estormi_briefing.compose.composer import _fact_fallback

    entry = {
        "label": "mail",
        "title": "Important notice: your account will be closed on 10 Aug",
        "text": "Important notice: your account will be closed on 10 Aug: © Coinbase 2026 | Coinbase Luxembourg S.A.",
        "deadline_iso": "",
    }
    out = _fact_fallback(entry, "French")
    assert "©" not in out and "Luxembourg" not in out
    assert "10 Aug" in out


# ── schedule-claim guard (READINESS) ──────────────────────────────────────────


def test_schedule_claim_detects_unbacked_sport_assertion():
    from estormi_briefing.compose.composer import _schedule_claim

    line = "Ta récupération est excellente, attaque ta séance de musculation prévue ce soir."
    assert _schedule_claim(line, {"planned": False}) != ""
    assert _schedule_claim(line, {"planned": True}) == ""
    assert _schedule_claim(line, None) != ""  # no advice → nothing backs the claim


def test_schedule_claim_allows_suggestion_phrasing():
    from estormi_briefing.compose.composer import _schedule_claim

    line = "Récupération 81% : le créneau de 18h est idéal pour une séance de musculation."
    assert _schedule_claim(line, {"planned": False}) == ""


async def test_readiness_schedule_claim_rewritten_then_kept():
    rows = _rows()
    rows["health_rows"] = [{"when_label": "07:00", "text": "Recovery 81%, sommeil 8h05"}]
    readiness_outputs = iter(
        [
            "READINESS: Attaque ta séance de musculation prévue sur ton créneau libre.",
            "READINESS: Récupération 81% — le créneau libre est idéal pour ta séance.",
        ]
    )

    async def fake_llm(prompt: str, **kw) -> str:
        if "json_schema" in kw:
            return json.dumps({"myday_threads": [{"ids": ["A1"], "angle": "a"}], "around": []})
        if kw.get("gbnf_grammar", "").strip().startswith('root ::= "READINESS'):
            return next(readiness_outputs)
        return "Paragraphe factuel sans heure inventée."

    out = await compose_vision(
        _DATE, rows, fake_llm, advice={"planned": False, "facts": ["récupération 81% (vert)"]}
    )
    assert "prévue" not in out.splitlines()[0]
    assert "81%" in out.splitlines()[0]


async def test_readiness_omitted_when_schedule_claim_persists():
    rows = _rows()
    rows["health_rows"] = [{"when_label": "07:00", "text": "Recovery 81%, sommeil 8h05"}]

    async def fake_llm(prompt: str, **kw) -> str:
        if "json_schema" in kw:
            return json.dumps({"myday_threads": [{"ids": ["A1"], "angle": "a"}], "around": []})
        if kw.get("gbnf_grammar", "").strip().startswith('root ::= "READINESS'):
            return "READINESS: Ta séance de sport planifiée t'attend à 18h."
        return "Paragraphe factuel sans heure inventée."

    out = await compose_vision(_DATE, rows, fake_llm, advice={"planned": False, "facts": []})
    assert "READINESS" not in out  # omitted: wrong beats missing


# ── tentative / event-type flags in the registry ─────────────────────────────


def test_registry_carries_tentative_and_event_type_flags():
    rows = {
        "calendar": [
            {"title": "Quiz cyber", "when": "13:30", "group_type": "work", "tentative": True},
            {
                "title": "Matinée absent",
                "when": "09:00",
                "group_type": "work",
                "event_type": "outOfOffice",
            },
            {"title": "Daily", "when": "09:45", "group_type": "work"},
        ]
    }
    reg = build_registry(rows, "2026-06-12")
    texts = {e["id"]: e["text"] for e in reg}
    assert "tentative" in texts["A1"] and "peut-être" in texts["A1"]
    assert "out of office" in texts["A2"]
    assert "(tentative" not in texts["A3"] and "absence" not in texts["A3"]


# ── two-quills composition ────────────────────────────────────────────────────


async def test_compose_orders_stages_for_swap_economy_and_adds_challenger():
    """READINESS + lede (plan's tier) run BEFORE the writers (other tier), and
    the lede pool carries a 'lede_alt' challenger — two model residencies per
    composition instead of four in two-quills mode."""
    rows = _rows()
    rows["health_rows"] = [{"when_label": "07:00", "text": "Recovery 66%"}]
    stages: list[str] = []

    async def fake_llm(prompt: str, **kw) -> str:
        stages.append(kw.get("stage") or ("plan" if "json_schema" in kw else "?"))
        if "json_schema" in kw:
            return json.dumps(
                {"myday_threads": [{"ids": ["A1", "A2"], "angle": "a"}], "around": []}
            )
        if kw.get("stage") == "readiness":
            return "READINESS: Base correcte à 66%, garde la journée légère."
        if kw.get("stage") in ("lede", "lede_alt"):
            return "La journée s'ouvre sur la session de 10:00."
        return "La session de 10:00 occupe la matinée."

    await compose_vision(_DATE, rows, fake_llm, bestof_n=2)
    assert "lede_alt" in stages
    first_writer = stages.index("writer")
    assert stages.index("readiness") < first_writer
    assert stages.index("lede") < first_writer
    assert stages.index("lede_alt") < first_writer


def test_strain_conflict_flags_high_claim_on_rest_day():
    from estormi_briefing.compose.composer import _strain_conflict

    rest = "WHOOP — Sun 21 Jun. Recovery 75%. Day: strain 0.3, 680 kcal."
    # Low-strain night described as high strain / an effort to recover from.
    assert _strain_conflict("Après un strain élevé, marche légère.", rest)
    assert _strain_conflict("Gros strain hier : récupère doucement.", rest)
    # No high-strain claim, or a genuinely high day → clean.
    assert _strain_conflict("Récupération solide, gère ton énergie.", rest) == ""
    assert _strain_conflict("Strain élevé hier soir.", "Day: strain 18.8") == ""


async def test_readiness_omits_stale_day_figure():
    """A figure from an older night (98% yesterday) must not read as today's
    (75%): it is a phantom and, if the model insists, the steer is dropped."""
    from estormi_briefing.compose.composer import _write_readiness

    rows = {
        "health_rows": [
            "WHOOP — Sun 21 Jun. Recovery 75%, sommeil 7h57. Day: strain 0.3.",
            "WHOOP — Sat 20 Jun. Recovery 98%, sommeil 7h38. Day: strain 18.8.",
        ]
    }

    async def cites_yesterday(prompt, **kw):
        return "READINESS: Récupération à 98 % : pousse fort aujourd'hui."

    assert await _write_readiness(cites_yesterday, rows, "French") == ""


async def test_readiness_keeps_latest_day_figure():
    from estormi_briefing.compose.composer import _write_readiness

    rows = {
        "health_rows": [
            "WHOOP — Sun 21 Jun. Recovery 75%, sommeil 7h57. Day: strain 0.3.",
            "WHOOP — Sat 20 Jun. Recovery 98%, sommeil 7h38. Day: strain 18.8.",
        ]
    }

    async def cites_today(prompt, **kw):
        return "READINESS: Récupération à 75 % : gère ton énergie en réunion."

    out = await _write_readiness(cites_today, rows, "French")
    assert "75" in out and "98" not in out
