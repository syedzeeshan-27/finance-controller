"""AI Finance Controller — the unified dashboard.

One daily close across all three loops (settlement reconciliation, cash
forecast, tax matching) with a single severity-ranked exception queue, plus
per-loop drill-down tabs. Every number shown here is produced by plain
Python over the generated world; the golden files are never read by this
app — what you see is what the engines actually decided.

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


# Working-capital floor for cash-crunch alerts: per merchant in
# data/merchants.json (`low_cash_threshold_paise`, null = no floor, e.g. a
# personal account); generated seed worlds default to Rs 10 lakh.
DEFAULT_THRESHOLD_PAISE = 100_000_000
_THRESHOLDS: dict[str, int | None] = {}


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
                    _THRESHOLDS[d] = m.get("low_cash_threshold_paise",
                                           DEFAULT_THRESHOLD_PAISE)
    for s in available_seeds():
        d = os.path.join(SEEDS_DIR, s)
        if all(d != existing for _, existing in options):
            options.append((f"world {s}", d))
            _THRESHOLDS.setdefault(d, DEFAULT_THRESHOLD_PAISE)
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
st.sidebar.caption("One daily close: reconciliation · cash forecasting · "
                   "tax matching")
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
    "gstr2b": (os.path.isfile(os.path.join(data_dir, "gstr2b.csv")),
               "GSTR-2B purchase data"),
    "manifest": (os.path.isfile(os.path.join(data_dir,
                                             "golden_manifest.json")),
                 "generated-world manifest (golden_manifest.json)"),
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
 tab_forecast, tab_tax, tab_benchmark) = st.tabs(
    ["🏦 Daily Close", "📊 Overview", "🔗 Matches", "🚩 Exceptions",
     "🧭 Journey", "📈 Forecast", "🧾 Tax", "📏 Benchmark"])


# --- Daily close ---------------------------------------------------------------

@st.cache_data(show_spinner="Closing the day…")
def run_close(data_dir: str, stamp: float, horizon: int,
              threshold_paise: int | None) -> dict:
    from controller.close import daily_close
    return daily_close(data_dir, horizon=horizon,
                       threshold_paise=threshold_paise).to_dict()


with tab_close:
    # per-merchant default; the widget key includes the world so switching
    # merchants picks up that merchant's floor instead of a stale value
    _default_thr = _THRESHOLDS.get(data_dir, DEFAULT_THRESHOLD_PAISE)
    thr_l = st.number_input("Low-cash alert threshold (Rs lakh; 0 = off)",
                            min_value=0.0, max_value=100.0,
                            value=(_default_thr or 0) / 10_000_000,
                            step=0.5, key=f"close_threshold::{data_dir}")
    threshold_paise = int(thr_l * 10_000_000) or None
    if threshold_paise is None:
        st.caption("No low-cash floor configured for this merchant — "
                   "cash-crunch alerts are off. Set `low_cash_threshold_paise` "
                   "in `data/merchants.json` to enable them.")
    close = run_close(data_dir, _dir_stamp(data_dir), 14, threshold_paise)
    # workflow overlay lives OUTSIDE the cache: the close stays a pure
    # function of the world; operator state is read fresh on every rerun
    queue = QS.overlay(close["queue"], QS.load_state(data_dir))
    sev = close["counts"]["by_severity"]
    panel = close["verify_panel"]

    cash_paise = close["cash"]["balance_paise"]
    min_bal = close["forecast"]["min_balance"]
    at_risk = sum(i["money_at_risk_paise"] for i in queue)
    itc_now = (close["tax_summary"]["itc_claimable_now_paise"]
               if close["tax_summary"] else None)
    k1, k2, k3 = st.columns(3)
    k1.metric("Cash in bank", inr_whole(cash_paise),
              help=f"{inr(cash_paise)} · close date {close['close_date']} · "
                   f"{close['cash']['statement_rows']} statement rows")
    k2.metric(f"Min balance ({min_bal['date']})", inr_whole(min_bal["paise"]),
              help=f"{inr(min_bal['paise'])} · lowest point of the 14-day "
                   "forecast path")
    k3.metric("ITC claimable now",
              inr_whole(itc_now) if itc_now is not None else "n/a",
              help=inr(itc_now) if itc_now is not None else
                   "no tax data in this world")
    k4, k5 = st.columns(2)
    k4.metric("Exception queue",
              f"{sev['S1']} S1 · {sev['S2']} S2 · {sev['S3']} S3",
              help="S1 act today · S2 chase externally · S3 review")
    k5.metric("₹ at risk in queue", inr_whole(at_risk), help=inr(at_risk))

    tax_v = "n/a" if panel["tax"] is None else panel["tax"]
    ok = (panel["leg_a"] == 0 and panel["forecast"] == 0
          and (panel["tax"] in (0, None)))
    (st.success if ok else st.error)(
        f"Independent verifiers on this exact output — leg A: "
        f"{panel['leg_a']} · tax: {tax_v} · forecast: {panel['forecast']} "
        "violations. One pass: the full-world reconciliation ran once and "
        "was injected into the tax and forecast surfaces.")
    for w in close["warnings"]:
        st.caption(f"⚠️ {w}")

    fcol, scol, wcol = st.columns(3)
    sev_pick = fcol.multiselect("Severity", ["S1", "S2", "S3"],
                                default=["S1", "S2", "S3"])
    src_pick = scol.multiselect(
        "Source", sorted(close["counts"]["by_source"]),
        default=sorted(close["counts"]["by_source"]))
    wf_pick = wcol.multiselect(
        "Workflow", list(QS.WORKFLOWS),
        default=[w for w in QS.WORKFLOWS if w != "resolved"])
    shown = [i for i in queue
             if f"S{i['severity']}" in sev_pick and i["source"] in src_pick
             and i["workflow"] in wf_pick]

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


# --- Forecast -----------------------------------------------------------------

@st.cache_data(show_spinner="Forecasting…")
def run_forecast(data_dir: str, stamp: float, cutoff_iso: str, horizon: int,
                 threshold_paise: int) -> dict:
    from datetime import date as _date
    from forecast.backtest import actuals_for
    from forecast.forecaster import forecast as _forecast
    from forecast.slicing import build_input, load_world

    fworld = load_world(data_dir)
    cutoff = _date.fromisoformat(cutoff_iso)
    inp = build_input(fworld, cutoff)
    result = _forecast(inp, horizon, threshold_paise=threshold_paise)
    actual = actuals_for(fworld, cutoff, horizon)
    return {"result": result.to_dict(), "actual": actual,
            "history_days": inp.history_days}


with tab_forecast:
    from datetime import date as _date, timedelta as _timedelta

    if _has("manifest"):
        manifest_path = os.path.join(data_dir, "golden_manifest.json")
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        world_days = manifest["period"]["days"]
        world_start = _date.fromisoformat(manifest["period"]["start"])

        if world_days < 120:
            st.info("This world is too short for a meaningful forecast backdrop. "
                    "Generate a longer one: "
                    "`python -m recon.generate --seed 42 --days 180` "
                    "and pick `42d180` in the sidebar.")
        else:
            horizon = 14
            c1, c2 = st.columns(2)
            cutoff_idx = c1.slider(
                "Forecast from day (cutoff)", min_value=98,
                max_value=world_days - horizon - 1, value=min(120, world_days - 15),
                help="Everything up to this day is history; the forecast covers "
                     "the next 14 days. Actual future data is shown only as the "
                     "grey truth line - the forecaster cannot see it.")
            cutoff = world_start + _timedelta(days=cutoff_idx)
            threshold_l = c2.number_input(
                "Low-cash alert threshold (Rs lakh)", min_value=1.0, max_value=100.0,
                value=10.0, step=0.5)
            threshold_paise = int(threshold_l * 10_000_000)

            fc = run_forecast(data_dir, _dir_stamp(data_dir), cutoff.isoformat(),
                              horizon, threshold_paise)
            result, actual = fc["result"], fc["actual"]

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Opening balance", inr(result["opening_balance_paise"]))
            m2.metric(f"Forecast min balance ({result['min_balance']['date']})",
                      inr(result["min_balance"]["paise"]))
            m3.metric("Known in-flight money",
                      inr(sum(x["amount_paise"] for x in result["in_flight"])))
            m4.metric("Upcoming obligations",
                      inr(sum(o["amount_paise"] for o in result["obligations"])))

            if result["first_below_threshold"]:
                st.warning(f"⚠️ Balance is forecast to drop below "
                           f"{inr(threshold_paise)} on "
                           f"**{result['first_below_threshold']}**. Plan liquidity.")
            for w in result["warnings"]:
                st.caption(f"⚠️ {w}")

            import altair as alt
            rows = []
            for d, actual_bal in zip(result["days"], actual["balances"]):
                rows.append({
                    "date": d["date"],
                    "forecast": d["balance"] / 100,
                    "actual": actual_bal / 100,
                    "lo80": (d["lo80"] / 100) if d["lo80"] is not None else None,
                    "hi80": (d["hi80"] / 100) if d["hi80"] is not None else None,
                })
            chart_df = pd.DataFrame(rows)
            base = alt.Chart(chart_df).encode(x=alt.X("date:T", title=None))
            band = base.mark_area(opacity=0.18).encode(
                y=alt.Y("lo80:Q", title="balance (Rs)"), y2="hi80:Q")
            line_f = base.mark_line(point=True).encode(
                y="forecast:Q", color=alt.value("#1f77b4"))
            line_a = base.mark_line(strokeDash=[4, 3]).encode(
                y="actual:Q", color=alt.value("#888888"))
            thr_df = pd.DataFrame({"y": [threshold_paise / 100]})
            rule = alt.Chart(thr_df).mark_rule(strokeDash=[2, 2],
                                               color="#d62728").encode(y="y:Q")
            st.altair_chart((band + line_f + line_a + rule).properties(height=340),
                            width="stretch")
            st.caption("Blue: forecast balance path. Shaded: self-calibrated 80% "
                       "band. Dashed grey: what actually happened (held-out future "
                       "- the forecaster never saw it). Red: alert threshold.")
            # Absolute-terms accuracy, read from the committed backtest so the
            # dashboard can never drift from reports/forecast_backtest.md.
            bt_path = os.path.join(REPORTS_DIR, "forecast_backtest.json")
            if os.path.isfile(bt_path):
                with open(bt_path, encoding="utf-8") as f:
                    bt = json.load(f)
                fc = bt["strategies"].get("forecaster")
                if fc:
                    agg, tot = fc["aggregate"], fc["crossings_total"]
                    hits = sum(c["hit"] for c in tot.values())
                    misses = sum(c["miss"] for c in tot.values())
                    alarms = sum(c["false_alarm"] for c in tot.values())
                    n_origins = len(bt["seeds"]) * bt["origins_per_seed"]
                    # whole rupees, same rounding as the markdown report
                    mae_rs = inr(int(round(agg["balance_mae_paise"]["mean"] / 100))
                                 * 100).rsplit(".", 1)[0]
                    st.caption(
                        f"How good is it, in absolute terms? Backtest over "
                        f"{len(bt['seeds'])} generated worlds × "
                        f"{bt['origins_per_seed']} origins = {n_origins} held-out "
                        f"fortnights (`reports/forecast_backtest.md`): the balance "
                        f"path sits within {mae_rs} of the truth "
                        f"on average ({agg['balance_mae_pct']['mean']:.1%} of the "
                        f"opening balance); the daily line is not day-precise "
                        f"(daily-flow WAPE {agg['wape']['mean']:.1%}); cash-drop "
                        f"alerts at ₹{'/'.join(tot)} depths: {hits} of "
                        f"{hits + misses} true drops caught, {alarms} false alarms "
                        "— a small sample by nature, stated as such.")

            left, right = st.columns(2)
            with left:
                st.subheader("Upcoming obligations (detected from history)")
                if result["obligations"]:
                    odf = pd.DataFrame(result["obligations"])
                    odf["amount"] = odf["amount_paise"].map(inr)
                    st.dataframe(odf[["due_date", "key", "amount", "basis"]],
                                 width="stretch", height=260)
                else:
                    st.caption("none in the horizon")
                st.subheader("Known in-flight money")
                if result["in_flight"]:
                    idf = pd.DataFrame(result["in_flight"])
                    idf["amount"] = idf["amount_paise"].map(inr)
                    st.dataframe(idf[["expected_date", "kind", "id", "amount"]],
                                 width="stretch", height=220)
                else:
                    st.caption("nothing pending")
            with right:
                st.subheader("Needs attention")
                if result["attention"]:
                    for a in result["attention"]:
                        st.error(f"**{a['id']}** · {inr(a['amount_paise'])} · "
                                 f"expected {a['expected_date']} "
                                 f"({a['days_overdue']} days overdue) — {a['note']}")
                else:
                    st.caption("no overdue settlements")
                st.subheader("How this forecast is built")
                st.markdown(
                    "- **Known money first**: captured payments and processed "
                    "settlements are projected by settlement mechanics (T+2), "
                    "not statistics.\n"
                    "- **Detected obligations**: payroll, rent, GST, TDS and "
                    "subscriptions found by periodicity analysis of the "
                    "statement itself.\n"
                    "- **Weekday statistics** cover only the remainder — future "
                    "sales and variable spend.\n"
                    "- **The band is earned, not assumed**: the same forecaster "
                    "is re-run at six historical cutoffs and its measured errors "
                    "become the band.")


# --- Tax ----------------------------------------------------------------------

@st.cache_data(show_spinner="Matching taxes…")
def run_tax(data_dir: str, stamp: float) -> list[dict]:
    from tax.engine import reconcile_tax
    from tax.io_tax import build_tax_input
    return [d.to_dict() for d in reconcile_tax(build_tax_input(data_dir))]


with tab_tax:
    if _has("gstr2b"):
        from tax import schemas as TS

        tax_dec = run_tax(data_dir, _dir_stamp(data_dir))
        itc = [d for d in tax_dec if d["loop"] == "itc"]
        tds = [d for d in tax_dec if d["loop"] == "tds"]
        obl = [d for d in tax_dec if d["loop"] == "obligation"]

        def claim_amt(d: dict) -> int:
            sides = [x for x in (d["books_paise"], d["filed_paise"])
                     if x is not None]
            return min(sides) if sides else 0

        claimable = sum(claim_amt(d) for d in itc
                        if d["status"] in TS.CLAIMABLE_NOW)
        deferred = sum(claim_amt(d) for d in itc
                       if d["status"] == TS.ITC_DEFERRED_NEXT_PERIOD)
        at_risk = sum(-(d["discrepancy_paise"] or 0) for d in itc
                      if d["status"] == TS.ITC_MISSING_IN_2B)
        blocked = sum(d["books_paise"] or 0 for d in itc
                      if d["status"] == TS.BLOCKED_CREDIT_NO_ITC)
        tds_ok = sum(1 for d in tds if d["status"] == TS.TDS_CREDIT_MATCHED)
        tds_total = sum(1 for d in tds
                        if d["book_ids"]
                        and d["status"] != TS.TDS_DUPLICATE_26AS)
        obl_ok = sum(1 for d in obl if d["status"] == TS.PAID_ON_TIME)

        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.metric("ITC claimable now", inr(claimable),
                  help="matched + head-mismatch + the lower figure of amount "
                       "mismatches — everything GSTR-2B actually supports")
        c2.metric("Deferred to next period", inr(deferred))
        c3.metric("ITC at risk", inr(at_risk),
                  help="purchases the vendor never filed — chase them")
        c4.metric("Blocked (do not claim)", inr(blocked),
                  help="Section 17(5): food, vacation travel — filed in 2B "
                       "but never claimable")
        c5.metric("TDS credits in 26AS", f"{tds_ok}/{tds_total}")
        c6.metric("Obligations on time", f"{obl_ok}/{len(obl)}")

        problems = [d for d in itc + tds + obl
                    if d["kind"] == "exception"
                    or d["status"] in (TS.UNVERIFIABLE_PRIOR_PERIOD,)]
        if problems:
            st.warning(f"{len(problems)} tax item(s) need attention — "
                       "listed below with evidence.")

        sub_itc, sub_tds, sub_obl = st.tabs(
            ["Input GST vs GSTR-2B", "TDS vs Form 26AS",
             "Obligation compliance"])

        def tax_table(ds: list[dict]) -> pd.DataFrame:
            return pd.DataFrame([{
                "status": d["status"],
                "confidence": d["confidence"],
                "books side": ";".join(d["book_ids"]),
                "filed side": ";".join(d["filed_ids"]),
                "books ₹": inr(d["books_paise"]),
                "filed ₹": inr(d["filed_paise"]),
                "discrepancy": inr(d["discrepancy_paise"]),
                "decided by": d["pass_name"],
                "evidence": " | ".join(e["rule"] for e in d["evidence"]),
            } for d in ds])

        with sub_itc:
            statuses = sorted({d["status"] for d in itc})
            interesting = [s for s in statuses
                           if s not in (TS.ITC_MATCHED, TS.NO_ITC_APPLICABLE,
                                        TS.BLOCKED_CREDIT_NO_ITC)]
            picked = st.multiselect("Status filter", statuses,
                                    default=interesting or statuses)
            shown = [d for d in itc if d["status"] in picked]
            st.dataframe(tax_table(shown), width="stretch",
                         height=340)
            st.subheader("Inspect")
            pick = st.selectbox(
                "Decision", shown,
                format_func=lambda d: f"{d['status']} · "
                                      f"{';'.join(d['book_ids']) or '—'} ↔ "
                                      f"{';'.join(d['filed_ids']) or '—'}")
            if pick:
                st.info(pick["explanation"])
                for e in pick["evidence"]:
                    st.markdown(f"- `{e['rule']}` — {e['detail']}")
                if pick["candidates"]:
                    st.markdown("**Nearest candidates (all rejected)**")
                    for c in pick["candidates"]:
                        st.markdown(f"- `{c['id']}` · "
                                    f"{inr(c['gst_paise'])} — "
                                    f"{c['rejected_because']}")

        with sub_tds:
            st.caption("The books side of this loop is Stage 1's output: "
                       "every 1% marketplace-TDS deduction the reconciliation "
                       "engine decomposed at credit, matched against what the "
                       "deductor actually filed.")
            st.dataframe(tax_table(tds), width="stretch", height=260)

        with sub_obl:
            st.caption("GST liability = 3% of previous month's gross captured "
                       "(floored to ₹10); TDS deposit = 10% of the previous "
                       "month's actual payroll debit. Both recomputed from "
                       "first principles and compared with the statement.")
            st.dataframe(tax_table(obl), width="stretch", height=440)


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
