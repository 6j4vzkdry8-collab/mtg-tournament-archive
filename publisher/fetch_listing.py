"""mtggoldfish listing & tournament page parsers.

Three public functions:
  - get_tournaments(fmat, session): hit /tournaments/<fmat>, return the most
    recent ~10 tournament refs (link + format).
  - get_tournament_info(link, session): hit /tournament/<id>, return metadata
    (tournament_name / date / format / source_tour_id).
  - get_decks(link, session): hit /tournament/<id>, return list of deck refs
    (with win/loss or place); follows next page automatically.

Dependencies: requests + lxml + a caller-supplied requests.Session.
"""
import time

import requests
from lxml import etree


class GoldfishPageFormatError(RuntimeError):
    """The page returned by mtggoldfish does not match the expected layout
    (404 / redesign / blocking / rate limit).
    """


DEFAULT_TIMEOUT = (10, 30)
NEXT_PAGE_SLEEP = 3.0


def get_tournaments(fmat: str, session: requests.Session) -> list[dict]:
    """
    return:
        [
            {'link': '/tournament/63818', 'format': 'standard'},
            {'link': '/tournament/63812', 'format': 'standard'},
            ...
        ]
    """
    url = "https://www.mtggoldfish.com/tournaments/%s#paper" % fmat
    resp = session.get(url, timeout=DEFAULT_TIMEOUT)
    resp.close()
    if resp.status_code != 200:
        raise GoldfishPageFormatError(
            "unexpected status %s for /tournaments/%s" % (resp.status_code, fmat)
        )
    # Layout note: there is sometimes an extra <div class='tournaments-recent-events'>
    # wrapper between main/div and similar-events-container. Use a relative
    # contains() xpath so an extra layer or extra modifier class doesn't break.
    tournaments = etree.HTML(resp.text).xpath(
        "//div[contains(concat(' ', normalize-space(@class), ' '),"
        " ' similar-events-container ')]/h4"
    )
    return [
        {
            "link": tour.xpath("./a[1]/@href")[0],
            "format": fmat,
        }
        for tour in tournaments
    ]


def get_tournament_info(link: str, session: requests.Session) -> dict:
    """
    return:
        {
            'tournament_name': 'Standard League 2026-05-15',
            'date': '2026-05-15',
            'format': 'standard',
            'source_tour_id': '63818',
        }

    The format field is parsed from the 'Format: Standard' line in p[2]. The
    listing endpoint also exposes format, but the by-id enumeration entrypoint
    does not, so we always emit format here for a single source of truth.
    """
    resp = session.get(
        "https://www.mtggoldfish.com%s#paper" % link, timeout=DEFAULT_TIMEOUT
    )
    resp.close()
    if resp.status_code != 200:
        # mtggoldfish occasionally has gaps in the tournament id sequence
        # (deleted tournaments / Rails autoincrement rollback) -> 404.
        # Surface as a parse failure so the caller's GoldfishPageFormatError
        # except block can classify it as a soft skip rather than misleading
        # IndexError.
        raise GoldfishPageFormatError(
            "unexpected status %s for tournament %s" % (resp.status_code, link)
        )
    dom = etree.HTML(resp.text)
    name_nodes = dom.xpath("/html/body/main/div/h2/text()")
    if not name_nodes:
        raise GoldfishPageFormatError("missing h2 on %s" % link)
    tournament_name = name_nodes[0]
    p2_texts = dom.xpath("/html/body/main/div/p[2]/text()")
    if len(p2_texts) < 2:
        raise GoldfishPageFormatError("missing p[2] segments on %s" % link)
    # p2_texts is 4 segments:
    #   ['\nFormat: Standard\n', '\n Date: 2026-05-15\n', '\nSource:\n', '\n']
    tournament_date = p2_texts[1][8:-1]
    fmat_raw = p2_texts[0].strip()  # 'Format: Standard'
    if fmat_raw.lower().startswith("format:"):
        fmat = fmat_raw[len("format:"):].strip().lower()
    else:
        fmat = ""
    source_tour_id = link.split("/")[2]
    return {
        "tournament_name": tournament_name,
        "date": tournament_date,
        "format": fmat,
        "source_tour_id": source_tour_id,
    }


def get_decks(link: str, session: requests.Session) -> list[dict]:
    """
    return:
        [{'link': '/deck/3453012', 'place': 1}, ...]               # PLACE mode
        [{'link': '/deck/3453309', 'win': 5, 'loss': 0}, ...]      # WIN-LOSS mode
    """
    resp = session.get(
        "https://www.mtggoldfish.com%s#paper" % link, timeout=DEFAULT_TIMEOUT
    )
    resp.close()
    if resp.status_code != 200:
        raise GoldfishPageFormatError(
            "unexpected status %s for tournament %s" % (resp.status_code, link)
        )
    text_body = resp.text
    deck_list = etree.HTML(text_body).xpath(
        '//table[@class="table-tournament"]/tr[not(contains(@style,"display: none;"))]'
    )
    if not deck_list:
        return []

    def _to_int(x: str) -> int:
        return int(x) if x else 0

    result: list[dict] = []
    for deck in deck_list:
        td1 = deck.xpath("./td[1]/text()")
        td2_href = deck.xpath("./td[2]/a/@href")
        if not td1 or not td2_href:
            continue
        record = td1[0][1:-1]
        deck_link = td2_href[0]
        # mtggoldfish occasionally puts a promo row at the top of the deck
        # table (e.g. "/deck/custom/standard") with a non-numeric trailing
        # segment. Filter to /deck/<digits> only.
        if not deck_link.startswith("/deck/") or not deck_link.split("/")[-1].isdigit():
            continue
        if "\xa0" in record or "-" in record:
            record = record.replace("\xa0", "")
            # win-loss mode
            parts = record.split("-")
            if len(parts) < 2:
                continue
            try:
                result.append(
                    {
                        "link": deck_link,
                        "win": _to_int(parts[0]),
                        "loss": _to_int(parts[1]),
                    }
                )
            except (ValueError, TypeError):
                continue
            continue
        # place mode
        try:
            result.append(
                {
                    "link": deck_link,
                    "place": int(td1[0][1:-1][:-2]),
                }
            )
        except (ValueError, TypeError):
            continue

    # if it has next page
    next_page_elems = etree.HTML(text_body).xpath(
        '//li[contains(@class, "next")]/a[contains(@class, "page-link") and contains(@rel, "next")]'
    )
    if len(next_page_elems) > 0:
        next_page_link = next_page_elems[0].get("href")
        if next_page_link:
            time.sleep(NEXT_PAGE_SLEEP)
            result += get_decks(next_page_link, session)

    return result
