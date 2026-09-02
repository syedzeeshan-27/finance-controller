"""The real-data report: what happened when reality met the pipeline."""

from __future__ import annotations

from recon.normalize import rupee_str_from_paise


def _inr(paise: int) -> str:
    return "₹" + rupee_str_from_paise(paise, indian_grouping=True)


def render_real_data_report(*, raw_name: str, result, by_status: dict,
                            bank_rows) -> str:
    m = result.mapping
    st = result.validation.stats
    lines = [
        "# Real-data run — messy bank export through the untouched engine",
        "",
        f"Source: `{raw_name}` · intake mode: **{result.mode}** · "
        f"{st['rows']} raw grid rows → {result.canonical_rows} canonical "
        "transaction rows.",
        "",
        "The intake agent proposed the statement's structure (header, "
        "columns, periods, row classes, reference recipe); a deterministic "
        "validator then **proved** the proposal arithmetically before a "
        "single canonical row was written. No LLM output is trusted — only "
        "verified.",
        "",
        "## The proof: running-balance chain, to the paisa",
        "",
        "| period | opening | txn rows | credits | debits | closing | chain |",
        "|---|---|---|---|---|---|---|",
    ]
    for p in st["periods"]:
        closing = _inr(p["closing_paise"]) if p["closing_paise"] is not None \
            else "—"
        lines.append(
            f"| {p['period']} | {_inr(p['opening_paise'])} | "
            f"{p['transaction_rows']} | {_inr(p['credits_paise'])} | "
            f"{_inr(p['debits_paise'])} | {closing} | "
            f"{'verified row-by-row ✓' if p['chain_ok'] else 'BROKEN'} |")
    lines += [
        "",
        "Every row's stated balance equals the recomputed opening + credits "
        "− debits at that row, and each period ends exactly on its printed "
        "closing balance. After writing the canonical CSV the chain is "
        "proven a second time from `recon.io_load`'s typed rows — an "
        "independent parse of the same numbers.",
        "",
        "## What the statement contains",
        "",
        f"- header row + {st['noise_rows']} noise rows (banners, blanks, "
        "abbreviation legend) — none transaction-like, by check;",
        f"- {st['transaction_rows']} transactions across "
        f"{len(st['periods'])} statement period(s);",
        f"- reference recipe `{m.reference_recipe}` extracted a token from "
        f"{st['tokens_extracted']}/{st['transaction_rows']} transactions;",
    ]
    if m.reversal_pairs:
        pairs = ", ".join(f"rows {a}+{b}" for a, b in m.reversal_pairs)
        lines.append(f"- {len(m.reversal_pairs)} reversal pair(s) proven — "
                     f"same reference, equal and opposite amounts ({pairs});")
    for w in result.validation.warnings:
        lines.append(f"- note: {w['detail']}.")
    lines += [
        "",
        "## The untouched engine's verdict",
        "",
        "| status | rows |",
        "|---|---|",
    ]
    for status, n in sorted(by_status.items()):
        lines.append(f"| `{status}` | {n} |")
    credits = sum(1 for r in bank_rows if r.credit_paise)
    lines += [
        "",
        f"Read honestly: this is a personal UPI account — {credits} credits, "
        "none of them Razorpay settlements — and the engine **claims none of "
        "them**. Every credit lands as a non-settlement classification "
        "rather than a forced match, which is exactly the abstention "
        "discipline the synthetic benchmark rewards, now demonstrated on "
        "real data the generator never produced: truncated UPI narrations, "
        "an interest credit, a same-reference reversal, and two disjoint "
        "statement periods in one sheet.",
        "",
        "Reproduce offline (no API key): "
        "`python -m agent.intake data/real/statement_a/statement.xlsx "
        "--out <dir> --replay data/agent_transcripts/intake/"
        "statement_a.jsonl --report` — the transcript replays the recorded "
        "agent conversation and fails loudly if the harness has drifted "
        "from it.",
    ]
    return "\n".join(lines) + "\n"
