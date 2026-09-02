"""GSTR-1 (sales side), v1: the outward-supply summary the merchant would
file, derived from captured payments.

    python -m tax.gstr1 <data_dir> [--report [PATH]]

Honest scope, stated plainly: this world is B2C-small (no buyer GSTINs, no
invoice-level B2B section, no e-invoice/IRN), so GSTR-1 v1 is the
period-wise B2C(Others) summary — gross captured, transaction count, and
the output-liability figure under the same published simplification the
compliance loop already grades against (`rules.gst_liability`, 3% of
gross). One rule source: the number GSTR-1 reports here is BY CONSTRUCTION
the number Loop 3 expects the merchant to pay for that period — the two
surfaces cannot drift apart.
"""

from __future__ import annotations

import argparse
import os
from collections import Counter

from recon import io_load
from recon.normalize import parse_iso_date, rupee_str_from_paise
from tax import rules as TX


def build_gstr1(payments: list[dict]) -> dict:
    """period -> {gross_paise, invoice_count, liability_paise} over
    captured payments (B2C Others aggregation)."""
    gross: Counter = Counter()
    count: Counter = Counter()
    for p in payments:
        if p["status"] != "captured":
            continue
        period = TX.period_of(parse_iso_date(p["created_at"]))
        gross[period] += p["amount_paise"]
        count[period] += 1
    return {period: {"gross_paise": gross[period],
                     "invoice_count": count[period],
                     "liability_paise": TX.gst_liability(gross[period])}
            for period in sorted(gross)}


def _inr(paise: int) -> str:
    return "₹" + rupee_str_from_paise(paise, indian_grouping=True)


def render_gstr1_md(world: str, table: dict) -> str:
    lines = [
        f"# GSTR-1 summary (v1, B2C Others) — world {world} · GSTIN "
        f"{TX.MERCHANT_GSTIN}",
        "",
        "Outward supplies from captured payments; the liability column is "
        "the same published `gst_liability` rule Loop 3 grades statutory "
        "payments against — one rule source, no drift.",
        "",
        "| period | invoices | gross captured | output liability |",
        "|---|---|---|---|",
    ]
    for period, row in table.items():
        lines.append(f"| {period} | {row['invoice_count']} | "
                     f"{_inr(row['gross_paise'])} | "
                     f"{_inr(row['liability_paise'])} |")
    lines += [
        "",
        "Out of scope in v1 (documented, not hidden): B2B invoice-level "
        "sections (the world has no buyer GSTINs), credit/debit notes, "
        "e-invoice/IRN, multi-GSTIN.",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="GSTR-1 v1 sales-side summary.")
    ap.add_argument("data_dir")
    ap.add_argument("--report", nargs="?", const="", default=None,
                    metavar="PATH")
    args = ap.parse_args()

    payments = io_load.load_payments(args.data_dir)
    table = build_gstr1(payments)
    world = os.path.basename(os.path.normpath(args.data_dir))
    for period, row in table.items():
        print(f"{period}: {row['invoice_count']} invoices · gross "
              f"{_inr(row['gross_paise'])} · liability "
              f"{_inr(row['liability_paise'])}")
    if args.report is not None:
        path = args.report or os.path.join("reports", f"gstr1_{world}.md")
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(render_gstr1_md(world, table))
        print(f"report: {path}")


if __name__ == "__main__":
    main()
