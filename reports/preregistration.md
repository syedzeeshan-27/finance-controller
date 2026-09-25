# Pre-registration: resolver evaluation

Written before any live resolver call. (Two live calls came earlier: a transport smoke test
on a toy exchange, and the statement-intake run. Neither touches the resolver or its worlds.)
Part 1 was frozen before the first dev-seed run; part 2 is frozen after dev-seed prompt work
and before the single held-out run. The hashes live in `reports/preregistration.json`,
written by `python -m agent.eval_resolve --pin 1` and `--pin 2` (each part is pinned once, and
part 2 refuses to pin if any part-1 file changed).
`python -m agent.eval_resolve` recomputes them and prints "matches pre-registration: yes / NO
(changed: ...)" in every report it writes.

## Part 1: frozen before any agent run

**What is measured.** For every held-out world (seeds 1001-1005, template family B), the
frozen engine (`src/recon/engine.py` at tag `engine-frozen`) runs first. The items it leaves
for a human go to the resolver agent. The grading unit is one golden row per settlement and per
bank credit, restricted to held-out case rows (the case tags in `recon.holdout.CASE_TAGS`).
Clean filler is reported separately.

**Categories** (`agent/eval_resolve.py`):
- auto-resolved correctly: an auto match whose settlement+credit group equals the golden
  group, or a correct non-settlement class;
- auto-resolved wrongly: any other auto outcome. This includes a match that closes an
  unexplained amount difference and a non-settlement class on a record the golden file
  resolves differently;
- correctly abstained: left for a human when the golden answer is not an auto resolution;
- wrongly abstained: left for a human when the golden answer is a match or a non-settlement
  class.

Golden groups are built only over links where both ends are matched-family rows. Proposal
metrics are agent match proposals rejected by the verifier, and accepted proposals that are
wrong. Cost per item is the list-price equivalent of recorded token usage; latency is the
recorded wall clock.

**Items sent to the agent** (`agent/resolve.py`): engine decisions with status
`exception_missing_bank`, `exception_missing_settlement`, `ambiguous_abstain`,
`duplicate_credit`, or a matched-family decision at `needs_review`.
- Order: credit-side first by (value date, id), then settlement-side by (created date, id).
- An item is skipped when one of its records appears in an earlier item's match proposal.
- An item is also skipped when the deterministic enumerator (`agent/solver.py`) finds no
  explanation the verifier could accept (`prefilter_skip`).
- The agent sees only an allowlisted copy of `settlements.csv`, `bank_statement.csv` and
  `settlement_merchants.csv`. Engine-claimed records are hidden.
- The agent gets one submission, no verdict and no retry, and at most 4 API calls per item.

**Verifier** (`agent/resolve_verifier.py`, stdlib only): rules R1-R12 as documented in the
module. The strict set is the one used for the result. The brief-minimum set (R1-R8) is scored
on the same proposals as an ablation. The money-figure grammar and its tests
(`tests/test_resolve_verifier.py`) are frozen with it. Multi-figure deductions are rejected.

**Ablations:** "engine + deterministic solver, no LLM" (a unique verifiable explanation, else
abstain); naive and exact-UTR-then-amount baselines on the same rows.

**Crash policy:**
- A run that stops on the daily quota, on `AGENT_MAX_USD`, or on a provider outage that
  outlasts the retries resumes later with `--resume`. Finished items replay from their transcripts, byte-checked, and unfinished items
  run live. That is the same run, not a rerun.
- A bug fix that changes the replay of an already-recorded item invalidates the run. The run is
  then repeated in full and reported as "run 2" next to run 1.
- Prompt changes after part 2 are not allowed for the held-out run.

## Part 2: frozen before the held-out run

Once dev-seed prompt work ends, two more hashes join part 1 in `reports/preregistration.json`:
the prompt and tool fingerprint (which also covers max calls and max tokens per call), and the
`resolve.py` file. The model id, thinking level and function-calling mode are pinned as
settings. The evaluation compares every pinned hash with the code, and every pinned setting
with the meta line of every recorded transcript. The report says "matches pre-registration:
yes" only when all of them hold. The held-out seeds are then run once.

**As pinned (2026-09-25T12:06:20Z).** Model `claude-sonnet-5`; thinking level `medium`, which
for Anthropic is the `output_config.effort` value (Sonnet 5 thinks adaptively); function
calling `auto`, with the loop forcing `submit_resolution` on the last allowed call. The
settings were read from the one Sonnet dev run (seeds 1000 and 1006), and no prompt change
followed it. Before that, two dev runs on the Gemini free tier shaped the prompt: run 1
finished world 1000, run 2 stopped on provider overload. Their transcripts are not kept,
because their settings differ from the pinned ones. The switch from Gemini to Sonnet was the
user's call, made before any held-out run, and is recorded as an amendment in the JSON.

## Corrections added after the held-out run (2026-09-25)

The text above is left as it was written. An independent read-through against the code found
the points below; none of them changes a metric, an item or a verdict.
- When this file was written: parts 1 and 2 above predate every live resolver call. The "As
  pinned" paragraph was added just after part 2 was pinned, as the held-out run was starting,
  and holds no held-out result. This file is not hashed; the pinned hashes and settings are in
  `preregistration.json`.
- "Two live calls came earlier" means two live runs, neither of which read the resolver's
  worlds: a 2-call Gemini transport smoke test on a toy exchange (with the resolver's tool
  schemas), and the statement-intake batch (the real statement plus 15 look-alikes, 92 calls),
  whose last three files were recorded while the first Gemini dev run was under way. The
  recorded Sonnet intake and investigator transcripts predate this work.
- Besides `--pin 1` and `--pin 2`, `eval_resolve.py` was re-pinned twice with `--pin amend`: for
  the provider switch (before part 2), and for report-only fixes after the held-out run. The
  second records a fingerprint of the held-out numbers from before the fix, and the report
  states they did not move. Part 2 refuses to pin if any part-1 file differs from its last
  pinned or amended hash.
- "Frozen before any agent run" in part 1 means before any resolver run; the grading code was
  later amended as above.
- "Engine-claimed records are hidden": they are left out of the first message and of every
  search. `get_narration` still returns a claimed credit asked for by id, flagged
  `already_matched_by_engine`, and rule R4 rejects any proposal that uses one.
- The brief-minimum ablation is R1-R8 plus the batch conflict rule R12, which always runs.
- The money-figure grammar is frozen inside `resolve_verifier.py`. Its tests are not hashed;
  they were last changed before part 1 was pinned.
- The Gemini dev runs: run 1 finished world 1000 and stopped on provider overload at the first
  item of world 1006; the prompt was then revised once. Run 2, on that final prompt, stopped on
  provider overload partway through world 1000, and no prompt change followed it. Their
  transcripts were moved out of the repo because part 2 reads the pinned settings from the dev
  transcripts and refuses a mix.
- "Cost per item is the list-price equivalent": for the Sonnet runs it is also what was billed.
