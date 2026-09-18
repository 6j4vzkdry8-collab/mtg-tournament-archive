"""Mirror Scryfall bulk files onto OSS. Karn pulls the gz over the intranet.

End-to-end per run:

    GET /bulk-data  ->  compare each type's `id` with scryfall/latest.json
        |
        v
    download jsonl.gz for types whose id changed  ->  overwrite fixed keys
        |
        v
    GET /sets  ->  scryfall/sets.json  (when all_cards changed)
        |
        v
    overwrite scryfall/latest.json  (meta only; written last)

OSS layout (prefix from OSS_PREFIX, hardcoded to `scryfall` in CI):

    scryfall/all-cards.jsonl.gz
    scryfall/rulings.jsonl.gz
    scryfall/sets.json
    scryfall/latest.json     # { all_cards: {id, updated_at, compressed_size}, ... }

Fixed keys, overwrite in place: OSS never accumulates dated copies.

UA must identify the app. Scryfall 400s generic_user_agent on the
python-requests default.

CLI:
  python -m publisher.scryfall_publish              # upload to OSS
  python -m publisher.scryfall_publish --dry-run    # download only, no OSS
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys
import tempfile
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from publisher.oss_uploader import OssUploader

SCRYFALL_UA = "MTGso/1.0 (+https://mtgso.cn)"
BULK_INDEX_URL = "https://api.scryfall.com/bulk-data"
SETS_URL = "https://api.scryfall.com/sets"

# (bulk type, OSS object name under the prefix)
TARGETS = (
    ("all_cards", "all-cards.jsonl.gz"),
    ("rulings", "rulings.jsonl.gz"),
)

# Scryfall asks for 50–100ms between requests.
INTER_REQUEST_SLEEP = 0.1


def _now_iso_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(msg: str) -> None:
    print(f"[{_now_iso_utc()}] {msg}", file=sys.stderr, flush=True)


def _build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": SCRYFALL_UA,
            "Accept": "application/json",
        }
    )
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        status=5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        backoff_factor=2.0,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


def _meta_from_bulk(item: dict) -> dict:
    return {
        "id": item["id"],
        "updated_at": item["updated_at"],
        "compressed_size": item.get("compressed_size"),
    }


def _download_gz(session: requests.Session, url: str, dest: str, label: str) -> None:
    """Stream a jsonl.gz to disk. Do not send Accept: application/json."""
    _log(f"downloading {label} -> {dest}")
    with session.get(
        url,
        stream=True,
        timeout=(30, 120),
        headers={"User-Agent": SCRYFALL_UA, "Accept": "*/*"},
    ) as resp:
        resp.raise_for_status()
        tot = max(int(resp.headers.get("Content-Length", 0)), 1)
        got = 0
        last_log = time.time()
        with open(dest, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    continue
                fh.write(chunk)
                got += len(chunk)
                now = time.time()
                if now - last_log >= 5:
                    _log(
                        f"  {label}: {got / 1024 / 1024:.1f} / {tot / 1024 / 1024:.1f} MB "
                        f"({min(99.9, got / tot * 100):.1f}%)"
                    )
                    last_log = now
    _log(f"downloaded {label}: {got / 1024 / 1024:.1f} MB")


def run(dry_run: bool = False) -> int:
    session = _build_session()
    _log("GET %s" % BULK_INDEX_URL)
    index = session.get(BULK_INDEX_URL, timeout=(15, 30)).json()
    time.sleep(INTER_REQUEST_SLEEP)
    by_type = {item["type"]: item for item in index.get("data", [])}
    missing = [typ for typ, _ in TARGETS if typ not in by_type]
    if missing:
        _log("bulk-data missing types: %s" % ",".join(missing))
        return 1

    uploader: OssUploader | None = None
    existing: dict = {}
    if not dry_run:
        uploader = OssUploader()
        loaded = uploader.get_json(uploader.key_for("latest.json"))
        if isinstance(loaded, dict):
            existing = loaded
            _log("OSS latest.json present")
        else:
            _log("OSS latest.json missing (first run or empty prefix)")
    else:
        _log("dry-run: skip OSS")

    new_latest: dict = {}
    uploaded_any = False
    all_cards_changed = False

    with tempfile.TemporaryDirectory(prefix="scryfall-") as tmp:
        for typ, oss_name in TARGETS:
            item = by_type[typ]
            meta = _meta_from_bulk(item)
            new_latest[typ] = meta
            prev_id = (existing.get(typ) or {}).get("id")
            if prev_id and prev_id == meta["id"]:
                _log(f"{typ}: id={meta['id']} unchanged, skip")
                continue
            if prev_id:
                _log(f"{typ}: id {prev_id} -> {meta['id']}, downloading")
            else:
                _log(f"{typ}: no previous id, downloading")

            local = os.path.join(tmp, oss_name)
            _download_gz(session, item["jsonl_download_uri"], local, typ)
            time.sleep(INTER_REQUEST_SLEEP)
            if uploader is not None:
                key = uploader.key_for(oss_name)
                _log(f"OSS put {key}")
                uploader.put_file(key, local)
            uploaded_any = True
            if typ == "all_cards":
                all_cards_changed = True

        if all_cards_changed or not existing:
            _log("GET %s" % SETS_URL)
            sets_payload = session.get(SETS_URL, timeout=(15, 30)).json()
            n_sets = len(sets_payload.get("data") or [])
            _log(f"sets: {n_sets} entries")
            if uploader is not None:
                uploader.put_json(uploader.key_for("sets.json"), sets_payload)
            uploaded_any = True
        else:
            _log("sets.json unchanged (all_cards id match)")

    if uploader is not None and uploaded_any:
        # Pointer last: Karn reading latest.json can assume the gz is already there.
        uploader.put_json(uploader.key_for("latest.json"), new_latest)
        _log("OSS latest.json updated")
    elif uploader is not None:
        _log("nothing uploaded, leave latest.json as-is")

    _log("done")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Mirror Scryfall bulk data to OSS")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Download from Scryfall, skip OSS",
    )
    args = parser.parse_args()
    try:
        sys.exit(run(dry_run=args.dry_run))
    except Exception as e:
        _log(f"FATAL {type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    main()
