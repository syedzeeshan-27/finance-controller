# Tax-matching benchmark

Seeds [42, 43, 44, 45, 46] on 180-day worlds. Graded per golden record: the deciding decision must carry the expected disposition AND the expected counterparty set. Verification is independent (own parsers, own rule implementations) and hard-fails the engine on any violation; baseline violations are reported as evidence.

| strategy | disposition acc | ITC loop | TDS loop | compliance | precision | recall | F1 | false ITC claims | verify violations |
|---|---|---|---|---|---|---|---|---|---|
| naive_tax | 51.3% | 1155/2290 | 41/68 | 45/60 | 59.3% | 81.9% | 0.688 | Rs 165,084 | 330 |
| tax_engine | 100.0% | 2290/2290 | 68/68 | 60/60 | 100.0% | 100.0% | 1.000 | Rs 0 | 0 |

## Money on the table (totals across seeds)

| metric | naive_tax | tax_engine |
|---|---|---|
| input credit claimed | Rs 646,701 | Rs 545,661 |
| **claimed falsely** (blocked/unknown/mispaired) | Rs 165,084 | Rs 0 |
| ITC at risk (vendor never filed) — identified | Rs 5,364 of Rs 15,437 | Rs 15,437 of Rs 15,437 |
| TDS credits at risk — identified | Rs 1,875 of Rs 1,875 | Rs 1,875 of Rs 1,875 |
| short-paid liability — detected | Rs 0 of Rs 8,810 | Rs 8,810 of Rs 8,810 |

## Per-scenario disposition accuracy (all seeds pooled)

| scenario | naive_tax | tax_engine |
|---|---|---|
| tax_26as_amount_mismatch | 0/10 | 10/10 |
| tax_26as_clean | 26/28 | 28/28 |
| tax_26as_duplicate | 10/15 | 15/15 |
| tax_26as_missing | 5/5 | 5/5 |
| tax_26as_wrong_quarter | 0/10 | 10/10 |
| tax_amount_mismatch | 0/48 | 48/48 |
| tax_blocked_credit | 0/588 | 588/588 |
| tax_clean | 1024/1140 | 1140/1140 |
| tax_duplicate_2b | 18/30 | 30/30 |
| tax_fee_invoice | 60/60 | 60/60 |
| tax_filed_late | 0/36 | 36/36 |
| tax_invoice_typo | 24/36 | 36/36 |
| tax_missing_2b | 16/24 | 24/24 |
| tax_no_itc | 0/290 | 290/290 |
| tax_obligation_clean | 45/45 | 45/45 |
| tax_obligation_late_paid | 0/5 | 5/5 |
| tax_obligation_short_paid | 0/5 | 5/5 |
| tax_obligation_unverifiable | 0/5 | 5/5 |
| tax_unknown_2b | 13/18 | 18/18 |
| tax_wrong_head | 0/20 | 20/20 |

Ground truth is minted at generation time (`golden_tax.csv`); the engine derives the books side blind from the statement, settlements, payments and the Stage 1 reconciliation output, and a behavioral test pins its decisions byte-identical with the answer keys deleted or corrupted.
