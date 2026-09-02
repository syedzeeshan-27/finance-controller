"""Live Razorpay API client — pure fetch, stdlib only, no interpretation.

Basic-auth GETs against https://api.razorpay.com/v1 with count/skip
pagination, returning raw entity dicts exactly as the API sends them.
Mapping to the canonical world lives in `ingest.pull`; nothing here parses
amounts or dates. Used only in `pull --live`; fixtures cover everything
offline (and are what the tests exercise — no network in the suite).
"""

from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request

BASE = "https://api.razorpay.com/v1"
_PAGE = 100


def _get(path: str, key_id: str, key_secret: str, params: dict) -> dict:
    token = base64.b64encode(f"{key_id}:{key_secret}".encode()).decode()
    url = f"{BASE}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _paged(path: str, key_id: str, key_secret: str,
           from_ts: int | None, to_ts: int | None) -> list[dict]:
    items: list[dict] = []
    skip = 0
    while True:
        params: dict = {"count": _PAGE, "skip": skip}
        if from_ts is not None:
            params["from"] = from_ts
        if to_ts is not None:
            params["to"] = to_ts
        batch = _get(path, key_id, key_secret, params).get("items", [])
        items.extend(batch)
        if len(batch) < _PAGE:
            return items
        skip += _PAGE


def fetch_payments(key_id: str, key_secret: str,
                   from_ts: int | None = None,
                   to_ts: int | None = None) -> list[dict]:
    return _paged("payments", key_id, key_secret, from_ts, to_ts)


def fetch_orders(key_id: str, key_secret: str,
                 from_ts: int | None = None,
                 to_ts: int | None = None) -> list[dict]:
    return _paged("orders", key_id, key_secret, from_ts, to_ts)
