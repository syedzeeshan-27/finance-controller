# Daily-close audit

Seeds [42, 43, 44, 45, 46] on 180-day worlds. Every golden record whose expected disposition is queue-worthy under the published triage policy must surface in the unified queue under that exact status; every non-synthetic queue item must be justified by a golden row. The daily_close row is additionally gated by verify_close (independent re-typed policy + the leg A stage verifier re-run on the embedded decisions); the naive_engines row feeds the SAME triage layer with the naive leg A baseline, and its stage-verifier violation count is reported as evidence.

| strategy | queue recall | queue precision | verify violations |
|---|---|---|---|
| daily_close | 1101/1101 (100.0%) | 100.0% | 0 |
| naive_engines | 1024/1101 (93.0%) | 60.0% | 0 |

One full daily close (both legs, one pass, verifiers included) processes ~20,749 records/second.

The queue never sees the golden files (package canary test); the synthetic excluded from precision is `needs_review`, a confidence overlay on matched decisions.
