"""mtggoldfish v2 endpoint scrapers: widget + component.

Two endpoints make up a complete deck record without depending on cookies:

    GET /widgets/deck/js?deckId={id}
        - reachable from any IP without cookies
        - body is JS: elem.innerHTML = "<HTML containing deck title + author +
          deck-representative-cards-container>";
        - has the full deck name + player + 3 representative card names
        - does *not* contain the full card list (only 3 representative images)

    GET /deck/component?id={id}&type=paper&deck=&selector=%23deck-{id}-tab-paper&view_mode_override=
        - requires x-requested-with: XMLHttpRequest + referer, otherwise 422
        - reachable from any IP without cookies
        - has the full main + sideboard card table (in render order, with
          Sideboard appearing after main)
        - the full card name lives in <span class='card_name'>/<a> text nodes
          (note: data-card-name truncates apostrophes, "Urza's Cave" becomes
          "Urza")

Dependencies: requests + lxml + a caller-supplied requests.Session.
"""
import re

import requests
from lxml import etree

DEFAULT_TIMEOUT = (10, 30)


class GoldfishV2Error(RuntimeError):
    """Generic v2 fetch error (parse failure / HTTP error / empty body)."""


# ---------------------------------------------------------------------------
# JS string -> HTML unescape
# ---------------------------------------------------------------------------
def _unescape_js_string(raw: str) -> str:
    """Unescape JS string literals back to real HTML.

    JS source uses \\' \\" \\/ \\n etc. for these chars; we reverse them.

    Important: do NOT call html.unescape() here. HTML entities must be left
    for lxml to handle. Otherwise we break attribute quoting:

        aria-label='Image of Artist&#39;s Talent'
            -> html.unescape() turns &#39; into '
        aria-label='Image of Artist's Talent'    <- this ' closes the attribute early
            -> lxml parser
        aria-label = "Image of Artist"           <- wrong!

    Leaving entities for lxml: lxml unescapes them when accessing attribute
    or text values, so cover_card_name / card names come out as the correct
    "Artist's Talent" / "Summoner's Pact".
    """
    return (
        raw
        .replace(r"\'", "'")
        .replace(r'\"', '"')
        .replace(r'\/', '/')
        .replace(r'\n', '\n')
        .replace(r'\t', '\t')
    )


def _extract_inner_html(js_body: str) -> str:
    """Pull the elem.innerHTML = '...'; payload out of the widget / component
    JS response body. widget uses double quotes, component uses single; try
    both.
    """
    for quote in ("'", '"'):
        # non-greedy + DOTALL because the innerHTML literal contains real \n
        m = re.search(
            r"elem\.innerHTML\s*=\s*"
            + re.escape(quote)
            + r"(.*?)"
            + re.escape(quote)
            + r"\s*;\s*\n",
            js_body,
            re.DOTALL,
        )
        if m:
            return _unescape_js_string(m.group(1))
    raise GoldfishV2Error("can not find elem.innerHTML in JS response")


# ---------------------------------------------------------------------------
# widget endpoint: deck title + player + cover
# ---------------------------------------------------------------------------
def fetch_widget(deck_id: int, session: requests.Session) -> dict:
    """GET /widgets/deck/js?deckId=X, parse out deck name / player_name /
    cover_card_name.

    @return {"name", "player_name", "cover_card_name"}
    @raise GoldfishV2Error
    """
    url = f"https://www.mtggoldfish.com/widgets/deck/js?deckId={deck_id}"
    try:
        resp = session.get(
            url,
            timeout=DEFAULT_TIMEOUT,
            # widget endpoint requires Accept: */* (Rails enforces strict
            # mime matching; the session default of text/html gets a 406)
            headers={"Accept": "*/*"},
        )
    except requests.RequestException as e:
        raise GoldfishV2Error(f"widget GET failed for deck {deck_id}: {e}") from e
    if resp.status_code != 200:
        raise GoldfishV2Error(f"widget HTTP {resp.status_code} for deck {deck_id}")
    if not resp.text:
        raise GoldfishV2Error(f"widget empty body for deck {deck_id}")

    html = _extract_inner_html(resp.text)
    dom = etree.HTML(html)
    if dom is None:
        raise GoldfishV2Error(f"widget html unparseable for deck {deck_id}")

    title_nodes = dom.xpath('//h1[@class="title"]/text()')
    if not title_nodes:
        raise GoldfishV2Error(f"widget missing h1.title for deck {deck_id}")
    name = title_nodes[0].strip()
    if not name:
        # widget title is "\n<deck name>\n" with whitespace lines
        name = " ".join(t.strip() for t in title_nodes if t.strip())
    if not name:
        raise GoldfishV2Error(f"widget h1.title is empty for deck {deck_id}")

    author_nodes = dom.xpath('//h1[@class="title"]/span[@class="author"]/text()')
    player_name = author_nodes[0][3:].strip() if author_nodes else ""

    cover_nodes = dom.xpath(
        '//div[@class="deck-representative-cards-container"]/div[@role="img"]/@aria-label'
    )
    # cover_nodes look like ['Image of Amulet of Vigor', ...]; take the first
    cover_card_name = ""
    if cover_nodes:
        cover_card_name = cover_nodes[0].removeprefix("Image of ").strip()

    return {
        "name": name,
        "player_name": player_name,
        "cover_card_name": cover_card_name,
    }


# ---------------------------------------------------------------------------
# component endpoint: full cards table
# ---------------------------------------------------------------------------
def fetch_component_cards(deck_id: int, session: requests.Session) -> list[dict]:
    """GET /deck/component?...&type=paper&..., parse out the full main +
    sideboard card list.

    Returns [{"amount": N, "card_name_en": str, "is_main": 1 or 0}, ...] in
    page order (main first, Sideboard / Companion after).
    """
    url = (
        "https://www.mtggoldfish.com/deck/component"
        f"?id={deck_id}&type=paper&deck=&selector=%23deck-{deck_id}-tab-paper"
        "&view_mode_override="
    )
    try:
        resp = session.get(
            url,
            timeout=DEFAULT_TIMEOUT,
            headers={
                # The component endpoint requires three headers:
                #   - X-Requested-With + Referer: otherwise 422
                #   - Accept: */*: the body is JS, not text/html, and the
                #     session default of text/html gets a 406
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"https://www.mtggoldfish.com/deck/{deck_id}",
                "Accept": "*/*",
            },
        )
    except requests.RequestException as e:
        raise GoldfishV2Error(f"component GET failed for deck {deck_id}: {e}") from e
    if resp.status_code != 200:
        raise GoldfishV2Error(f"component HTTP {resp.status_code} for deck {deck_id}")
    if not resp.text:
        raise GoldfishV2Error(f"component empty body for deck {deck_id}")

    html = _extract_inner_html(resp.text)
    dom = etree.HTML(html)
    if dom is None:
        raise GoldfishV2Error(f"component html unparseable for deck {deck_id}")

    # Section names that count as non-main (sideboard-like). Everything else
    # (Creatures / Spells / Artifacts / Enchantments / Planeswalkers / Lands /
    # Battles / Tribal / and the actual mainboard rollup) is treated as main.
    #
    # Important: the component view sometimes lists Companion *before* main
    # ("Companion (1)" -> "Creatures (N)" -> ... -> "Lands (N)" -> "Sideboard
    # (N)"). A simple state machine that flips is_main false as soon as it
    # sees any sideboard/companion header would mark the entire mainboard as
    # sideboard, leading to PK collisions on (d_id, c_id, is_main) when the
    # real Sideboard contains the same card later. Instead, decide is_main
    # for each section header independently from its label.
    SIDEBOARD_HEADER_KEYWORDS = ("sideboard", "companion", "maybeboard", "wishboard")

    cards: list[dict] = []
    # Before any category header is seen, default to main (defensive)
    is_main = True
    for tr in dom.xpath('//tr'):
        cls = tr.get("class") or ""
        if "deck-category-header" in cls:
            # e.g. " Creatures (10) ", " Companion (1) ", " Sideboard (15) including Companion "
            hdr_text = " ".join(tr.itertext()).lower()
            is_main = not any(kw in hdr_text for kw in SIDEBOARD_HEADER_KEYWORDS)
            continue
        qty = tr.get("data-card-quantity")
        if not qty:
            continue
        # Do NOT use data-card-name: mtggoldfish truncates apostrophes
        # ("Urza's Cave" becomes "Urza"). The full name lives in
        # span.card_name > a text.
        name_a = tr.xpath('.//span[contains(@class,"card_name")]/a/text()')
        if name_a:
            name = name_a[0].strip()
        else:
            # fallback: a.data-card-id with trailing " [SET]" stripped
            a_id = tr.xpath('.//a[@data-card-id]/@data-card-id')
            if a_id:
                name = re.sub(r"\s*\[[^\]]+\]\s*$", "", a_id[0]).strip()
            else:
                name = (tr.get("data-card-name") or "").strip()
        if not name:
            continue
        cards.append(
            {
                "amount": int(qty),
                "card_name_en": name,
                "is_main": 1 if is_main else 0,
            }
        )
    if not cards:
        raise GoldfishV2Error(f"component parsed 0 cards for deck {deck_id}")
    return cards
