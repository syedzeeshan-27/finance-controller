"""Statement intake at batch scale: every export in a folder, one table.

    python -m agent.intake --batch data/real/ --report
    python -m agent.intake --batch data/real/ --synthetic data/lookalikes/ --report

Each `.xlsx` / `.csv` under a folder (skipping `world/` and `golden/`
sub-folders) goes through the same intake agent and the same deterministic
validator as the single-file path, with at most 3 submit attempts. A file
passes on attempt 1, 2 or 3, or fails with the validator's error codes (or
the agent error). Transcripts live in `data/agent_transcripts/intake_batch/`:
a file with a transcript replays offline; a file without one runs live when
an API key is configured, and is reported "not run" otherwise.

Synthetic look-alikes (`--synthetic`) are reported in their own table,
headed as synthetic. When a look-alike has a golden structure file
(`<dir>/golden/<stem>.json`), the accepted mapping's header row, column
roles and transaction rows are graded against it, because a balance chain
that closes does not prove the narration and reference columns were not
swapped.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from agent import intake as I
from agent.pricing import Budget, BudgetExceeded, BudgetedTransport
from agent.gemini import QuotaExhausted
from agent.provider import AgentError, ReplayTransport, RunStop, make_live_transport
from agent.rawgrid import load_grid
from agent.resolve import transcript_stats

BATCH_MAX_ATTEMPTS = 3
TRANSCRIPTS_ROOT = os.path.join("data", "agent_transcripts", "intake_batch")
DEFAULT_REPORT = os.path.join("reports", "intake_eval.md")

_BANKS = (  # (display name, bank names, IFSC prefixes, file-name markers)
    ("HDFC Bank", ("HDFC BANK",), ("HDFC0",), ("hdfc",)),
    ("ICICI Bank", ("ICICI BANK",), ("ICIC0",), ("icici",)),
    ("State Bank of India", ("STATE BANK OF INDIA",), ("SBIN0",), ("sbi",)),
    ("Axis Bank", ("AXIS BANK",), ("UTIB0",), ("axis",)),
    ("Kotak Mahindra Bank", ("KOTAK MAHINDRA",), ("KKBK0",), ("kotak",)),
)
_ROLES = ("date", "narration", "debit", "credit", "balance")


def statement_files(folder: str) -> list[str]:
    out = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d not in ("world", "golden"))
        for name in sorted(files):
            if os.path.splitext(name)[1].lower() in (".xlsx", ".csv"):
                out.append(os.path.join(root, name))
    return out


def detect_bank(grid: list[list[str]], path: str) -> str:
    """The statement's own bank: a bank name beats an IFSC code, and the
    earliest hit wins, because counterparties' banks (Razorpay pays from an
    Axis account, for one) show up in narrations further down."""
    rows = [" ".join(r).upper() for r in grid[:40]]
    for kind in (1, 2):                       # 1 = bank names, 2 = IFSC prefixes
        best = None
        for bank in _BANKS:
            hit = next((i for i, t in enumerate(rows)
                        if any(m in t for m in bank[kind])), None)
            if hit is not None and (best is None or hit < best[0]):
                best = (hit, bank[0])
        if best:
            return best[1]
    low = os.path.basename(path).lower()
    for name, _, _, file_markers in _BANKS:
        if any(low.startswith(m) for m in file_markers):
            return name
    return "unknown"


def layout_limits(golden: dict) -> list[str]:
    """Layout features of a golden structure file that the mapping schema
    cannot express (it needs separate debit/credit columns, and opening and
    closing balances as rows of the table)."""
    out = []
    if (golden.get("columns") or {}).get("amount") is not None:
        out.append("one amount column with a Dr/Cr marker")
    periods = golden.get("periods", [])
    if any(p.get("opening_row") is None for p in periods):
        out.append("opening balance only in a summary line")
    if any(p.get("closing_row") is None for p in periods):
        out.append("closing balance only in a summary line")
    return out


def grade_roles(mapping: I.StatementMapping, golden: dict) -> list[str]:
    """Mismatches between an accepted mapping and a golden structure file."""
    issues = []
    if mapping.header_row != golden.get("header_row"):
        issues.append(f"header row {mapping.header_row} (golden {golden.get('header_row')})")
    cols = golden.get("columns", {})
    for role in _ROLES:
        want = cols.get(role)
        if want is not None and mapping.columns.get(role) != want:
            issues.append(f"{role} column {mapping.columns.get(role)} (golden {want})")
    got_rows = sorted(r for p in mapping.periods for r in p.transaction_rows)
    want_rows = sorted(r for p in golden.get("periods", [])
                       for r in p.get("transaction_rows", []))
    if got_rows != want_rows:
        issues.append(f"{len(got_rows)} transaction rows (golden {len(want_rows)})")
    return issues


@dataclass
class FileResult:
    file: str
    bank: str
    rows: int                   # rows in the file, as the grid loader sees them
    result: str                 # "attempt 1" | "attempt 2" | "attempt 3" | "failed" | "not run"
    reason: str = ""
    roles: str = ""
    transactions: int = 0       # transaction rows in the accepted mapping
    layout: str = ""            # look-alikes: what the mapping schema cannot express
    transcript: str = ""
    api_calls: int = 0
    cost_usd: float = 0.0
    synthetic: bool = False
    extra: dict = field(default_factory=dict)


def _transcript_path(folder: str, path: str) -> str:
    rel = os.path.relpath(path, folder).replace(os.sep, "__").replace("/", "__")
    base = os.path.basename(os.path.normpath(folder))
    return os.path.join(TRANSCRIPTS_ROOT, base, rel + ".jsonl")


def run_file(folder: str, path: str, synthetic: bool, budget: Budget,
             transport_factory=None, offline: bool = False) -> FileResult:
    grid = load_grid(path)
    fr = FileResult(file=os.path.relpath(path).replace(os.sep, "/"),
                    bank=detect_bank(grid, path), rows=len(grid),
                    result="not run", synthetic=synthetic)
    tpath = _transcript_path(folder, path)
    fr.transcript = os.path.relpath(tpath).replace(os.sep, "/")
    golden_path = os.path.join(os.path.dirname(path), "golden",
                               os.path.splitext(os.path.basename(path))[0] + ".json")
    golden = None
    if os.path.exists(golden_path):
        with open(golden_path, encoding="utf-8") as f:
            golden = json.load(f)
        fr.layout = "; ".join(layout_limits(golden)) or "expressible"
    if os.path.exists(tpath):
        transport = ReplayTransport(tpath)
    elif offline:
        fr.reason = "no recorded transcript (offline run)"
        return fr
    else:
        try:
            factory = transport_factory or make_live_transport
            transport = BudgetedTransport(factory(tpath, task=f"intake {fr.file}"), budget)
        except AgentError as exc:
            fr.reason = f"not run: {exc}"
            return fr
    stats: dict = {}
    try:
        mapping, report = I._agent_mapping(grid, transport,
                                           max_rejections=BATCH_MAX_ATTEMPTS,
                                           stats=stats)
        fr.result = f"attempt {stats['attempts']}"
        fr.transactions = report.stats["transaction_rows"]
        if golden is not None:
            issues = grade_roles(mapping, golden)
            fr.roles = "all correct" if not issues else "; ".join(issues)
    except RunStop:
        # budget, daily quota or provider failure: not the agent's result;
        # drop the partial recording so the file runs live next time
        if os.path.exists(tpath) and not isinstance(transport, ReplayTransport):
            os.remove(tpath)
        raise
    except I.IntakeError as exc:
        fr.result = "failed"
        codes = sorted({e["code"] for e in (exc.report.errors if exc.report else [])})
        fr.reason = (", ".join(codes) + f" (after {stats.get('attempts', 0)} attempt(s))"
                     if codes else "the model stopped without submitting a mapping"
                     if not stats.get("attempts") else str(exc))
    if os.path.exists(tpath):
        fr.api_calls, fr.cost_usd, _ = transcript_stats(tpath)
    return fr


def run_batch(real: list[str], synthetic: list[str], offline: bool = False,
              transport_factory=None) -> tuple[list[FileResult], str]:
    budget = Budget()
    results: list[FileResult] = []
    stopped = ""
    for folders, is_synth in ((real, False), (synthetic, True)):
        for folder in folders:
            for path in statement_files(folder):
                if stopped:
                    results.append(FileResult(os.path.relpath(path).replace(os.sep, "/"),
                                              "", 0, "not run", reason=f"{stopped} stop",
                                              synthetic=is_synth))
                    continue
                try:
                    results.append(run_file(folder, path, is_synth, budget,
                                            transport_factory, offline))
                except BudgetExceeded:
                    stopped = "budget"
                    results.append(FileResult(os.path.relpath(path).replace(os.sep, "/"),
                                              "", 0, "not run", reason="AGENT_MAX_USD reached",
                                              synthetic=is_synth))
                except QuotaExhausted:
                    stopped = "quota"
                    results.append(FileResult(os.path.relpath(path).replace(os.sep, "/"),
                                              "", 0, "not run", reason="daily quota reached",
                                              synthetic=is_synth))
                except RunStop as exc:
                    stopped = "provider"
                    results.append(FileResult(os.path.relpath(path).replace(os.sep, "/"),
                                              "", 0, "not run",
                                              reason=f"provider error: {str(exc)[:200]}",
                                              synthetic=is_synth))
    return results, stopped


def _table(rows: list[FileResult], synthetic: bool) -> list[str]:
    head = "| file | bank | rows | result | failure reason |"
    sep = "|---|---|---|---|---|"
    if synthetic:
        head += " layout the mapping cannot express | column roles vs golden |"
        sep += "---|---|"
    out = [head, sep]
    for r in rows:
        result = r.result + (f" ({r.transactions} transactions)" if r.transactions else "")
        line = f"| `{r.file}` | {r.bank} | {r.rows} | {result} | {r.reason or '—'} |"
        if synthetic:
            limits = "" if r.layout in ("", "expressible") else r.layout
            line += f" {limits or '—'} | {r.roles or '—'} |"
        out.append(line)
    return out


def _summary(rows: list[FileResult]) -> str:
    counts = {k: sum(1 for r in rows if r.result == k)
              for k in ("attempt 1", "attempt 2", "attempt 3", "failed", "not run")}
    line = (f"{len(rows)} file(s): passed on attempt 1 / 2 / 3: {counts['attempt 1']} / "
            f"{counts['attempt 2']} / {counts['attempt 3']} · failed {counts['failed']} · "
            f"not run {counts['not run']}")
    graded = [r for r in rows if r.layout]
    if graded:
        ok = [r for r in graded if r.layout == "expressible"]
        passed = sum(1 for r in ok if r.result.startswith("attempt"))
        line += (f". {len(ok)} of {len(graded)} layouts can be expressed by the mapping "
                 f"schema; {passed} of those {len(ok)} passed.")
    return line


def render(results: list[FileResult], stopped: str, real: list[str],
           synthetic: list[str]) -> str:
    real_rows = [r for r in results if not r.synthetic]
    synth_rows = [r for r in results if r.synthetic]
    cost = sum(r.cost_usd for r in results)
    lines = ["# Statement intake at batch scale", ""]
    if stopped:
        lines += [f"> **INCOMPLETE**: stopped early ({stopped}). Files after the stop "
                  "are listed as not run.", ""]
    lines += [
        "Each file goes through the intake agent and the deterministic validator "
        f"(the running-balance chain must close to the paisa) with at most "
        f"{BATCH_MAX_ATTEMPTS} submit attempts. Offline reruns replay the recorded "
        f"transcripts. List-price cost of the recorded calls: ${cost:.4f} "
        "(free tier: $0 billed).",
        "",
        f"## Real statements ({', '.join(real) or 'none'})",
        "",
        _summary(real_rows),
        "",
        *_table(real_rows, synthetic=False),
        "",
        "Only one real statement is available to this project today (a personal UPI "
        "account; its bank name was removed when it was anonymised, so the bank reads "
        "unknown). Add anonymised exports to `data/real/` and rerun the command above; "
        "the table fills in.",
    ]
    if synthetic:
        lines += [
            "",
            f"## Synthetic look-alikes ({', '.join(synthetic)}) — NOT real statements",
            "",
            "Built by a separate agent that never saw the intake code, from public "
            "descriptions of HDFC, ICICI, SBI, Axis and Kotak export layouts. They "
            "measure robustness to layout variety, not real-world accuracy.",
            "",
            _summary(synth_rows),
            "",
            "The mapping schema needs separate debit and credit columns, and opening and "
            "closing balances as rows of the table. Layouts without them (a single amount "
            "column with a Dr/Cr marker; a balance given only in a summary line) cannot pass "
            "whatever the model does. Which files those are is read from each file's golden "
            "structure file, not from the results. Supporting them is a schema change, left "
            "for later.",
            "",
            *_table(synth_rows, synthetic=True),
        ]
    return "\n".join(lines) + "\n"


def main(args) -> None:
    results, stopped = run_batch(args.batch, args.synthetic or [], offline=args.offline)
    for r in results:
        print(f"{r.result:<10} {r.bank:<22} {r.file}" + (f"  [{r.reason}]" if r.reason else ""))
    if args.report is not None:
        path = args.report or DEFAULT_REPORT
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(render(results, stopped, args.batch, args.synthetic or []))
        with open(os.path.splitext(path)[0] + ".json", "w", encoding="utf-8",
                  newline="\n") as f:
            json.dump([r.__dict__ for r in results], f, indent=1, sort_keys=True)
            f.write("\n")
        print(f"report: {path}")
