# Agent evaluation — held-out worlds

Worlds: 1001, 1002, 1003, 1004, 1005 · role: heldout · model: `claude-sonnet-5` (thinking level medium, function calling auto) · 241 held-out case rows in 130 cases (settlement and bank-credit rows; clean filler reported separately).

Prompt/tools sha256 `24e1e556cdfd79de` · verifier `75a52a0552d93c44` · holdout `a1791aa42ca459a6` · matches pre-registration: **yes**.

## Engine only vs engine + agent (all held-out case rows)

| Metric | Engine only | Engine + agent |
|---|---|---|
| Auto-resolved correctly | 40 | 131 |
| Auto-resolved wrongly | 0 | 0 |
| Correctly abstained | 70 | 70 |
| Wrongly abstained (left for human) | 131 | 40 |
| Agent proposals rejected by verifier | n/a | 0 |
| **Wrong proposals the verifier accepted** | n/a | **0** |
| Cost per item (USD), median latency (s) | n/a | $0.0101 billed · 6.82 s |

Units: the first four rows count golden rows (one per settlement and per bank credit); the proposal rows count agent proposals. Wilson 95% intervals for the auto-resolved-wrongly rate: engine (0.0, 0.0157), engine + agent (0.0, 0.0157).

## Per scenario

### `chargeback_netted`

| Metric | Engine only | Engine + agent |
|---|---|---|
| Auto-resolved correctly | 0 | 10 |
| Auto-resolved wrongly | 0 | 0 |
| Correctly abstained | 0 | 0 |
| Wrongly abstained (left for human) | 10 | 0 |
| Agent proposals rejected by verifier | n/a | 0 |
| **Wrong proposals the verifier accepted** | n/a | **0** |
| Cost per item (USD), median latency (s) | n/a | $0.0083 billed · 5.1 s |

### `deduction_stated`

| Metric | Engine only | Engine + agent |
|---|---|---|
| Auto-resolved correctly | 0 | 30 |
| Auto-resolved wrongly | 0 | 0 |
| Correctly abstained | 0 | 0 |
| Wrongly abstained (left for human) | 30 | 0 |
| Agent proposals rejected by verifier | n/a | 0 |
| **Wrong proposals the verifier accepted** | n/a | **0** |
| Cost per item (USD), median latency (s) | n/a | $0.0085 billed · 6.08 s |

### `deduction_unstated`

| Metric | Engine only | Engine + agent |
|---|---|---|
| Auto-resolved correctly | 0 | 0 |
| Auto-resolved wrongly | 0 | 0 |
| Correctly abstained | 10 | 10 |
| Wrongly abstained (left for human) | 0 | 0 |
| Agent proposals rejected by verifier | n/a | 0 |
| **Wrong proposals the verifier accepted** | n/a | **0** |
| Cost per item (USD), median latency (s) | n/a | n/a (no live calls) |

### `merged_3plus`

| Metric | Engine only | Engine + agent |
|---|---|---|
| Auto-resolved correctly | 0 | 41 |
| Auto-resolved wrongly | 0 | 0 |
| Correctly abstained | 0 | 0 |
| Wrongly abstained (left for human) | 41 | 0 |
| Agent proposals rejected by verifier | n/a | 0 |
| **Wrong proposals the verifier accepted** | n/a | **0** |
| Cost per item (USD), median latency (s) | n/a | $0.0143 billed · 9.25 s |

### `never_paid`

| Metric | Engine only | Engine + agent |
|---|---|---|
| Auto-resolved correctly | 0 | 0 |
| Auto-resolved wrongly | 0 | 0 |
| Correctly abstained | 10 | 10 |
| Wrongly abstained (left for human) | 0 | 0 |
| Agent proposals rejected by verifier | n/a | 0 |
| **Wrong proposals the verifier accepted** | n/a | **0** |
| Cost per item (USD), median latency (s) | n/a | n/a (no live calls) |

### `orphan_near_net`

| Metric | Engine only | Engine + agent |
|---|---|---|
| Auto-resolved correctly | 0 | 0 |
| Auto-resolved wrongly | 0 | 0 |
| Correctly abstained | 10 | 10 |
| Wrongly abstained (left for human) | 0 | 0 |
| Agent proposals rejected by verifier | n/a | 0 |
| **Wrong proposals the verifier accepted** | n/a | **0** |
| Cost per item (USD), median latency (s) | n/a | n/a (no live calls) |

### `orphan_rzp_credit`

| Metric | Engine only | Engine + agent |
|---|---|---|
| Auto-resolved correctly | 0 | 0 |
| Auto-resolved wrongly | 0 | 0 |
| Correctly abstained | 5 | 5 |
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
| Wrongly abstained (left for human) | 10 | 10 |
| Agent proposals rejected by verifier | n/a | 0 |
| **Wrong proposals the verifier accepted** | n/a | **0** |
| Cost per item (USD), median latency (s) | n/a | n/a (no live calls) |

### `ref_merchant_name`

| Metric | Engine only | Engine + agent |
|---|---|---|
| Auto-resolved correctly | 10 | 10 |
| Auto-resolved wrongly | 0 | 0 |
| Correctly abstained | 0 | 0 |
| Wrongly abstained (left for human) | 0 | 0 |
| Agent proposals rejected by verifier | n/a | 0 |
| **Wrong proposals the verifier accepted** | n/a | **0** |
| Cost per item (USD), median latency (s) | n/a | n/a (no live calls) |

### `ref_settlement_id`

| Metric | Engine only | Engine + agent |
|---|---|---|
| Auto-resolved correctly | 10 | 10 |
| Auto-resolved wrongly | 0 | 0 |
| Correctly abstained | 0 | 0 |
| Wrongly abstained (left for human) | 0 | 0 |
| Agent proposals rejected by verifier | n/a | 0 |
| **Wrong proposals the verifier accepted** | n/a | **0** |
| Cost per item (USD), median latency (s) | n/a | n/a (no live calls) |

### `refund_netted`

| Metric | Engine only | Engine + agent |
|---|---|---|
| Auto-resolved correctly | 0 | 10 |
| Auto-resolved wrongly | 0 | 0 |
| Correctly abstained | 0 | 0 |
| Wrongly abstained (left for human) | 10 | 0 |
| Agent proposals rejected by verifier | n/a | 0 |
| **Wrong proposals the verifier accepted** | n/a | **0** |
| Cost per item (USD), median latency (s) | n/a | $0.0081 billed · 5.27 s |

### `twins_no_clue`

| Metric | Engine only | Engine + agent |
|---|---|---|
| Auto-resolved correctly | 0 | 0 |
| Auto-resolved wrongly | 0 | 0 |
| Correctly abstained | 30 | 30 |
| Wrongly abstained (left for human) | 0 | 0 |
| Agent proposals rejected by verifier | n/a | 0 |
| **Wrong proposals the verifier accepted** | n/a | **0** |
| Cost per item (USD), median latency (s) | n/a | n/a (no live calls) |

### `twins_with_clue`

| Metric | Engine only | Engine + agent |
|---|---|---|
| Auto-resolved correctly | 0 | 0 |
| Auto-resolved wrongly | 0 | 0 |
| Correctly abstained | 5 | 5 |
| Wrongly abstained (left for human) | 30 | 30 |
| Agent proposals rejected by verifier | n/a | 0 |
| **Wrong proposals the verifier accepted** | n/a | **0** |
| Cost per item (USD), median latency (s) | n/a | n/a (no live calls) |

### `utr_damaged_multi`

| Metric | Engine only | Engine + agent |
|---|---|---|
| Auto-resolved correctly | 20 | 20 |
| Auto-resolved wrongly | 0 | 0 |
| Correctly abstained | 0 | 0 |
| Wrongly abstained (left for human) | 0 | 0 |
| Agent proposals rejected by verifier | n/a | 0 |
| **Wrong proposals the verifier accepted** | n/a | **0** |
| Cost per item (USD), median latency (s) | n/a | n/a (no live calls) |

## Clean filler (sanity, not part of the totals)

Engine only: {'auto_correct': 567, 'auto_wrong': 0, 'abstain_correct': 0, 'abstain_wrong': 0} · engine + agent: {'auto_correct': 567, 'auto_wrong': 0, 'abstain_correct': 0, 'abstain_wrong': 0}

## What the LLM adds, and what the stricter verifier buys

| Column | Auto correct | Auto wrong | Correctly abstained | Wrongly abstained |
|---|---|---|---|---|
| Engine only | 40 | 0 | 70 | 131 |
| Engine + deterministic solver (no LLM) | 74 | 0 | 70 | 97 |
| Engine + agent (strict verifier, the result) | 131 | 0 | 70 | 40 |
| Engine + agent (brief-minimum verifier) | 131 | 0 | 70 | 40 |
| Baseline: naive (amount within Rs 1, 3 days) | 56 | 32 | 47 | 106 |
| Baseline: exact UTR, then exact amount | 62 | 28 | 48 | 103 |

With the brief-minimum verifier the same proposals would give 0 wrong accepted and 0 rejected (strict: 0 and 0).

Engine false matches on held-out rows, by confidence tier: none.

## The 10 worst failures

Agent errors first (a wrong match accepted, a wrong or right match rejected, a golden match not proposed), then queue items whose rows the combined system still leaves for a human although the golden file resolves them; by amount within each kind.

| # | world | item | scenario | kind | amount (paise) | transcript | why |
|---|---|---|---|---|---|---|---|
| 1 | 1004 | `credit:BANK000069` | twins_with_clue | left_for_human | 8,665,731 | `-` | never sent to the agent: the enumerator found no explanation the verifier could accept — The line carries only the UTR's last 5 digits (REF ..17228). Telling equal-amount twins apart needs the settlement id, the full UTR or a 10+ character piece of it, so even the right answer fails the rival rule. |
| 2 | 1004 | `settlement:setl_8VN5NtC6FFxuqn` | twins_with_clue | left_for_human | 8,665,731 | `-` | never sent to the agent: the enumerator found no explanation the verifier could accept — Settlement side of the BANK000069 twin pair (UTR tail ..17228): same rule, same outcome. |
| 3 | 1005 | `credit:BANK000109` | twins_with_clue | left_for_human | 6,371,011 | `-` | never sent to the agent: the enumerator found no explanation the verifier could accept — The line carries only the UTR's last 5 digits (REF ..32750). Telling equal-amount twins apart needs the settlement id, the full UTR or a 10+ character piece of it, so even the right answer fails the rival rule. |
| 4 | 1005 | `settlement:setl_ZynOHw0AeKAf43` | twins_with_clue | left_for_human | 6,371,011 | `-` | never sent to the agent: the enumerator found no explanation the verifier could accept — Settlement side of the BANK000109 twin pair (UTR tail ..32750): same rule, same outcome. |
| 5 | 1002 | `credit:BANK000125` | outward_neft_return | left_for_human | 5,651,400 | `-` | never sent to the agent: the enumerator found no explanation the verifier could accept — A returned outward NEFT payment (RTN:NEFT/.../INCORRECT A/C NO), not a settlement. The engine does not recognise this wording as non-settlement, and the resolver can only answer match, exception or abstain. |
| 6 | 1001 | `credit:BANK000138` | outward_neft_return | left_for_human | 5,472,300 | `-` | never sent to the agent: the enumerator found no explanation the verifier could accept — A returned outward NEFT payment (INW RETURN NEFT ... BENEFICIARY A/C CLOSED), not a settlement. The engine does not recognise this wording as non-settlement, and the resolver can only answer match, exception or abstain. |
| 7 | 1005 | `credit:BANK000045` | outward_neft_return | left_for_human | 5,221,704 | `-` | never sent to the agent: the enumerator found no explanation the verifier could accept — A returned outward NEFT payment (RTN:NEFT/.../INCORRECT A/C NO), not a settlement. The engine does not recognise this wording as non-settlement, and the resolver can only answer match, exception or abstain. |
| 8 | 1003 | `credit:BANK000142` | twins_with_clue | left_for_human | 5,099,682 | `-` | never sent to the agent: the enumerator found no explanation the verifier could accept — The line carries only the UTR's last 5 digits (REF ..10359). Telling equal-amount twins apart needs the settlement id, the full UTR or a 10+ character piece of it, so even the right answer fails the rival rule. |
| 9 | 1003 | `settlement:setl_P2lx7EA53fUcnG` | twins_with_clue | left_for_human | 5,099,682 | `-` | never sent to the agent: the enumerator found no explanation the verifier could accept — Settlement side of the BANK000142 twin pair (UTR tail ..10359): same rule, same outcome. |
| 10 | 1001 | `credit:BANK000022` | twins_with_clue | left_for_human | 3,893,556 | `-` | never sent to the agent: the enumerator found no explanation the verifier could accept — The line names the merchant without spaces (A/C JHAROKHADECOR). The verifier looks for 'Jharokha Decor' as written, so even the right answer fails its rival rule and the item was never sent. |

## Notes

- Grading reads the golden files; the agent never does (its tools read an allowlisted copy of settlements, bank statement and merchant sidecar).
- Prompt and tool work happened on the dev seeds only (family A templates); the held-out seeds use family B and were run once.
- The held-out set was checked by a separate reviewer agent: answers sound; one known structural tell (merged-group settlements are all created on weekends) changes no answer. See docs/provenance/holdout_review.md.
- Cost is the list price of the recorded token usage, which is what the provider billed (prompt caching included).
- Pre-registration amended 2026-09-24T14:16:38Z (eval_resolve.py): Provider switched from the Gemini free tier to Claude Sonnet 5 (user decision, 2026-09-24, before any held-out run). eval_resolve.py now pins part-2 settings from the dev transcripts instead of Gemini environment variables, words the cost note by provider, and gained this amendment step. No metric, category, grading or item-selection logic changed.
- Pre-registration amended 2026-09-25T12:16:01Z (eval_resolve.py, after part 2): Report-only fixes found while reading the held-out report, after the single held-out run: (1) the '10 worst failures' list counted accepted, correct proposals as missed matches (a missing condition); it now lists agent errors first, then queue items whose rows are still left for a human, with the reason; (2) the cost cell said '$0 billed, free tier' for every provider, and Anthropic runs now say 'billed'; (3) dev reports said 'held-out case rows'. No metric, category, grading, item-selection, prompt or verifier logic changed, and no item was re-run. Every held-out number is unchanged by this amendment: yes (fingerprint of the numbers before and after).
