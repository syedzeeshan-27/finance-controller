"""Webhook inbox consumer: signed events in, canonical rows out.

    python -m ingest.webhook_inbox <inbox_dir> <out_dir> [--secret S]

Real Razorpay webhooks need a public HTTPS endpoint; a buildathon repo
running an always-on server would be theatre. What actually matters about
webhook ingestion is (a) verifying X-Razorpay-Signature — HMAC-SHA256 of
the raw body with the webhook secret — and (b) idempotent consumption.
Both are implemented here over an inbox directory of recorded event files
(the official event envelope, one JSON per delivery, optional sidecar
`<name>.sig` holding the signature header value). Point any tunnel/receiver
at the inbox and this consumer is the pipeline's webhook half.

Events understood: payment.captured, payment.failed (payload.payment.entity)
and order.paid (payload.order.entity). Consumption is idempotent by entity
id — redelivered events (webhooks retry!) update rather than duplicate.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys

from recon import schemas as S

from ingest.pull import orders_from_entities, payments_from_entities
from ingest.razorpay_files import write_csv


class SignatureError(Exception):
    pass


def verify_signature(body: bytes, signature: str, secret: str) -> None:
    want = hmac.new(secret.encode("utf-8"), body,
                    hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, signature.strip()):
        raise SignatureError("X-Razorpay-Signature mismatch — body was "
                             "tampered with or the secret is wrong")


def _merge(path: str, columns: list[str], key: str,
           new_rows: list[dict]) -> int:
    import csv
    existing: dict[str, dict] = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8", newline="") as f:
            existing = {r[key]: r for r in csv.DictReader(f)}
    for r in new_rows:
        existing[str(r[key])] = {c: str(r[c]) for c in columns}
    write_csv(path, columns, list(existing.values()))
    return len(existing)


def consume_inbox(inbox_dir: str, out_dir: str,
                  secret: str | None = None, *,
                  allow_unsigned: bool = False) -> dict:
    """Fail closed: without a secret nothing is consumed unless the caller
    explicitly accepts unsigned events (offline fixtures they trust); with a
    secret, an event lacking its `.sig` sidecar is skipped, never trusted."""
    if secret is None and not allow_unsigned:
        raise SystemExit("refusing to consume unsigned webhook events: pass "
                         "--secret (or set RAZORPAY_WEBHOOK_SECRET), or "
                         "--allow-unsigned for offline fixtures you trust")
    payment_entities: list[dict] = []
    order_entities: list[dict] = []
    consumed = skipped = unverified_skipped = 0
    for name in sorted(os.listdir(inbox_dir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(inbox_dir, name)
        with open(path, "rb") as f:
            body = f.read()
        sig_path = path[:-5] + ".sig"
        if secret is not None:
            if not os.path.exists(sig_path):
                print(f"WARNING: {name}: no .sig sidecar - skipped, not "
                      "consumed", file=sys.stderr)
                unverified_skipped += 1
                continue
            with open(sig_path, encoding="utf-8") as f:
                verify_signature(body, f.read(), secret)
        event = json.loads(body.decode("utf-8"))
        kind = event.get("event", "")
        payload = event.get("payload", {})
        if kind in ("payment.captured", "payment.failed"):
            payment_entities.append(payload["payment"]["entity"])
            consumed += 1
        elif kind == "order.paid":
            order_entities.append(payload["order"]["entity"])
            consumed += 1
        else:
            skipped += 1

    os.makedirs(out_dir, exist_ok=True)
    n_pay = _merge(os.path.join(out_dir, "payments.csv"),
                   S.PAYMENTS_COLUMNS, "payment_id",
                   payments_from_entities(payment_entities))
    n_ord = _merge(os.path.join(out_dir, "order_book.csv"),
                   S.ORDER_BOOK_COLUMNS, "order_id",
                   orders_from_entities(order_entities))
    return {"consumed": consumed, "skipped": skipped,
            "unverified_skipped": unverified_skipped,
            "payments_total": n_pay, "orders_total": n_ord}


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Consume recorded Razorpay webhook events "
                    "(signature-verified, idempotent).")
    ap.add_argument("inbox_dir")
    ap.add_argument("out_dir")
    ap.add_argument("--secret", default=os.environ.get(
        "RAZORPAY_WEBHOOK_SECRET"),
        help="webhook secret for X-Razorpay-Signature verification "
             "(default: RAZORPAY_WEBHOOK_SECRET env)")
    ap.add_argument("--allow-unsigned", action="store_true",
                    help="consume events without signature checks — "
                         "offline fixtures you trust only")
    args = ap.parse_args()
    stats = consume_inbox(args.inbox_dir, args.out_dir, args.secret,
                          allow_unsigned=args.allow_unsigned)
    print(f"consumed {stats['consumed']} event(s), skipped "
          f"{stats['skipped']}, unverified-skipped "
          f"{stats['unverified_skipped']} -> {stats['payments_total']} "
          f"payment(s), {stats['orders_total']} order(s) in {args.out_dir}")


if __name__ == "__main__":
    main()
