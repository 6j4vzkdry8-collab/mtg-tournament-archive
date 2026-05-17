"""Thin Aliyun OSS upload wrapper.

Design notes:
  1. Every PUT carries x-oss-object-acl: private to override any bucket
     default ACL, so consumers can never anonymous-GET our objects.
  2. All config is read from environment variables, never committed:
        OSS_AK_ID / OSS_AK_SECRET / OSS_BUCKET / OSS_PREFIX / OSS_ENDPOINT
     In CI these are wired through Secrets and the runtime log masks them.
  3. object_exists(key) is exposed for HEAD-based dedup in publish.py.
  4. update_latest_pointer uses the two-step atomic pattern: write the new
     manifest first, then overwrite latest.json. Consumers always look at
     latest.json, so the moment they see a new latest.json the manifest it
     points at is already fully written. OSS single-object PUT is itself
     atomic, so latest.json is never seen half-written.
"""
from __future__ import annotations

import json
import os
from typing import Optional

import oss2

# Force private ACL on every PUT regardless of the bucket default.
PRIVATE_HEADERS = {"x-oss-object-acl": "private"}


class OssUploader:
    def __init__(
        self,
        ak_id: Optional[str] = None,
        ak_secret: Optional[str] = None,
        bucket_name: Optional[str] = None,
        prefix: Optional[str] = None,
        endpoint: Optional[str] = None,
    ):
        ak_id = ak_id or os.environ.get("OSS_AK_ID")
        ak_secret = ak_secret or os.environ.get("OSS_AK_SECRET")
        bucket_name = bucket_name or os.environ.get("OSS_BUCKET")
        prefix = prefix or os.environ.get("OSS_PREFIX")
        endpoint = endpoint or os.environ.get("OSS_ENDPOINT")

        missing = [
            n
            for n, v in (
                ("OSS_AK_ID", ak_id),
                ("OSS_AK_SECRET", ak_secret),
                ("OSS_BUCKET", bucket_name),
                ("OSS_PREFIX", prefix),
                ("OSS_ENDPOINT", endpoint),
            )
            if not v
        ]
        if missing:
            raise RuntimeError(
                "OSS config missing: %s. Set them via env (or CI Secrets)."
                % ",".join(missing)
            )

        # Trim trailing slashes for clean prefix concatenation later
        self.prefix = prefix.rstrip("/")
        self.bucket_name = bucket_name
        self._auth = oss2.Auth(ak_id, ak_secret)
        self._bucket = oss2.Bucket(self._auth, endpoint, bucket_name)

    # ------------------------------------------------------------------
    # path helpers: every object goes through this prefix builder
    # ------------------------------------------------------------------
    def key_for(self, *parts: str) -> str:
        return "/".join([self.prefix, *[p.strip("/") for p in parts if p]])

    # ------------------------------------------------------------------
    # head_object dedup probe
    # ------------------------------------------------------------------
    def object_exists(self, key: str) -> bool:
        try:
            self._bucket.head_object(key)
            return True
        except oss2.exceptions.NoSuchKey:
            return False
        except oss2.exceptions.NotFound:
            return False

    # ------------------------------------------------------------------
    # PUT: force ACL=private + ContentType=application/json
    # ------------------------------------------------------------------
    def put_json(self, key: str, payload: dict | list) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        headers = dict(PRIVATE_HEADERS)
        headers["Content-Type"] = "application/json; charset=utf-8"
        self._bucket.put_object(key, body, headers=headers)

    # ------------------------------------------------------------------
    # Atomic latest pointer update: caller must PUT the manifest first.
    # ------------------------------------------------------------------
    def update_latest_pointer(self, manifest_name: str) -> None:
        """Overwrite <prefix>/latest.json so it points at manifests/<manifest_name>.

        Contract: callers must put_json(manifests/<manifest_name>) successfully
        before invoking this. Each step is a single OSS PUT and OSS guarantees
        atomicity per object, so consumers reading latest.json always see a
        pointer at a fully-written manifest.
        """
        latest_key = self.key_for("latest.json")
        self.put_json(latest_key, {"latest_manifest": manifest_name})
