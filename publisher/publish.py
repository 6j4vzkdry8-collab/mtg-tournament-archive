"""publisher main entry point.

End-to-end flow per run:

    listing (5 formats)
        |
        v
    tournament details + deck id list  ->  tournaments/<id>.json (overwrite)
        |
        v
    per deck widget + component        ->  decks/<id>.json (skip-if-exists)
        |
        v
    manifest + atomic latest.json update

Design notes:
  - Tournament details are always re-fetched: the listing only exposes the
    most recent ~10 entries, and we need each detail page to discover its
    deck id list. Overwriting the tournament JSON in OSS is idempotent and
    cheap.
  - Decks are deduped via OSS head_object: each deck = 2 endpoint hits +
    sleep ~10s, so skipping cached decks is the dominant win.
  - Failure tolerance: a single tour / deck failure only adds an entry to
    manifest.errors; the rest of the run keeps going.
  - --dry-run: skip OSS uploads, dump a summary to stdout. Useful for local
    schema verification.

CLI:
  python -m publisher.publish              # upload to OSS
  python -m publisher.publish --dry-run    # local check, no upload
  python -m publisher.publish --formats standard,modern   # subset
  python -m publisher.publish --max-decks 5               # debug: cap decks
"""
from __future__ import annotations

import argparse
import datetime
import random
import sys
import time
import traceback

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from publisher.fetch_deck import (
    GoldfishV2Error,
    fetch_component_cards,
    fetch_widget,
)
from publisher.fetch_listing import (
    GoldfishPageFormatError,
    get_decks,
    get_tournament_info,
    get_tournaments,
)

DEFAULT_FORMATS = ["standard", "modern", "legacy", "pioneer", "pauper"]

# Realistic browser UA. The mtggoldfish CDN happily 200s this; a default
# python-requests UA gets weird treatment.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)


def _build_session() -> requests.Session:
    """Reusable Session for the whole run. keep-alive amortises TCP setup
    across hundreds of requests.

    The session has a urllib3 Retry adapter mounted so 429 burst limits get
    absorbed at the network layer. Backoff schedule:
        retry 1 -> sleep 15s
        retry 2 -> sleep 30s
        retry 3 -> sleep 60s
        retry 4 -> sleep 120s (BACKOFF_MAX cap)
        retry 5 -> sleep 120s (cap)
    Total worst-case ~5 minutes. respect_retry_after_header is False because
    the upstream CDN sometimes returns Retry-After: 0 on 429, which would
    make urllib3 retry instantly and burn through retries in milliseconds.
    Force the explicit backoff_factor instead.
    """
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": BROWSER_UA,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            # No br: requests does not auto-decompress brotli; lxml chokes
            # on the raw bytes.
            "Accept-Encoding": "gzip, deflate",
            "Referer": "https://www.mtggoldfish.com/",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
        }
    )
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        status=5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        backoff_factor=15.0,
        respect_retry_after_header=False,
        # If we exhaust retries and still get 429, return the response to
        # the caller; fetch_widget / fetch_component_cards will translate
        # it into a GoldfishV2Error.
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


def _now_iso_utc() -> str:
    """ISO 8601 UTC stamp, e.g. '2026-05-16T07:15:00Z'."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(msg: str) -> None:
    """Stderr logger. CI log handlers automatically mask Secrets."""
    print(f"[{_now_iso_utc()}] {msg}", file=sys.stderr, flush=True)


def collect_listing(
    session: requests.Session,
    formats: list[str],
    sleep_between: float = 5.0,
) -> tuple[list[dict], list[dict]]:
    """For each format, hit get_tournaments. Returns (tournaments, errors)."""
    tournaments: list[dict] = []
    errors: list[dict] = []
    for i, fmat in enumerate(formats):
        try:
            tours = get_tournaments(fmat, session)
            tournaments.extend(tours)
            _log(f"listing format={fmat}: {len(tours)} tournaments")
        except (
            GoldfishPageFormatError,
            requests.RequestException,
        ) as e:
            errors.append(
                {
                    "stage": "listing",
                    "path": f"/tournaments/{fmat}",
                    "type": type(e).__name__,
                    "msg": str(e),
                }
            )
            _log(f"listing format={fmat} FAIL: {type(e).__name__}: {e}")
        if i < len(formats) - 1:
            time.sleep(sleep_between)
    return tournaments, errors


def fetch_tournament_full(
    link: str,
    fmat_hint: str,
    session: requests.Session,
) -> dict:
    """Pull tournament detail + deck id list and assemble the tournament JSON.

    fmat_hint comes from the listing layer; used as a fallback if the detail
    page parser cannot extract a Format line.
    """
    info = get_tournament_info(link, session)
    fmat = (info.get("format") or fmat_hint or "").lower()
    decks = get_decks(link, session)
    return {
        "source_tour_id": info["source_tour_id"],
        "tournament_name": info["tournament_name"],
        "date": info["date"],
        "format": fmat,
        "decks": decks,
    }


def fetch_deck_full(deck_id: int, session: requests.Session) -> dict:
    """Combine widget + component into a single deck JSON."""
    meta = fetch_widget(deck_id, session)
    cards = fetch_component_cards(deck_id, session)
    return {
        "deck_id": deck_id,
        "name": meta["name"],
        "player_name": meta["player_name"],
        "cover_card_name": meta["cover_card_name"],
        "cards": cards,
    }


def _deck_id_from_link(link: str) -> int | None:
    tail = link.rstrip("/").split("/")[-1]
    return int(tail) if tail.isdigit() else None


def run(
    formats: list[str],
    dry_run: bool,
    max_decks_per_tour: int | None = None,
) -> int:
    """Main flow. @return: process exit code (0=ok, non-0=hard failure)."""
    session = _build_session()
    uploader = None
    if not dry_run:
        # Lazy import so dry-run does not require oss2 to be installed
        from publisher.oss_uploader import OssUploader

        uploader = OssUploader()
        _log("oss prefix configured (bucket+prefix from env, masked in CI log)")

    run_at = _now_iso_utc()
    _log(f"run starting at {run_at}, formats={formats}, dry_run={dry_run}")

    # ---- 1. listing ----
    tournaments, errors = collect_listing(session, formats)
    _log(f"listing done: {len(tournaments)} tournaments total, {len(errors)} errors")

    # ---- 2. tournaments: always full re-fetch (overwrite OSS) ----
    tour_results: list[dict] = []
    deck_refs: list[dict] = []
    for i, tour_ref in enumerate(tournaments):
        link = tour_ref["link"]
        fmat_hint = tour_ref["format"]
        try:
            tour_full = fetch_tournament_full(link, fmat_hint, session)
        except (
            GoldfishPageFormatError,
            requests.RequestException,
            IndexError,
        ) as e:
            errors.append(
                {
                    "stage": "tournament",
                    "path": link,
                    "type": type(e).__name__,
                    "msg": str(e),
                }
            )
            _log(f"tournament {link} FAIL: {type(e).__name__}: {e}")
            continue
        tour_results.append(tour_full)
        # Carry win/loss/place from the listing into the manifest
        for d in tour_full["decks"]:
            did = _deck_id_from_link(d["link"])
            if did is None:
                continue
            deck_refs.append(
                {
                    "deck_id": did,
                    "tournament_id": tour_full["source_tour_id"],
                    "format": tour_full["format"],
                    "win": d.get("win", 0) or 0,
                    "loss": d.get("loss", 0) or 0,
                    "place": d.get("place", 0) or 0,
                }
            )
        _log(
            f"tournament [{i+1}/{len(tournaments)}] {link} fmt={tour_full['format']} "
            f"date={tour_full['date']} decks={len(tour_full['decks'])}"
        )
        if uploader is not None:
            key = uploader.key_for("tournaments", f"{tour_full['source_tour_id']}.json")
            uploader.put_json(key, tour_full)
        # Inter-tournament jitter to avoid request bursts that look like
        # automation patterns to upstream rate limiters
        if i < len(tournaments) - 1:
            time.sleep(random.uniform(3.0, 6.0))

    _log(
        f"tournaments done: {len(tour_results)} ok / {len(deck_refs)} deck refs collected"
    )

    # ---- 3. decks: skip-if-exists via head_object ----
    # Group within tournament + dedupe deck_id (just in case listing repeats)
    seen_deck_ids: set[int] = set()
    new_deck_refs: list[dict] = []
    for ref in deck_refs:
        if ref["deck_id"] in seen_deck_ids:
            continue
        seen_deck_ids.add(ref["deck_id"])
        new_deck_refs.append(ref)

    if max_decks_per_tour is not None:
        # Debug: cap each tournament to at most N decks
        per_tour_count: dict[str, int] = {}
        truncated: list[dict] = []
        for ref in new_deck_refs:
            t = ref["tournament_id"]
            if per_tour_count.get(t, 0) >= max_decks_per_tour:
                continue
            per_tour_count[t] = per_tour_count.get(t, 0) + 1
            truncated.append(ref)
        _log(
            f"max-decks-per-tour={max_decks_per_tour}: {len(new_deck_refs)} -> {len(truncated)}"
        )
        new_deck_refs = truncated

    # Refs that end up in the manifest. OSS-cached entries count too.
    fetched_deck_refs: list[dict] = []
    fetched_count = 0
    skipped_existing = 0
    failed_count = 0

    for i, ref in enumerate(new_deck_refs):
        deck_id = ref["deck_id"]
        # head_object dedup (skipped in dry-run so we exercise the full
        # fetch path locally)
        if uploader is not None:
            key = uploader.key_for("decks", f"{deck_id}.json")
            if uploader.object_exists(key):
                skipped_existing += 1
                fetched_deck_refs.append(ref)
                if (i + 1) % 50 == 0:
                    _log(
                        f"deck progress [{i+1}/{len(new_deck_refs)}]: "
                        f"fetched={fetched_count} skipped={skipped_existing} failed={failed_count}"
                    )
                continue

        try:
            deck_full = fetch_deck_full(deck_id, session)
        except (
            GoldfishV2Error,
            requests.RequestException,
        ) as e:
            errors.append(
                {
                    "stage": "deck",
                    "path": f"/deck/{deck_id}",
                    "type": type(e).__name__,
                    "msg": str(e),
                }
            )
            failed_count += 1
            _log(f"deck {deck_id} FAIL: {type(e).__name__}: {e}")
            # A failed deck does not enter the manifest. The next run sees
            # head_object=false and retries the fetch.
            continue

        # Merge listing-derived win/loss/place into the deck JSON so consumers
        # don't have to cross-reference the tournament JSON.
        deck_full["link"] = f"/deck/{deck_id}"
        deck_full["win"] = ref["win"]
        deck_full["loss"] = ref["loss"]
        deck_full["place"] = ref["place"]

        if uploader is not None:
            key = uploader.key_for("decks", f"{deck_id}.json")
            uploader.put_json(key, deck_full)
        fetched_count += 1
        fetched_deck_refs.append(ref)

        # Per-deck jitter (2-5s). The widget/component endpoints have
        # generous limits but are still subject to bursty IP-level rate
        # limiting on shared CI runners.
        if i < len(new_deck_refs) - 1:
            time.sleep(random.uniform(2.0, 5.0))

        if (i + 1) % 20 == 0:
            _log(
                f"deck progress [{i+1}/{len(new_deck_refs)}]: "
                f"fetched={fetched_count} skipped={skipped_existing} failed={failed_count}"
            )

    _log(
        f"decks done: fetched={fetched_count} skipped={skipped_existing} failed={failed_count}"
    )

    # ---- 4. manifest + latest.json ----
    manifest_name = f"{run_at}.json"
    manifest = {
        "run_at": run_at,
        "tournaments": [
            {
                "source_tour_id": t["source_tour_id"],
                "format": t["format"],
                "date": t["date"],
            }
            for t in tour_results
        ],
        "decks": fetched_deck_refs,
        "stats": {
            "tournaments_ok": len(tour_results),
            "decks_fetched": fetched_count,
            "decks_skipped_existing": skipped_existing,
            "decks_failed": failed_count,
        },
        "errors": errors,
    }

    if uploader is not None:
        manifest_key = uploader.key_for("manifests", manifest_name)
        uploader.put_json(manifest_key, manifest)
        uploader.update_latest_pointer(manifest_name)
        _log(f"manifest written: {manifest_name}; latest.json updated")
    else:
        # Dry-run: dump a manifest preview to stdout
        import json as _json

        print(
            "===== DRY RUN manifest preview ====="
            "\n" + _json.dumps(manifest, ensure_ascii=False, indent=2)[:4000]
        )

    # Treat scattered deck failures as soft. Only a hard zero-tournaments
    # outcome should turn the CI job red.
    if len(tour_results) == 0:
        _log("HARD FAILURE: 0 tournaments fetched; returning non-zero exit code")
        return 2
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="don't upload to OSS; only print summary to stdout",
    )
    parser.add_argument(
        "--formats",
        default=",".join(DEFAULT_FORMATS),
        help="comma-separated formats (default: %s)" % ",".join(DEFAULT_FORMATS),
    )
    parser.add_argument(
        "--max-decks",
        type=int,
        default=None,
        help="debug only: max decks per tournament (default: no limit)",
    )
    args = parser.parse_args(argv)
    formats = [f.strip().lower() for f in args.formats.split(",") if f.strip()]
    try:
        return run(formats, args.dry_run, args.max_decks)
    except KeyboardInterrupt:
        _log("interrupted by user")
        return 130
    except Exception:
        _log("UNEXPECTED FAILURE:")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
