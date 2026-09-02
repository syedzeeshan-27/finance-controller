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
                  secret: str | None = None) -> dict:
    payment_entities: list[dict] = []
    order_entities: list[dict] = []
    consumed = skipped = 0
    for name in sorted(os.listdir(inbox_dir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(inbox_dir, name)
        with open(path, "rb") as f:
            body = f.read()
        sig_path = path[:-5] + ".sig"
        if secret is not None and os.path.exists(sig_path):
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
    args = ap.parse_args()
    stats = consume_inbox(args.inbox_dir, args.out_dir, args.secret)
    print(f"consumed {stats['consumed']} event(s), skipped "
          f"{stats['skipped']} -> {stats['payments_total']} payment(s), "
          f"{stats['orders_total']} order(s) in {args.out_dir}")


if __name__ == "__main__":
    main()
