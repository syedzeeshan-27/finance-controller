"""Deterministic generator for synthetic Indian bank-statement look-alikes.

    python scripts/make_lookalikes.py        # writes data/lookalikes/

Written by a separate agent that never saw the intake agent's code, from the
brief in docs/provenance/lookalike_author_brief.md. The files are synthetic
look-alikes, NOT real statements; reports/intake_eval.md keeps them in their
own table.

Every cell we ever write is a plain Python str, for both .xlsx and .csv, so
that agent.rawgrid.load_grid() returns identical text regardless of format.
No wall-clock, no unseeded randomness: every RNG is random.Random(<fixed
seed string>), and every date/property is a fixed constant.
"""
from __future__ import annotations

import bisect
import csv
import datetime
import json
import os
import random
import re
import zipfile

import openpyxl

HERE = os.path.dirname(os.path.abspath(__file__))
FIXED_DT = datetime.datetime(2024, 1, 1, 0, 0, 0)

KNOWN_ROLES = ["date", "narration", "chq_ref", "value_date", "debit",
               "credit", "balance", "drcr_indicator", "amount"]

# ---------------------------------------------------------------- fake data
FAKE_FIRST = ["RAHUL", "PRIYA", "AMIT", "SUNITA", "VIKRAM", "ANITA", "SURESH",
              "KAVITA", "DEEPAK", "MEENA", "SANJAY", "POOJA", "ARJUN", "NEHA"]
FAKE_LAST = ["KUMAR", "SHARMA", "PATEL", "REDDY", "NAIR", "GUPTA", "IYER",
             "RAO", "VERMA", "JOSHI", "MENON", "DESAI", "SINGH", "PILLAI"]
FAKE_TAG = ["TEST", "DEMO", "SAMPLE", "FAKE", "MOCK", "SYNTH"]
FAKE_VPA_SUFFIX = ["@oktestbank", "@testupi", "@demobank", "@fakeybl",
                    "@sampaytm", "@mockicici"]
FAKE_CO = ["ACME", "GLOBEX", "INITECH", "UMBRELLA", "WAYNE", "STARK",
           "HOOLI", "SOYLENT", "VOID", "QUANTA"]
FAKE_CO_SUFFIX = ["SOLUTIONS", "ENTERPRISES", "TESTCORP", "INDUSTRIES", "LABS"]
FAKE_MERCHANTS = ["TEST MART", "DEMO FUEL STATION", "SAMPLE SUPERMARKET",
                   "FAKE ELECTRONICS", "MOCK PHARMACY", "SYNTH CAFE",
                   "DUMMY BOOKSTORE", "PLACEHOLDER BAKERY"]
FAKE_CITIES = ["TEST NAGAR", "DEMO PURAM", "SAMPLE CITY", "MOCKVILLE", "SYNTHTOWN"]

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def fake_name(rng):
    return f"{rng.choice(FAKE_FIRST)} {rng.choice(FAKE_TAG)} {rng.choice(FAKE_LAST)}"


def fake_vpa(rng):
    u = f"{rng.choice(FAKE_FIRST).lower()}{rng.choice(FAKE_TAG).lower()}{rng.randint(10, 99)}"
    return u + rng.choice(FAKE_VPA_SUFFIX)


def fake_company(rng):
    return f"{rng.choice(FAKE_CO)} {rng.choice(FAKE_TAG)} {rng.choice(FAKE_CO_SUFFIX)} PVT LTD"


def fake_ifsc(rng, bankcode):
    return f"{bankcode}0TEST{rng.randint(10, 99)}"


def rrn(rng):
    return str(rng.randrange(10 ** 11, 10 ** 12))


def ref6(rng):
    return str(rng.randrange(100000, 999999))


def utr(rng, bankcode):
    return f"{bankcode}{rng.randrange(10 ** 10, 10 ** 11)}"


# ------------------------------------------------------------- formatting
def indian_group(intstr):
    if len(intstr) <= 3:
        return intstr
    last3 = intstr[-3:]
    rest = intstr[:-3]
    parts = []
    while len(rest) > 2:
        parts.insert(0, rest[-2:])
        rest = rest[:-2]
    if rest:
        parts.insert(0, rest)
    return ",".join(parts) + "," + last3


def fmt_amt(paise_abs, grouping):
    rupees, cents = divmod(paise_abs, 100)
    s = str(rupees)
    if grouping:
        s = indian_group(s)
    return f"{s}.{cents:02d}"


def fmt_balance(paise, grouping, suffix):
    s = fmt_amt(abs(paise), grouping)
    return s + (" " + suffix if suffix else "")


def fmt_date(d, fmt):
    if fmt == "%d-%b-%Y":
        return f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year:04d}"
    if fmt == "%d %b %Y":
        return f"{d.day:02d} {MONTHS[d.month - 1]} {d.year:04d}"
    if fmt == "%d-%b-%y":
        return f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year % 100:02d}"
    if fmt == "%d/%m/%y":
        return f"{d.day:02d}/{d.month:02d}/{d.year % 100:02d}"
    if fmt == "%d/%m/%Y":
        return f"{d.day:02d}/{d.month:02d}/{d.year:04d}"
    if fmt == "%d-%m-%Y":
        return f"{d.day:02d}-{d.month:02d}-{d.year:04d}"
    raise ValueError(fmt)


# --------------------------------------------------------------- txn model
class Txn:
    __slots__ = ("date", "value_date", "narration", "ref", "paise")

    def __init__(self, date, value_date, narration, ref, paise):
        self.date = date
        self.value_date = value_date
        self.narration = narration
        self.ref = ref
        self.paise = paise  # signed: + credit, - debit


def spread_dates(rng, start, end, n):
    days = (end - start).days
    offs = sorted(rng.randrange(0, days + 1) for _ in range(n))
    return [start + datetime.timedelta(o) for o in offs]


def gen_category(rng, kind, bankcode):
    """Returns (narration, ref, paise_abs, is_credit)."""
    if kind == "UPI":
        is_credit = rng.random() < 0.5
        r = rrn(rng)
        return f"UPI/{r}/{fake_name(rng)}/{fake_vpa(rng)}", r, rng.randrange(10000, 500000), is_credit
    if kind == "NEFT":
        is_credit = rng.random() < 0.5
        u = utr(rng, bankcode)
        tag = "CR" if is_credit else "DR"
        narr = f"NEFT {tag}-{fake_ifsc(rng, bankcode)}-{fake_name(rng)}-{u}"
        return narr, u, rng.randrange(100000, 5000000), is_credit
    if kind == "RTGS":
        is_credit = rng.random() < 0.5
        u = utr(rng, bankcode)
        tag = "CR" if is_credit else "DR"
        narr = f"RTGS {tag}-{fake_ifsc(rng, bankcode)}-{fake_name(rng)}-{u}"
        return narr, u, rng.randrange(20000000, 100000001), is_credit
    if kind == "IMPS":
        is_credit = rng.random() < 0.5
        r = rrn(rng)
        mode = "MOB" if rng.random() < 0.5 else "P2A"
        return f"IMPS/{r}/{mode}/{fake_name(rng)}", r, rng.randrange(10000, 200000), is_credit
    if kind == "ATM":
        ref = "ATM" + str(rng.randrange(100000, 999999))
        narr = f"ATM WDL-{ref}-{rng.choice(FAKE_CITIES)}"
        return narr, ref, rng.randrange(10, 101) * 10000, False
    if kind == "POS":
        ref = ref6(rng)
        narr = f"POS/{rng.choice(FAKE_MERCHANTS)}/{ref}"
        return narr, ref, rng.randrange(5000, 800000), False
    if kind == "SALARY":
        u = utr(rng, bankcode)
        narr = f"NEFT CR-{fake_ifsc(rng, bankcode)}-{fake_company(rng)}-SALARY-{u}"
        return narr, u, rng.randrange(3000000, 15000000), True
    if kind == "RENT":
        r = rrn(rng)
        narr = f"UPI/{r}/{fake_name(rng)}/RENT PAYMENT/{fake_vpa(rng)}"
        return narr, r, rng.randrange(800000, 3500000), False
    if kind == "INTEREST":
        return "INTEREST CREDIT AS PER TARIFF", "INT", rng.randrange(5000, 50000), True
    if kind == "GST_CHARGE":
        return "A/C MAINTENANCE CHARGES INCL. GST @18%", "CHG", rng.randrange(1000, 25000), False
    if kind == "RAZORPAY":
        u = utr(rng, bankcode)
        narr = f"NEFT CR-UTIB0RZP001-RAZORPAY SOFTWARE PVT LTD-{u}"
        return narr, u, rng.randrange(1500000, 8000000), True
    raise ValueError(kind)


def make_transactions(rng, bankcode, period_start, period_end, n, include_razorpay):
    required = ["UPI", "NEFT", "IMPS", "RTGS", "ATM", "POS", "SALARY", "RENT",
                "INTEREST", "GST_CHARGE"]
    kinds = list(required)
    if include_razorpay:
        kinds += ["RAZORPAY"] * rng.randint(1, 3)
    fill_pool = ["UPI", "POS", "NEFT", "ATM", "IMPS"]
    while len(kinds) < n - 1:  # reserve 1 slot for the reversal txn
        kinds.append(rng.choice(fill_pool))
    rng.shuffle(kinds)
    dates = spread_dates(rng, period_start, period_end, len(kinds))
    txns = []
    for kind, d in zip(kinds, dates):
        narration, ref, paise_abs, is_credit = gen_category(rng, kind, bankcode)
        txns.append(Txn(d, d, narration, ref, paise_abs if is_credit else -paise_abs))
    txns.sort(key=lambda t: t.date)

    debit_idxs = [i for i, t in enumerate(txns) if t.paise < 0]
    orig = txns[debit_idxs[len(debit_idxs) // 3]]
    rev_date = min(orig.date + datetime.timedelta(days=rng.randint(1, 5)), period_end)
    rev_narr = f"REVERSAL-{orig.ref}-WRONGLY DEBITED ON {fmt_date(orig.date, '%d/%m/%Y')}"
    rev = Txn(rev_date, rev_date, rev_narr, f"RV{orig.ref}", -orig.paise)
    bisect.insort(txns, rev, key=lambda t: t.date)

    prefix = 0
    min_prefix = 0
    for t in txns:
        prefix += t.paise
        min_prefix = min(min_prefix, prefix)
    opening = rng.randint(100000, 2000000) + max(0, -min_prefix)
    balances = []
    running = opening
    for t in txns:
        running += t.paise
        balances.append(running)
    return txns, balances, opening, running


# -------------------------------------------------------------- grid build
def row_adder(grid, roles, ncols):
    def add(cells, role="noise"):
        row = list(cells) + [""] * (ncols - len(cells))
        grid.append(row)
        roles.append(role)
        return len(grid) - 1
    return add


def columns_map_from_spec(spec):
    idx = {role: i for i, (role, _label) in enumerate(spec)}
    return {r: idx.get(r) for r in KNOWN_ROLES}


def render_txn_row(spec, txn, bal_paise, date_fmt, grouping, bal_suffix="", serial=None, extra=None):
    cells = []
    for role, _label in spec:
        if extra and role in extra:
            cells.append(extra[role])
            continue
        if role == "date":
            cells.append(fmt_date(txn.date, date_fmt))
        elif role == "value_date":
            cells.append(fmt_date(txn.value_date, date_fmt))
        elif role == "narration":
            cells.append(txn.narration)
        elif role == "chq_ref":
            cells.append(txn.ref)
        elif role == "debit":
            cells.append(fmt_amt(-txn.paise, grouping) if txn.paise < 0 else "")
        elif role == "credit":
            cells.append(fmt_amt(txn.paise, grouping) if txn.paise > 0 else "")
        elif role == "amount":
            cells.append(fmt_amt(abs(txn.paise), grouping))
        elif role == "drcr_indicator":
            cells.append("Cr" if txn.paise > 0 else "Dr")
        elif role == "balance":
            cells.append(fmt_balance(bal_paise, grouping, bal_suffix))
        elif role == "sl_no":
            cells.append(str(serial) if serial is not None else "")
        else:
            cells.append("")
    return cells


def render_marker_row(spec, label, date_val, date_fmt, balance_paise, grouping, bal_suffix=""):
    cells = []
    for role, _label in spec:
        if role == "date":
            cells.append(fmt_date(date_val, date_fmt))
        elif role == "narration":
            cells.append(label)
        elif role == "balance":
            cells.append(fmt_balance(balance_paise, grouping, bal_suffix))
        else:
            cells.append("")
    return cells


# ---------------------------------------------------------------- writers
_ZIP_FIXED_DT = (2024, 1, 1, 0, 0, 0)


def _freeze_xlsx_modified(path):
    """openpyxl.save() (a) stamps docProps/core.xml's <dcterms:modified> with the
    real wall-clock time, and (b) writes every zip member with 'now' as its DOS
    entry timestamp (openpyxl/writer/excel.py; plain-string writestr() calls
    default to time.localtime()) -- no matter what we set on wb.properties
    beforehand. Rewrite both so two runs of the generator produce byte-identical
    .xlsx files."""
    with zipfile.ZipFile(path, "r") as zin:
        infos = zin.infolist()
        contents = {i.filename: zin.read(i.filename) for i in infos}
    fixed = FIXED_DT.strftime("%Y-%m-%dT%H:%M:%SZ")
    core = contents["docProps/core.xml"].decode("utf-8")
    core = re.sub(r"(<dcterms:modified[^>]*>)[^<]*(</dcterms:modified>)",
                   rf"\g<1>{fixed}\g<2>", core)
    contents["docProps/core.xml"] = core.encode("utf-8")
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for i in infos:
            zi = zipfile.ZipInfo(filename=i.filename, date_time=_ZIP_FIXED_DT)
            zi.compress_type = i.compress_type
            zi.external_attr = i.external_attr
            zi.create_system = 0      # ZipInfo stamps the host OS (0 Windows, 3 Unix)
            zout.writestr(zi, contents[i.filename])
    os.replace(tmp, path)


def write_xlsx(path, grid):
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in grid:
        ws.append(row)
    wb.properties.created = FIXED_DT
    wb.properties.modified = FIXED_DT
    wb.properties.creator = "synthetic"
    wb.properties.lastModifiedBy = "synthetic"
    wb.save(path)
    _freeze_xlsx_modified(path)


def write_csv(path, grid):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerows(grid)


def save_golden(out_dir, meta):
    stem = os.path.splitext(meta["file"])[0]
    path = os.path.join(out_dir, "golden", stem + ".json")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(meta, indent=2))
        f.write("\n")


def derive_golden(file_name, bank, layout_notes, columns, date_format, grid, roles, periods_meta):
    header_row = roles.index("header")
    repeated_header_rows = [i for i, r in enumerate(roles) if r == "repeated_header"]
    noise_rows = [i for i, r in enumerate(roles) if r == "noise"]
    periods = []
    total_txn = 0
    credits_total = 0
    debits_total = 0
    for p, pm in enumerate(periods_meta):
        opening_row = next((i for i, r in enumerate(roles) if r == f"opening:{p}"), None)
        closing_row = next((i for i, r in enumerate(roles) if r == f"closing:{p}"), None)
        txn_rows = [i for i, r in enumerate(roles) if r == f"txn:{p}"]
        periods.append({
            "opening_balance_paise": pm["opening_balance_paise"],
            "opening_row": opening_row,
            "closing_balance_paise": pm["closing_balance_paise"],
            "closing_row": closing_row,
            "transaction_rows": txn_rows,
        })
        total_txn += len(txn_rows)
        credits_total += pm["credits_paise"]
        debits_total += pm["debits_paise"]
    return {
        "file": file_name, "bank": bank, "layout_notes": layout_notes,
        "header_row": header_row, "columns": columns, "date_format": date_format,
        "periods": periods, "repeated_header_rows": repeated_header_rows,
        "noise_rows": noise_rows, "transactions": total_txn,
        "credits_paise": credits_total, "debits_paise": debits_total,
    }


def cd_split(txns):
    credits = sum(t.paise for t in txns if t.paise > 0)
    debits = sum(-t.paise for t in txns if t.paise < 0)
    return credits, debits


# ------------------------------------------------------------------- HDFC
def build_hdfc_1(out_dir):
    bankcode = "HDFC"
    spec = [("date", "Date"), ("narration", "Narration"), ("chq_ref", "Chq./Ref.No."),
            ("value_date", "Value Dt"), ("debit", "Withdrawal Amt."),
            ("credit", "Deposit Amt."), ("balance", "Closing Balance")]
    ncols = len(spec)
    grid, roles = [], []
    add = row_adder(grid, roles, ncols)
    rng = random.Random("hdfc_1")
    period_start, period_end = datetime.date(2024, 1, 1), datetime.date(2024, 1, 31)
    date_fmt = "%d/%m/%y"

    add(["HDFC BANK LTD"])
    add(["TEST BRANCH, TEST NAGAR, MUMBAI 000001"])
    add([f"IFSC: {fake_ifsc(rng, bankcode)}   MICR: 000000000"])
    add([f"Account No: XXXXXXXX{rng.randint(1000, 9999)}"])
    add([f"Account Name: {fake_name(rng)}"])
    add(["Account Type: Savings   Currency: INR"])
    add(["Statement of Account for the period 01/01/2024 to 31/01/2024"])
    add([""])
    add([lbl for _, lbl in spec], "header")

    n_txn = rng.randint(25, 80)
    txns, balances, opening, closing = make_transactions(
        rng, bankcode, period_start, period_end, n_txn, include_razorpay=False)
    add(render_marker_row(spec, "B/F BALANCE", period_start, date_fmt, opening, True), "opening:0")
    for t, bal in zip(txns, balances):
        add(render_txn_row(spec, t, bal, date_fmt, True), "txn:0")
    add(render_marker_row(spec, "CLOSING BALANCE", period_end, date_fmt, closing, True), "closing:0")

    add([""])
    add(["**Withdrawal Amt = Dr, Deposit Amt = Cr"])
    add(["This is a computer generated statement and does not require a signature."])
    gen_dt = period_end + datetime.timedelta(days=3)
    add([f"Generated on {fmt_date(gen_dt, '%d/%m/%Y')} 11:42:07"])

    credits, debits = cd_split(txns)
    meta = derive_golden(
        "hdfc_1.xlsx", "HDFC Bank",
        "classic HDFC layout, dd/mm/yy dates, Indian grouping, opening+closing rows in table",
        columns_map_from_spec(spec), date_fmt, grid, roles,
        [{"opening_balance_paise": opening, "closing_balance_paise": closing,
          "credits_paise": credits, "debits_paise": debits}])
    write_xlsx(os.path.join(out_dir, "hdfc_1.xlsx"), grid)
    save_golden(out_dir, meta)


def build_hdfc_2(out_dir):
    bankcode = "HDFC"
    spec = [("date", "Txn Date"), ("value_date", "Value Date"), ("narration", "Description"),
            ("chq_ref", "Ref No."), ("debit", "Debit"), ("credit", "Credit"), ("balance", "Balance")]
    ncols = len(spec)
    rng = random.Random("hdfc_2")
    period_start, period_end = datetime.date(2024, 2, 1), datetime.date(2024, 2, 29)
    date_fmt = "%d-%m-%Y"
    n_txn = rng.randint(25, 80)
    txns, balances, opening, closing = make_transactions(
        rng, bankcode, period_start, period_end, n_txn, include_razorpay=True)

    grid, roles = [], []
    add = row_adder(grid, roles, ncols)
    add(["HDFC BANK LTD"])
    add(["TEST BRANCH-2, DEMO COLONY, PUNE 000002"])
    add([f"IFSC: {fake_ifsc(rng, bankcode)}   Branch Code: 0002"])
    add([f"Account No: XXXXXXXX{rng.randint(1000, 9999)}   Account Name: {fake_name(rng)}"])
    add(["Statement Period: 01/02/2024 To 29/02/2024"])
    add([f"Opening Balance as on 01-02-2024: {fmt_balance(opening, True, '')}"])
    add([""])
    header_cells = [lbl for _, lbl in spec]
    add(header_cells, "header")

    mid = len(txns) // 2
    for i, (t, bal) in enumerate(zip(txns, balances)):
        if i == mid:
            add(header_cells, "repeated_header")
        add(render_txn_row(spec, t, bal, date_fmt, True), "txn:0")
    add(render_marker_row(spec, "CLOSING BALANCE", period_end, date_fmt, closing, True), "closing:0")

    add([""])
    add(["Legend: DR=Debit CR=Credit"])
    add(["Statement generated by internet banking - unsigned computer printout."])
    gen_dt = period_end + datetime.timedelta(days=2)
    add([f"Generated on {fmt_date(gen_dt, '%d-%m-%Y')} 09:15:41"])

    credits, debits = cd_split(txns)
    meta = derive_golden(
        "hdfc_2.xlsx", "HDFC Bank",
        "dd-mm-yyyy dates, opening balance only in summary block, repeated header mid-table, Razorpay credit present",
        columns_map_from_spec(spec), date_fmt, grid, roles,
        [{"opening_balance_paise": opening, "closing_balance_paise": closing,
          "credits_paise": credits, "debits_paise": debits}])
    write_xlsx(os.path.join(out_dir, "hdfc_2.xlsx"), grid)
    save_golden(out_dir, meta)


def build_hdfc_3(out_dir):
    bankcode = "HDFC"
    spec = [("sl_no", "Sl No"), ("date", "Date"), ("narration", "Narration"),
            ("chq_ref", "Chq/Ref No"), ("debit", "Withdrawal Amt"),
            ("credit", "Deposit Amt"), ("balance", "Balance")]
    ncols = len(spec)
    rng = random.Random("hdfc_3")
    period_start, period_end = datetime.date(2024, 3, 1), datetime.date(2024, 3, 31)
    date_fmt = "%d-%b-%Y"
    n_txn = rng.randint(25, 80)
    txns, balances, opening, closing = make_transactions(
        rng, bankcode, period_start, period_end, n_txn, include_razorpay=False)

    grid, roles = [], []
    add = row_adder(grid, roles, ncols)
    add(["HDFC BANK LTD - ACCOUNT STATEMENT (CSV EXPORT)"])
    add([f"Account No: XXXXXXXX{rng.randint(1000, 9999)}"])
    add([f"Account Name: {fake_name(rng)}"])
    add(["Period: 01-Mar-2024 to 31-Mar-2024"])
    add([""])
    header_cells = [lbl for _, lbl in spec]
    add(header_cells, "header")
    add(render_marker_row(spec, "OPENING BALANCE", period_start, date_fmt, opening, False), "opening:0")
    for i, (t, bal) in enumerate(zip(txns, balances), start=1):
        add(render_txn_row(spec, t, bal, date_fmt, False, serial=i), "txn:0")

    add([""])
    add([f"Closing Balance as on 31-Mar-2024: {fmt_balance(closing, False, '')}"])
    add(["This is a computer generated statement and does not require a signature."])

    credits, debits = cd_split(txns)
    meta = derive_golden(
        "hdfc_3.csv", "HDFC Bank",
        "CSV export, dd-Mon-yyyy dates, plain (no comma) amounts, serial-number column, "
        "closing balance only in footer summary",
        columns_map_from_spec(spec), date_fmt, grid, roles,
        [{"opening_balance_paise": opening, "closing_balance_paise": closing,
          "credits_paise": credits, "debits_paise": debits}])
    write_csv(os.path.join(out_dir, "hdfc_3.csv"), grid)
    save_golden(out_dir, meta)


# ------------------------------------------------------------------ ICICI
def build_icici_1(out_dir):
    bankcode = "ICIC"
    spec = [("sl_no", "Sr No."), ("value_date", "Value Date"), ("date", "Transaction Date"),
            ("chq_ref", "Cheque Number"), ("narration", "Transaction Remarks"),
            ("debit", "Withdrawal Amount (INR)"), ("credit", "Deposit Amount (INR)"),
            ("balance", "Balance (INR)")]
    ncols = len(spec)
    rng = random.Random("icici_1")
    period_start, period_end = datetime.date(2024, 4, 1), datetime.date(2024, 4, 30)
    date_fmt = "%d/%m/%Y"
    n_txn = rng.randint(25, 80)
    txns, balances, opening, closing = make_transactions(
        rng, bankcode, period_start, period_end, n_txn, include_razorpay=True)

    grid, roles = [], []
    add = row_adder(grid, roles, ncols)
    add(["ICICI BANK LIMITED"])
    add(["Registered Office: ICICI Bank Towers, Bandra Kurla Complex, Mumbai - 000051"])
    add([f"MICR: 000229011   IFSC: {fake_ifsc(rng, bankcode)}"])
    add([f"Account Number: XXXXXXXX{rng.randint(1000, 9999)}   Account Name: {fake_name(rng)}"])
    add(["Statement of Account : 01/04/2024 to 30/04/2024"])
    add([""])
    header_cells = [lbl for _, lbl in spec]
    add(header_cells, "header")
    add(render_marker_row(spec, "OPENING BALANCE", period_start, date_fmt, opening, True), "opening:0")
    for i, (t, bal) in enumerate(zip(txns, balances), start=1):
        add(render_txn_row(spec, t, bal, date_fmt, True, serial=i), "txn:0")
    add(render_marker_row(spec, "CLOSING BALANCE", period_end, date_fmt, closing, True), "closing:0")

    add([""])
    add(["*Balance (INR) is the balance as of the transaction date."])
    gen_dt = period_end + datetime.timedelta(days=1)
    add([f"This statement was generated on {fmt_date(gen_dt, '%d/%m/%Y')}."])

    credits, debits = cd_split(txns)
    meta = derive_golden(
        "icici_1.xlsx", "ICICI Bank",
        "classic ICICI layout with Sr No/Value Date/Transaction Date, dd/mm/yyyy, Indian grouping, "
        "Razorpay credit present",
        columns_map_from_spec(spec), date_fmt, grid, roles,
        [{"opening_balance_paise": opening, "closing_balance_paise": closing,
          "credits_paise": credits, "debits_paise": debits}])
    write_xlsx(os.path.join(out_dir, "icici_1.xlsx"), grid)
    save_golden(out_dir, meta)


def build_icici_2(out_dir):
    bankcode = "ICIC"
    spec = [("date", "Transaction Date"), ("value_date", "Value Date"), ("narration", "Transaction Remarks"),
            ("chq_ref", "Cheque No"), ("debit", "Withdrawal Amt (INR)"),
            ("credit", "Deposit Amt (INR)"), ("balance", "Balance (INR)")]
    ncols = len(spec)
    date_fmt = "%d/%m/%Y"

    # Split ONE file-wide 25-80 transaction budget across the two concatenated
    # periods, so the file as a whole still respects "25-80 transactions per
    # file" even though it covers two statement periods.
    n_total = random.Random("icici_2_total").randint(25, 80)
    n0 = n_total // 2
    n1 = n_total - n0

    rng0 = random.Random("icici_2_p0")
    p0_start, p0_end = datetime.date(2024, 5, 1), datetime.date(2024, 5, 31)
    txns0, bal0, open0, close0 = make_transactions(rng0, bankcode, p0_start, p0_end, n0, include_razorpay=False)

    rng1 = random.Random("icici_2_p1")
    p1_start, p1_end = datetime.date(2024, 6, 1), datetime.date(2024, 6, 30)
    txns1, bal1, open1, close1 = make_transactions(rng1, bankcode, p1_start, p1_end, n1, include_razorpay=True)

    grid, roles = [], []
    add = row_adder(grid, roles, ncols)
    rng = random.Random("icici_2")
    add(["ICICI BANK LIMITED"])
    add([f"Account Number: XXXXXXXX{rng.randint(1000, 9999)}   Account Name: {fake_name(rng)}"])
    add(["Combined Statement of Account : 01/05/2024 to 30/06/2024"])
    add([""])
    header_cells = [lbl for _, lbl in spec]
    add(header_cells, "header")

    add(render_marker_row(spec, "OPENING BALANCE", p0_start, date_fmt, open0, True), "opening:0")
    for t, bal in zip(txns0, bal0):
        add(render_txn_row(spec, t, bal, date_fmt, True), "txn:0")
    add(render_marker_row(spec, "CLOSING BALANCE", p0_end, date_fmt, close0, True), "closing:0")

    add([""])
    add(["-- Statement continues for the next period --"])
    add([""])

    add(render_marker_row(spec, "OPENING BALANCE", p1_start, date_fmt, open1, True), "opening:1")
    for t, bal in zip(txns1, bal1):
        add(render_txn_row(spec, t, bal, date_fmt, True), "txn:1")
    add(render_marker_row(spec, "CLOSING BALANCE", p1_end, date_fmt, close1, True), "closing:1")

    add([""])
    add(["This is a system generated statement covering two billing periods."])

    c0, d0 = cd_split(txns0)
    c1, d1 = cd_split(txns1)
    meta = derive_golden(
        "icici_2.xlsx", "ICICI Bank",
        "two statement periods (May+Jun 2024) concatenated in one sheet, no Sr No column, dd/mm/yyyy",
        columns_map_from_spec(spec), date_fmt, grid, roles,
        [{"opening_balance_paise": open0, "closing_balance_paise": close0,
          "credits_paise": c0, "debits_paise": d0},
         {"opening_balance_paise": open1, "closing_balance_paise": close1,
          "credits_paise": c1, "debits_paise": d1}])
    write_xlsx(os.path.join(out_dir, "icici_2.xlsx"), grid)
    save_golden(out_dir, meta)


def build_icici_3(out_dir):
    bankcode = "ICIC"
    spec = [("date", "Txn Date"), ("narration", "Description"),
            ("debit", "Debit"), ("credit", "Credit"), ("balance", "Balance")]
    ncols = len(spec)
    rng = random.Random("icici_3")
    period_start, period_end = datetime.date(2024, 7, 1), datetime.date(2024, 7, 31)
    date_fmt = "%d-%b-%Y"
    n_txn = rng.randint(25, 80)
    txns, balances, opening, closing = make_transactions(
        rng, bankcode, period_start, period_end, n_txn, include_razorpay=False)

    grid, roles = [], []
    add = row_adder(grid, roles, ncols)
    add(["ICICI Bank - Mini Statement Export"])
    add([f"Account Number: XXXXXXXX{rng.randint(1000, 9999)}"])
    add([f"Customer Name: {fake_name(rng)}"])
    add([f"Opening Balance: {fmt_amt(opening, False)}"])
    add([""])
    header_cells = [lbl for _, lbl in spec]
    add(header_cells, "header")
    for t, bal in zip(txns, balances):
        add(render_txn_row(spec, t, bal, date_fmt, False), "txn:0")
    add(render_marker_row(spec, "CLOSING BALANCE", period_end, date_fmt, closing, False), "closing:0")
    add([""])
    add(["End of statement."])

    credits, debits = cd_split(txns)
    meta = derive_golden(
        "icici_3.csv", "ICICI Bank",
        "CSV minimal export: no Sr No/Value Date/Cheque columns, dd-Mon-yyyy, opening balance only in "
        "summary line",
        columns_map_from_spec(spec), date_fmt, grid, roles,
        [{"opening_balance_paise": opening, "closing_balance_paise": closing,
          "credits_paise": credits, "debits_paise": debits}])
    write_csv(os.path.join(out_dir, "icici_3.csv"), grid)
    save_golden(out_dir, meta)


# -------------------------------------------------------------------- SBI
def build_sbi_1(out_dir):
    bankcode = "SBIN"
    spec = [("date", "Txn Date"), ("value_date", "Value Date"), ("narration", "Description"),
            ("chq_ref", "Ref No./Cheque No."), ("debit", "Debit"), ("credit", "Credit"),
            ("balance", "Balance")]
    ncols = len(spec)
    rng = random.Random("sbi_1")
    period_start, period_end = datetime.date(2024, 8, 1), datetime.date(2024, 8, 31)
    date_fmt = "%d %b %Y"
    n_txn = rng.randint(25, 80)
    txns, balances, opening, closing = make_transactions(
        rng, bankcode, period_start, period_end, n_txn, include_razorpay=False)

    grid, roles = [], []
    add = row_adder(grid, roles, ncols)
    add(["STATE BANK OF INDIA"])
    add(["TEST BRANCH, DEMO ROAD, DELHI 000003"])
    add([f"IFSC : {fake_ifsc(rng, bankcode)}   Branch Code : 00003"])
    add([f"CIF No. : {rng.randrange(10 ** 8, 10 ** 9)}   Account No : XXXXXXXX{rng.randint(1000, 9999)}"])
    add([f"Account Name : {fake_name(rng)}"])
    add(["Date : 01 Aug 2024 To 31 Aug 2024"])
    add([""])
    header_cells = [lbl for _, lbl in spec]
    add(header_cells, "header")
    add(render_marker_row(spec, "B/F", period_start, date_fmt, opening, True, "Cr"), "opening:0")
    for t, bal in zip(txns, balances):
        add(render_txn_row(spec, t, bal, date_fmt, True, "Cr"), "txn:0")
    add(render_marker_row(spec, "C/F", period_end, date_fmt, closing, True, "Cr"), "closing:0")

    add([""])
    add(["Cr : Credit   Dr : Debit"])
    add(["This is a computer generated statement and does not require signature/seal."])
    gen_dt = period_end + datetime.timedelta(days=2)
    add([f"Generated On : {fmt_date(gen_dt, '%d %b %Y')}"])

    credits, debits = cd_split(txns)
    meta = derive_golden(
        "sbi_1.xlsx", "State Bank of India",
        "classic SBI layout, dd Mon yyyy dates, Indian grouping, balances suffixed Cr, opening+closing in "
        "table",
        columns_map_from_spec(spec), date_fmt, grid, roles,
        [{"opening_balance_paise": opening, "closing_balance_paise": closing,
          "credits_paise": credits, "debits_paise": debits}])
    write_xlsx(os.path.join(out_dir, "sbi_1.xlsx"), grid)
    save_golden(out_dir, meta)


def build_sbi_2(out_dir):
    bankcode = "SBIN"
    spec = [("date", "Transaction Date"), ("chq_ref", "Cheque No"), ("narration", "Description"),
            ("value_date", "Value Date"), ("debit", "Debit"), ("credit", "Credit"), ("balance", "Balance")]
    ncols = len(spec)
    rng = random.Random("sbi_2")
    period_start, period_end = datetime.date(2024, 9, 1), datetime.date(2024, 9, 30)
    date_fmt = "%d/%m/%y"
    n_txn = rng.randint(25, 80)
    txns, balances, opening, closing = make_transactions(
        rng, bankcode, period_start, period_end, n_txn, include_razorpay=True)

    grid, roles = [], []
    add = row_adder(grid, roles, ncols)
    add(["STATE BANK OF INDIA"])
    add([f"Account No : XXXXXXXX{rng.randint(1000, 9999)}   Account Name : {fake_name(rng)}"])
    add(["Statement Period : 01/09/24 To 30/09/24"])
    add([f"Opening Balance : {fmt_balance(opening, True, 'Cr')}"])
    add([""])
    header_cells = [lbl for _, lbl in spec]
    add(header_cells, "header")
    for t, bal in zip(txns, balances):
        add(render_txn_row(spec, t, bal, date_fmt, True, "Cr"), "txn:0")
    add([""])
    add([f"Closing Balance : {fmt_balance(closing, True, 'Cr')}"])
    add(["Cr : Credit   Dr : Debit. Contains a Razorpay settlement credit."])

    credits, debits = cd_split(txns)
    meta = derive_golden(
        "sbi_2.xlsx", "State Bank of India",
        "dd/mm/yy dates, opening and closing balance only in summary lines (not in table), Razorpay credit "
        "present",
        columns_map_from_spec(spec), date_fmt, grid, roles,
        [{"opening_balance_paise": opening, "closing_balance_paise": closing,
          "credits_paise": credits, "debits_paise": debits}])
    write_xlsx(os.path.join(out_dir, "sbi_2.xlsx"), grid)
    save_golden(out_dir, meta)


def build_sbi_3(out_dir):
    bankcode = "SBIN"
    spec = [("sl_no", "Sl.No"), ("date", "Txn Date"), ("narration", "Narration"),
            ("debit", "Withdrawal"), ("credit", "Deposit"), ("balance", "Balance")]
    ncols = len(spec)
    rng = random.Random("sbi_3")
    period_start, period_end = datetime.date(2024, 10, 1), datetime.date(2024, 10, 31)
    date_fmt = "%d-%m-%Y"
    n_txn = rng.randint(25, 80)
    txns, balances, opening, closing = make_transactions(
        rng, bankcode, period_start, period_end, n_txn, include_razorpay=False)

    grid, roles = [], []
    add = row_adder(grid, roles, ncols)
    add(["STATE BANK OF INDIA - CSV STATEMENT"])
    add([f"Account No : XXXXXXXX{rng.randint(1000, 9999)}"])
    add([f"Account Name : {fake_name(rng)}"])
    add(["Period : 01-10-2024 to 31-10-2024"])
    add([""])
    header_cells = [lbl for _, lbl in spec]
    add(header_cells, "header")
    add(render_marker_row(spec, "OPENING BALANCE", period_start, date_fmt, opening, False), "opening:0")
    for i, (t, bal) in enumerate(zip(txns, balances), start=1):
        add(render_txn_row(spec, t, bal, date_fmt, False, serial=i), "txn:0")
    add([""])
    add([f"Closing Balance as on 31-10-2024 : {fmt_amt(closing, False)}"])

    credits, debits = cd_split(txns)
    meta = derive_golden(
        "sbi_3.csv", "State Bank of India",
        "CSV export, dd-mm-yyyy dates, plain amounts, no Cr/Dr suffix, serial numbers, closing balance only "
        "in footer summary",
        columns_map_from_spec(spec), date_fmt, grid, roles,
        [{"opening_balance_paise": opening, "closing_balance_paise": closing,
          "credits_paise": credits, "debits_paise": debits}])
    write_csv(os.path.join(out_dir, "sbi_3.csv"), grid)
    save_golden(out_dir, meta)


# ------------------------------------------------------------------- Axis
def build_axis_1(out_dir):
    bankcode = "UTIB"
    spec = [("date", "Tran Date"), ("chq_ref", "Chq No"), ("narration", "Particulars"),
            ("debit", "Debit"), ("credit", "Credit"), ("balance", "Balance"), ("branch", "Init. Br")]
    ncols = len(spec)
    rng = random.Random("axis_1")
    period_start, period_end = datetime.date(2024, 11, 1), datetime.date(2024, 11, 30)
    date_fmt = "%d-%b-%Y"
    n_txn = rng.randint(25, 80)
    txns, balances, opening, closing = make_transactions(
        rng, bankcode, period_start, period_end, n_txn, include_razorpay=True)
    branch = str(rng.randint(1, 999)).zfill(4)

    grid, roles = [], []
    add = row_adder(grid, roles, ncols)
    add(["AXIS BANK LIMITED"])
    add(["TEST BRANCH, DEMO SECTOR, BENGALURU 000004"])
    add([f"IFSC: {fake_ifsc(rng, bankcode)}   Branch: {branch}"])
    add([f"Account No: XXXXXXXX{rng.randint(1000, 9999)}   Customer Name: {fake_name(rng)}"])
    add(["Statement From 01-Nov-2024 To 30-Nov-2024"])
    add([""])
    header_cells = [lbl for _, lbl in spec]
    add(header_cells, "header")
    add(render_marker_row(spec, "OPENING BALANCE", period_start, date_fmt, opening, True), "opening:0")
    for t, bal in zip(txns, balances):
        add(render_txn_row(spec, t, bal, date_fmt, True, extra={"branch": branch}), "txn:0")
    add(render_marker_row(spec, "CLOSING BALANCE", period_end, date_fmt, closing, True), "closing:0")

    add([""])
    add(["Init. Br. = Initiating Branch Code"])
    add(["This is a computer generated statement and does not require a signature."])

    credits, debits = cd_split(txns)
    meta = derive_golden(
        "axis_1.xlsx", "Axis Bank",
        "classic Axis layout with Init. Br. branch-code column, dd-Mon-yyyy, Indian grouping, Razorpay "
        "credit present",
        columns_map_from_spec(spec), date_fmt, grid, roles,
        [{"opening_balance_paise": opening, "closing_balance_paise": closing,
          "credits_paise": credits, "debits_paise": debits}])
    write_xlsx(os.path.join(out_dir, "axis_1.xlsx"), grid)
    save_golden(out_dir, meta)


def build_axis_2(out_dir):
    bankcode = "UTIB"
    spec = [("date", "Tran Date"), ("narration", "Particulars"), ("chq_ref", "Chq No"),
            ("debit", "Debit"), ("credit", "Credit"), ("balance", "Balance")]
    ncols = len(spec)
    rng = random.Random("axis_2")
    period_start, period_end = datetime.date(2024, 12, 1), datetime.date(2024, 12, 31)
    date_fmt = "%d/%m/%Y"
    n_txn = rng.randint(25, 80)
    txns, balances, opening, closing = make_transactions(
        rng, bankcode, period_start, period_end, n_txn, include_razorpay=False)

    grid, roles = [], []
    add = row_adder(grid, roles, ncols)
    add(["AXIS BANK LIMITED"])
    add([f"Account No: XXXXXXXX{rng.randint(1000, 9999)}   Customer Name: {fake_name(rng)}"])
    add(["Statement From 01/12/2024 To 31/12/2024"])
    add([""])
    header_cells = [lbl for _, lbl in spec]
    add(header_cells, "header")
    add(render_marker_row(spec, "OPENING BALANCE", period_start, date_fmt, opening, True), "opening:0")
    mid = len(txns) // 2
    for i, (t, bal) in enumerate(zip(txns, balances)):
        if i == mid:
            add(header_cells, "repeated_header")
        add(render_txn_row(spec, t, bal, date_fmt, True), "txn:0")
    add(render_marker_row(spec, "CLOSING BALANCE", period_end, date_fmt, closing, True), "closing:0")

    add([""])
    add(["*** Page break: header repeated above (PDF to Excel conversion artifact) ***"])

    credits, debits = cd_split(txns)
    meta = derive_golden(
        "axis_2.xlsx", "Axis Bank",
        "no branch-code column, dd/mm/yyyy, repeated header row mid-table from a PDF page-break conversion",
        columns_map_from_spec(spec), date_fmt, grid, roles,
        [{"opening_balance_paise": opening, "closing_balance_paise": closing,
          "credits_paise": credits, "debits_paise": debits}])
    write_xlsx(os.path.join(out_dir, "axis_2.xlsx"), grid)
    save_golden(out_dir, meta)


def build_axis_3(out_dir):
    bankcode = "UTIB"
    spec = [("date", "Date"), ("narration", "Particulars"), ("amount", "Amount"),
            ("drcr_indicator", "Dr/Cr"), ("balance", "Balance")]
    ncols = len(spec)
    rng = random.Random("axis_3")
    period_start, period_end = datetime.date(2025, 1, 1), datetime.date(2025, 1, 31)
    date_fmt = "%d/%m/%y"
    n_txn = rng.randint(25, 80)
    txns, balances, opening, closing = make_transactions(
        rng, bankcode, period_start, period_end, n_txn, include_razorpay=False)

    grid, roles = [], []
    add = row_adder(grid, roles, ncols)
    add(["AXIS BANK - QUICK STATEMENT EXPORT"])
    add([f"Account No: XXXXXXXX{rng.randint(1000, 9999)}"])
    add([f"Customer Name: {fake_name(rng)}"])
    add([f"Opening Balance: {fmt_amt(opening, False)}"])
    add([""])
    header_cells = [lbl for _, lbl in spec]
    add(header_cells, "header")
    for t, bal in zip(txns, balances):
        add(render_txn_row(spec, t, bal, date_fmt, False), "txn:0")
    add(render_marker_row(spec, "CLOSING BALANCE", period_end, date_fmt, closing, False), "closing:0")
    add([""])
    add(["Dr/Cr: single Amount column with Debit/Credit indicator."])

    credits, debits = cd_split(txns)
    meta = derive_golden(
        "axis_3.csv", "Axis Bank",
        "CSV export, single Amount + Dr/Cr indicator column instead of split Debit/Credit, dd/mm/yy, "
        "opening balance only in summary line",
        columns_map_from_spec(spec), date_fmt, grid, roles,
        [{"opening_balance_paise": opening, "closing_balance_paise": closing,
          "credits_paise": credits, "debits_paise": debits}])
    write_csv(os.path.join(out_dir, "axis_3.csv"), grid)
    save_golden(out_dir, meta)


# ------------------------------------------------------------------ Kotak
def build_kotak_1(out_dir):
    bankcode = "KKBK"
    spec = [("sl_no", "Sl. No."), ("date", "Date"), ("narration", "Narration"),
            ("chq_ref", "Chq/Ref No"), ("amount", "Amount"), ("drcr_indicator", "Dr / Cr"),
            ("balance", "Balance")]
    ncols = len(spec)
    rng = random.Random("kotak_1")
    period_start, period_end = datetime.date(2025, 2, 1), datetime.date(2025, 2, 28)
    date_fmt = "%d/%m/%Y"
    n_txn = rng.randint(25, 80)
    txns, balances, opening, closing = make_transactions(
        rng, bankcode, period_start, period_end, n_txn, include_razorpay=False)

    grid, roles = [], []
    add = row_adder(grid, roles, ncols)
    add(["KOTAK MAHINDRA BANK LIMITED"])
    add(["TEST BRANCH, DEMO LAYOUT, CHENNAI 000005"])
    add([f"IFSC: {fake_ifsc(rng, bankcode)}   MICR: 000006000"])
    add([f"Account No: XXXXXXXX{rng.randint(1000, 9999)}   Account Name: {fake_name(rng)}"])
    add(["Statement of Account for 01/02/2025 to 28/02/2025"])
    add([""])
    header_cells = [lbl for _, lbl in spec]
    add(header_cells, "header")
    add(render_marker_row(spec, "OPENING BALANCE", period_start, date_fmt, opening, True), "opening:0")
    for i, (t, bal) in enumerate(zip(txns, balances), start=1):
        add(render_txn_row(spec, t, bal, date_fmt, True, serial=i), "txn:0")
    add(render_marker_row(spec, "CLOSING BALANCE", period_end, date_fmt, closing, True), "closing:0")

    add([""])
    add(["Dr = Debit, Cr = Credit"])
    add(["This is a computer generated statement and does not require a signature."])

    credits, debits = cd_split(txns)
    meta = derive_golden(
        "kotak_1.xlsx", "Kotak Mahindra Bank",
        "classic Kotak layout, single Amount + Dr/Cr column, dd/mm/yyyy, Indian grouping, serial numbers",
        columns_map_from_spec(spec), date_fmt, grid, roles,
        [{"opening_balance_paise": opening, "closing_balance_paise": closing,
          "credits_paise": credits, "debits_paise": debits}])
    write_xlsx(os.path.join(out_dir, "kotak_1.xlsx"), grid)
    save_golden(out_dir, meta)


def build_kotak_2(out_dir):
    bankcode = "KKBK"
    spec = [("date", "Date"), ("narration", "Description"), ("chq_ref", "Chq/Ref No."),
            ("debit", "Withdrawal"), ("credit", "Deposit"), ("balance", "Balance")]
    ncols = len(spec)
    rng = random.Random("kotak_2")
    period_start, period_end = datetime.date(2025, 3, 1), datetime.date(2025, 3, 31)
    date_fmt = "%d-%b-%y"
    n_txn = rng.randint(25, 80)
    txns, balances, opening, closing = make_transactions(
        rng, bankcode, period_start, period_end, n_txn, include_razorpay=True)

    grid, roles = [], []
    add = row_adder(grid, roles, ncols)
    add(["KOTAK MAHINDRA BANK LIMITED"])
    add([f"Account No: XXXXXXXX{rng.randint(1000, 9999)}   Account Name: {fake_name(rng)}"])
    add(["Statement of Account for 01-Mar-25 to 31-Mar-25"])
    add([""])
    header_cells = [lbl for _, lbl in spec]
    add(header_cells, "header")
    add(render_marker_row(spec, "OPENING BALANCE", period_start, date_fmt, opening, True), "opening:0")
    for t, bal in zip(txns, balances):
        add(render_txn_row(spec, t, bal, date_fmt, True), "txn:0")

    add([""])
    add([f"Closing Balance as on 31-Mar-25: {fmt_amt(closing, True)}"])
    add(["Contains a Razorpay settlement credit (NEFT)."])

    credits, debits = cd_split(txns)
    meta = derive_golden(
        "kotak_2.xlsx", "Kotak Mahindra Bank",
        "split Withdrawal/Deposit columns (not single Amount), dd-Mon-yy dates, closing balance only in "
        "footer summary, Razorpay credit present",
        columns_map_from_spec(spec), date_fmt, grid, roles,
        [{"opening_balance_paise": opening, "closing_balance_paise": closing,
          "credits_paise": credits, "debits_paise": debits}])
    write_xlsx(os.path.join(out_dir, "kotak_2.xlsx"), grid)
    save_golden(out_dir, meta)


def build_kotak_3(out_dir):
    bankcode = "KKBK"
    spec = [("date", "Date"), ("narration", "Narration"), ("chq_ref", "Ref No"),
            ("amount", "Amount"), ("drcr_indicator", "Dr/Cr"), ("balance", "Running Balance")]
    ncols = len(spec)
    rng = random.Random("kotak_3")
    period_start, period_end = datetime.date(2025, 4, 1), datetime.date(2025, 4, 30)
    date_fmt = "%d %b %Y"
    n_txn = rng.randint(25, 80)
    txns, balances, opening, closing = make_transactions(
        rng, bankcode, period_start, period_end, n_txn, include_razorpay=True)

    grid, roles = [], []
    add = row_adder(grid, roles, ncols)
    add(["KOTAK MAHINDRA BANK - CSV EXPORT"])
    add([f"Account No: XXXXXXXX{rng.randint(1000, 9999)}"])
    add([f"Account Name: {fake_name(rng)}"])
    add([f"Opening Balance: {fmt_amt(opening, False)}"])
    add([""])
    header_cells = [lbl for _, lbl in spec]
    add(header_cells, "header")
    for t, bal in zip(txns, balances):
        add(render_txn_row(spec, t, bal, date_fmt, False), "txn:0")
    add(render_marker_row(spec, "CLOSING BALANCE", period_end, date_fmt, closing, False), "closing:0")
    add([""])
    add(["End of statement. Contains a Razorpay settlement credit."])

    credits, debits = cd_split(txns)
    meta = derive_golden(
        "kotak_3.csv", "Kotak Mahindra Bank",
        "CSV export, single Amount + Dr/Cr column labelled 'Running Balance', dd Mon yyyy dates, plain "
        "amounts, opening balance only in summary line",
        columns_map_from_spec(spec), date_fmt, grid, roles,
        [{"opening_balance_paise": opening, "closing_balance_paise": closing,
          "credits_paise": credits, "debits_paise": debits}])
    write_csv(os.path.join(out_dir, "kotak_3.csv"), grid)
    save_golden(out_dir, meta)


def generate_all(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(os.path.join(out_dir, "golden"), exist_ok=True)
    build_hdfc_1(out_dir)
    build_hdfc_2(out_dir)
    build_hdfc_3(out_dir)
    build_icici_1(out_dir)
    build_icici_2(out_dir)
    build_icici_3(out_dir)
    build_sbi_1(out_dir)
    build_sbi_2(out_dir)
    build_sbi_3(out_dir)
    build_axis_1(out_dir)
    build_axis_2(out_dir)
    build_axis_3(out_dir)
    build_kotak_1(out_dir)
    build_kotak_2(out_dir)
    build_kotak_3(out_dir)
    write_readme(out_dir)


def write_readme(out_dir):
    text = (
        "# Synthetic bank-statement look-alikes\n\n"
        "These files are **synthetic look-alikes, not real statements.**\n\n"
        "They were built by `make_lookalikes.py` from public, general knowledge of how "
        "HDFC Bank, ICICI Bank, State Bank of India, Axis Bank and Kotak Mahindra Bank "
        "lay out their internet-banking statement exports (column names/order, date "
        "formats, balance conventions). No real people, accounts, VPAs or statements "
        "were used or referenced. Every name, account number, IFSC, VPA and amount is "
        "fabricated by a seeded random generator; any resemblance to a real person or "
        "account is coincidental.\n\n"
        "Each `<bank>_<n>.xlsx`/`.csv` file has a matching `golden/<stem>.json` that "
        "records its header row, column layout, balance periods and noise rows, as "
        "described in `docs/provenance/lookalike_author_brief.md`.\n\n"
        "Regenerate with `python scripts/make_lookalikes.py`.\n"
    )
    with open(os.path.join(out_dir, "README.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


if __name__ == "__main__":
    generate_all(os.path.join(os.path.dirname(HERE), "data", "lookalikes"))
