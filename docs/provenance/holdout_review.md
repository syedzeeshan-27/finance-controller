# Independent review of the held-out set

`src/recon/holdout.py` was written by one isolated agent (brief: `holdout_author_brief.md`) and
checked by a second isolated agent that never saw the engine and did not use the author's tests.
Its script regenerated every world and checked the written CSV files only. Two rounds, 2026-09-24.

## Round 1 (holdout v1.0.0)

Answers sound (checks 1-9 and 11 pass). Check 10 failed on two tells that change no answer:
no case settlement was created in the last 12-15 days of a world, and only case credits arrived
1-3 days after their settlement's expected date. The author fixed both in v1.1.0: the statement
now runs 14 days past the last payment day, and one delay rule (25% of credits land 1-3 days
late) applies to case and filler credits alike.

## Round 2 (holdout v1.1.0, the version used)

| Check | Result |
|---|---|
| 1 One golden row per settlement and credit, links symmetric | pass (550 settlements, 569 credits) |
| 2 Matched groups close to the paisa; each deduction is one figure in the narration | pass (464 groups, 35 with a deduction) |
| 3 Every matched pair within 0-10 days | pass (494 pairs) |
| 4 Every golden match is defensible from the visible files alone | pass |
| 5 No-clue twins really carry no clue, and are graded abstain | pass (14 credits, 28 twins) |
| 6 Clue twins: the clue names exactly one twin | pass (21 credits) |
| 7 Every exception is really unmatchable | pass |
| 8 Deterministic across processes and hash seeds | pass (56 of 56 files) |
| 9 Dev (family A) and held-out (family B) share no narration template | pass |
| 10 No structural tells separating case rows from filler | fail, one tell (below) |
| 11 At least 150 held-out case rows; every case family present | pass (241 rows) |

**Known tell, not fixed:** every settlement in a merged group (3-4 settlements paid as one
credit) is created on a Saturday or Sunday (weekend batches paid on Monday), as are 16 of the 28
no-clue twins; filler is 20% weekend. It changes no answer. It cannot create a wrong accepted
match: the verifier accepts a merge only when the amounts add up exactly and no rival fits. The
resolver prompt says nothing about weekdays. A model tuned on the dev seeds could still learn it
as a shortcut, so it is listed here and in the README limits.
