"""Deterministic correlation-graph spine for the briefing.

These tests pin the *correctness invariant* the graph exists to enforce:
facts only fuse into a thread when they share a curated anchor within a date
window, and never on a coincidental shared word. This is the property the old
prompt-only approach could not test.
"""

from __future__ import annotations

import pytest

import estormi_briefing.compose.graph as bg

pytestmark = pytest.mark.unit


# ── Lexicon ──────────────────────────────────────────────────────────────────


class TestLexicon:
    def test_harvests_wa_labels_partner_and_extra(self):
        lex = bg.build_lexicon(["Hédy", "Alice & Bob"], partner_name="Marie", extra=["Jean Dupont"])
        assert {"hédy", "alice", "bob", "marie", "jean dupont"} <= lex

    def test_drops_single_letter_noise(self):
        assert bg.build_lexicon(["A", "Bo"]) == {"bo"}

    def test_splits_compound_labels(self):
        assert bg.build_lexicon(["Hédy, Marc"]) == {"hédy", "marc"}


# ── Anchor matching ──────────────────────────────────────────────────────────


class TestPeopleMatching:
    def test_matches_known_name_in_text(self):
        f = bg.collect_facts(
            day="2026-06-03",
            calendar=[
                {"title": "Déjeuner avec Hédy", "when": "12:30", "date_ts": "2026-06-03T12:30:00Z"}
            ],
            reminders=[],
            wa_items=[],
            context_rows=[],
            lexicon={"hédy"},
        )
        assert f[0].people == frozenset({"hédy"})

    def test_no_coincidental_substring_match(self):
        # "marc" must not fire inside "marché" — matching is token-based.
        f = bg.collect_facts(
            day="2026-06-03",
            calendar=[
                {"title": "Aller au marché", "when": "10:00", "date_ts": "2026-06-03T10:00:00Z"}
            ],
            reminders=[],
            wa_items=[],
            context_rows=[],
            lexicon={"marc"},
        )
        assert f[0].people == frozenset()


# ── Topic terms ──────────────────────────────────────────────────────────────


class TestTopicTerms:
    def test_harvests_salient_title_tokens(self):
        terms = bg.build_topic_terms(["Déclaration impôts", "Runner v2 w/ Alex"])
        assert "impôts" in terms
        assert "déclaration" in terms
        assert "runner" in terms

    def test_drops_months_stopwords_and_short_tokens(self):
        terms = bg.build_topic_terms(["Réunion en août avec v2 data"])
        assert "août" not in terms  # month — the anti-fusion guard
        assert "avec" not in terms  # stopword
        assert "réunion" not in terms  # generic filler
        assert "v2" not in terms  # too short
        assert "data" in terms

    def test_excludes_known_person_names(self):
        terms = bg.build_topic_terms(["Dîner Camille impôts"], exclude={"Camille"})
        assert "camille" not in terms
        assert "impôts" in terms

    def test_mines_extractor_phrases(self):
        terms = bg.build_topic_terms(
            [], extra_texts=["vérifier le traitement des revenus mobiliers"]
        )
        assert "revenus" in terms
        assert "mobiliers" in terms


# ── Thread formation ─────────────────────────────────────────────────────────


def _facts_two_sources_same_person():
    return bg.collect_facts(
        day="2026-06-03",
        calendar=[{"title": "Dîner avec Hédy", "when": "20:00", "date_ts": "2026-06-03T20:00:00Z"}],
        reminders=[],
        wa_items=[{"label": "Hédy", "text": "je ramène le magret", "date": "2026-06-02"}],
        context_rows=[],
        lexicon={"hédy"},
    )


class TestBuildThreads:
    def test_links_calendar_and_whatsapp_on_shared_person(self):
        threads = bg.build_threads(_facts_two_sources_same_person())
        assert len(threads) == 1
        assert threads[0].is_cross_source
        assert threads[0].sources == {"calendar", "whatsapp"}

    def test_topic_anchor_links_calendar_to_message(self):
        # The real "impôts" case: the calendar names the SUBJECT ("Déclaration
        # impôts"), not the contact, and the WhatsApp tail shares only the topic
        # word — person-only anchors miss it; the topic anchor catches it.
        terms = bg.build_topic_terms(["Déclaration impôts"])
        facts = bg.collect_facts(
            day="2026-06-03",
            calendar=[
                {"title": "Déclaration impôts", "when": "19:00", "date_ts": "2026-06-03T19:00:00Z"}
            ],
            reminders=[],
            wa_items=[
                {
                    "label": "Camille",
                    "text": "il y a un écart sur les impôts à vérifier",
                    "date": "2026-06-02",
                }
            ],
            context_rows=[],
            lexicon={"camille"},
            topic_terms=terms,
        )
        threads = bg.build_threads(facts)
        assert len(threads) == 1
        assert threads[0].sources == {"calendar", "whatsapp"}
        assert "topic:impôts" in threads[0].anchors

    def test_mail_does_not_topic_link(self):
        # A newsletter mentioning a calendar topic word ("data") must NOT fuse
        # with the event — mail is too noisy for bare topic anchors (it can only
        # join a thread via a person/place). This kills the LinkedIn/FDJ spam
        # threads seen on real data.
        terms = bg.build_topic_terms(["Daily : Data Lake"])
        facts = bg.collect_facts(
            day="2026-06-03",
            calendar=[
                {"title": "Daily : Data Lake", "when": "09:30", "date_ts": "2026-06-03T09:30:00Z"}
            ],
            reminders=[],
            wa_items=[],
            context_rows=[
                {
                    "source": "mail",
                    "title": "Votre casse-tête est disponible",
                    "text": "Découvrez nos offres Data Engineer",
                    "date": "2026-06-03",
                }
            ],
            lexicon=set(),
            topic_terms=terms,
        )
        assert bg.build_threads(facts) == []

    def test_unknown_conversation_placeholder_never_anchors(self):
        # The WhatsApp connector labels every unresolvable chat
        # "unknown conversation" (prompts._conversation_label). It is alphabetic
        # and so would pass _is_real_name, but it names no one — if it became a
        # shared person-anchor, two unrelated unresolved chats would fuse into one
        # bogus thread (finding #8). It must enter neither the lexicon nor a fact.
        assert bg.build_lexicon(["unknown conversation"]) == set()
        facts = bg.collect_facts(
            day="2026-06-03",
            calendar=[],
            reminders=[],
            wa_items=[
                {
                    "label": "unknown conversation",
                    "text": "on se voit demain",
                    "date": "2026-06-03",
                },
                {
                    "label": "unknown conversation",
                    "text": "la facture EDF est en retard",
                    "date": "2026-06-03",
                },
            ],
            context_rows=[],
            lexicon=bg.build_lexicon(["unknown conversation"]),
        )
        assert all(not f.anchors() for f in facts)
        assert bg.build_threads(facts) == []

    def test_masked_phone_never_anchors(self):
        # An unresolved WhatsApp contact ("+33∙∙∙∙∙15") must not become a person
        # anchor — the thread is carried by the topic, the phone stays out of it.
        terms = bg.build_topic_terms(["Déclaration impôts"])
        facts = bg.collect_facts(
            day="2026-06-03",
            calendar=[
                {"title": "Déclaration impôts", "when": "19:00", "date_ts": "2026-06-03T19:00:00Z"}
            ],
            reminders=[],
            wa_items=[
                {"label": "+33∙∙∙∙∙15", "text": "écart sur les impôts", "date": "2026-06-03"}
            ],
            context_rows=[],
            lexicon=set(),
            topic_terms=terms,
        )
        threads = bg.build_threads(facts)
        assert len(threads) == 1
        assert not any(a.startswith("person:") for a in threads[0].anchors)

    def test_month_word_never_links(self):
        # Two facts sharing only the month "août" must NOT fuse — the original
        # cross-source-fusion bug. "août" is excluded from topic terms.
        terms = bg.build_topic_terms(["Plan week-end Saint-Jacut", "Deadline rapport"])
        facts = bg.collect_facts(
            day="2026-06-03",
            calendar=[
                {"title": "Deadline rapport", "when": "All day", "date_ts": "2026-08-29T00:00:00Z"}
            ],
            reminders=[],
            wa_items=[
                {
                    "label": "Hédy",
                    "text": "le week-end à Saint-Jacut fin août ça tient",
                    "date": "2026-08-28",
                }
            ],
            context_rows=[],
            lexicon={"hédy"},
            topic_terms=terms,
        )
        assert bg.build_threads(facts) == []

    def test_no_link_without_shared_anchor(self):
        # Same day, but the two facts share no known person — date alone is not
        # a link (the "coincidental August" fusion the briefing must avoid).
        facts = bg.collect_facts(
            day="2026-06-03",
            calendar=[
                {"title": "Réunion budget", "when": "09:00", "date_ts": "2026-06-03T09:00:00Z"}
            ],
            reminders=[],
            wa_items=[{"label": "Hédy", "text": "on se voit quand?", "date": "2026-06-03"}],
            context_rows=[],
            lexicon={"hédy"},
        )
        assert bg.build_threads(facts) == []

    def test_bare_date_not_shifted_west_of_utc(self, monkeypatch):
        # A bare YYYY-MM-DD is a local calendar date. In a timezone west of UTC,
        # routing it through parse_iso (UTC midnight) + astimezone used to roll it
        # back a day, so a same-day WhatsApp item fell outside the window of the
        # day's calendar event. graph.py binds LOCAL_TZ by value at import, so we
        # patch the name inside the graph module, not estormi_briefing.day.day.
        from zoneinfo import ZoneInfo

        monkeypatch.setattr(bg, "LOCAL_TZ", ZoneInfo("America/Los_Angeles"))
        # The bare date must localise to itself, never to the day before.
        assert bg._local_date("2026-06-03") == "2026-06-03"
        # End-to-end: bare-date WhatsApp + same-day calendar event must still fuse
        # under the tightest window, which only holds if the bare date is not
        # shifted off 2026-06-03.
        facts = bg.collect_facts(
            day="2026-06-03",
            calendar=[
                {"title": "Dîner avec Hédy", "when": "20:00", "date_ts": "2026-06-03T20:00:00Z"}
            ],
            reminders=[],
            wa_items=[{"label": "Hédy", "text": "je ramène le magret", "date": "2026-06-03"}],
            context_rows=[],
            lexicon={"hédy"},
        )
        threads = bg.build_threads(facts, window_days=0)
        assert len(threads) == 1
        assert threads[0].anchors == {"person:hédy"}

    def test_date_window_excludes_far_apart_facts(self):
        facts = bg.collect_facts(
            day="2026-06-03",
            calendar=[
                {"title": "Dîner avec Hédy", "when": "20:00", "date_ts": "2026-06-03T20:00:00Z"}
            ],
            reminders=[],
            wa_items=[{"label": "Hédy", "text": "joyeux anniversaire!", "date": "2026-05-01"}],
            context_rows=[],
            lexicon={"hédy"},
        )
        assert bg.build_threads(facts, window_days=3) == []

    def test_singletons_dropped(self):
        facts = bg.collect_facts(
            day="2026-06-03",
            calendar=[
                {"title": "Dîner avec Hédy", "when": "20:00", "date_ts": "2026-06-03T20:00:00Z"}
            ],
            reminders=[],
            wa_items=[],
            context_rows=[],
            lexicon={"hédy"},
        )
        assert bg.build_threads(facts) == []

    def test_dominant_thread_is_most_cross_source(self):
        # Thread A: 3 sources on Hédy. Thread B: 2 sources on Marc. A wins.
        facts = bg.collect_facts(
            day="2026-06-03",
            calendar=[
                {"title": "Dîner avec Hédy", "when": "20:00", "date_ts": "2026-06-03T20:00:00Z"},
                {"title": "Café avec Marc", "when": "15:00", "date_ts": "2026-06-03T15:00:00Z"},
            ],
            reminders=[
                {
                    "title": "Confirmer le resto à Hédy",
                    "when": "",
                    "date_ts": "2026-06-03T09:00:00Z",
                }
            ],
            wa_items=[
                {"label": "Hédy", "text": "je ramène le magret", "date": "2026-06-03"},
                {"label": "Marc", "text": "à 15h ça marche", "date": "2026-06-03"},
            ],
            context_rows=[],
            lexicon={"hédy", "marc"},
        )
        threads = bg.build_threads(facts)
        assert len(threads) == 2
        assert threads[0].anchors == {"person:hédy"}
        assert len(threads[0].sources) == 3


# ── Rendering ────────────────────────────────────────────────────────────────


class TestRenderThreads:
    def test_renders_cross_source_only_and_flags_dominant(self):
        rows = bg.render_threads(bg.build_threads(_facts_two_sources_same_person()))
        assert len(rows) == 1
        assert rows[0]["dominant"] is True
        assert rows[0]["anchor"] == "Hédy"
        assert set(rows[0]["sources"]) == {"calendar", "whatsapp"}

    def test_empty_when_no_threads(self):
        assert bg.render_threads([]) == []
