"""Stage 3: tax matching — the merchant's tax position, reconciled.

Three deterministic loops over data the world already produces:
input GST credits (merchant books vs GSTR-2B), TDS credits (observed
deductions vs Form 26AS), and tax-obligation compliance (recomputed
liability vs the actual payment debit). Ground truth is minted at
generation time; the engine derives the books side blind to that truth; an
independent verifier hard-fails the benchmark on any violated invariant.
"""
