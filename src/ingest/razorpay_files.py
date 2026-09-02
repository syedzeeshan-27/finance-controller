"""Razorpay official settlement formats -> canonical world CSVs.

Input shapes are the documented API schemas, verbatim:

- the settlement entity (`GET /v1/settlements`):
  {"id": "setl_…", "entity": "settlement", "amount": <paise>, "status": …,
   "fees": <paise>, "tax": <paise>, "utr": …, "created_at": <epoch>}
  wrapped in the standard collection envelope
  {"entity": "collection", "count": N, "items": [...]}

- the per-transaction settlement report (`settlement.reports`), one row per
  payment / refund / adjustment:
  entity_id, type, debit, credit, amount, currency, fee, tax, on_hold,
  settled, created_at, settled_at, settlement_id, credit_type, description,
  settlement_utr, order_id, method

Amounts are integer paise in both — passed through untouched.

CLI:  python -m ingest.razorpay_files <fixture_dir> <out_dir>
      (expects settlements_api.json and settlement_recon_report.csv)
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import os

from recon import schemas as S


def _iso(epoch: int | str) -> str:
    dt = _dt.datetime.fromtimestamp(int(epoch), tz=_dt.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _iso_date(epoch: int | str) -> str:
    return _iso(epoch)[:10]


def settlements_from_api(collection: dict,
                         report_rows: list[dict] | None = None) -> list[dict]:
    """Settlement entities -> canonical settlements.csv rows.

    payment_count and settled_at come from the per-transaction report when
    provided (the entity itself carries neither)."""
    by_settlement: dict[str, list[dict]] = {}
    for r in report_rows or []:
        by_settlement.setdefault(r["settlement_id"], []).append(r)

    out = []
    for item in collection["items"]:
        if item.get("entity") != "settlement":
            continue
        mine = by_settlement.get(item["id"], [])
        payments = [r for r in mine if r["type"] == "payment"]
        settled_at = (_iso_date(payments[0]["settled_at"]) if payments
                      else _iso_date(item["created_at"]))
        out.append({
            "settlement_id": item["id"],
            "amount_paise": int(item["amount"]),      # paise passthrough
            "fees_paise": int(item["fees"]),
            "tax_paise": int(item["tax"]),
            "utr": item["utr"],
            "payment_count": len(payments),
            "status": item["status"],
            "created_at": _iso(item["created_at"]),
            "settled_at": settled_at,
        })
    return out


def payments_from_report(report_rows: list[dict]) -> list[dict]:
    """Per-transaction settlement report rows -> canonical payments.csv rows.

    Only `type == "payment"` rows become payments; refunds and adjustments
    are settlement-side artifacts the canonical schema models elsewhere."""
    out = []
    for r in report_rows:
        if r["type"] != "payment":
            continue
        out.append({
            "payment_id": r["entity_id"],
            "order_id": r["order_id"],
            "method": r["method"],
            "amount_paise": int(r["amount"]),         # paise passthrough
            "fee_paise": int(r["fee"]),
            "tax_paise": int(r["tax"]),
            "status": "captured",
            "created_at": _iso(r["created_at"]),
            "settlement_id": r["settlement_id"],
        })
    return out


def load_fixture_dir(fixture_dir: str) -> tuple[dict, list[dict]]:
    with open(os.path.join(fixture_dir, "settlements_api.json"),
              encoding="utf-8") as f:
        collection = json.load(f)
    with open(os.path.join(fixture_dir, "settlement_recon_report.csv"),
              encoding="utf-8", newline="") as f:
        report_rows = list(csv.DictReader(f))
    return collection, report_rows


def write_csv(path: str, columns: list[str], rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Convert official Razorpay settlement formats to the "
                    "canonical world CSVs.")
    ap.add_argument("fixture_dir")
    ap.add_argument("out_dir")
    args = ap.parse_args()

    collection, report_rows = load_fixture_dir(args.fixture_dir)
    settlements = settlements_from_api(collection, report_rows)
    payments = payments_from_report(report_rows)
    write_csv(os.path.join(args.out_dir, "settlements.csv"),
              S.SETTLEMENTS_COLUMNS, settlements)
    write_csv(os.path.join(args.out_dir, "payments.csv"),
              S.PAYMENTS_COLUMNS, payments)
    print(f"wrote {len(settlements)} settlement(s), {len(payments)} "
          f"payment(s) -> {args.out_dir}")


if __name__ == "__main__":
    main()
