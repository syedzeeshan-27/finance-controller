"""Pull payments/orders from Razorpay (fixtures by default, test-mode API
with --live) into canonical world CSVs.

    python -m ingest.pull --fixtures data/fixtures/razorpay --out DIR
    python -m ingest.pull --live --out DIR [--from-ts N] [--to-ts N]
        (needs RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET)

Entity mapping is verbatim from the official payment/order schemas: amounts
are already integer paise (passed through), epochs become ISO timestamps,
`settlement_id` stays empty — test mode never settles successfully (see
data/fixtures/razorpay/README.md), which is exactly what the recon engine
should then report.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os

from recon import schemas as S

from ingest.razorpay_files import write_csv

_ORDER_STATUS = {"paid": "paid", "created": "created",
                 "attempted": "created", "cancelled": "cancelled"}


def _iso(epoch: int | str) -> str:
    dt = _dt.datetime.fromtimestamp(int(epoch), tz=_dt.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def payments_from_entities(entities: list[dict]) -> list[dict]:
    out = []
    for e in entities:
        if e.get("entity") != "payment":
            continue
        out.append({
            "payment_id": e["id"],
            "order_id": e.get("order_id") or "",
            "method": e.get("method", ""),
            "amount_paise": int(e["amount"]),         # paise passthrough
            "fee_paise": int(e.get("fee") or 0),
            "tax_paise": int(e.get("tax") or 0),
            "status": e["status"],
            "created_at": _iso(e["created_at"]),
            "settlement_id": "",   # test mode never settles; leave honest
        })
    return out


def orders_from_entities(entities: list[dict]) -> list[dict]:
    out = []
    for e in entities:
        if e.get("entity") != "order":
            continue
        out.append({
            "order_id": e["id"],
            "receipt": e.get("receipt") or "",
            "amount_paise": int(e["amount"]),         # paise passthrough
            "status": _ORDER_STATUS.get(e["status"], "created"),
            "created_at": _iso(e["created_at"]),
        })
    return out


def _load_collection(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)["items"]


def write_world(out_dir: str, payments: list[dict],
                orders: list[dict]) -> None:
    write_csv(os.path.join(out_dir, "payments.csv"),
              S.PAYMENTS_COLUMNS, payments)
    write_csv(os.path.join(out_dir, "order_book.csv"),
              S.ORDER_BOOK_COLUMNS, orders)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Ingest Razorpay payments/orders into canonical CSVs.")
    ap.add_argument("--out", required=True)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--fixtures", metavar="DIR",
                      default=os.path.join("data", "fixtures", "razorpay"),
                      help="read payments_api.json / orders_api.json from "
                           "this directory (default; fully offline)")
    mode.add_argument("--live", action="store_true",
                      help="pull from the test-mode API "
                           "(RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET)")
    ap.add_argument("--from-ts", type=int, default=None)
    ap.add_argument("--to-ts", type=int, default=None)
    args = ap.parse_args()

    if args.live:
        key_id = os.environ.get("RAZORPAY_KEY_ID")
        key_secret = os.environ.get("RAZORPAY_KEY_SECRET")
        if not key_id or not key_secret:
            raise SystemExit("--live needs RAZORPAY_KEY_ID and "
                             "RAZORPAY_KEY_SECRET in the environment")
        from ingest import razorpay_api
        pay_entities = razorpay_api.fetch_payments(
            key_id, key_secret, args.from_ts, args.to_ts)
        order_entities = razorpay_api.fetch_orders(
            key_id, key_secret, args.from_ts, args.to_ts)
        source = "live test-mode API"
    else:
        pay_entities = _load_collection(
            os.path.join(args.fixtures, "payments_api.json"))
        order_entities = _load_collection(
            os.path.join(args.fixtures, "orders_api.json"))
        source = f"fixtures ({args.fixtures})"

    payments = payments_from_entities(pay_entities)
    orders = orders_from_entities(order_entities)
    write_world(args.out, payments, orders)
    print(f"wrote {len(payments)} payment(s), {len(orders)} order(s) "
          f"from {source} -> {args.out}")


if __name__ == "__main__":
    main()
