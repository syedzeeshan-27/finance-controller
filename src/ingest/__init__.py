"""Adapters from Razorpay's official data formats to the canonical world.

Two sources, one rule: field names and units are taken verbatim from the
official API schemas (amounts are already integer paise — passed through,
never converted), and every adapter's output must load through the untouched
`recon.io_load` parsers.

Platform limitation, stated rather than papered over: Razorpay test mode
yields no successful settlement records (docs: no real money moves in test
mode and settling is a live-mode function; observed: test-mode settlement
entries only ever show failed), so settlement-side ingestion runs on
schema-faithful fixtures (data/fixtures/razorpay/) while payment/order
ingestion (ingest.pull) can also run live against the test-mode API.
"""
