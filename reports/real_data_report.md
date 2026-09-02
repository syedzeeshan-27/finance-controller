# Real-data run — messy bank export through the untouched engine

Source: `statement.xlsx` · intake mode: **replay** · 65 raw grid rows → 48 canonical transaction rows.

The intake agent proposed the statement's structure (header, columns, periods, row classes, reference recipe); a deterministic validator then **proved** the proposal arithmetically before a single canonical row was written. No LLM output is trusted — only verified.

## The proof: running-balance chain, to the paisa

| period | opening | txn rows | credits | debits | closing | chain |
|---|---|---|---|---|---|---|
| 0 | ₹7,881.25 | 38 | ₹4,198.00 | ₹5,830.00 | ₹6,249.25 | verified row-by-row ✓ |
| 1 | ₹42,498.25 | 10 | ₹7,350.00 | ₹26,872.00 | ₹22,976.25 | verified row-by-row ✓ |

Every row's stated balance equals the recomputed opening + credits − debits at that row, and each period ends exactly on its printed closing balance. After writing the canonical CSV the chain is proven a second time from `recon.io_load`'s typed rows — an independent parse of the same numbers.

## What the statement contains

- header row + 12 noise rows (banners, blanks, abbreviation legend) — none transaction-like, by check;
- 48 transactions across 2 statement period(s);
- reference recipe `{'kind': 'delimited', 'delimiter': '/', 'index': 1, 'token_regex': '\\d{12}'}` extracted a token from 47/48 transactions;
- 1 reversal pair(s) proven — same reference, equal and opposite amounts (rows 35+36);
- note: statement holds 2 disjoint periods; continuity across the gap before period 1 is not required.

## The untouched engine's verdict

| status | rows |
|---|---|
| `non_settlement_credit` | 12 |
| `out_of_scope` | 36 |

Read honestly: this is a personal UPI account — 12 credits, none of them Razorpay settlements — and the engine **claims none of them**. Every credit lands as a non-settlement classification rather than a forced match, which is exactly the abstention discipline the synthetic benchmark rewards, now demonstrated on real data the generator never produced: truncated UPI narrations, an interest credit, a same-reference reversal, and two disjoint statement periods in one sheet.

Reproduce offline (no API key): `python -m agent.intake data/real/statement_a/statement.xlsx --out <dir> --replay data/agent_transcripts/intake/statement_a.jsonl --report` — the transcript replays the recorded agent conversation and fails loudly if the harness has drifted from it.
