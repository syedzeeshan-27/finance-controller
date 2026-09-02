# Daily-close audit

Seeds [42, 43, 44, 45, 46] on 180-day worlds. Every golden record whose expected disposition is queue-worthy under the published triage policy must surface in the unified queue under that exact status; every non-synthetic queue item must be justified by a golden row. The daily_close row is additionally gated by verify_close (independent re-typed policy + all three stage verifiers re-run on the embedded decisions); the naive_engines row feeds the SAME triage layer with the naive Stage 1 + Stage 3 baselines, and its stage-verifier violation counts are reported as evidence.

| strategy | queue recall | money recall | queue precision | money at stake | verify violations |
|---|---|---|---|---|---|
| daily_close | 1266/1266 (100.0%) | 100.0% | 100.0% | Rs 27,210, surfaced Rs 27,210 | 0 |
| naive_engines | 1053/1266 (83.1%) | 22.0% | 48.1% | Rs 27,210, surfaced Rs 5,364 | 330 |

One full daily close (all three loops, one pass, verifiers included) processes ~4,811 records/second.

The queue never sees the golden files (package canary test); the synthetics excluded from precision are `needs_review` — a confidence overlay on matched decisions — and the threshold-breach item, which exists only relative to a user-chosen threshold.
