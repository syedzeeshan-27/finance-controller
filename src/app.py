"""AI Finance Controller — the reconciliation dashboard.

One daily close for a Razorpay merchant: settlements reconciled against the
bank statement, payments applied to orders, and a single severity-ranked
exception queue, plus drill-down tabs. Every number shown here is produced
by plain Python over the world on disk; the golden files are never read by
this app — what you see is what the engines actually decided.

Run:  streamlit run src/app.py     (port 8501, see .streamlit/config.toml)
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
import streamlit as st

from recon import io_load, schemas as S
from recon.engine import reconcile_leg_a
from recon.explain import attach_explanations, llm_mode
from recon.journey import build_journeys
from recon.leg_b import reconcile_leg_b
from recon.normalize import rupee_str_from_paise
from controller import queue_state as QS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEEDS_DIR = os.path.join(ROOT, "data", "seeds")
REPORTS_DIR = os.path.join(ROOT, "reports")

st.set_page_config(page_title="AI Finance Controller",
                   page_icon="🏦", layout="wide")


def inr(paise: int | None) -> str:
    if paise is None:
        return "—"
    sign = "-" if paise < 0 else ""
    return f"{sign}₹{rupee_str_from_paise(abs(paise), indian_grouping=True)}"


def inr_whole(paise: int | None) -> str:
    """Whole rupees for headline metrics, which truncate at laptop width;
    the paise-exact value goes in the metric's help text."""
    if paise is None:
        return "—"
    return inr(int(round(paise / 100)) * 100).rsplit(".", 1)[0]


def available_seeds() -> list[str]:
    if not os.path.isdir(SEEDS_DIR):
        return []
    return sorted(
        (d for d in os.listdir(SEEDS_DIR)
         if os.path.isfile(os.path.join(SEEDS_DIR, d, "settlements.csv"))),
        key=lambda x: (x != "42", x))


def available_worlds() -> list[tuple[str, str]]:
    """(label, data_dir) options: merchant registry first, seeds after."""
    options: list[tuple[str, str]] = []
    registry = os.path.join(ROOT, "data", "merchants.json")
    if os.path.exists(registry):
        with open(registry, encoding="utf-8") as f:
            for m in json.load(f):
                d = os.path.join(ROOT, *m["data_dir"].split("/"))
                if os.path.isfile(os.path.join(d, "settlements.csv")):
                    options.append((m["name"], d))
    for s in available_seeds():
        d = os.path.join(SEEDS_DIR, s)
        if all(d != existing for _, existing in options):
            options.append((f"world {s}", d))
    return options


def _dir_stamp(data_dir: str) -> float:
    return max((os.path.getmtime(os.path.join(data_dir, f))
                for f in os.listdir(data_dir)), default=0.0)


@st.cache_data(show_spinner="Reconciling…")
def run_reconciliation(data_dir: str, stamp: float) -> dict:
    settlements = io_load.load_settlements(data_dir)
    bank_rows = io_load.load_bank_rows(data_dir)
    payments = io_load.load_payments(data_dir)
    orders = io_load.load_orders(data_dir)

    decisions_a = reconcile_leg_a(settlements, bank_rows)
    attach_explanations(decisions_a)
    decisions_b = reconcile_leg_b(payments, orders)
    journeys = build_journeys(orders, payments, decisions_a, decisions_b)

    return {
        "settlements": settlements, "bank_rows": bank_rows,
        "payments": payments, "orders": orders,
        "decisions_a": [d for d in decisions_a],
        "decisions_b": decisions_b, "journeys": journeys,
    }


# --- Sidebar ------------------------------------------------------------------

st.sidebar.title("🏦 AI Finance Controller")
st.sidebar.caption("One daily close: settlements ↔ bank · payments ↔ "
                   "orders · one exception queue")
worlds = available_worlds()
if not worlds:
    st.error("No generated world found. Run `python -m recon.generate --seed 42` "
             "(with `src` on PYTHONPATH) and reload.")
    st.stop()
world_label = st.sidebar.selectbox("Merchant / world",
                                   [label for label, _ in worlds], index=0)
data_dir = dict(worlds)[world_label]
# Honest label: the dashboard never calls a model. attach_explanations()
# defaults to deterministic templates and nothing here passes use_llm=True;
# a configured key only enables live agent runs from the CLI.
_key_present = llm_mode() not in ("unavailable", "mock(deterministic templates)")
st.sidebar.caption(
    "Engine: deterministic multi-pass · Explanations: deterministic templates"
    + (" (API key found; LLM rephrasing stays off in the dashboard)"
       if _key_present else ""))

world = run_reconciliation(data_dir, _dir_stamp(data_dir))
A: list[S.Decision] = world["decisions_a"]

matches = [d for d in A if d.status in S.MATCHED_FAMILY]
exceptions = [d for d in A if d.kind == "exception"]
abstained = [d for d in A if d.status == S.AMBIGUOUS_ABSTAIN]
needs_review = [d for d in A if d.confidence == S.CONF_NEEDS_REVIEW]

# --- World capabilities -------------------------------------------------------
# Bank-only worlds (agent.intake output: a real statement + header-only
# PSP-side files) lack the data several tabs are about. Dependent sections
# check here and say plainly what is missing instead of crashing; nothing is
# ever synthesized to fill the gap.

_CAPS: dict[str, tuple[bool, str]] = {
    "payments": (bool(world["payments"]), "payments"),
    "orders": (bool(world["orders"]), "order book"),
    "settlements": (bool(world["settlements"]), "settlement advice"),
}


def _has(*required: str) -> bool:
    """True when the world has every listed capability; otherwise render the
    standard not-available note and return False. The app is a flat script,
    so callers indent their section body under this check."""
    missing = [label for cap, (ok, label) in _CAPS.items()
               if cap in required and not ok]
    if not missing:
        return True
    st.info(
        f"**Not available for this world** — it has no {' or '.join(missing)}. "
        "The dashboard shows only what a world's data can prove; nothing is "
        "synthesized to fill the gap. Switch to **Meridian Craftworks "
        "(seed 42)** in the sidebar for the full demo.")
    return False


st.title("AI Finance Controller — one close, one queue")
st.caption(
    f"{len(world['payments'])} payments · {len(world['settlements'])} settlements · "
    f"{len(world['bank_rows'])} bank rows · {len(world['orders'])} orders — "
    "every decision below carries its evidence; nothing is force-matched.")

(tab_close, tab_overview, tab_matches, tab_exceptions, tab_journey,
 tab_benchmark) = st.tabs(
    ["🏦 Daily Close", "📊 Overview", "🔗 Matches", "🚩 Exceptions",
     "🧭 Journey", "📏 Benchmark"])


# --- Daily close ---------------------------------------------------------------

@st.cache_data(show_spinner="Closing the day…")
def run_close(data_dir: str, stamp: float) -> dict:
    from controller.close import daily_close
    return daily_close(data_dir).to_dict()


with tab_close:
    close = run_close(data_dir, _dir_stamp(data_dir))
    # workflow overlay lives OUTSIDE the cache: the close stays a pure
    # function of the world; operator state is read fresh on every rerun
    queue = QS.overlay(close["queue"], QS.load_state(data_dir))
    sev = close["counts"]["by_severity"]
    panel = close["verify_panel"]

    cash_paise = close["cash"]["balance_paise"]
    at_risk = sum(i["money_at_risk_paise"] for i in queue)
    rs = close["recon_summary"]
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Cash in bank", inr_whole(cash_paise),
              help=f"{inr(cash_paise)} · close date {close['close_date']} · "
                   f"{close['cash']['statement_rows']} statement rows")
    k2.metric("Auto-reconciled",
              (f"{rs['matched']} of {rs['in_scope']}"
               if rs["match_rate"] is not None else "n/a"),
              help=("settlement-side decisions matched without a human "
                    "(bank debits and unrelated inflows excluded)"))
    k3.metric("Exception queue",
              f"{sev['S1']} S1 · {sev['S2']} S2 · {sev['S3']} S3",
              help="S1 act today · S2 chase externally · S3 review")
    k4.metric("₹ at risk in queue", inr_whole(at_risk), help=inr(at_risk))

    (st.success if panel["leg_a"] == 0 else st.error)(
        f"Independent leg A verifier on this exact output: {panel['leg_a']} "
        "violations. One pass: the full-world reconciliation ran once; leg B "
        "and the journeys reuse its decisions.")
    for w in close["warnings"]:
        st.caption(f"⚠️ {w}")

    # One "All" chip is the default; the individual options stay available
    # to narrow. "All" (or an empty pick) means everything, so the table can
    # never end up blank by accident.
    ALL = "All"
    ALL_OPEN = "All open"
    sources = sorted(close["counts"]["by_source"])
    open_states = [w for w in QS.WORKFLOWS if w != "resolved"]

    def _exclusive_all(key: str, specials: tuple[str, ...]) -> None:
        """Keep the special 'All'-style entries and specific picks mutually
        exclusive: choosing a specific option drops the special, choosing a
        special drops everything else, and clearing the box falls back to
        the first special (the default view)."""
        picks = list(st.session_state[key])
        prev = st.session_state.get(f"{key}::prev", [specials[0]])
        chosen = [p for p in picks if p in specials]
        if not picks:
            picks = [specials[0]]
        elif chosen and len(picks) > 1:
            fresh = [p for p in chosen if p not in prev]
            picks = ([fresh[-1]] if fresh
                     else [p for p in picks if p not in specials])
        st.session_state[key] = picks
        st.session_state[f"{key}::prev"] = picks

    fcol, scol, wcol = st.columns(3)
    k_sev, k_src, k_wf = (f"flt_sev::{data_dir}", f"flt_src::{data_dir}",
                          f"flt_wf::{data_dir}")
    sev_pick = fcol.multiselect("Severity", [ALL, "S1", "S2", "S3"],
                                default=[ALL], key=k_sev,
                                on_change=_exclusive_all, args=(k_sev, (ALL,)),
                                help="All = every severity; pick S1/S2/S3 "
                                     "to narrow")
    src_pick = scol.multiselect("Source", [ALL] + sources, default=[ALL],
                                key=k_src, on_change=_exclusive_all,
                                args=(k_src, (ALL,)), help="All = every source")
    wf_pick = wcol.multiselect("Workflow", [ALL_OPEN] + list(QS.WORKFLOWS),
                               default=[ALL_OPEN], key=k_wf,
                               on_change=_exclusive_all,
                               args=(k_wf, (ALL_OPEN,)),
                               help="All open = everything except resolved; "
                                    "pick 'resolved' to see closed items")
    sev = ["S1", "S2", "S3"] if (ALL in sev_pick or not sev_pick) else sev_pick
    src = sources if (ALL in src_pick or not src_pick) else src_pick
    wf = open_states if (ALL_OPEN in wf_pick or not wf_pick) else wf_pick
    shown = [i for i in queue
             if f"S{i['severity']}" in sev and i["source"] in src
             and i["workflow"] in wf]

    n_resolved = sum(1 for i in queue if i["workflow"] == "resolved")
    st.subheader(f"{len(shown)} queue items — worst first"
                 + (f" · {n_resolved} resolved" if n_resolved else ""))
    qdf = pd.DataFrame([{
        "id": i["item_id"],
        "sev": f"S{i['severity']}",
        "what": i["title"],
        "workflow": i["workflow"] + (" ⚠" if i["reopened"] else ""),
        "assignee": i["assignee"] or "",
        "status": i["status"],
        "source": i["source"],
        "records": ";".join(i["record_ids"]),
        "₹ at risk": inr(i["money_at_risk_paise"]),
        "overdue": (f"{i['days_overdue']}d" if i["days_overdue"] is not None
                    else ""),
    } for i in shown])
    st.dataframe(qdf, width="stretch", height=380)

    st.subheader("Inspect")
    pick = st.selectbox(
        "Queue item", shown,
        format_func=lambda i: f"[S{i['severity']}] {i['title']} · "
                              f"{';'.join(i['record_ids'])} · "
                              f"{inr(i['money_at_risk_paise'])}",
        key="close_pick")
    if pick:
        if pick["detail"]:
            st.info(pick["detail"])
        st.markdown(f"**Suggested action:** {pick['suggested_action']}")
        if pick["evidence"]:
            st.markdown("**What was found**")
            for e in pick["evidence"]:
                st.markdown(f"- `{e['rule']}` — {e['detail']}")
        if pick["candidates"]:
            st.markdown("**Candidates considered (all rejected)**")
            for cand in pick["candidates"]:
                amt = (f" · {inr(cand['amount_paise'])}"
                       if cand.get("amount_paise") is not None else "")
                st.markdown(f"- `{cand['id'] or '(none)'}`{amt} — "
                            f"{cand['note']}")

        if pick["reopened"]:
            st.warning("Reopened: this item was acted on when its status "
                       f"was `{QS.load_state(data_dir)['items'][pick['item_id']]['status_at_action']}`, "
                       f"but the data now says `{pick['status']}`. "
                       "Re-review before trusting the old resolution.")
        if pick["actions"]:
            with st.expander(f"Audit trail — {len(pick['actions'])} "
                             "action(s)"):
                for a in pick["actions"]:
                    extra = "".join([
                        f" → {a['assignee']}" if a.get("assignee") else "",
                        f" until {a['snooze_until']}"
                        if a.get("snooze_until") else "",
                        f" — {a['note']}" if a.get("note") else "",
                    ])
                    st.markdown(f"- `{a['at']}` **{a['action']}** by "
                                f"{a['by']}{extra}")

        with st.form(key=f"act_{pick['item_id']}"):
            st.markdown(f"**Act on `{pick['item_id']}`** — workflow state "
                        "only; the close itself is never edited.")
            note = st.text_input("Note", key=f"note_{pick['item_id']}")
            c1, c2, c3 = st.columns(3)
            assignee = c2.text_input("Assignee", value=pick["assignee"] or "")
            until = c3.date_input("Snooze until", value=None,
                                  format="YYYY-MM-DD")
            b1, b2, b3, b4 = st.columns(4)
            do_resolve = b1.form_submit_button("✅ Resolve",
                                               width="stretch")
            do_assign = b2.form_submit_button("👤 Assign",
                                              width="stretch")
            do_snooze = b3.form_submit_button("💤 Snooze",
                                              width="stretch")
            do_note = b4.form_submit_button("📝 Note only",
                                            width="stretch")
            action = ("resolve" if do_resolve else "assign" if do_assign
                      else "snooze" if do_snooze else "note" if do_note
                      else None)
            if action:
                QS.append_action(
                    data_dir, pick["item_id"], action=action, note=note,
                    assignee=assignee or None,
                    snooze_until=str(until) if until else None,
                    status_at_action=pick["status"])
                st.rerun()

    st.caption(
        "Queue policy: S1 = statutory exposure, realised bank-side cash "
        "error or projected insolvency; S2 = quantified money at risk "
        "needing an external chase; S3 = review. Ordered by severity, then "
        "money at risk. Measured against minted ground truth in "
        "`reports/close_audit.md`; the aggregation itself is checked by an "
        "independent verifier (`python -m controller.close <world> --json` "
        "then `python -m controller.verify_close <world> <close.json>`).")


# --- Overview -----------------------------------------------------------------

with tab_overview:
    reconciled_paise = sum(d.received_paise or 0 for d in matches)
    exception_paise = sum((d.expected_paise or d.received_paise or 0)
                          for d in exceptions
                          if d.status not in (S.OUT_OF_SCOPE, S.NON_SETTLEMENT_CREDIT))
    # The brief's own metric: of the records this loop is about (bank debits
    # and unrelated inflows excluded), how many were auto-reconciled.
    in_scope = [d for d in A
                if d.status not in (S.OUT_OF_SCOPE, S.NON_SETTLEMENT_CREDIT)]
    for_review = [d for d in exceptions
                  if d.status not in (S.OUT_OF_SCOPE, S.NON_SETTLEMENT_CREDIT)]
    c1, c2, c3 = st.columns(3)
    c1.metric("Settlements", len(world["settlements"]))
    c2.metric("Auto-reconciled", f"{len(matches)} decisions",
              help="matched / split / merged / matched-with-discrepancy")
    c3.metric("Match rate",
              f"{len(matches) / len(in_scope):.1%}" if in_scope else "n/a",
              help=f"{len(matches)} of {len(in_scope)} in-scope records "
                   "auto-reconciled (bank debits and unrelated inflows are "
                   "excluded); everything else goes to a human — the same "
                   "line the close report prints")
    c4, c5, c6 = st.columns(3)
    c4.metric("₹ reconciled", inr_whole(reconciled_paise),
              help=inr(reconciled_paise))
    c5.metric("Exceptions for review", len(for_review),
              help="missing credits/settlements, duplicates, ambiguous cases")
    c6.metric("₹ in exceptions", inr_whole(exception_paise),
              help=inr(exception_paise))

    st.divider()
    left, right = st.columns(2)
    with left:
        st.subheader("Decisions by status")
        counts = pd.Series([d.status for d in A]).value_counts()
        st.dataframe(counts.rename("count"), width="stretch")
    with right:
        st.subheader("Match confidence")
        if _has("settlements"):
            conf = pd.Series([d.confidence for d in matches]).value_counts()
            st.dataframe(conf.rename("count"), width="stretch")
            st.caption(
                "`exact` = verbatim UTR + exact paise · `high` = damaged UTR "
                "or decomposed discrepancy, corroborated by exact amounts · "
                "`medium` = amount unique in both directions · `needs_review` "
                "= matched but with an unexplained residual — a human signs "
                "off.")


def _decision_table(ds: list[S.Decision]) -> pd.DataFrame:
    return pd.DataFrame([{
        "status": d.status,
        "confidence": d.confidence,
        "settlements": ";".join(d.settlement_ids),
        "bank credits": ";".join(d.bank_txn_ids),
        "expected": inr(d.expected_paise),
        "received": inr(d.received_paise),
        "discrepancy": inr(d.discrepancy_paise),
        "decided by": d.pass_name,
        "evidence": " | ".join(e["rule"] for e in d.evidence),
    } for d in ds])


# --- Matches ------------------------------------------------------------------

with tab_matches:
    if _has("settlements"):
        st.subheader(f"{len(matches)} matched decisions")
        tiers = st.multiselect("Confidence filter", list(S.CONFIDENCE_TIERS),
                               default=list(S.CONFIDENCE_TIERS))
        shown = [d for d in matches if d.confidence in tiers]
        st.dataframe(_decision_table(shown), width="stretch",
                     height=420)
        st.subheader("Inspect a decision")
        pick = st.selectbox(
            "Decision", shown,
            format_func=lambda d: f"{d.status} · {';'.join(d.settlement_ids)} "
                                  f"↔ {';'.join(d.bank_txn_ids)}")
        if pick:
            st.info(pick.explanation)
            st.markdown("**Evidence**")
            for e in pick.evidence:
                st.markdown(f"- `{e['rule']}` — {e['detail']}")
            if pick.discrepancy_breakdown:
                st.markdown("**Discrepancy breakdown**")
                for b in pick.discrepancy_breakdown:
                    st.markdown(f"- {b['label']}: {inr(b['amount_paise'])}")


# --- Exceptions ---------------------------------------------------------------

# One action table for the whole product (controller.triage owns it); the
# benign non-settlement credit is queue-excluded, so its copy lives here.
from controller.triage import SUGGESTED_ACTIONS as _TRIAGE_ACTIONS

_SUGGESTED_ACTION = {
    **_TRIAGE_ACTIONS,
    S.NON_SETTLEMENT_CREDIT: "No action — unrelated inflow, excluded from "
                             "settlement reconciliation.",
}

with tab_exceptions:
    reviewable = [d for d in exceptions if d.status != S.OUT_OF_SCOPE]
    order = {S.AMBIGUOUS_ABSTAIN: 0, S.EXCEPTION_MISSING_BANK: 1,
             S.EXCEPTION_MISSING_SETTLEMENT: 2, S.DUPLICATE_CREDIT: 3,
             S.NON_SETTLEMENT_CREDIT: 4}
    reviewable.sort(key=lambda d: order.get(d.status, 9))
    st.subheader(f"{len(reviewable)} exceptions — every one explains itself")
    st.caption("This queue is the product: what was found, what candidates "
               "exist, why it could not be safely decided, and what to do next.")
    if needs_review:
        st.warning(f"{len(needs_review)} matched decision(s) additionally carry "
                   "an unexplained residual (confidence `needs_review`) — "
                   "listed under Matches.")
    for d in reviewable:
        rid = ";".join(d.settlement_ids + d.bank_txn_ids)
        amount = d.expected_paise if d.expected_paise is not None else d.received_paise
        with st.expander(f"**{d.status}** · {rid} · {inr(amount)}"):
            st.info(d.explanation)
            st.markdown("**What was found**")
            for e in d.evidence:
                st.markdown(f"- `{e['rule']}` — {e['detail']}")
            if d.candidates:
                st.markdown("**Candidates considered**")
                for c in d.candidates:
                    label = c.get("record_id") or "(none)"
                    amt = c.get("amount_paise")
                    amt_s = f" · {inr(amt)}" if amt else ""
                    st.markdown(f"- `{label}`{amt_s} — {c.get('reason', '')}")
            action = _SUGGESTED_ACTION.get(d.status)
            if action:
                st.markdown(f"**Suggested action:** {action}")


# --- Journey ------------------------------------------------------------------

with tab_journey:
    if _has("orders"):
        jdf = pd.DataFrame(world["journeys"])
        jdf["order_amount"] = jdf["order_amount_paise"].map(inr)
        jdf = jdf.drop(columns=["order_amount_paise"])
        broken_only = st.toggle("Show only broken journeys", value=True)
        view = jdf[jdf["first_break"] != ""] if broken_only else jdf
        st.subheader(f"{len(view)} of {len(jdf)} order journeys"
                     + (" with a broken link" if broken_only else ""))
        st.caption("order → payment → settlement → bank credit; `first_break` "
                   "names the first stage where the money trail stops.")
        st.dataframe(view, width="stretch", height=480)


# --- Benchmark ----------------------------------------------------------------

with tab_benchmark:
    st.caption("Engine benchmark — independent of the merchant/world selected "
               "above: committed results over the generated seed worlds 42-46 "
               "with minted ground truth (regenerate via repro step 4).")
    results_path = os.path.join(REPORTS_DIR, "benchmark_results.json")
    if not os.path.exists(results_path):
        st.info("No benchmark results yet. Run "
                "`python -m recon.benchmark --seeds 42,43,44,45,46` "
                "(with `src` on PYTHONPATH).")
    else:
        with open(results_path, encoding="utf-8") as f:
            results = json.load(f)
        st.caption(f"Seeds {results['seeds']} · ground truth generated "
                   "deterministically at data-generation time; graded by exact "
                   "comparison — never by the system itself, never by an LLM. "
                   f"LLM mode during run: {results['llm_mode']}")
        rows = []
        for name, r in results["strategies"].items():
            agg = r["aggregate"]

            def m(key, pct=True):
                v = agg[key]
                if v is None:
                    return "—"
                return f"{v['mean']:.1%}" if pct else f"{v['mean']:,.0f}"

            vio = sum(r["per_seed"][s]["verify_violation_count"]
                      for s in r["per_seed"])
            rows.append({
                "strategy": name, "precision": m("precision"),
                "recall": m("recall"), "F1": m("f1"),
                "disposition accuracy": m("disposition_accuracy"),
                "auto-resolved": m("auto_resolution_rate"),
                "records/sec": m("throughput_records_per_sec", pct=False),
                "invariant violations": vio,
            })
        st.subheader("Leg A: strategy comparison (mean across seeds)")
        st.dataframe(pd.DataFrame(rows), width="stretch")

        st.subheader("Per-scenario disposition accuracy (first seed)")
        strategies = list(results["strategies"])
        first_seed = str(results["seeds"][0])
        scenario_rows = []
        base = results["strategies"][strategies[0]]["per_seed"][first_seed]["per_scenario"]
        for tag in sorted(base):
            row = {"scenario": tag, "rows": base[tag]["total"]}
            for name in strategies:
                sc = results["strategies"][name]["per_seed"][first_seed]["per_scenario"].get(tag)
                row[name] = f"{sc['accuracy']:.0%}" if sc else "—"
            scenario_rows.append(row)
        st.dataframe(pd.DataFrame(scenario_rows), width="stretch", height=560)

        lb = results["leg_b"]["aggregate"]["disposition_accuracy"]
        st.subheader("Leg B: payment ↔ order book")
        st.metric("Disposition accuracy (mean across seeds)",
                  f"{lb['mean']:.1%}" if lb else "—")
        for n in results["notes"]:
            st.caption(f"• {n}")
