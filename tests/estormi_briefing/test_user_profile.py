"""Tests for the live user profile (auto-observed section + grounding)."""

from __future__ import annotations

import json

import pytest

from estormi_briefing.compose.user_profile import (
    AUTO_MARKERS,
    MAX_PROFILE_CHARS,
    auto_marker,
    distinctive_tokens,
    impact_grounded,
    merge_profile,
    propose_observations,
    split_profile,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# split / merge
# --------------------------------------------------------------------------


def test_auto_marker_per_language():
    assert auto_marker("fr") == AUTO_MARKERS[1]
    assert auto_marker("en") == AUTO_MARKERS[0]
    assert auto_marker("de") == AUTO_MARKERS[0]


@pytest.mark.parametrize("lang", ["fr", "en"])
def test_split_merge_round_trip(lang):
    user_text = "Je suis prof de maths.\nJ'attends un briefing concis."
    observations = ["Tennis le mardi avec Marc", "Projet de rénovation en cours"]

    merged = merge_profile(user_text, observations, lang)
    assert auto_marker(lang) in merged

    user_part, auto_part = split_profile(merged)
    assert user_part.strip() == user_text
    assert auto_part.strip() == ("- Tennis le mardi avec Marc\n- Projet de rénovation en cours")
    # The marker line itself never leaks into either part.
    for marker in AUTO_MARKERS:
        assert marker not in user_part
        assert marker not in auto_part


def test_split_without_marker_returns_text_unchanged():
    text = "Just my own prose, no auto section."
    assert split_profile(text) == (text, "")


def test_split_uses_first_marker_line_only():
    text = f"mine\n{AUTO_MARKERS[0]}\n- obs one\n{AUTO_MARKERS[1]}\n- obs two"
    user_part, auto_part = split_profile(text)
    assert user_part == "mine"
    # Everything below the FIRST marker is the auto part, later markers included.
    assert "- obs one" in auto_part
    assert AUTO_MARKERS[1] in auto_part


def test_merge_empty_observations_keeps_user_part_unchanged():
    assert merge_profile("My prose.\n\n", [], "en") == "My prose."
    assert auto_marker("en") not in merge_profile("My prose.", [], "en")


def test_merge_cap_drops_from_the_end_never_cuts_user_part():
    user_text = "U" * (MAX_PROFILE_CHARS - 120)
    observations = ["first observation kept maybe", "second observation " + "x" * 100]

    merged = merge_profile(user_text, observations, "en")
    assert len(merged) <= MAX_PROFILE_CHARS
    assert user_text in merged  # never cut
    assert "first observation kept maybe" in merged
    assert "second observation" not in merged  # dropped from the END


def test_merge_oversized_user_part_returned_whole():
    user_text = "U" * (MAX_PROFILE_CHARS + 500)
    merged = merge_profile(user_text, ["obs"], "fr")
    assert merged == user_text  # user prose is never cut, even over the cap
    assert auto_marker("fr") not in merged


# --------------------------------------------------------------------------
# propose_observations
# --------------------------------------------------------------------------

_SIGNALS = [
    "Tennis avec Marc mardi soir au club",
    "Réunion projet rénovation cuisine, devis reçu",
    "Course à pied samedi matin, 10km",
]


async def test_propose_happy_path():
    seen_prompts: list[str] = []

    async def fake_llm(prompt: str, **kw) -> str:
        seen_prompts.append(prompt)
        assert kw["json_schema"]["required"] == ["observations"]
        return json.dumps(
            {
                "observations": [
                    "Joue au tennis avec Marc le mardi",
                    "Projet de rénovation de cuisine en cours",
                ]
            }
        )

    out = await propose_observations(fake_llm, "Je suis prof.", "", _SIGNALS, language="French")
    assert out == [
        "Joue au tennis avec Marc le mardi",
        "Projet de rénovation de cuisine en cours",
    ]
    # The prompt carries the three inputs the model needs.
    assert "Je suis prof." in seen_prompts[0]
    assert "Tennis avec Marc mardi soir au club" in seen_prompts[0]
    assert "French" in seen_prompts[0]


async def test_propose_returns_empty_on_bad_json():
    async def fake_llm(prompt: str, **kw) -> str:
        return "not json at all"

    assert await propose_observations(fake_llm, "", "", _SIGNALS) == []


async def test_propose_returns_empty_on_llm_exception():
    async def fake_llm(prompt: str, **kw) -> str:
        raise TimeoutError("decode stalled")

    assert await propose_observations(fake_llm, "", "", _SIGNALS) == []


async def test_propose_drops_ungrounded_observation():
    async def fake_llm(prompt: str, **kw) -> str:
        return json.dumps(
            {
                "observations": [
                    "Joue au tennis avec Marc le mardi",
                    "Collectionne les timbres anciens",  # never in the signals
                ]
            }
        )

    out = await propose_observations(fake_llm, "", "", _SIGNALS)
    assert out == ["Joue au tennis avec Marc le mardi"]


async def test_propose_dedupes_case_and_whitespace_insensitively():
    async def fake_llm(prompt: str, **kw) -> str:
        return json.dumps(
            {
                "observations": [
                    "Joue au tennis avec Marc",
                    "joue  au TENNIS avec marc",
                    "Court 10km le samedi",
                ]
            }
        )

    out = await propose_observations(fake_llm, "", "", _SIGNALS)
    assert out == ["Joue au tennis avec Marc", "Court 10km le samedi"]


async def test_propose_drops_overlong_observation():
    long_obs = "tennis " * 30  # grounded but > 160 chars

    async def fake_llm(prompt: str, **kw) -> str:
        return json.dumps({"observations": [long_obs, "Tennis le mardi"]})

    assert await propose_observations(fake_llm, "", "", _SIGNALS) == ["Tennis le mardi"]


# --------------------------------------------------------------------------
# distinctive_tokens / impact_grounded
# --------------------------------------------------------------------------


def test_distinctive_tokens_keeps_digit_bearing_tokens():
    tokens = distinctive_tokens("Préparer le GR20 avec un sac gr200 cette semaine")
    assert "gr200" in tokens  # digit-bearing, >=2 chars
    assert "gr20" in tokens
    assert "préparer" in tokens
    assert "avec" not in tokens  # stopword
    assert "cette" not in tokens  # stopword
    assert "semaine" not in tokens  # stopword
    assert "sac" not in tokens  # short, no digit
    assert "le" not in tokens


def test_impact_grounded_prefix_matching_positive_and_negative():
    profile_tokens = distinctive_tokens("Je veux investir régulièrement en bourse.")
    # 'investissements' matches 'investir' on prefix-6 ('invest').
    assert impact_grounded("Les investissements européens progressent fortement", profile_tokens)
    # No shared distinctive vocabulary: invented hook.
    assert not impact_grounded("La météo perturbe les récoltes de café au Brésil", profile_tokens)
