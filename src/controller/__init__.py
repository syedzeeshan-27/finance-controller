"""The finance controller: one daily close for a Razorpay merchant.

Leg A reconciles settlements against the bank statement, leg B applies
payments to orders. `daily_close` runs both in ONE pass over the world (the
full-world leg A reconciliation executes exactly once) and folds every
decision that needs a human into ONE severity-ranked exception queue with
evidence and a suggested action per item.

Modules:
  schemas      ExceptionItem / DailyClose shapes
  triage       the published severity + money-at-risk + action policy
  overdue      settlements past their expected credit date (queue enrichment)
  close        the one-pass orchestrator, markdown report and CLI
  verify_close independent verifier of the aggregation layer
  audit        queue recall/precision measured against minted ground truth
"""
