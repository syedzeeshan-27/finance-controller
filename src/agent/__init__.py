"""The agentic layer: an LLM that *proposes*, deterministic code that *disposes*.

Design contract (enforced by tests/test_agent_intake.py's quarantine tests):

- No decision module (recon/tax/forecast/controller) imports this package.
- The agent's only influence on the pipeline is through artifacts that
  deterministic validators prove correct arithmetically (e.g. a statement
  mapping whose running-balance chain must reproduce to the paisa) or
  through advisory text attached to already-final items.
- Every live run records a JSONL transcript; committed transcripts replay
  byte-identically with no API key (request hashes are checked), so the
  full repro remains offline and deterministic.
"""
