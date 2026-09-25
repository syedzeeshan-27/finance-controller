# Synthetic bank-statement look-alikes

These files are **synthetic look-alikes, not real statements.**

They were built by `make_lookalikes.py` from public, general knowledge of how HDFC Bank, ICICI Bank, State Bank of India, Axis Bank and Kotak Mahindra Bank lay out their internet-banking statement exports (column names/order, date formats, balance conventions). No real people, accounts, VPAs or statements were used or referenced. Every name, account number, IFSC, VPA and amount is fabricated by a seeded random generator; any resemblance to a real person or account is coincidental.

Each `<bank>_<n>.xlsx`/`.csv` file has a matching `golden/<stem>.json` that records its header row, column layout, balance periods and noise rows, as described in `docs/provenance/lookalike_author_brief.md`.

Regenerate with `python scripts/make_lookalikes.py`.
