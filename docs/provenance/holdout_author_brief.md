# Brief: write an independent held-out test set (`src/recon/holdout.py`)

## Where you work
- Work ONLY inside this sandbox directory (the folder that contains this BRIEF.md).
  Do not read, list, search or open anything outside it. If something you need is
  missing, write it yourself inside `holdout.py`.
- Python: the global `python` (3.13). Run tests from the sandbox root with
  `PYTHONUTF8=1 python -m pytest`. Give Python Windows-style paths (`C:/...`), never
  Git-Bash paths (`/c/...`).
- Do not modify `src/recon/{generate,schemas,io_load,normalize}.py` or the existing tests.

## Context
This project reconciles Razorpay settlements (`settlements.csv`) against the credits on a
merchant's bank statement (`bank_statement.csv`). `src/recon/generate.py` builds synthetic
worlds plus golden answer files written at construction time. A matching engine (deliberately
NOT in this sandbox; do not look for it) was written by the same author as the generator and
scores 100% on generator worlds, so those numbers prove little.

Your job: build an independent held-out set of worlds containing cases the existing generator
never produces, with defensible ground truth, so the engine (and later an AI resolver) can be
measured honestly. You are not told how the engine works on purpose. Do not try to design
cases that beat or favour any particular algorithm; design realistic cases with defensible
answers.

You may import building blocks from `recon.generate` (ids, orders/payments, settlements,
`_force_net`, noise, `_assemble_statement`, writers...), the vocabulary and column orders from
`recon.schemas`, and the money/date helpers from `recon.normalize`.

## Case families
The brief's list, verbatim:
1. deductions of arbitrary amounts, explained only in the narration text (e.g. "less chgs 59.00", "TDS 194O 412.30")
2. settlement referenced by settlement id or merchant name in narration, no UTR
3. UTR with 2+ character damage
4. three or more settlements merged into one credit
5. a refund or chargeback netted off the credit
6. same-amount twins where the narration does carry a distinguishing clue
7. twins with no clue at all (correct answer stays "abstain")

Plus:
8. negative controls: settlements that were never paid (no credit), and Razorpay-looking
   credits that belong to no settlement, some with amounts deliberately close to a real
   settlement net (or equal to a net minus a plausible charge). Correct answer: exception.
9. one extra case family of your own choosing that a real Indian bank statement could
   plausibly contain and none of the above covers. Tag it distinctly; keep it small.

The two example strings quoted in family 1 may appear in family A templates at most (see
below); family B must use other wording.

## Ground-truth policy
- `expected_disposition` is what a careful human reconciler could DEFEND from the observable
  data only (`settlements.csv`, `bank_statement.csv`, `settlement_merchants.csv`). Hidden truth
  that no observable evidence supports goes in `notes` (e.g. "truth: paid by BANK000123"), not
  in the disposition. Example: no-clue twins → `ambiguous_abstain` for both twin settlements and
  the credit, with the real payer recorded in `notes`.
- A credit is matchable to settlement(s) only if its value date is 0..10 days after each
  matched settlement's `created_at` date.
- A match whose credit amount differs from the settlement net(s) is defensible only if the
  narration states the difference as an explicit figure (charges, TDS, refund, chargeback...)
  and the arithmetic closes exactly to the paisa. If the figure is not in the narration, the
  credit is not defensibly matchable: use the exception/abstain disposition the evidence
  supports and put the truth in `notes`. A few such cases are realistic and welcome.
- Use the vocabulary in `recon/schemas.py` (`matched`, `matched_merged`,
  `matched_with_discrepancy`, `matched_split`, `ambiguous_abstain`, `exception_missing_bank`,
  `exception_missing_settlement`, `non_settlement_credit`, `duplicate_credit`).
  `expected_discrepancy_paise` = received − expected (negative for deductions), on the
  settlement row(s) and the credit row of the group. `counterparty_ids` exactly as
  `generate.py` does it (settlement rows list credit txn_ids, credit rows list settlement ids,
  `;`-joined).

## World format (hard requirements)
- Same recon-side files as `generate.write_world`: `settlements.csv`, `bank_statement.csv`,
  `payments.csv`, `order_book.csv`, `golden_leg_a.csv`, `golden_leg_b.csv`,
  `golden_manifest.json`, all loadable by the UNCHANGED `recon.io_load`.
- Merchant attribution: settlements belong to one of 2-4 merchant brands (one business, several
  Razorpay accounts, one bank account). Write `settlement_merchants.csv` with columns
  `settlement_id,merchant_name` covering EVERY settlement (not only case settlements). A merchant
  name appears in a narration only where a case says so.
- Statement assembled canonically: all rows sorted by date, txn_ids assigned in order, running
  balance recomputed after every mutation; the balance chain is continuous.
- No structural tells: case settlements look like ordinary settlements in every non-narration
  column (`created_at`/`settled_at` follow the generator's normal rules, and `settled_at` is
  derived from `created_at`, never from the actual credit date). No readable or sequential ids
  that reveal case membership. Clean filler is interleaved in time with the cases.
- Every settlement and every bank credit has exactly one `golden_leg_a` row; debits have none.
- Deterministic: same seed → byte-identical files, also across separate processes with
  different `PYTHONHASHSEED` (never iterate over sets or dicts of strings without sorting).
- Write every file with `\n` line endings (`newline="\n"`, csv `lineterminator="\n"`).

## Seeds and template families
- Two DISJOINT narration template families. Family "A" is for dev seeds `1000` and `1006`;
  family "B" is for held-out seeds `1001`-`1005`. Disjoint means different phrasings and formats
  for the same case types (deduction wording, clue formats, merchant-name styles, damage styles
  where it makes sense), so a model tuned on A has to generalise to B. Select the family by
  seed inside `holdout.py`.
- Size: across 1001-1005, at least 150 `golden_leg_a` rows carrying holdout case tags (families
  1-9, not clean filler). Target 180-250, so roughly 35-50 case rows per seed. Keep clean filler
  realistic but modest (a 45-60 day world is fine). An LLM will later process each unresolved
  case on a small daily request quota, so do not go beyond ~50 case rows per seed.

## Interface (exact)
`src/recon/holdout.py` must provide:
- `HOLDOUT_VERSION = "1.0.0"`
- `DEV_SEEDS = (1000, 1006)`, `HELDOUT_SEEDS = (1001, 1002, 1003, 1004, 1005)`
- `CASE_TAGS: dict[str, str]`: every holdout scenario_tag → one-line description (families 1-9).
  The docstring says which tag clean filler rows carry.
- `build_holdout_world(seed: int, days: int = <your default>) -> dict` (the same shape as
  `generate.build_world` plus `"settlement_merchants"`)
- `write_holdout_world(world: dict, out_dir: str) -> None`
- `generate_holdout(seed: int, out_dir: str | None = None) -> str` (default `data/holdout/<seed>`)
- CLI: `python -m recon.holdout --seed N [--out DIR] [--verify-determinism]`, and `--all` to write
  every dev and held-out seed under `data/holdout/`.
- `golden_manifest.json` includes `holdout_version`, `seed`, `family` ("A"/"B"), `role`
  ("dev"/"heldout"), counts and scenario counts.

## Tests (`tests/test_holdout.py`), at minimum
- determinism in-process and across two subprocesses with different `PYTHONHASHSEED`;
- every settlement and bank credit has exactly one golden row, counterparties exist, debits have none;
- every world loads through `recon.io_load`; the balance chain is continuous;
- every golden matched-family group closes: Σ settlement nets + expected_discrepancy ==
  Σ credit amounts, and a non-zero discrepancy figure appears verbatim in a narration of the
  group's credit(s);
- no-clue twins: nothing in the credit's narration/ref distinguishes the twins (decide and
  document what "distinguishes" means, e.g. ids, UTR fragments of 4+ characters, differing
  merchant names);
- clue twins: the clue is present and points to exactly one twin;
- families A and B share no template (compare rendered narration skeletons);
- at least 150 held-out case rows across `HELDOUT_SEEDS`, and each of families 1-9 is present
  in the held-out seeds;
- `settlement_merchants.csv` covers every settlement;
- no structural tells (for example, the created_at time of day and the settled_at rule are
  identical for case and filler settlements).

## What to return
A short summary (at most 25 lines): files written, default `days`, per-seed case-row counts,
the tag list with one-line descriptions, and the test results. Do not paste narration
templates or example narrations in the summary.
