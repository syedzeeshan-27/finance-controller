"""The unified finance controller: one daily close across all three loops.

Stage 1 reconciles settlements against the bank statement, Stage 2 forecasts
the cash path, Stage 3 closes the Indian tax loops. This package is the final
stage: a single `daily_close` that runs all of them in ONE pass over the world
(the full-world Stage 1 reconciliation executes exactly once and is injected
into the tax and forecast surfaces) and folds every decision that needs a
human into ONE severity-ranked exception queue with evidence and a suggested
action per item.

Modules:
  schemas      ExceptionItem / DailyClose shapes
  triage       the published severity + money-at-risk + action policy
  close        the one-pass orchestrator, markdown report and CLI
  verify_close independent verifier of the aggregation layer
  audit        queue recall/precision measured against minted ground truth
"""
