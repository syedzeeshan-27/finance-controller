"""Reproduce every number in README.md / reports/ from a clean checkout.

Usage (from anywhere; the script pins its own working directory):
    python scripts/repro.py            # all steps
    python scripts/repro.py --steps 2-5
    python scripts/repro.py --skip-install

Cross-platform single source of truth — scripts/repro.ps1 and
scripts/repro.sh are thin wrappers around this file. Requires Python 3.11+.
No API key needed: every step is deterministic and offline. The agent
evaluations replay the recorded transcripts (request hashes checked), so a
changed prompt, tool or tool result fails loudly instead of drifting.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(*module_args: str) -> None:
    """Run `python -m <module_args>` from the repo root with src importable."""
    env = {**os.environ, "PYTHONPATH": "src", "PYTHONUTF8": "1"}
    subprocess.run([sys.executable, "-m", *module_args],
                   cwd=ROOT, env=env, check=True)


def _exists(*parts: str) -> bool:
    return os.path.exists(os.path.join(ROOT, *parts))


def _skip(reason: str) -> None:
    print(f"SKIPPED: {reason}")


def step_1_install() -> None:
    run("pip", "install", "-q", "-r", "requirements.txt",
        "-r", "requirements-dev.txt")


def step_2_tests() -> None:
    run("pytest")


def step_3_determinism() -> None:
    run("recon.generate", "--seed", "42", "--verify-determinism")
    run("recon.generate", "--seed", "42", "--days", "180",
        "--verify-determinism")
    if _exists("src", "recon", "holdout.py"):
        for seed in ("1000", "1001", "1002", "1003", "1004", "1005", "1006"):
            run("recon.holdout", "--seed", seed, "--verify-determinism")
    else:
        _skip("src/recon/holdout.py not present")


def step_4_recon_benchmark() -> None:
    run("recon.benchmark", "--seeds", "42,43,44,45,46")


def step_5_close() -> None:
    run("controller.audit", "--seeds", "42,43,44,45,46", "--days", "180")
    run("controller.close", "data/seeds/42d180", "--report", "--json")
    run("controller.verify_close", "data/seeds/42d180",
        "reports/daily_close_42d180.json")


def step_6_holdout_worlds() -> None:
    # Regenerates every dev and held-out world in place; git shows no diff
    # when the generator is deterministic (the worlds are committed).
    if _exists("src", "recon", "holdout.py"):
        run("recon.holdout", "--all")
    else:
        _skip("src/recon/holdout.py not present")


def step_7_agent_eval_dev() -> None:
    if _exists("data", "agent_transcripts", "resolve", "1000", "manifest.json"):
        run("agent.eval_resolve", "--seeds", "1000,1006", "--replay")
    else:
        _skip("no recorded dev-seed resolver transcripts yet")


def step_8_agent_eval_heldout() -> None:
    if _exists("data", "agent_transcripts", "resolve", "1001", "manifest.json"):
        run("agent.eval_resolve", "--seeds", "1001-1005", "--replay")
    else:
        _skip("no recorded held-out resolver transcripts yet")


def step_9_real_intake() -> None:
    # Offline: replays the committed LIVE agent recording (request hashes
    # checked — any harness drift fails loudly), re-proves the mapping with
    # the deterministic validator, and regenerates the real-data report.
    # Output goes to out/ (gitignored); byte-equality with the committed
    # world is pinned by tests/test_agent_intake.py.
    run("agent.intake", "data/real/statement_a/statement.xlsx",
        "--out", "out/real_intake",
        "--replay", "data/agent_transcripts/intake/statement_a.jsonl",
        "--report")


def step_10_intake_batch() -> None:
    args = ["agent.intake", "--batch", "data/real/", "--offline", "--report"]
    if _exists("data", "lookalikes"):
        args[4:4] = ["--synthetic", "data/lookalikes/"]
    run(*args)


def step_11_ingest() -> None:
    run("ingest.razorpay_files", "data/fixtures/razorpay", "out/rzp_ingest")
    run("ingest.pull", "--out", "out/rzp_ingest_live_shape")
    run("ingest.webhook_inbox", "data/fixtures/razorpay/webhooks",
        "out/rzp_webhooks", "--secret", "rzp_demo_webhook_secret")


def step_12_investigate() -> None:
    # Offline: replays the committed LIVE investigator recording (request
    # hashes checked) on a fresh copy of the seed-42 world, so the advisory
    # note lands in that copy's workflow state and data/state/42 stays yours.
    import shutil
    world = os.path.join(ROOT, "out", "investigate_world")
    shutil.rmtree(world, ignore_errors=True)
    shutil.rmtree(os.path.join(ROOT, "data", "state", "out_investigate_world"),
                  ignore_errors=True)
    shutil.copytree(os.path.join(ROOT, "data", "seeds", "42"), world)
    run("agent.investigate", "out/investigate_world", "6a547f24b524",
        "--replay",
        "data/agent_transcripts/investigate/seed42_6a547f24b524.jsonl")


STEPS: list[tuple[int, str, object]] = [
    (1, "install dependencies", step_1_install),
    (2, "test suite", step_2_tests),
    (3, "generator and holdout determinism", step_3_determinism),
    (4, "reconciliation benchmark (5 seeds, independently verified; naive and "
        "UTR baselines)", step_4_recon_benchmark),
    (5, "daily close: audit vs minted truth + the close itself, "
        "re-verified from its own JSON", step_5_close),
    (6, "held-out and dev worlds regenerated (committed; no diff expected)",
     step_6_holdout_worlds),
    (7, "resolver agent on the dev worlds: recorded replay, verified, graded",
     step_7_agent_eval_dev),
    (8, "resolver agent on the held-out worlds: recorded replay, verified, "
        "graded", step_8_agent_eval_heldout),
    (9, "real bank statement intake: recorded agent replay, proven by the "
        "validator + real-data report", step_9_real_intake),
    (10, "statement intake at batch scale: recorded replays (real + "
         "synthetic look-alikes)", step_10_intake_batch),
    (11, "Razorpay ingestion: official-schema fixtures + signed webhook "
         "inbox (offline)", step_11_ingest),
    (12, "investigator agent: recorded replay on one queue item (offline, "
         "request-hash checked)", step_12_investigate),
]


def _parse_steps(spec: str) -> set[int]:
    wanted: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            wanted.update(range(int(lo), int(hi) + 1))
        else:
            wanted.add(int(part))
    return wanted


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Reproduce every committed benchmark artifact.")
    ap.add_argument("--steps", default=None, metavar="SPEC",
                    help="which steps to run, e.g. '2-5' or '1,3,5' "
                         "(default: all)")
    ap.add_argument("--skip-install", action="store_true",
                    help="skip step 1 (pip install)")
    args = ap.parse_args()

    wanted = _parse_steps(args.steps) if args.steps else {n for n, _, _ in STEPS}
    if args.skip_install:
        wanted.discard(1)
    unknown = wanted - {n for n, _, _ in STEPS}
    if unknown:
        ap.error(f"unknown step(s): {sorted(unknown)}")

    total = len(STEPS)
    for n, title, fn in STEPS:
        if n not in wanted:
            continue
        print(f"== {n}/{total} {title} ".ljust(72, "="), flush=True)
        fn()

    print()
    print("Done. Reconciliation: reports/benchmark_results.json / "
          "benchmark_report.md")
    print("Agent evaluation:     reports/agent_eval.md / .json (held-out), "
          "agent_eval_dev.md / .json")
    print("Statement intake:     reports/intake_eval.md, real_data_report.md")
    print("Daily close:          reports/close_audit.json / close_audit.md / "
          "daily_close_42d180.md / daily_close_42d180.json")
    print("Dashboard:            streamlit run src/app.py")


if __name__ == "__main__":
    main()
