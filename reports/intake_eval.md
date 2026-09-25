# Statement intake at batch scale

Each file goes through the intake agent and the deterministic validator (the running-balance chain must close to the paisa) with at most 3 submit attempts. Offline reruns replay the recorded transcripts. List-price cost of the recorded calls: $0.2170 (free tier: $0 billed).

## Real statements (data/real/)

1 file(s): passed on attempt 1 / 2 / 3: 0 / 1 / 0 · failed 0 · not run 0

| file | bank | rows | result | failure reason |
|---|---|---|---|---|
| `data/real/statement_a/statement.xlsx` | unknown | 65 | attempt 2 (48 transactions) | — |

Only one real statement is available to this project today (a personal UPI account; its bank name was removed when it was anonymised, so the bank reads unknown). Add anonymised exports to `data/real/` and rerun the command above; the table fills in.

## Synthetic look-alikes (data/lookalikes/) — NOT real statements

Built by a separate agent that never saw the intake code, from public descriptions of HDFC, ICICI, SBI, Axis and Kotak export layouts. They measure robustness to layout variety, not real-world accuracy.

15 file(s): passed on attempt 1 / 2 / 3: 0 / 1 / 3 · failed 11 · not run 0. 6 of 15 layouts can be expressed by the mapping schema; 4 of those 6 passed.

The mapping schema needs separate debit and credit columns, and opening and closing balances as rows of the table. Layouts without them (a single amount column with a Dr/Cr marker; a balance given only in a summary line) cannot pass whatever the model does. Which files those are is read from each file's golden structure file, not from the results. Supporting them is a schema change, left for later.

| file | bank | rows | result | failure reason | layout the mapping cannot express | column roles vs golden |
|---|---|---|---|---|---|---|
| `data/lookalikes/axis_1.xlsx` | Axis Bank | 63 | attempt 3 (51 transactions) | — | — | all correct |
| `data/lookalikes/axis_2.xlsx` | Axis Bank | 83 | attempt 3 (73 transactions) | — | — | all correct |
| `data/lookalikes/axis_3.csv` | Axis Bank | 70 | failed | the model stopped without submitting a mapping | one amount column with a Dr/Cr marker; opening balance only in a summary line | — |
| `data/lookalikes/hdfc_1.xlsx` | HDFC Bank | 46 | attempt 3 (31 transactions) | — | — | all correct |
| `data/lookalikes/hdfc_2.xlsx` | HDFC Bank | 50 | failed | E_BALANCE_PARSE, E_DATE_PARSE, E_REVERSAL_PAIR (after 3 attempt(s)) | opening balance only in a summary line | — |
| `data/lookalikes/hdfc_3.csv` | HDFC Bank | 71 | failed | E_BALANCE_PARSE, E_REVERSAL_PAIR (after 3 attempt(s)) | closing balance only in a summary line | — |
| `data/lookalikes/icici_1.xlsx` | ICICI Bank | 86 | failed | E_REVERSAL_PAIR (after 3 attempt(s)) | — | — |
| `data/lookalikes/icici_2.xlsx` | ICICI Bank | 50 | attempt 2 (36 transactions) | — | — | all correct |
| `data/lookalikes/icici_3.csv` | ICICI Bank | 61 | failed | E_BALANCE_PARSE, E_DATE_PARSE, E_RECIPE_YIELD, E_REVERSAL_PAIR (after 3 attempt(s)) | opening balance only in a summary line | — |
| `data/lookalikes/kotak_1.xlsx` | Kotak Mahindra Bank | 60 | failed | E_COLUMN_COLLISION (after 3 attempt(s)) | one amount column with a Dr/Cr marker | — |
| `data/lookalikes/kotak_2.xlsx` | Kotak Mahindra Bank | 82 | failed | E_ROW_DOUBLE_CLASSIFIED (after 3 attempt(s)) | closing balance only in a summary line | — |
| `data/lookalikes/kotak_3.csv` | Kotak Mahindra Bank | 80 | failed | E_COLUMN_COLLISION (after 3 attempt(s)) | one amount column with a Dr/Cr marker; opening balance only in a summary line | — |
| `data/lookalikes/sbi_1.xlsx` | State Bank of India | 81 | failed | the model stopped without submitting a mapping | — | — |
| `data/lookalikes/sbi_2.xlsx` | State Bank of India | 46 | failed | E_ROW_DOUBLE_CLASSIFIED (after 3 attempt(s)) | opening balance only in a summary line; closing balance only in a summary line | — |
| `data/lookalikes/sbi_3.csv` | State Bank of India | 35 | failed | E_ROW_DOUBLE_CLASSIFIED (after 3 attempt(s)) | closing balance only in a summary line | — |
