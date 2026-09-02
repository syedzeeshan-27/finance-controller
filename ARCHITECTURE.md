# Architecture and results

Everything the [README](README.md) summarises, in depth. All numbers come from committed files in [reports/](reports/), regenerated end to end by `python scripts/repro.py`.

**Contents**

1. [Words used in this document](#words-used-in-this-document)
2. [Design principles](#design-principles)
3. [The data](#the-data)
4. [Module map](#module-map)
5. [Stage 1: Reconciliation](#stage-1-reconciliation)
6. [Stage 2: Cash forecasting](#stage-2-cash-forecasting)
7. [Stage 3: Tax matching](#stage-3-tax-matching)
8. [The agent layer and the real bank statement](#the-agent-layer-and-the-real-bank-statement)
9. [Razorpay ingestion](#razorpay-ingestion)
10. [The daily close and the queue](#the-daily-close-and-the-queue)
11. [Why the benchmark can be trusted](#why-the-benchmark-can-be-trusted)
12. [Reading the 100% honestly](#reading-the-100-honestly)
13. [Repository layout](#repository-layout)

---

## Words used in this document

- **Settlement.** Razorpay collects a batch of captured payments, deducts its fee and tax, and pays the net amount to the merchant's bank account. One settlement carries one UTR.
- **UTR.** Unique Transaction Reference: the bank transfer reference that travels with the money. It is the only legitimate key joining a settlement to a bank credit.
- **Settlement-to-bank matching.** Proving each settlement landed in the bank as a credit. This is the hard half of reconciliation. (In the code this is called "leg A".)
- **Payment-to-order matching.** Proving each captured payment belongs to an order and each order was paid. Simpler, because payments carry an order id. (In the code, "leg B".)
- **Abstain.** The engine's explicit "I cannot decide this from the evidence", shown with the candidates it considered.
- **Paise.** One hundredth of a rupee. Every amount in memory is an integer number of paise.
- **Golden file.** The answer key the generator writes at the moment it builds each record. Engines never read it; only graders do.
- **Seed.** The number that fixes the random generator so a world regenerates identically.
- **GSTR-2B.** The monthly statement the GST portal produces for a business, listing the input tax credit available based on what its vendors filed.
- **ITC.** Input tax credit: GST paid on purchases that can be set off against GST collected on sales.
- **Form 26AS.** The income-tax statement showing tax deducted at source (TDS) against a taxpayer, as filed by whoever deducted it.
- **WAPE.** Weighted absolute percentage error: total absolute error divided by total absolute actual.
- **Verifier.** A module that shares no code with an engine, re-reads the raw files with its own parsers, and checks the engine's claims.

## Design principles

The track brief quotes the 2026 builder consensus: verification capacity, not generation speed, is the bottleneck. That sentence is the specification.

- **Deterministic core.** Matching and every rupee of arithmetic is plain Python over integer paise. No floats, no tolerances, no LLM anywhere near a decision. A match rests on reference evidence (the UTR) or exact-paise amount equality. Never on "close enough".
- **Abstention is a first-class verdict.** When two candidates cannot be told apart on observable evidence, the engine refuses to guess and shows a reviewer both with reasons. The benchmark rewards this and grades a guess as a false match.
- **Independent verification.** Each stage has a verifier that shares no code with its engine. Any violation fails the whole run.
- **The agent proposes, arithmetic decides.** The AI layer (Claude, through the Anthropic SDK, in a hand-written tool loop) never touches a match. The intake agent proposes a bank statement's structure and a validator proves it to the paisa or rejects it. The investigator agent drafts advisory notes on queue items that are already final. No decision module imports the agent package (checked by test), and engine decisions are pinned identical with the agent layer present or absent. Every agent run records a transcript with per-request hashes, and replay re-runs it offline and fails loudly if anything drifted. The whole reproduction runs with no API key.

## The data

Six input files describe one merchant. All amounts are integer paise except the bank statement, which carries rupee strings on purpose, because normalising a real bank export is part of the job.

| File | One row per | What it carries |
|---|---|---|
| `payments.csv` | Razorpay payment | id, order id, method, amount, fee, tax, status, timestamp, settlement id |
| `settlements.csv` | Razorpay settlement | id, net amount, fees, tax, UTR, payment count, status, created and settled dates |
| `bank_statement.csv` | bank statement line | id, dates, narration, reference, debit, credit, running balance |
| `order_book.csv` | merchant order | id, receipt, amount, status, timestamp |
| `gstr2b.csv` | line a vendor filed against the merchant | vendor GSTIN, invoice number, period, taxable value, GST, tax head |
| `form26as.csv` | TDS entry a deductor filed | deductor, amount, financial-year quarter |

**Where they come from.** `src/recon/generate.py` builds a synthetic merchant for a given seed and number of days: an order book with weekday, trend and month-end volume shape; payments across UPI, card, netbanking and wallet with realistic fee rates; settlements on a T+N cycle with UTRs; a bank statement whose credits carry the settlement money through messy narrations; recurring bills (payroll, rent, GST, TDS, AWS, insurance, telecom) on a schedule; and the tax side, GSTR-2B and Form 26AS, with filing defects injected. Seed 42 is committed. Other seeds regenerate on demand.

**Ground truth is written at construction time.** When the generator builds a split settlement it writes "this settlement should match these three credits" into the golden file right then. No later labelling step, no LLM. The world is built clean first, then labelled scenario mutations are applied to chosen subsets, and every mutation writes its own golden rows.

**Ambiguity is constructed, not accidental.** A separation audit keeps every settlement net amount at least ₹10 apart from every other, except the pairs a scenario deliberately places closer: twins are equal to the paisa, near-collisions are ₹1 to ₹9 apart. Tests assert this per world. So "unique amount" in the golden file is provably unique, and "ambiguous" is provably undecidable.

**Scenarios injected.** Settlement side, fifteen types: clean UTR, truncated UTR, separator-mangled UTR, one-character typo, no UTR but unique amount, ambiguous twins, near-collision, missing bank credit, orphan bank credit, duplicate bank credit, split settlement (one to many), merged credits (many to one), matched with a known deduction (bank charge or 1% TDS), delayed settlement, and noise credits the engine must leave alone. Payment side, eight: paid clean, unpaid, cancelled, duplicate payment, orphan payment, amount mismatch, refunded, failed then captured. Tax side: amount mismatch, missing in 2B, filed late, duplicate line, wrong tax head, invoice typo, unknown invoice, blocked credit, short-paid and late-paid statutory payments.

**One real file.** `data/real/statement_a/statement.xlsx` is an anonymised Indian bank statement export with everything the generator never produces: banner rows before the header, two disjoint statement periods in one sheet, an interest credit mixed into the transactions, UPI narrations truncated mid-way, a same-reference reversal pair, and an abbreviation legend at the bottom.

## Module map

| Module | Job |
|---|---|
| `src/recon/generate.py` | synthetic merchant world, scenario injection, golden files |
| `src/recon/normalize.py` | integer-paise money, dates, UTR canonicalisation; the only parsers |
| `src/recon/io_load.py` | the only CSV loader; engines and baselines share it |
| `src/recon/engine.py` | settlement-to-bank matching: five evidence-scored passes |
| `src/recon/leg_b.py` | payment-to-order matching: duplicates, orphans, mismatches, refunds |
| `src/recon/journey.py` | order to payment to settlement to bank trace; names the first broken link |
| `src/recon/baselines.py` | the naive baseline, graded identically to the engine |
| `src/recon/benchmark.py` | golden-row grading, per-scenario tables, multi-seed runs, report writer |
| `src/recon/verify.py` | independent invariant checker; no shared code with the engine |
| `src/recon/explain.py` | display-only explanations from deterministic templates |
| `src/forecast/slicing.py` | the leakage wall: the only file reader, strict cutoff filters |
| `src/forecast/pipeline.py` | layer one: money already captured, via the reconciliation engine |
| `src/forecast/recurring.py` | layer two: recurring-bill detection with explicit gates |
| `src/forecast/residual.py` | layer three: weekday trimmed means with a clamped trend |
| `src/forecast/bands.py` | self-calibrating empirical 80% bands |
| `src/forecast/backtest.py` | rolling-origin harness, metrics, ablations, reports |
| `src/forecast/verify.py` | independent invariants on every forecast result |
| `src/tax/rules.py` | every published tax rule in one place |
| `src/tax/registry.py` | vendor master: GSTINs, narration markers, credit eligibility |
| `src/tax/books.py` | the merchant's side of the books, derived from world data and Stage 1 output |
| `src/tax/engine.py` | the three tax loops |
| `src/tax/baselines.py` | the naive amount-only tax matcher |
| `src/tax/benchmark.py` | golden-row grading, money metrics, reports |
| `src/tax/verify.py` | independent tax verifier with its own re-typed rules |
| `src/tax/gstr1.py` | GSTR-1 v1: sales-side summary from captured payments |
| `src/controller/schemas.py` | the queue item and daily close records |
| `src/controller/triage.py` | the published queue policy: severities, money at risk, actions, ordering |
| `src/controller/close.py` | the one-pass daily close and its CLI |
| `src/controller/verify_close.py` | independent close verifier |
| `src/controller/audit.py` | queue recall and precision against ground truth |
| `src/controller/queue_state.py` | operator actions on queue items with an append-only audit trail |
| `src/controller/snapshots.py` | close snapshots and day-over-day deltas |
| `src/model.py` | Anthropic client wrapper with an offline mock |
| `src/agent/provider.py` | live transport (records transcripts) and replay transport (hash-checked, offline) |
| `src/agent/loop.py` | the hand-written, bounded tool-use loop |
| `src/agent/rawgrid.py` | raw statement exports as a string grid |
| `src/agent/cells.py` | parsers for messy cells such as "1,23,456.78 Cr" |
| `src/agent/mapping.py` | the strict mapping schema the agent must submit |
| `src/agent/validator.py` | the proof: running-balance chain to the paisa |
| `src/agent/apply.py` | proven mapping to canonical CSVs |
| `src/agent/intake.py` | intake orchestration and CLI: record, replay, or hand-written mapping |
| `src/agent/investigate.py` | read-only evidence tools; advisory notes onto queue items |
| `src/ingest/razorpay_files.py` | official settlement entity and settlement report to canonical CSVs |
| `src/ingest/razorpay_api.py` | live API client: Basic auth, pagination, pure fetch |
| `src/ingest/pull.py` | payments and orders ingestion: fixtures by default, test-mode API with `--live` |
| `src/ingest/webhook_inbox.py` | signed webhook events, HMAC verified, consumed idempotently |
| `src/app.py` | the Streamlit dashboard |
| `scripts/repro.py` | the 10-step reproduction |

## Stage 1: Reconciliation

### How settlement-to-bank matching works

Five passes in `src/recon/engine.py`. Later passes see only records earlier passes left unclaimed.

0. **Scope and extraction.** Bank debits are out of scope. Each credit gets reference tokens extracted from its narration, and is tagged "settlement-shaped" if it carries a Razorpay marker or a UTR-shaped token.
1. **Exact UTR.** The settlement's UTR appears in the credit, verbatim or with separators mangled. Identity is established, and timing does not matter: a late credit with the right UTR still matches. Then amounts. Equal: matched at "exact" confidence. Not equal but explainable by a known deduction (₹29.50 bank charge, or 1% TDS): matched with discrepancy at "high". Several credits carry the same UTR: if all equal the amount, the first is the match and the rest are duplicates; if they sum to the amount, it is a split.
2. **Damaged UTR.** A fragment of ten or more characters, or a one-character corruption, counts only if the amount is also exactly equal. A damaged reference alone never matches.
3. **Merges.** A UTR-certain credit larger than its settlement matches only if the leftover equals exactly one other open settlement.
4. **Unique exact amount.** No reference at all. Exact paise equality inside a 10-day window, unique in both directions. Any tie and every party abstains with its candidate set listed.
5. **Residuals.** Whatever is left becomes an honest exception with the three nearest-miss candidates and why each was rejected: settlement never reached the bank, credit with no settlement, or non-settlement credit.

Confidence tiers: exact (UTR verbatim, amount equal), high (damaged UTR with exact amount, a decomposed deduction, a split, a merge), medium (unique exact amount, no reference), needs review (UTR certain but the leftover cannot be explained). Exceptions carry no tier.

### Results

From `reports/benchmark_report.md`, generated by `python -m recon.benchmark --seeds 42,43,44,45,46`. Per seed the graded batch is about 229 golden records (109 settlements and 120 bank credits) out of about 2,200 records processed. Both strategies run on identical data through identical parsing and grading.

| Strategy | Precision | Recall | F1 | Correct label | Correct abstentions | Verifier violations |
|---|---|---|---|---|---|---|
| Naive (amount within ₹1, 3-day window) | 97.5% | 79.2% | 87.4% | 74.1% | 0 of 30 | 0 |
| This engine | **100.0%** | **100.0%** | **100.0%** | **100.0%** | **30 of 30** | **0** |

Mean over five seeds. Payment-to-order matching: 100% correct labels over about 1,665 golden rows per seed.

Where the baseline fails (seed 42, share of correct labels per scenario):

| Scenario | Naive | This engine |
|---|---|---|
| Ambiguous twins (abstention required) | 0% | 100% |
| Split settlement (one to many) | 0% | 100% |
| Merged credits (many to one) | 0% | 100% |
| Bank charge or TDS deduction | 0% | 100% |
| Delayed settlement | 25% | 100% |
| Duplicate bank credit | 67% | 100% |
| Noise credits (must not claim) | 0% | 100% |

Throughput, measured on an Intel Core i5-1334U laptop, single process, noisy run to run: the engine parses and matches a 374-record batch in 10 to 20 ms, around 22,000 to 35,000 records per second. A full daily close over all six files (4,957 rows, three loops plus verifiers) runs at 5,000 to 7,000 records per second.

### How grading works

Every settlement and every bank credit is graded once, in `src/recon/benchmark.py`. For a record whose correct answer is a match, the engine is correct only if its status equals the expected status and its linked counterparties equal the golden set exactly. Matching two of three split parts is wrong. A wrong match counts as both a false positive and a missed match. For ambiguous and duplicate records, the status must match and the candidate list must cover the golden counterparties, because the reviewer has to be shown the right options. Forcing a match on one of those is a false positive.

## Stage 2: Cash forecasting

### How it works

Three deterministic layers, each separable in the output:

1. **Money already captured.** Payments captured but not yet settled are known future credits, not estimates. This layer reuses the Stage 1 engine to find what is in flight. Overdue settlements go to an attention list and never silently into the path.
2. **Recurring bills.** Payroll, rent, GST, TDS and subscriptions are detected from bank narration templates with explicit gates: at least three occurrences, a consistent gap (27 to 34 days for monthly), a consistent day-of-month anchor, and a stable amount. A key that fails a gate falls through to statistics instead of being guessed. The GST bill's next amount is computed from the published books rule (3% of last month's gross captured) rather than extrapolated.
3. **Weekday statistics.** For everything the first two layers cannot know: a trimmed mean over the trailing eight same-weekdays, dropping the min and max, times a trend ratio clamped between 0.8 and 1.25. Below 56 days of history this layer refuses to run. Below 98 days the bands are withheld. Both with warnings, not silence.

**Bands.** The same forecaster is re-run at six internal historical cutoffs (T-7 to T-42) and its errors against already-observed days become per-horizon error quantiles. No parametric assumption. Realised coverage is measured by the backtest, never asserted.

**Leakage wall.** `src/forecast/slicing.py` is the only file reader and applies strict cutoff filters. A test proves the forecaster's output is byte-identical whether future rows exist, are deleted, or are corrupted on disk.

### Results

From `reports/forecast_backtest.md`, generated by `python -m forecast.backtest` on seeds 42 to 51: 10 seeds, 7 rolling cutoffs each, 14-day horizon, 180-day worlds, so 70 held-out fortnights. Ten seeds rather than five because cash-drop events are rare by construction and the alert sample had to be visible.

| Strategy | Daily WAPE | Balance error (% of opening) | 80% band coverage | 14-day-low error | Alerts caught / missed / false |
|---|---|---|---|---|---|
| Zero net flow | 100.0% | ₹2,76,037 (6.3%) | none | ₹1,06,909 | 0 / 21 / 0 |
| Trailing 28-day mean | 83.3% | ₹1,83,033 (4.0%) | none | ₹1,05,498 | 0 / 21 / 0 |
| Naive weekday | 113.2% | ₹2,44,263 (5.3%) | none | ₹1,68,959 | 0 / 21 / 21 |
| **This forecaster** | **59.0%** | **₹76,454 (1.7%)** | **80.3%** | **₹30,041** | **17 / 4 / 2** |
| Without the captured-money layer | 60.9% | ₹81,284 (1.8%) | 81.8% | ₹36,301 | 17 / 4 / 3 |
| Without the recurring layer | 67.6% | ₹1,63,767 (3.5%) | 75.3% | ₹87,281 | 0 / 21 / 0 |

How to read it. WAPE is on the daily net flow, which is spiky by construction (settlement batches, the first-of-month bill cluster, noise debits), so 59% is not a day-by-day promise. What a cash planner reads is the balance path, within ₹76,454 of the truth on average (1.7% of a roughly ₹45 lakh opening balance, ₹1,23,230 by day 14), the 14-day low (within ₹30,041, dated within two days 96% of the time), and the alerts. The 80% band covers 80.3% on average but ranges from 53% to 97% per world: honest on average, not per merchant. Across 70 fortnights and three alert depths there were 21 true drops; the forecaster caught 17 with 2 false alarms, every baseline caught none, and the weekday baseline raised 21 false alarms. Most true drops fall in the same calendar fortnight across worlds, the one with payroll and rent on the first, so they are less independent than the count suggests. Every event is listed per origin in the report.

The ablations price each layer. Removing recurring-bill detection costs about ₹87,000 of balance error and every one of the 17 alert hits. Removing the captured-money layer costs less overall but its value sits where it should: day-one error goes from ₹16,245 to ₹23,421. Recurring detection found 70 of 70 scheduled bills across the 10 worlds with the right period, anchor and amount (within 10%), with zero false positives, graded against the generator's `golden_obligations.csv`. The GST bill's perfect amount score is the books rule being right, not the forecaster being clever; the other six keys were 10 of 10 either way.

## Stage 3: Tax matching

### How it works

Three loops in `src/tax/engine.py`. Later passes see only what earlier passes left unclaimed, like Stage 1.

1. **Input GST credit.** The merchant's purchases, derived from the bank statement plus a vendor registry (GST split by one published rule), plus a monthly consolidated Razorpay fee invoice recomputed from settlement fee and tax sums, matched against what vendors filed in GSTR-2B. Invoice reference first, exact amounts as corroboration, singleton pairing last. A damaged reference alone never matches. Outcomes: matched, deferred to next period, amount mismatch, tax-head mismatch, missing in 2B (vendor never filed), duplicate line, unknown invoice (filed against the merchant with no purchase behind it), and blocked credit where Section 17(5) says no credit regardless of what was filed.
2. **TDS credits.** The books side is Stage 1's output: every 1% marketplace TDS deduction the reconciliation engine decomposed at a bank credit, matched against Form 26AS by amount and financial-year quarter.
3. **Compliance.** GST liability (3% of the prior month's gross captured) and TDS deposits (10% of the prior month's actual posted payroll) recomputed from first principles and compared against the statutory payment debits: on time, short, late, not paid, or unverifiable because the basis predates the statement. Refusing to grade is better than guessing.

All rules live in `src/tax/rules.py`: the inclusive-GST split (taxable = total × 100 + 59, integer-divided by 118; GST is the remainder so the pair always sums exactly), the invoice reference rule (the last run of five or more digits in a narration), the liability and deposit rates, due days (20th for GST, 7th for TDS, rolled off Sundays), and the CGST/SGST split (intra-state GST halves, odd paisa to SGST, IGST never splits). `src/tax/verify.py` re-implements every rule with its own arithmetic and the two are forced to agree by test. `python -m tax.gstr1` produces a version-one sales-side GSTR-1 summary (B2C Others per period) using the same liability rule loop three grades against.

### Results

From `reports/tax_benchmark.md`, generated by `python -m tax.benchmark --seeds 42,43,44,45,46 --days 180`: about 460 to 520 golden tax records per world across GSTR-2B lines, purchases, Form 26AS entries, TDS events and statutory payment periods. Both strategies parse identical inputs and are graded identically (label and counterparty set must both match).

| Strategy | Correct label | Precision | Recall | Credit claimed falsely | Verifier violations |
|---|---|---|---|---|---|
| Naive (amount within ₹1, first come first served) | 51.3% | 59.3% | 81.9% | ₹1,65,084 | 330 |
| This engine | **100.0%** | **100.0%** | **100.0%** | **₹0** | **0** |

The money view is the point. Across five worlds the engine claims ₹5,45,661 of input credit, every paisa backed by a GSTR-2B line the vendor actually filed, and claims ₹0 falsely. The naive matcher claims ₹1,65,084 it must not: Section 17(5)-blocked food and travel credits, unknown invoices filed against the merchant, cross-vendor amount coincidences. The engine surfaces all ₹15,437 of credit at risk (vendor never filed), all ₹1,875 of TDS missing from 26AS, and all ₹8,810 of short-paid liability. Naive finds a third, all, and none of those, and scores zero on every defect scenario that matters: blocked 0 of 588, wrong head 0 of 20, filed late 0 of 36, short, late and unverifiable obligations 0 of 15.

Trust notes, as in Stage 1: ground truth is minted at generation time (`golden_tax.csv`) with defects injected under quotas; a behavioural test pins the engine's decisions byte-identical with the answer keys deleted or corrupted on disk; and the verifier hard-fails the benchmark on any violation. The naive baseline's 330 violations (double claims, ineligible claims) are the proof the checks have teeth.

## The agent layer and the real bank statement

### Intake: the agent finds the structure, arithmetic proves it

`python -m agent.intake` hands a raw export (Excel or CSV) to an agent as a grid of strings. The agent's only way to respond is a strict-schema mapping: header row, column roles, statement periods, row classes, a reference-extraction recipe, reversal pairs. The proposal is accepted only when `src/agent/validator.py` proves it: within every period, opening balance plus credits minus debits must reproduce the running-balance column row by row to the paisa and land exactly on the printed closing balance. Hiding a transaction, swapping debit and credit, or mislocating the header is arithmetically impossible to sneak past. The canonical output is then reloaded through the untouched production parsers and the chain is proven a second time.

Result on the real statement (`reports/real_data_report.md`): 65 raw rows became 48 canonical transactions across two periods, both balance chains verified to the paisa, the UPI reference recipe extracting a token from 47 of 48 rows, the reversal pair proven (same reference, equal and opposite amounts). Then the untouched engine claimed none of it: a personal UPI account has no Razorpay settlements, and all 12 credits landed as non-settlement credits instead of forced matches. The same abstention discipline the benchmark rewards, on data with no answer key.

The committed recording, `data/agent_transcripts/intake/statement_a.jsonl`, is a real live run: three API calls in which the agent looked at the grid, submitted a mapping the validator rejected, and resubmitted a repaired one that passed, producing a world byte-identical to the committed canonical statement. Repro step 8 and a test replay it with no key.

### Investigation: read-only tools, advisory output

`src/agent/investigate.py` gives an agent read-only tools over a precomputed close: the queue item, every decision mentioning a record, the statement around a date, the cash headline. Its only output is a note appended to the item's workflow trail as `agent:investigator`. A test pins that the close itself is byte-identical before and after. One real recorded run is committed (`data/agent_transcripts/investigate/seed42_6a547f24b524.jsonl`): three API calls, 8,302 input and 747 output tokens, about $0.04, on an S1 item where a ₹62,630.19 settlement never reached the bank. The agent searched the decisions and the statement window and drafted the note an operator would want: the settlement genuinely never landed, the orphan credit in the same window is not its counterpart, escalate to the PSP with the UTR.

### The runtime

`src/agent/loop.py` and `src/agent/provider.py` are a hand-written tool-use loop over the Anthropic SDK: strict tool schemas, all tool results returned in one message, hard call budgets, a failing tool becomes an error result rather than a dropped call. Live runs (`--record`) write a JSONL transcript with a SHA-256 of every outgoing request. Replay (`--replay`) re-runs the recorded conversation offline and raises on any drift between the recording and what the current code would send.

The dashboard degrades gracefully on bank-only worlds: Daily Close, Overview and Exceptions stay live on the real statement, and every section that needs Razorpay-side or generated data says plainly that it is unavailable. Nothing is synthesised to fill the gap.

## Razorpay ingestion

`src/ingest/` covers the real Razorpay formats with schema-faithful adapters over the official settlement entity, the per-transaction settlement report, payment and order entities, and signed webhook events. See `data/fixtures/razorpay/README.md`.

- `razorpay_api.py`: Basic auth against `api.razorpay.com/v1`, count and skip pagination, pure fetch, no interpretation.
- `pull.py`: maps payment and order entities to the canonical CSVs (amounts already in paise pass through, epochs become ISO timestamps). Fixtures by default; `--live` hits the test-mode API.
- `razorpay_files.py`: the settlement entity and settlement report to canonical CSVs.
- `webhook_inbox.py`: verifies `X-Razorpay-Signature` (HMAC-SHA256 of the raw body with the webhook secret, constant-time comparison) and consumes idempotently by entity id, so redeliveries update rather than duplicate. The committed inbox deliberately contains a redelivery.

The platform limitation: test mode yields no successful settlements. Razorpay documents that no real money moves in test mode and that settling is a live-mode function, and hands-on, test-mode settlement entries only ever showed "failed". So settlement-side ingestion is fixtures-first, and `ingest.pull --live` is unverified against a production account.

## The daily close and the queue

### One pass

`src/controller/close.py` loads a world once, runs the full settlement-to-bank reconciliation exactly once (a counting test pins it), and hands those decisions to payment-to-order matching, the journey tracer, the tax loops and the forecaster through guarded seams whose outputs are byte-identical to the standalone paths. Every decision that needs a human is triaged into one queue. The three stage verifiers run on the same outputs and their counts ship inside the close as the trust panel. The process exits non-zero if any verifier objects.

`python -m controller.close data/seeds/42d180 --report --json` writes the committed close (`reports/daily_close_42d180.md`) and its JSON twin, which `python -m controller.verify_close` re-checks independently.

### The triage policy

Published in one place, `src/controller/triage.py`, and total by test: every status from every loop is either queued at a severity or excluded with a reason.

- **S1, act today.** Statutory exposure (not paid, paid short, paid late), realised bank-side cash errors (settlement never reached the bank, duplicate credit), projected balance below the merchant's floor.
- **S2, chase externally.** Quantified money at risk with someone to chase: ambiguous abstains, credits with no settlement, credit the vendor never filed, duplicate or unknown 2B lines, TDS missing or duplicated in 26AS, payment-to-order amount breaks, duplicate payments.
- **S3, review.** Matched with an unexplained residual, unpaid orders, tax-head or quarter mismatches, unverifiable prior periods.

Within a tier, money at risk descending. Money at risk is a per-status mapping and always non-negative: a settlement that never arrived is worth its expected amount, a duplicate credit its extra posting, a wrong tax head zero because the money is right and only the bucket is wrong. The forecast's overdue-settlement attention list is provably a subset of the reconciliation exceptions at close time, so it fuses onto those items as "days overdue" enrichment instead of double-counting.

### The queue is actionable

Every item has a stable id (SHA-1 of source plus the sorted record ids; status deliberately excluded). Operators resolve, assign, snooze or annotate from the dashboard or `python -m controller.queue_state`. Workflow state is an append-only audit trail under `data/state/`, overlaid onto the freshly computed queue. The close never reads it, so same world in, byte-identical close out. An item whose underlying status changes after being resolved is flagged reopened instead of silently vanishing.

### Closes are incremental

`--as-of` re-runs the unmodified close over the world as it stood that day (raw files row-sliced into a temporary world, so the verifiers see exactly what the engines see). `--snapshot` freezes the queue, and the next close's report gains a "since last close" delta: new, carried, gone, with gone split into resolved by operator and resolved by the data. `--merchants data/merchants.json` closes every registered merchant; the registry includes the real-statement world, and multi-period exports get an honest "statement gap" warning rather than a false violation.

### Results

From `reports/close_audit.md`, generated by `python -m controller.audit --seeds 42,43,44,45,46 --days 180`. Every golden record whose expected outcome is queue-worthy under the published policy must surface in the queue under that exact status, and every queue item must be justified by a golden row. The contrast row feeds the same triage layer with the naive Stage 1 and Stage 3 baselines.

| Strategy | Queue recall | Money recall | Queue precision | Verifier violations |
|---|---|---|---|---|
| Naive engines | 1053 of 1266 (83.1%) | 22.0% | 48.1% | 330 |
| This project | **1266 of 1266 (100.0%)** | **100.0%** | **100.0%** | **0** |

`src/controller/verify_close.py` re-types the whole policy from literals with its own parsers, re-checks the queue both ways against the decisions embedded in the close, re-derives cash from the raw statement, and re-runs all three stage verifiers. A close that misreports its own trust panel is itself a violation.

## Why the benchmark can be trusted

1. **Ground truth is minted, not labelled.** The generator records the correct outcome for every record at the moment it constructs it. No LLM labels anything and the system never grades itself.
2. **"Unique" and "ambiguous" are proved, not assumed.** The separation audit keeps all settlement amounts at least ₹10 apart except designed collisions, and tests assert this per world.
3. **The scenarios are the reference list from real settlement operations.** UTR corruption of three kinds, missing money in both directions, duplicates, splits, merges, known deductions, delayed credits, near-collisions, constructed ambiguity, and noise the engine must leave alone.
4. **A leak canary.** If the naive matcher ever scores near the engine, the world got too easy. That check is visible in every report.
5. **Determinism end to end.** `python -m recon.generate --seed 42 --verify-determinism` generates twice and hash-compares. 323 tests pin the parsers, the generator invariants, every engine pass, the graders' arithmetic, the forecaster's layers and leakage wall, the tax rules and the head-split agreement, the close's one-pass and triage totality, queue-state purity, snapshot deltas, the ingestion adapters and webhook signatures, the intake validator's rejection suite, agent-loop discipline and replay integrity, all four verifiers' ability to catch corrupted output, the golden-blindness canaries, and the agent quarantine.

## Reading the 100% honestly

The engines, and the close audit that inherits from them, score perfectly because the benchmark's scenarios are deterministic constructions and each engine exploits exactly the evidence its scenarios leave behind, including abstaining on the cases constructed to be undecidable. That is the designed behaviour, verified independently. It is not a claim that real bank or GST data would reconcile at 100%. What matters: nothing is force-matched, every decision is evidenced, the failure modes the baselines exhibit are covered, and the whole loop reproduces from a clean checkout. What was never measured at all (real Razorpay settlement data, real GST and TDS data, production volumes) is listed in the README under [What is real and what is synthetic](README.md#what-is-real-and-what-is-synthetic).

What 100% does not cover: scenario types the generator does not produce (fraud, currency conversion, multiple payment providers interleaved, narration dialects beyond the real fixture and the templates). On the tax side, GSTR-1 is a version-one B2C summary (invoice-level B2B sections need buyer GSTINs the world does not model), the 3%-of-gross liability is a documented simplification of output GST net of input credit, the CGST/SGST split is published arithmetic applied on the books side rather than a per-return filing engine, and e-invoicing and multi-GSTIN merchants are out of scope.

Known engineering debt, stated rather than hidden: `src/recon/generate.py` is 1,800 lines and its two largest functions would split cleanly by scenario; `src/app.py` is an 800-line Streamlit script rather than composed views; a few rupee formatters and CSV readers are duplicated where the verifiers' copies are deliberate and the rest is drift; and `reports/daily_close_42d180.json` embeds every decision, 1.9 MB of committed evidence. None of it changes a number.

## Repository layout

- `data/seeds/42/`: the committed world (other seeds regenerate on demand)
- `data/real/statement_a/`: the anonymised real bank statement, its proven mapping and canonical world
- `data/fixtures/razorpay/`: official-schema API and webhook fixtures
- `data/merchants.json`: the merchant registry (dashboard selector and `close --merchants`)
- `data/state/` (gitignored): operator workflow state and close snapshots
- `data/agent_transcripts/`: the two recorded agent runs
- `reports/`: committed benchmark artifacts every number above is taken from
- `scripts/repro.py`: the 10-step reproduction; `repro.ps1` and `repro.sh` are wrappers
- `tests/`: 323 tests

### Status

Stage 1 reconciliation, Stage 2 cash forecasting, Stage 3 tax matching and the unified daily close are all done. Post-review hardening is done: agent intake proven on a real bank statement, an actionable queue with an audit trail, incremental as-of closes with deltas, a merchant registry, official-schema Razorpay ingestion with signed webhooks, the CGST/SGST split and GSTR-1 v1, and a one-command cross-platform reproduction.
