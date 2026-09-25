# Brief: synthetic Indian bank-statement look-alikes, with golden structure files

## Where you work
- Work ONLY inside this sandbox folder (the one containing this BRIEF.md). Do not read, list
  or open anything outside it.
- Python: the global `python` (3.13, has `openpyxl` and `pytest`). Set `PYTHONUTF8=1`. Give
  Python Windows-style paths (`C:/...`), never `/c/...`.

## Why this exists
A tool will be evaluated on how well it reads messy bank-statement exports (finding the
header, the columns, the transaction rows and the balance chain). The owner has no real
statements to share, so we need realistic synthetic look-alikes. They will be reported in a
separate table clearly labelled "synthetic, not real statements". You are not shown the tool
on purpose. Make the files as realistic and varied as real exports are. Do not make them easy
or hard for any particular approach.

## What to build
1. `make_lookalikes.py`: a deterministic generator (fixed seeds, no wall-clock, no randomness
   outside `random.Random(seed)`) that writes every file below. Running it twice gives
   byte-identical output.
2. Output under `lookalikes/`: about 15 statement files, 3 each for HDFC Bank, ICICI Bank,
   State Bank of India, Axis Bank and Kotak Mahindra Bank, named `<bank>_<n>.<xlsx|csv>` (e.g.
   `hdfc_1.xlsx`). Mix formats: at least 9 `.xlsx`, at least 3 `.csv`.
3. `lookalikes/golden/<file stem>.json` for every file (format below).
4. `lookalikes/README.md` stating plainly that these are synthetic look-alikes built from public
   descriptions of each bank's export layout, not real statements, with no real people or
   accounts in them.
5. `test_lookalikes.py` (pytest) proving the files and goldens are consistent (list below).

## Realism (per bank, three variants each; vary them)
Follow each bank's publicly known internet-banking export style as closely as you can from
general knowledge: column names and order, date formats (`dd/mm/yy`, `dd/mm/yyyy`,
`dd-Mon-yyyy`, `dd Mon yyyy`...), separate withdrawal/deposit columns or a single amount with
a Dr/Cr column, balances with or without a `Cr`/`Dr` suffix, Indian digit grouping
(`1,23,456.78`), serial-number columns, value-date columns, cheque/ref columns. Include the
messiness real exports have:
- banner and account-detail rows above the header (bank name, branch, IFSC, masked account
  number, customer name, statement period), blank separator rows, footer legends,
  "generated on" lines, abbreviation keys;
- an opening balance that sometimes sits in its own row inside the table and sometimes only in
  a summary block above or below it; the same for the closing balance;
- occasionally a repeated header row mid-table (page breaks from PDF-to-Excel conversion) and
  occasionally two statement periods concatenated in one sheet;
- 25-80 transactions per file: UPI (`UPI/<12-digit RRN>/...`), NEFT, IMPS, RTGS, ATM, POS/card,
  salary, rent, interest credit, bank charges with GST, a reversal of an earlier debit, and in
  some files Razorpay settlement credits (e.g. an NEFT credit from Razorpay Software Pvt Ltd
  carrying a UTR);
- only synthetic, obviously fake names, account numbers and VPAs.

Every file must have an internally correct running balance: opening + credits − debits
reproduces every row's stated balance to the paisa, within each statement period.

## How files are read (use this to index rows)
`ref/rawgrid.py` is the exact loader that will turn each file into a grid of strings (first
worksheet for `.xlsx`; the csv module for `.csv`; the coercion rules are in its docstring).
Every row index in your golden files is a 0-based index into `load_grid(path)`, and every
column index is a 0-based index into that grid's rows. Your tests must load files with this
loader.

## Golden structure file (`lookalikes/golden/<stem>.json`)
```json
{
  "file": "hdfc_1.xlsx",
  "bank": "HDFC Bank",
  "layout_notes": "one line on what makes this variant distinctive",
  "header_row": 12,
  "columns": {"date": 0, "narration": 1, "chq_ref": 2, "value_date": 3,
              "debit": 4, "credit": 5, "balance": 6, "drcr_indicator": null,
              "amount": null},
  "date_format": "%d/%m/%y",
  "periods": [
    {"opening_balance_paise": 1234567,
     "opening_row": 13,
     "closing_balance_paise": 2345678,
     "closing_row": 60,
     "transaction_rows": [14, 15, 16]}
  ],
  "repeated_header_rows": [],
  "noise_rows": [0, 1, 2],
  "transactions": 45,
  "credits_paise": 0,
  "debits_paise": 0
}
```
- `columns`: every role present in the file gets its column index; roles absent from this
  layout are `null`. Use `amount` + `drcr_indicator` for single-amount-column layouts (then
  `debit`/`credit` are `null`).
- `opening_row` / `closing_row`: the grid row holding that balance *inside the table*, or `null`
  when the balance only appears in a summary block (the `*_paise` values are always given).
- `noise_rows`: every grid row that is neither the header, a repeated header, an opening/closing
  row nor a transaction row.

## Tests (`test_lookalikes.py`)
- Running the generator twice gives identical bytes for every file.
- Every row of every grid is classified exactly once by its golden file.
- In every period the balance chain closes: opening + credits − debits equals each transaction
  row's balance and ends on the closing balance, computed with your own parsers from the grid
  cells.
- `transactions`, `credits_paise` and `debits_paise` agree with the grid.
- There are at least 15 files, 3 per bank, at least 9 `.xlsx` and 3 `.csv`, and no two files
  share a header layout.

## What to return
At most 20 lines: the files written, one line per bank describing its three variants, and the
test result. Do not paste file contents.
