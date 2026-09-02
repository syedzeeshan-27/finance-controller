"""Stage 2: cash forecasting — historical cash to future cash.

Deterministic, explainable, and honestly evaluated: the forecaster sees only
pre-cutoff data (enforced by API shape and proven by a leakage test); the
generator's own post-cutoff bank statement is the ground truth; a rolling-
origin backtest grades the forecaster against baselines it must beat.
"""
