# Architecture & Results — the full design doc

Everything the [README](README.md) summarizes, in depth. All numbers quoted
here are from committed artifacts in [reports/](reports/), regenerated end to
end by `python scripts/repro.py`.

**Contents**
1. [Design principles](#design-principles)
2. [Module map — every file and its job](#module-map)
3. [Stage 1 — Reconciliation](#stage-1--reconciliation)
4. [Stage 2 — Cash forecasting](#stage-2--cash-forecasting)
5. [Stage 3 — Tax matching](#stage-3--tax-matching)
6. [The agentic layer & real data](#the-agentic-layer--real-data)
7. [Final stage — the daily close & actionable queue](#final-stage--the-daily-close)
8. [Why the benchmark can be trusted](#why-the-benchmark-can-be-trusted)
9. [Read the engines' 100% honestly](#read-the-engines-100-honestly)
10. [Repository layout & roadmap](#repository-layout)

---

## Design principles

The 2026 builder consensus the track quotes — *verification capacity, not
generation speed, is the bottleneck* — is the spec:

- **Deterministic core.** Matching and every rupee of arithmetic is plain
  Python over integer paise. No floats, no tolerances-as-matches, no LLM
  anywhere near a decision. A match rests on reference evidence (UTR) or
  exact-paise amount equality — never on "close enough".
- **Abstention is a first-class verdict.** When two candidates cannot be told
  apart on observable evidence, the engine refuses to guess and shows a
  reviewer both candidates with reasons. The benchmark *rewards* this:
  guessing is graded as a false match.
- **Independent verification.** `src/recon/verify.py` shares no code with the
  engine. It re-reads the raw CSVs with its own parsers and checks every
  claim: nothing claimed twice, nothing dropped, every discrepancy
  decomposition sums, every confidence tier backed by its qualifying
  evidence. Any violation fails the whole benchmark run.
- **The agent proposes; arithmetic disposes.** The agentic layer (Claude via
  the Anthropic SDK, a hand-rolled visible tool loop) never touches a match:
  the intake agent proposes a statement's *structure* and a deterministic
  validator proves it to the paisa or rejects it; the investigator agent
  drafts advisory notes on already-final queue items. Quarantine is scanned
  and behavioral: no decision module imports `agent`, and engine decisions
  are pinned identical with the agent layer present or absent
  (`tests/test_agent_intake.py`). Every agent run records a JSONL transcript
  with per-request hashes; `--replay` re-runs a recorded conversation offline
  and fails loudly if the harness drifted. The entire repro runs with no key
  at all.

## Module map

| module | role |
|---|---|
| `src/recon/generate.py` | synthetic Razorpay-merchant world + scenario injection + golden files |
| `src/recon/normalize.py` | integer-paise money, dates, UTR canonicalisation — the only parsers |
| `src/recon/io_load.py` | the only CSV→typed-record loader (engines and baselines share it) |
| `src/recon/engine.py` | leg A: 6-pass evidence-scored matcher (pass order below) |
| `src/recon/leg_b.py` | payment ↔ order rules (duplicates, orphans, mismatches, refunds) |
| `src/recon/journey.py` | order → payment → settlement → bank trace, first broken link named |
| `src/recon/baselines.py` | the `naive` baseline — graded identically to the engine |
| `src/recon/benchmark.py` | golden-row grading, per-scenario tables, multi-seed, report writer |
| `src/recon/verify.py` | independent invariant checker — no shared code with the engine |
| `src/recon/explain.py` | display-only explanations (deterministic templates, LLM optional) |
| `src/forecast/slicing.py` | the leakage wall: the only file reader, strict cutoff filters |
| `src/forecast/pipeline.py` | layer a: known in-flight inflows via the recon engine |
| `src/forecast/recurring.py` | layer b: obligation detection (template keys + periodicity gates) |
| `src/forecast/residual.py` | layer c: weekday trimmed means + clamped trend |
| `src/forecast/bands.py` | self-calibrating empirical 80% bands (internal re-runs) |
| `src/forecast/backtest.py` | rolling-origin harness, metrics, ablations, reports |
| `src/forecast/verify.py` | independent invariants on every forecast result |
| `src/tax/rules.py` | every published tax rule (GST split, head split, liabilities, refs, periods) — one source |
| `src/tax/registry.py` | vendor master: GSTINs, narration markers, ITC posture (eligible/blocked/none) |
| `src/tax/books.py` | the merchant-books side, derived blind from world data + Stage 1 output |
| `src/tax/engine.py` | the three tax loops: evidence-scored passes, honest residuals |
| `src/tax/baselines.py` | the `naive_tax` amount-only matcher — graded identically |
| `src/tax/benchmark.py` | golden-row grading, money metrics, per-scenario tables, reports |
| `src/tax/verify.py` | independent tax verifier — own parsers, own re-typed rules |
| `src/tax/gstr1.py` | GSTR-1 v1: sales-side B2C summary from captured payments, one liability rule source |
| `src/controller/schemas.py` | `ExceptionItem` / `DailyClose` dataclasses |
| `src/controller/triage.py` | the published queue policy: severities, money at risk, actions, ordering |
| `src/controller/close.py` | the one-pass daily close, `--as-of` / `--snapshot` / `--merchants`, markdown + JSON, CLI |
| `src/controller/verify_close.py` | independent close verifier — re-typed policy + stage verifiers re-run |
| `src/controller/audit.py` | queue recall/precision vs minted truth, naive-engines contrast |
| `src/controller/queue_state.py` | operator workflow on queue items: stable ids, append-only audit trail, reopen detection |
| `src/controller/snapshots.py` | close snapshots + day-over-day delta keyed by stable item ids |
| `src/model.py` | Anthropic provider wrapper: offline mock without a key, `complete` for display text |
| `src/agent/provider.py` | agent transports: live (records JSONL transcripts) and replay (hash-checked, offline) |
| `src/agent/loop.py` | the hand-rolled, bounded, visible tool-use loop |
| `src/agent/rawgrid.py` | raw statement exports as a string grid, one published cell-coercion rule |
| `src/agent/cells.py` | messy-cell parsers ("1,23,456.78 Cr", parentheses negatives) — recon parsers stay untouched |
| `src/agent/mapping.py` | `StatementMapping` + the strict JSON schema the agent must submit |
| `src/agent/validator.py` | the proof: running-balance chain to the paisa, partition/anti-hiding checks |
| `src/agent/apply.py` | proven mapping → canonical world CSVs (production writers) |
| `src/agent/intake.py` | orchestration + CLI: record / replay / hand-mapped, post-apply re-proof through io_load |
| `src/agent/report.py` | the real-data report renderer |
| `src/agent/investigate.py` | read-only evidence tools; advisory notes onto queue items, never dispositions |
| `src/ingest/razorpay_files.py` | official settlement entity + settlement report → canonical CSVs (paise passthrough) |
| `src/ingest/razorpay_api.py` | stdlib-only live API client (Basic auth, pagination) — pure fetch |
| `src/ingest/pull.py` | payments/orders ingestion: fixtures by default, test-mode API with `--live` |
| `src/ingest/webhook_inbox.py` | signed webhook events (HMAC verified), idempotent consumption |
| `src/app.py` | Streamlit dashboard: daily close with an actionable queue, overview, matches, exceptions, journey, forecast, tax, benchmark |
| `scripts/repro.py` | the 9-step single-source repro (`repro.ps1` / `repro.sh` are wrappers) |

Engine pass order (later passes see only records earlier passes left unclaimed):

1. **Exact UTR** — verbatim or separator-mangled reference; window-independent
   (identity beats timing). Resolves clean, delayed, duplicate,
   split-by-shared-UTR and decomposable-discrepancy (bank charge ₹29.50 incl.
   GST, 1% TDS) cases.
2. **Fuzzy UTR** — ≥10-char fragment or one-character corruption, and only
   with exact-amount corroboration. A damaged reference alone never matches.
3. **Merge resolution** — a UTR-certain credit larger than its settlement
   matches only if the residual equals exactly one other open settlement.
4. **Unique exact amount** — no reference at all; requires exact paise
   equality unique in *both* directions inside a 10-day window. Any tie → all
   parties abstain.
5. **Residuals** — honest exceptions, each with top-3 nearest-miss candidates
   and per-candidate rejection reasons.

(Pass 0 splits scope and extracts UTR tokens; debits are out of scope for leg A.)

## Stage 1 — Reconciliation

All numbers are from `reports/benchmark_report.md`, generated by
`python -m recon.benchmark --seeds 42,43,44,45,46`. Per seed, the graded batch
is ~229 leg-A golden records (109 settlements + 120 bank credits, out of
~2,200 total records processed). Both strategies run on identical data through
identical parsing and grading:

| strategy | precision | recall | F1 | disposition accuracy | correct abstentions | invariant violations |
|---|---|---|---|---|---|---|
| `naive` (amount ±₹1, 3-day window) | 97.5% | 79.2% | 87.4% | 74.1% | 0/30 | 0 |
| `recon_engine` (this project) | **100.0%** | **100.0%** | **100.0%** | **100.0%** | **30/30** | **0** |

Mean over 5 seeds; throughput ≈ 41,000 records/sec for the engine (~7ms per
batch). Leg B (payment ↔ order book): 100% disposition accuracy over ~1,665
golden rows/seed.

Where the baseline actually fails (seed 42, disposition accuracy per scenario):

| scenario | naive | recon_engine |
|---|---|---|
| ambiguous twins (abstention required) | 0% | 100% |
| split settlement (1 → N credits) | 0% | 100% |
| merged credits (N → 1 credit) | 0% | 100% |
| bank charge / TDS discrepancy | 0% | 100% |
| delayed settlement (T+5..T+7) | 25% | 100% |
| duplicate bank credit | 67% | 100% |
| noise credits (must not claim) | 0% | 100% |

## Stage 2 — Cash forecasting

From `reports/forecast_backtest.md`, generated by `python -m forecast.backtest`
(seeds 42–51): 10 seeds × 7 rolling origins × 14-day horizon on 180-day worlds
= 70 held-out origins — twice the other stages' seed set, because
threshold-crossing events are rare by construction and the alert sample had to
be visible, not implied. Ground truth is the generator's **held-out
post-cutoff bank statement** — a leakage test proves the forecaster's output
is byte-identical whether future rows exist, are deleted, or are corrupted on
disk.

| strategy | WAPE (daily net) | balance MAE (% of opening) | coverage80 | 14-day-low err | alerts H/M/FA (₹2L·3L·4L pooled) |
|---|---|---|---|---|---|
| zero_net | 100.0% | ₹2,76,037 (6.3%) | — | ₹1,06,909 | 0/21/0 |
| trailing_mean_28 | 83.3% | ₹1,83,033 (4.0%) | — | ₹1,05,498 | 0/21/0 |
| naive_weekday | 113.2% | ₹2,44,263 (5.3%) | — | ₹1,68,959 | 0/21/21 |
| **forecaster** | **59.1%** | **₹77,208 (1.8%)** | **79.9%** | **₹30,041** | **17/4/2** |
| forecaster (no pipeline layer) | 61.1% | ₹82,179 (1.8%) | 81.5% | ₹36,301 | 17/4/3 |
| forecaster (no recurring layer) | 67.6% | ₹1,63,767 (3.5%) | 75.3% | ₹87,281 | 0/21/0 |

What the table says, honestly — two readings on purpose. **Absolute terms
first:** WAPE is measured on the daily net flow, a series that is spiky by
construction (settlement batches, the 1st-of-month obligation cluster, noise
debits), so 59% daily error is *not* a day-by-day promise — the number a cash
planner acts on is the balance path, which sits within ₹77,208 of the truth on
average (1.8% of a ~₹45L opening balance; ₹1,24,750 by day 14), and the 14-day
low, within ₹30,041 and dated within two days 96% of the time. The **80% band
covers 79.9%** of outcomes on average (self-calibrated from the forecaster's
own historical errors — measured, not asserted) but ranges from 53% to 97% per
world: honest on average, not per merchant. **Alerts:** across 70 origins ×
three depths there were 21 true drops; the forecaster caught 17 with 2 false
alarms (₹2L 10/1/2 · ₹3L 7/1/0 · ₹4L 0/2/0 — hits/misses/false alarms), while
every baseline caught none and the weekday baseline raised 21 false alarms.
That is still a small sample, and most true drops are the same calendar
fortnight — the one holding the 1st-of-month payroll + rent cluster — across
worlds, so they are less independent than the count suggests; every event is
listed per origin in the report. The ablations price each layer:
recurring-obligation detection is worth ~₹87k of balance MAE and all 17 hits;
the pipeline layer's value concentrates where it should — day-1 error (₹16.2k
vs ₹23.4k without it) and alert precision. Recurring detection found **70/70**
scheduled obligations across the 10 worlds with the right period and anchor,
and the amount within ±10% for 66/70 — the four misses are all the GST
obligation, whose amount tracks the prior month's gross and so varies month to
month — with **0 false positives**, graded against the generator's minted
`golden_obligations.csv`.

The forecaster is three deterministic layers, each separable in the output:

1. **Pipeline-known inflows** — money already captured is a *known* future
   credit (settlement mechanics, reusing the Stage 1 engine), not a statistic.
   Overdue settlements go to an attention list, never silently into the path.
2. **Recurring obligations** — payroll/rent/GST/TDS/subscriptions detected
   from narration-template periodicity with explicit gates; rejected keys fall
   through to statistics instead of being guessed.
3. **Weekday statistics** — trimmed means with a clamped trend ratio, only for
   what the first two layers genuinely cannot know. Below 56 days of history
   the layer refuses to run; below 98 days bands are withheld — with warnings,
   not silence.

## Stage 3 — Tax matching

From `reports/tax_benchmark.md`, generated by
`python -m tax.benchmark --seeds 42,43,44,45,46 --days 180`: ~460–520 golden
tax records per world across GSTR-2B lines, purchases, Form 26AS entries,
observed TDS events and statutory payment periods. Both strategies parse
identical inputs and are graded identically (disposition AND counterparty set
must match):

| strategy | disposition acc | precision | recall | **₹ claimed falsely** | verify violations |
|---|---|---|---|---|---|
| `naive_tax` (amount ±₹1, FCFS) | 51.3% | 59.3% | 81.9% | ₹1,65,084 | 330 |
| `tax_engine` (this project) | **100.0%** | **100.0%** | **100.0%** | **₹0** | **0** |

The money view is the point. Across 5 worlds the engine claims ₹5,45,661 of
input credit — every paisa of it backed by a GSTR-2B line the vendor actually
filed — and claims **₹0 falsely**; the naive matcher claims ₹1,65,084 it must
not (Section 17(5)-blocked food/travel credits, unknown invoices filed against
the merchant, cross-vendor amount coincidences). The engine surfaces **all**
₹15,437 of ITC at risk (vendor never filed), all ₹1,875 of TDS credits missing
from 26AS, and all ₹8,810 of short-paid liability; naive finds a third, all,
and none of those respectively — and scores 0% on every defect scenario that
matters (blocked 0/588, wrong-head 0/20, filed-late 0/36,
short/late/unverifiable obligations 0/15).

The three loops, and where their books side comes from:

1. **Input GST (ITC)** — purchases derived blind from the bank statement + a
   vendor registry (GST split by one published pair rule), plus a monthly
   consolidated Razorpay fee invoice recomputed from settlement fee/tax sums.
   Matching: invoice-reference identity first, exact amounts as corroboration,
   singleton pairing last; a damaged reference alone never matches, and an
   unequal refless singleton is refused (indistinguishable from
   missing+unknown).
2. **TDS credits** — the books side is literally Stage 1's output: every 1%
   marketplace-TDS deduction the reconciliation engine decomposed at credit,
   matched against what the deductor filed in Form 26AS (amount, and FY
   quarter).
3. **Compliance** — GST liability (3% of prior-month gross captured) and TDS
   deposits (10% of the prior month's *actual posted payroll*) recomputed from
   first principles and compared against the statement: on-time / short /
   late / not paid — and `unverifiable_prior_period` when the basis precedes
   the statement, because refusing to grade is better than guessing.

Head breadth: the CGST/SGST split is published arithmetic
(`tax.rules.split_heads` — intra-state GST halves, odd paisa to SGST, IGST
never splits), applied books-side to every 2B-filing purchase, re-typed
independently in `tax/verify.py` with agreement forced by test, and surfaced
in the close as "ITC claimable by head". `python -m tax.gstr1` produces the
sales-side GSTR-1 v1 summary (B2C Others, per period) using the **same**
liability rule Loop 3 grades statutory payments against — one rule source, no
drift.

Trust notes, Stage-1 style: ground truth is minted at generation time
(`golden_tax.csv`) with defects injected under quotas (amount mismatch,
missing, filed-late, duplicate, wrong tax head, invoice-number typo, unknown
invoice, blocked credits, short/late payments); a behavioral test pins the
engine's decisions byte-identical with the answer keys deleted or corrupted on
disk; and `src/tax/verify.py` re-implements every rule with its own parsers
and hard-fails the benchmark on any violation — the naive baseline's 330
violations (double-claims, ineligible claims) are the canary's proof the
checks have teeth.

## The agentic layer & real data

From `reports/real_data_report.md`, regenerated by repro step 8. The committed
fixture `data/real/statement_a/statement.xlsx` is a real (anonymized) Indian
bank statement export with everything the generator never produces: banner
rows before the header, **two disjoint statement periods in one sheet** (May
and July, with a June hole), an interest credit mixed into the transactions,
UPI narrations truncated mid-VPA, a same-reference REVERSAL row, and an
abbreviations legend at the bottom.

`python -m agent.intake` hands the raw grid to an agent whose only submission
channel is a strict-schema mapping proposal (header location, column roles,
period segmentation, row classes, a reference-extraction recipe, reversal
pairs). The proposal is accepted **only** when the deterministic validator
proves it: within every period, opening balance + credits − debits must
reproduce the running-balance column row by row to the paisa and land exactly
on the printed closing balance — so hiding a transaction, swapping
debit/credit, or mislocating the header is arithmetically impossible to sneak
past. The canonical output is then re-loaded through the untouched production
parsers and the chain is proven a second time.

Result on the real statement: 65 raw rows → 48 canonical transactions, both
period chains verified to the paisa, the UPI RRN recipe extracting references
from 47/48 rows, the reversal pair proven (same reference, equal and opposite
amounts) — and the untouched engine then **claims none of it**: a personal UPI
account has no Razorpay settlements, and every credit lands as
`non_settlement_credit` instead of a forced match. The same abstention
discipline the benchmark rewards, demonstrated on data with no answer key.

The dashboard degrades gracefully on such bank-only worlds: Daily Close,
Overview and Exceptions stay live on the real statement's reconciliation,
while every section that needs PSP-side or generated data (match confidence,
Matches, Journey, Forecast, Tax) says plainly that it is not available for
this world — nothing is synthesized to fill the gap. One capability check in
`src/app.py` (`_has`) covers all of them, and `tests/test_app_smoke.py` pins
both world classes rendering end-to-end without an exception.

The agent runtime (`src/agent/loop.py`, `src/agent/provider.py`) is a
hand-rolled tool-use loop over the Anthropic SDK: strict tool schemas, all
tool results returned in one message, hard call budgets, `pause_turn`
handling. Live runs (`--record`) write a JSONL transcript with a SHA-256 of
every outgoing request; `--replay` re-runs the recorded conversation offline
and raises on any drift between the recording and what the current harness
would send. The committed recording
(`data/agent_transcripts/intake/statement_a.jsonl`) is a real live run over
the real statement — three API calls in which the agent peeked at the grid,
submitted a mapping the validator **rejected**, and resubmitted a repaired
one that passed the balance-chain proof, producing a world byte-identical to
the committed canonical statement. Repro step 8 and
`test_intake_replay_reproduces_committed_world` replay it with no key. The investigator agent (`src/agent/investigate.py`) gets read-only
tools over a precomputed close (the queue item, every decision mentioning a
record, the statement around a date, the cash headline) and its only output is
an advisory note appended to the item's workflow trail as
`agent:investigator` — a test pins that the close itself is byte-identical
before and after.

Razorpay-side real formats are covered by schema-faithful adapters
(`src/ingest/`) over the official settlement entity, per-transaction
settlement report, payment/order entities and signed webhook events — see
`data/fixtures/razorpay/README.md`, including the platform limitation that
**test mode yields no successful settlements** (documented: no real money
moves and settling is a Live-mode function; observed hands-on: test-mode
settlement entries only ever show *failed*) — which is why settlement-side
ingestion is fixtures-first, while `ingest.pull --live` can hit the test-mode
API for payments/orders, and `ingest.webhook_inbox` verifies
`X-Razorpay-Signature` HMACs and consumes idempotently.

## Final stage — the daily close

From `reports/close_audit.md`, generated by
`python -m controller.audit --seeds 42,43,44,45,46 --days 180`. The unified
queue's measured claim: every golden record whose expected disposition is
queue-worthy under the published triage policy must surface in the queue under
that exact status, and every non-synthetic queue item must be justified by a
golden row. The contrast row feeds the **same** triage layer with the naive
Stage 1 + Stage 3 baselines:

| strategy | queue recall | money recall | queue precision | verify violations |
|---|---|---|---|---|
| `naive_engines` | 1053/1266 (83.1%) | 22.0% | 48.1% | 330 |
| `daily_close` (this project) | **1266/1266 (100.0%)** | **100.0%** | **100.0%** | **0** |

One full daily close — load once, reconcile once, forecast, run all three tax
loops, triage, verify — processes thousands of records per second and exits
nonzero if any verifier objects. `python -m controller.close
data/seeds/42d180 --report --json` writes the committed human-readable close
(`reports/daily_close_42d180.md`) and its JSON twin, which
`python -m controller.verify_close` re-checks independently.

**The queue is actionable, not a report.** Every item has a stable id
(`sha1(source | sorted record ids)` — status deliberately excluded), and
operators resolve / assign / snooze / annotate items from the dashboard or
`python -m controller.queue_state`. Workflow state is an append-only audit
trail under `data/state/`, overlaid onto the freshly computed queue —
`daily_close` never reads it, so "same world in, byte-identical close out"
survives (pinned by test), and an item whose underlying status changes after
being resolved is flagged **reopened** instead of silently vanishing.

**Closes are incremental.** `--as-of` re-runs the *unmodified* close over the
world as it stood that day (raw files row-sliced into a temp world, so the
verifiers see exactly what the engines see), `--snapshot` freezes the queue,
and the next close's report gains a "Since last close" delta — new / carried /
gone, with gone split into resolved-by-operator vs resolved-by-the-data. A
`--merchants data/merchants.json` rollup closes every registered merchant (the
committed registry includes the real-statement world; real multi-period
exports get an honest "statement gap" warning rather than a false violation —
the leg-A verifier tolerates a balance discontinuity only across a value-date
gap wider than 7 days).

The queue policy is published in one place (`src/controller/triage.py`) and
total by test — every status from every loop is either queued at a severity or
excluded with a reason:

- **S1 — act today**: statutory exposure (`not_paid`, `paid_short`,
  `paid_late`), realised bank-side cash errors (`exception_missing_bank`,
  `duplicate_credit`), projected insolvency (balance below the chosen
  threshold).
- **S2 — chase externally**: quantified money at risk with a counterparty to
  chase — ambiguous abstains, unexplained bank credits, ITC the vendor never
  filed, duplicate/unknown 2B lines, TDS missing or duplicated in 26AS,
  payment/order amount breaks.
- **S3 — review**: matched-with-residual, unpaid orders, head/quarter
  mismatches, credits with no observed deduction, unverifiable prior periods.

Design choices worth noticing: the forecast's overdue-settlement attention
list is provably a subset of the recon exceptions at close time, so it *fuses*
onto those queue items as `days overdue` enrichment instead of
double-counting; the order-side and payment-side halves of an amount mismatch
fuse into one item; and the full-world Stage 1 reconciliation runs exactly
once (a counting test pins it), injected into the tax and forecast surfaces
through guarded seams whose outputs are byte-identical to the standalone
paths. `src/controller/verify_close.py` re-types the whole policy from
literals with its own parsers, re-checks the queue both ways against the
decisions embedded in the close, re-derives cash from the raw statement, and
re-runs all three stage verifiers — a close that misreports its own trust
panel is itself a violation.

## Why the benchmark can be trusted

1. **Ground truth is minted, not labelled.** The generator records the correct
   disposition and counterparties for every record *at the moment it
   constructs it* (`src/recon/generate.py`). No LLM labels anything, and the
   system never grades itself.
2. **"Unique" and "ambiguous" are proved, not assumed.** A separation audit
   keeps all settlement amounts ≥ ₹10 apart except designed collisions (twins:
   equal to the paisa; near-collisions: ₹1–9 apart), and tests assert these
   properties per world.
3. **14 scenario types** cover the reference list from real settlement ops:
   UTR corruption (truncation, separators, typos), missing money in both
   directions, duplicates, splits, merges, decomposable deductions, delayed
   credits, amount near-collisions, constructed ambiguity, and noise the
   engine must leave alone.
4. **A leak canary.** If the naive matcher ever scores near the engine, the
   world got too easy — that check is visible in every report.
5. **Determinism end to end.** `python -m recon.generate --seed 42
   --verify-determinism` hash-compares two runs (at 90 and 180 days); 317
   pytest tests pin the parsers, generator invariants, every engine pass, the
   graders' math, the forecaster's layers, the leakage wall, the tax rules and
   world, the head-split agreement suite, the close's one-pass and triage
   policies, queue-state purity, snapshot deltas, ingestion adapters and
   webhook signatures, the intake validator's rejection suite, agent-loop
   discipline and replay integrity, all four independent verifiers' ability to
   catch corrupted or tampered output, the golden-blindness canaries, and the
   extended agent quarantine.

## Read the engines' 100% honestly

The engines — and the close audit that inherits from them — score perfectly
because the benchmark's scenarios are deterministic constructions and each
engine exploits exactly the evidence its scenarios leave behind — including
abstaining on the cases *constructed* to be undecidable. That is the designed
behaviour, verified independently, not a claim that real bank or GSTN data
would reconcile at 100%. The claims that matter: nothing is force-matched,
every decision is evidenced, failure modes the baselines exhibit are covered,
and the whole loop is reproducible from a clean checkout. What was never
measured at all — real Razorpay settlement data, real GST/TDS data, production
volumes — is listed up front in the README's
[Limitations](README.md#limitations--what-this-does-not-prove-and-why).

What 100% does **not** cover is scenario types the generator does not produce
(fraud, currency conversion, multi-PSP interleaving, narration dialects beyond
what the real fixture and the templates exercise). On the tax side: GSTR-1
ships as a **v1 B2C-Others summary** — invoice-level B2B sections need buyer
GSTINs the world doesn't model — the 3%-of-gross liability remains a
documented simplification of output GST net of ITC, the CGST/SGST split is
published arithmetic applied books-side rather than a per-return filing
engine, and e-invoice/IRN and true multi-GSTIN merchants stay out of scope —
the merchant registry is per-entity worlds, not one entity with many
registrations.

## Repository layout

- `data/seeds/42/` — the committed canonical world (other seeds regenerate on
  demand)
- `data/real/statement_a/` — the anonymized real bank statement + its proven
  mapping and canonical world
- `data/fixtures/razorpay/` — official-schema API/webhook fixtures (see its
  README for the test-mode settlement limitation)
- `data/merchants.json` — the merchant registry (dashboard selector +
  `close --merchants` rollup)
- `data/state/` (gitignored) — operator workflow state and close snapshots
- `reports/` — committed benchmark artifacts every claim above is pasted from
- `scripts/repro.py` — the 9-step repro; `repro.ps1` / `repro.sh` wrappers
- `tests/` — 317 tests (see the trust list above)

### Roadmap

Stage 1 — reconciliation: **done**. Stage 2 — cash forecasting: **done**.
Stage 3 — tax matching: **done** (and integrated backwards: the TDS loop
consumes Stage 1's reconciliation decisions, and compliance grading rides on
the same obligation schedule Stage 2 detects). Final stage — the unified
controller: **done**. Post-review hardening: **done** — agentic intake proven
on a real bank statement, actionable queue with an audit trail, incremental
as-of closes with deltas, a merchant registry, official-schema Razorpay
ingestion with signed webhooks, CGST/SGST split + GSTR-1 v1, and a
cross-platform one-command repro.
