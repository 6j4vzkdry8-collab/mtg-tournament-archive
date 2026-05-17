"""Smoke test: fetch real-world tournament + deck and validate schema.

Network access required, no OSS write. Local run:
    pip install -r requirements.txt
    pytest tests/test_fetch_smoke.py -v -s

Also serves as an early signal for upstream layout changes - if the two
endpoints still return the expected shape, pytest passes.
"""
import pytest
import requests

from publisher.fetch_deck import fetch_component_cards, fetch_widget
from publisher.fetch_listing import (
    get_decks,
    get_tournament_info,
    get_tournaments,
)
from publisher.publish import _build_session

# A known-real deck id; using an older one so it stays available even on
# slow publishing days.
KNOWN_DECK_ID = 7750116


@pytest.fixture(scope="module")
def session() -> requests.Session:
    s = _build_session()
    yield s
    s.close()


def test_widget_schema(session):
    out = fetch_widget(KNOWN_DECK_ID, session)
    assert isinstance(out, dict)
    assert isinstance(out.get("name"), str) and out["name"].strip()
    # player_name may be empty, but it must be a string
    assert isinstance(out.get("player_name"), str)
    assert isinstance(out.get("cover_card_name"), str)


def test_component_schema(session):
    cards = fetch_component_cards(KNOWN_DECK_ID, session)
    assert isinstance(cards, list)
    assert len(cards) > 0
    for c in cards:
        assert isinstance(c["amount"], int) and c["amount"] > 0
        assert isinstance(c["card_name_en"], str) and c["card_name_en"].strip()
        assert c["is_main"] in (0, 1)
    # At least one main-board entry must exist
    assert any(c["is_main"] == 1 for c in cards)


def test_listing_schema(session):
    tours = get_tournaments("standard", session)
    assert isinstance(tours, list)
    assert len(tours) > 0, "/tournaments/standard returned empty list (layout change?)"
    for t in tours:
        assert isinstance(t["link"], str) and t["link"].startswith("/tournament/")
        assert t["format"] == "standard"


def test_tournament_info_schema(session):
    # Use the first real tournament from the listing to drive detail parsing
    tours = get_tournaments("standard", session)
    assert tours, "no listing data to test"
    info = get_tournament_info(tours[0]["link"], session)
    assert isinstance(info["tournament_name"], str) and info["tournament_name"].strip()
    assert isinstance(info["date"], str) and len(info["date"]) == 10  # YYYY-MM-DD
    assert info["format"] in (
        "standard",
        "modern",
        "legacy",
        "pioneer",
        "pauper",
        "vintage",
        "alchemy",
        "explorer",
        "historic",
        "timeless",
        "premodern",
        "penny dreadful",
        "",
    )
    assert info["source_tour_id"].isdigit()


def test_get_decks_schema(session):
    tours = get_tournaments("standard", session)
    assert tours
    decks = get_decks(tours[0]["link"], session)
    assert isinstance(decks, list)
    assert len(decks) > 0
    for d in decks:
        assert d["link"].startswith("/deck/")
        # Either win/loss mode or place mode
        assert ("win" in d and "loss" in d) or "place" in d
