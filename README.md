# AI Finance Controller

Matches Razorpay settlements to the credits on a bank statement, so an accountant does not have to.

Three parts, in order:

1. **A rules engine** matches everything it can prove (same UTR, same amount, and a few known patterns). It never guesses. Anything unclear goes to a queue for a human.
2. **A Claude agent** looks at each queued item and proposes an answer, using read-only search tools over the same files.
3. **A verifier**, plain Python with no model in it, accepts a proposal only if the money adds up to the paisa and the bank line actually points at the settlements named. Anything it rejects stays with the human.

The engine is frozen and the agent is measured, not trusted: the whole point is that the verifier, not the model, decides what gets booked.

![Agent resolutions tab: each proposal, the narration with the evidence highlighted, and the verifier's verdict](docs/agent_resolutions.png)

## Results

Measured on a held-out test set: 241 rows across five synthetic businesses, written by a separate agent that never saw the engine, the agent or the verifier. The engine, prompt, model and settings were [pinned in advance](reports/preregistration.md), and the set was run once.

| Out of 241 rows | Engine alone | Engine + agent |
|---|---|---|
| Resolved automatically, and correctly | 40 | 131 |
| Resolved automatically, but wrongly | 0 | 0 |
| Left for a human, rightly (no safe answer exists) | 70 | 70 |
| Left for a human, though an answer existed | 131 | 40 |

The agent (Claude Sonnet 5) was sent 35 queue items, proposed 35 matches, and the verifier accepted all 35. None was wrong. It cleared every deduction stated on the bank line, every netted refund and chargeback, and every credit that paid three or more settlements at once. Every settlement that never reached the bank, every credit with no settlement behind it, and every pair of look-alike settlements with nothing to tell them apart stayed with a human, which is what the answer key says should happen.

Cost: $0.35 for the whole run, about a cent per item, median 7 seconds per item.

Two things to keep in mind when reading that table. "0 wrong" is 0 out of 35 proposals, so the true error rate could still be a few percent; the [full report](reports/agent_eval.md) gives the confidence intervals and a per-scenario breakdown. And about half of the agent's gain is arithmetic rather than judgement: a plain search that tries every combination the verifier would accept, with no model at all, gets 74 of the 131. The model adds the other 57.

## Run it

```bash
pip install -r requirements.txt -e . && streamlit run src/app.py
```

Opens at http://localhost:8501. No API key is needed: the dashboard replays the recorded agent runs. Every number in this README regenerates offline with `python scripts/repro.py` (its first step pip-installs the pinned requirements; `--skip-install` after that). Only the records-per-second figures differ between runs.

## Limits

- **All the data is synthetic.** The held-out set was written independently, but it is still generated. A reviewer found one pattern in it that could hint at the answer type (settlements paid together are all created on weekends); it changes no answer. The engine has never run on real Razorpay settlements, because Razorpay test mode cannot produce them. On the project's own generated worlds the engine scores 100%, but the same author wrote both the engine and the generator, so that number means little ([benchmark report](reports/benchmark_report.md)).
- **40 rows with a known answer still go to a human.** 30 are look-alike settlements where the bank line does name the right one, but in a form the verifier does not accept (a merchant name with the spaces removed, or only the last five digits of the UTR). The other 10 are returned outward payments the engine does not recognise. The rules were frozen before this showed up, so it is reported, not patched.
- **The agent is measured, not yet wired into the daily close.** Accepted answers show up in the dashboard's Agent resolutions tab. The daily close and its queue still use the engine's decisions only.
- **One model, one run.** No rerun variance was measured.
- **Reading bank statements is barely tested on real files.** Statement intake (an agent maps a raw export to columns, and a balance check proves the mapping) passed on the one real statement available. On 15 synthetic bank-format look-alikes, 4 pass; 9 use layouts the mapping schema cannot express yet ([intake report](reports/intake_eval.md)).

How it works: [ARCHITECTURE.md](ARCHITECTURE.md). MIT licensed.
