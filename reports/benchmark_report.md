# Reconciliation Benchmark Report

Seeds: [42, 43, 44, 45, 46] | leg A grading unit: one golden row per settlement and per bank credit | LLM mode: none (deterministic template explanations; no model is called)

Per-seed world (seed 42): 915 payments, 109 settlements, 265 bank statement rows (120 credits / 145 debits), 915 orders. Scenario mix is recorded in each world's `golden_manifest.json`.

## Leg A: settlement <-> bank credit (mean over seeds, min-max in brackets)

| strategy | precision | recall | F1 | false-match rate | disposition accuracy | correct abstentions | auto-resolved | records/sec | invariant violations |
|---|---|---|---|---|---|---|---|---|---|
| naive | 97.5% (96.9%-97.8%) | 79.2% (77.3%-80.8%) | 87.4% (86.2%-88.2%) | 2.5% (2.2%-3.1%) | 74.1% (72.1%-75.8%) | 0/30 | 70.5% (68.3%-72.9%) | 7,120 (5,231-8,885) | 0 |
| utr_then_amount | 90.7% (90.1%-91.3%) | 88.7% (87.9%-89.4%) | 89.7% (89.0%-90.3%) | 9.3% (8.7%-9.9%) | 82.3% (81.2%-83.3%) | 0/30 | 72.8% (71.7%-73.6%) | 45,500 (37,987-50,259) | 0 |
| recon_engine | 100.0% (100.0%-100.0%) | 100.0% (100.0%-100.0%) | 100.0% (100.0%-100.0%) | 0.0% (0.0%-0.0%) | 100.0% (100.0%-100.0%) | 30/30 | 90.4% (90.0%-90.7%) | 23,769 (22,580-25,112) | 0 |

## Leg A per-scenario disposition accuracy (seed 42)

| scenario | golden rows | naive | utr_then_amount | recon_engine |
|---|---|---|---|---|
| ambiguous_twins | 6 | 0% | 0% | 100% |
| clean_exact_utr | 84 | 100% | 100% | 100% |
| delayed_settlement | 8 | 25% | 100% | 100% |
| duplicate_bank_credit | 12 | 67% | 67% | 100% |
| matched_with_discrepancy | 14 | 0% | 100% | 100% |
| merged_credits | 6 | 0% | 0% | 100% |
| missing_bank_credit | 8 | 100% | 100% | 100% |
| near_collision | 12 | 100% | 100% | 100% |
| noise_credit | 9 | 0% | 0% | 100% |
| orphan_bank_credit | 5 | 100% | 100% | 100% |
| split_settlement | 15 | 0% | 0% | 100% |
| utr_absent_amount_unique | 16 | 100% | 100% | 100% |
| utr_mangled_separators | 8 | 100% | 100% | 100% |
| utr_mangled_typo | 8 | 100% | 100% | 100% |
| utr_truncated | 18 | 100% | 100% | 100% |

## Leg B: payment <-> order book

Disposition accuracy: 100.0% (100.0%-100.0%) | throughput: 33,834 (31,307-35,156) records/sec

| leg B exception class (seed 42) | correct | total |
|---|---|---|
| exception_amount_mismatch | 26 | 26 |
| exception_duplicate_payment | 8 | 8 |
| exception_payment_no_order | 6 | 6 |
| exception_unpaid_order | 34 | 34 |

## Notes

- Ground truth is generated deterministically at data-generation time; no LLM grades anything anywhere in this benchmark.
- The matching decisions themselves are fully deterministic, and the benchmark calls no model at all.
- A wrong match counts as both a false positive and a missed match.
- Timing covers CSV parse + matching, excludes grading/report writing.
- Baseline `naive` = amount within Rs 1, value date within 3 days of the expected settlement date, greedy best-gap, one settlement per credit, no references/UTRs - the first script anyone writes. It shares the engine's parsers, so parsing is never the differentiator.
