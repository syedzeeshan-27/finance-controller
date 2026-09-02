"""Live-ingestion layer: fixture pulls, signed webhook inbox, no network."""

import json
import os
import shutil

import pytest

from ingest.pull import (orders_from_entities, payments_from_entities,
                         write_world)
from ingest.webhook_inbox import (SignatureError, consume_inbox,
                                  verify_signature)
from recon import io_load

_FIXTURES = os.path.join("data", "fixtures", "razorpay")
_SECRET = "rzp_demo_webhook_secret"


def _entities(name):
    with open(os.path.join(_FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)["items"]


def test_fixture_pull_produces_loadable_world(tmp_path):
    payments = payments_from_entities(_entities("payments_api.json"))
    orders = orders_from_entities(_entities("orders_api.json"))
    write_world(str(tmp_path), payments, orders)
    loaded_p = io_load.load_payments(str(tmp_path))
    loaded_o = io_load.load_orders(str(tmp_path))
    assert len(loaded_p) == 4 and len(loaded_o) == 4
    by_id = {p["payment_id"]: p for p in loaded_p}
    assert by_id["pay_LxA01aB2cD3eF4"]["amount_paise"] == 100000
    assert by_id["pay_LxE05eF6gH7iJ8"]["status"] == "failed"
    assert all(p["settlement_id"] == "" for p in loaded_p)  # test mode: none
    attempted = next(o for o in loaded_o
                     if o["order_id"] == "order_LxOrd04dE5fG6h")
    assert attempted["status"] == "created"     # 'attempted' normalised


def test_webhook_signature_verifies_and_rejects_tampered_body():
    path = os.path.join(_FIXTURES, "webhooks", "001_payment_captured.json")
    with open(path, "rb") as f:
        body = f.read()
    with open(path[:-5] + ".sig", encoding="utf-8") as f:
        sig = f.read()
    verify_signature(body, sig, _SECRET)        # committed fixture verifies
    with pytest.raises(SignatureError):
        verify_signature(body + b" ", sig, _SECRET)
    with pytest.raises(SignatureError):
        verify_signature(body, sig, "wrong-secret")


def test_inbox_consume_is_idempotent_and_verified(tmp_path):
    out = str(tmp_path / "world")
    stats = consume_inbox(os.path.join(_FIXTURES, "webhooks"), out,
                          secret=_SECRET)
    assert stats["consumed"] == 3               # incl. one redelivery
    assert stats["payments_total"] == 1         # deduped by entity id
    assert stats["orders_total"] == 1
    first = open(os.path.join(out, "payments.csv"), "rb").read()
    consume_inbox(os.path.join(_FIXTURES, "webhooks"), out, secret=_SECRET)
    assert open(os.path.join(out, "payments.csv"), "rb").read() == first


def test_tampered_inbox_file_is_refused(tmp_path):
    inbox = tmp_path / "inbox"
    shutil.copytree(os.path.join(_FIXTURES, "webhooks"), inbox)
    victim = inbox / "001_payment_captured.json"
    body = victim.read_bytes().replace(b'"amount": 100000',
                                       b'"amount": 999999')
    victim.write_bytes(body)
    with pytest.raises(SignatureError):
        consume_inbox(str(inbox), str(tmp_path / "out"), secret=_SECRET)


def test_fixture_mode_never_opens_the_network(tmp_path, monkeypatch):
    import urllib.request

    def _explode(*_a, **_k):
        raise AssertionError("network touched in fixture mode")

    monkeypatch.setattr(urllib.request, "urlopen", _explode)
    payments = payments_from_entities(_entities("payments_api.json"))
    write_world(str(tmp_path), payments, [])
    assert (tmp_path / "payments.csv").exists()
