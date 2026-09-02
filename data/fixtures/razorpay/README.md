# Razorpay official-format fixtures

These files are **schema-faithful** to Razorpay's documented API formats —
field names, nesting, and units are verbatim from the official docs:

- `settlements_api.json` — the settlement entity as returned by
  `GET /v1/settlements`, inside the standard collection envelope. Amounts
  (`amount`, `fees`, `tax`) are integer paise, `created_at` is a Unix epoch,
  `utr` is the bank transfer reference.
- `settlement_recon_report.csv` — the per-transaction settlement report
  (`settlement.reports` / settlement recon), one row per payment / refund
  with `entity_id`, `type`, `debit`/`credit`/`amount` in paise, `fee`,
  `tax`, `settlement_id`, `settlement_utr`, `order_id`, `method`.
- `payments_api.json`, `orders_api.json` — payment and order entities as
  returned by `GET /v1/payments` / `GET /v1/orders`, consumed by
  `ingest.pull` (default fixture mode; `--live` pulls the same shapes from
  the test-mode API with `RAZORPAY_KEY_ID`/`RAZORPAY_KEY_SECRET`).
- `webhooks/` — recorded webhook deliveries in the official event envelope
  (`{"entity": "event", "event": "payment.captured", "payload": …}`), one
  JSON per delivery, each with a sidecar `.sig` holding the
  `X-Razorpay-Signature` value: HMAC-SHA256 of the exact file bytes with
  the demo secret `rzp_demo_webhook_secret`. `ingest.webhook_inbox`
  verifies every signature and consumes idempotently (the committed inbox
  deliberately contains a redelivery). These files are marked `-text` in
  `.gitattributes` so EOL conversion can never break the signatures.

## Why fixtures and not live settlement pulls

What Razorpay documents: Test Mode moves no real money ("No real money is
used in the test mode") and settling to a bank account is described only for
Live Mode ([Test and Live Modes](https://razorpay.com/docs/payments/dashboard/test-live-modes/));
the Settlements FAQ says nothing about test mode at all. What we observed
hands-on (Aug–Sep 2026): test-mode settlement entries do appear on the
dashboard, but only ever as **failed** — never processed — whichever payment
method was used. Either way, **no successful settlement → bank trail can be
obtained from test mode**. That is not a limitation of this project and
cannot be worked around from the outside.

The honest substitute is what you see here: adapters
(`src/ingest/razorpay_files.py`) written against the official schemas, fed
by fixtures that mirror those schemas exactly, with paise passed through
untouched and every output required to load through the same
`recon.io_load` parsers as generated worlds. If Razorpay ever settles a
live-mode account into this pipeline, the only change is the data source.
