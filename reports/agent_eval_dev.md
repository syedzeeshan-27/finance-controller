# Agent evaluation — dev worlds (prompt work happens here)

Worlds: 1000, 1006 · role: dev · model: `claude-sonnet-5` (thinking level medium, function calling auto) · 97 dev case rows in 52 cases (settlement and bank-credit rows; clean filler reported separately).

Prompt/tools sha256 `24e1e556cdfd79de` · verifier `75a52a0552d93c44` · holdout `a1791aa42ca459a6` · matches pre-registration: **yes**.

## Engine only vs engine + agent (all dev case rows)

| Metric | Engine only | Engine + agent |
|---|---|---|
| Auto-resolved correctly | 16 | 61 |
| Auto-resolved wrongly | 0 | 0 |
| Correctly abstained | 28 | 28 |
| Wrongly abstained (left for human) | 53 | 8 |
| Agent proposals rejected by verifier | n/a | 0 |
| **Wrong proposals the verifier accepted** | n/a | **0** |
| Cost per item (USD), median latency (s) | n/a | $0.0112 billed · 7.35 s |

Units: the first four rows count golden rows (one per settlement and per bank credit); the proposal rows count agent proposals. Wilson 95% intervals for the auto-resolved-wrongly rate: engine (0.0, 0.0381), engine + agent (0.0, 0.0381).

## Per scenario

### `chargeback_netted`

| Metric | Engine only | Engine + agent |
|---|---|---|
| Auto-resolved correctly | 0 | 4 |
| Auto-resolved wrongly | 0 | 0 |
| Correctly abstained | 0 | 0 |
| Wrongly abstained (left for human) | 4 | 0 |
| Agent proposals rejected by verifier | n/a | 0 |
| **Wrong proposals the verifier accepted** | n/a | **0** |
| Cost per item (USD), median latency (s) | n/a | $0.0087 billed · 9.43 s |

### `deduction_stated`

| Metric | Engine only | Engine + agent |
|---|---|---|
| Auto-resolved correctly | 0 | 12 |
| Auto-resolved wrongly | 0 | 0 |
| Correctly abstained | 0 | 0 |
| Wrongly abstained (left for human) | 12 | 0 |
| Agent proposals rejected by verifier | n/a | 0 |
| **Wrong proposals the verifier accepted** | n/a | **0** |
| Cost per item (USD), median latency (s) | n/a | $0.0088 billed · 7.28 s |

### `deduction_unstated`

| Metric | Engine only | Engine + agent |
|---|---|---|
| Auto-resolved correctly | 0 | 0 |
| Auto-resolved wrongly | 0 | 0 |
| Correctly abstained | 4 | 4 |
| Wrongly abstained (left for human) | 0 | 0 |
| Agent proposals rejected by verifier | n/a | 0 |
| **Wrong proposals the verifier accepted** | n/a | **0** |
| Cost per item (USD), median latency (s) | n/a | n/a (no live calls) |

### `merged_3plus`

| Metric | Engine only | Engine + agent |
|---|---|---|
| Auto-resolved correctly | 0 | 17 |
| Auto-resolved wrongly | 0 | 0 |
| Correctly abstained | 0 | 0 |
| Wrongly abstained (left for human) | 17 | 0 |
| Agent proposals rejected by verifier | n/a | 0 |
| **Wrong proposals the verifier accepted** | n/a | **0** |
| Cost per item (USD), median latency (s) | n/a | $0.0171 billed · 9.52 s |

### `never_paid`

| Metric | Engine only | Engine + agent |
|---|---|---|
| Auto-resolved correctly | 0 | 0 |
| Auto-resolved wrongly | 0 | 0 |
| Correctly abstained | 4 | 4 |
| Wrongly abstained (left for human) | 0 | 0 |
| Agent proposals rejected by verifier | n/a | 0 |
| **Wrong proposals the verifier accepted** | n/a | **0** |
| Cost per item (USD), median latency (s) | n/a | n/a (no live calls) |

### `orphan_near_net`

| Metric | Engine only | Engine + agent |
|---|---|---|
| Auto-resolved correctly | 0 | 0 |
| Auto-resolved wrongly | 0 | 0 |
| Correctly abstained | 4 | 4 |
| Wrongly abstained (left for human) | 0 | 0 |
| Agent proposals rejected by verifier | n/a | 0 |
| **Wrong proposals the verifier accepted** | n/a | **0** |
| Cost per item (USD), median latency (s) | n/a | n/a (no live calls) |

### `orphan_rzp_credit`

| Metric | Engine only | Engine + agent |
|---|---|---|
| Auto-resolved correctly | 0 | 0 |
| Auto-resolved wrongly | 0 | 0 |
| Correctly abstained | 2 | 2 |
| Wrongly abstained (left for human) | 0 | 0 |
| Agent proposals rejected by verifier | n/a | 0 |
| **Wrong proposals the verifier accepted** | n/a | **0** |
| Cost per item (USD), median latency (s) | n/a | n/a (no live calls) |

### `outward_neft_return`

| Metric | Engine only | Engine + agent |
|---|---|---|
| Auto-resolved correctly | 0 | 0 |
| Auto-resolved wrongly | 0 | 0 |
| Correctly abstained | 0 | 0 |
| Wrongly abstained (left for human) | 4 | 4 |
| Agent proposals rejected by verifier | n/a | 0 |
| **Wrong proposals the verifier accepted** | n/a | **0** |
| Cost per item (USD), median latency (s) | n/a | n/a (no live calls) |

### `ref_merchant_name`

| Metric | Engine only | Engine + agent |
|---|---|---|
| Auto-resolved correctly | 4 | 4 |
| Auto-resolved wrongly | 0 | 0 |
| Correctly abstained | 0 | 0 |
| Wrongly abstained (left for human) | 0 | 0 |
| Agent proposals rejected by verifier | n/a | 0 |
| **Wrong proposals the verifier accepted** | n/a | **0** |
| Cost per item (USD), median latency (s) | n/a | n/a (no live calls) |

### `ref_settlement_id`

| Metric | Engine only | Engine + agent |
|---|---|---|
| Auto-resolved correctly | 4 | 4 |
| Auto-resolved wrongly | 0 | 0 |
| Correctly abstained | 0 | 0 |
| Wrongly abstained (left for human) | 0 | 0 |
| Agent proposals rejected by verifier | n/a | 0 |
| **Wrong proposals the verifier accepted** | n/a | **0** |
| Cost per item (USD), median latency (s) | n/a | n/a (no live calls) |

### `refund_netted`

| Metric | Engine only | Engine + agent |
|---|---|---|
| Auto-resolved correctly | 0 | 4 |
| Auto-resolved wrongly | 0 | 0 |
| Correctly abstained | 0 | 0 |
| Wrongly abstained (left for human) | 4 | 0 |
| Agent proposals rejected by verifier | n/a | 0 |
| **Wrong proposals the verifier accepted** | n/a | **0** |
| Cost per item (USD), median latency (s) | n/a | $0.0089 billed · 5.15 s |

### `twins_no_clue`

| Metric | Engine only | Engine + agent |
|---|---|---|
| Auto-resolved correctly | 0 | 0 |
| Auto-resolved wrongly | 0 | 0 |
| Correctly abstained | 12 | 12 |
| Wrongly abstained (left for human) | 0 | 0 |
| Agent proposals rejected by verifier | n/a | 0 |
| **Wrong proposals the verifier accepted** | n/a | **0** |
| Cost per item (USD), median latency (s) | n/a | n/a (no live calls) |

### `twins_with_clue`

| Metric | Engine only | Engine + agent |
|---|---|---|
| Auto-resolved correctly | 0 | 8 |
| Auto-resolved wrongly | 0 | 0 |
| Correctly abstained | 2 | 2 |
| Wrongly abstained (left for human) | 12 | 4 |
| Agent proposals rejected by verifier | n/a | 0 |
| **Wrong proposals the verifier accepted** | n/a | **0** |
| Cost per item (USD), median latency (s) | n/a | $0.0096 billed · 6.26 s |

### `utr_damaged_multi`

| Metric | Engine only | Engine + agent |
|---|---|---|
| Auto-resolved correctly | 8 | 8 |
| Auto-resolved wrongly | 0 | 0 |
| Correctly abstained | 0 | 0 |
| Wrongly abstained (left for human) | 0 | 0 |
| Agent proposals rejected by verifier | n/a | 0 |
| **Wrong proposals the verifier accepted** | n/a | **0** |
| Cost per item (USD), median latency (s) | n/a | n/a (no live calls) |

## Clean filler (sanity, not part of the totals)

Engine only: {'auto_correct': 214, 'auto_wrong': 0, 'abstain_correct': 0, 'abstain_wrong': 0} · engine + agent: {'auto_correct': 214, 'auto_wrong': 0, 'abstain_correct': 0, 'abstain_wrong': 0}

## What the LLM adds, and what the stricter verifier buys

| Column | Auto correct | Auto wrong | Correctly abstained | Wrongly abstained |
|---|---|---|---|---|
| Engine only | 16 | 0 | 28 | 53 |
| Engine + deterministic solver (no LLM) | 30 | 0 | 28 | 39 |
| Engine + agent (strict verifier, the result) | 61 | 0 | 28 | 8 |
| Engine + agent (brief-minimum verifier) | 61 | 0 | 28 | 8 |
| Baseline: naive (amount within Rs 1, 3 days) | 20 | 16 | 18 | 43 |
| Baseline: exact UTR, then exact amount | 16 | 20 | 18 | 43 |

With the brief-minimum verifier the same proposals would give 0 wrong accepted and 0 rejected (strict: 0 and 0).

Engine false matches on held-out rows, by confidence tier: none.

## The 10 worst failures

Agent errors first (a wrong match accepted, a wrong or right match rejected, a golden match not proposed), then queue items whose rows the combined system still leaves for a human although the golden file resolves them; by amount within each kind.

| # | world | item | scenario | kind | amount (paise) | transcript | why |
|---|---|---|---|---|---|---|---|
| 1 | 1000 | `credit:BANK000098` | merged_3plus | missed_match | 8,825,866 | `data/agent_transcripts/resolve/1000/credit__BANK000098.jsonl` | golden answer is a match; the agent returned exception |
| 2 | 1000 | `credit:BANK000035` | outward_neft_return | left_for_human | 4,875,200 | `-` | never sent to the agent: the enumerator found no explanation the verifier could accept |
| 3 | 1000 | `credit:BANK000115` | outward_neft_return | left_for_human | 4,567,347 | `-` | never sent to the agent: the enumerator found no explanation the verifier could accept |
| 4 | 1000 | `credit:BANK000095` | twins_with_clue | left_for_human | 3,661,180 | `-` | never sent to the agent: the enumerator found no explanation the verifier could accept |
| 5 | 1000 | `credit:BANK000096` | twins_with_clue | left_for_human | 3,661,180 | `-` | never sent to the agent: the enumerator found no explanation the verifier could accept |
| 6 | 1000 | `settlement:setl_u32U85Tu0mV29T` | twins_with_clue | left_for_human | 3,661,180 | `-` | never sent to the agent: the enumerator found no explanation the verifier could accept |
| 7 | 1000 | `settlement:setl_wAaIUvgFjzamff` | twins_with_clue | left_for_human | 3,661,180 | `-` | never sent to the agent: the enumerator found no explanation the verifier could accept |
| 8 | 1006 | `credit:BANK000050` | outward_neft_return | left_for_human | 3,253,300 | `-` | never sent to the agent: the enumerator found no explanation the verifier could accept |
| 9 | 1006 | `credit:BANK000030` | outward_neft_return | left_for_human | 737,278 | `-` | never sent to the agent: the enumerator found no explanation the verifier could accept |

## Notes

- Grading reads the golden files; the agent never does (its tools read an allowlisted copy of settlements, bank statement and merchant sidecar).
- Prompt and tool work happened on the dev seeds only (family A templates); the held-out seeds use family B and were run once.
- The held-out set was checked by a separate reviewer agent: answers sound; one known structural tell (merged-group settlements are all created on weekends) changes no answer. See docs/provenance/holdout_review.md.
- Cost is the list price of the recorded token usage, which is what the provider billed (prompt caching included).
- Pre-registration amended 2026-09-24T14:16:38Z (eval_resolve.py): Provider switched from the Gemini free tier to Claude Sonnet 5 (user decision, 2026-09-24, before any held-out run). eval_resolve.py now pins part-2 settings from the dev transcripts instead of Gemini environment variables, words the cost note by provider, and gained this amendment step. No metric, category, grading or item-selection logic changed.
- Pre-registration amended 2026-09-25T12:16:01Z (eval_resolve.py, after part 2): Report-only fixes found while reading the held-out report, after the single held-out run: (1) the '10 worst failures' list counted accepted, correct proposals as missed matches (a missing condition); it now lists agent errors first, then queue items whose rows are still left for a human, with the reason; (2) the cost cell said '$0 billed, free tier' for every provider, and Anthropic runs now say 'billed'; (3) dev reports said 'held-out case rows'. No metric, category, grading, item-selection, prompt or verifier logic changed, and no item was re-run.
