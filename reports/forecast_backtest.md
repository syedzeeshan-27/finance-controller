# Cash Forecast Backtest Report

Seeds: [42, 43, 44, 45, 46, 47, 48, 49, 50, 51] x 7 rolling origins (cutoff days [106, 115, 124, 133, 142, 151, 160]) x 14-day horizon on 180-day worlds = 70 held-out origins.

## Headline (mean across seeds)

| strategy | WAPE (daily net) | bias | balance MAE | MAE % of opening | coverage80 | min-balance err | alerts H/M/FA (all depths) |
|---|---|---|---|---|---|---|---|
| zero_net | 100.0% | -63.4% | Rs 276,037 | 6.3% | - | Rs 106,909 | 0/21/0 |
| trailing_mean_28 | 83.3% | -6.2% | Rs 183,033 | 4.0% | - | Rs 105,498 | 0/21/0 |
| naive_weekday | 113.2% | -6.8% | Rs 244,263 | 5.3% | - | Rs 168,959 | 0/21/21 |
| forecaster | 59.1% | 1.9% | Rs 77,208 | 1.8% | 79.9% | Rs 30,041 | 17/4/2 |
| forecaster_no_pipeline | 61.1% | 2.2% | Rs 82,179 | 1.8% | 81.5% | Rs 36,301 | 17/4/3 |
| forecaster_no_recurring | 67.6% | 20.5% | Rs 163,767 | 3.5% | 75.3% | Rs 87,281 | 0/21/0 |

Mean opening balance at the origins: Rs 4,548,865 - the denominator of 'MAE % of opening'. WAPE is on the daily net flow; balance MAE and the min-balance error are on the balance path a cash planner reads.

## Cash-drop alerts - threshold sweep

An alert fires when a strategy's point path dips more than the depth below the origin's opening balance within the horizon; a true event is the same test on the held-out actuals. 70 origins x 3 depths. True events: 2L 11 · 3L 8 · 4L 2 - 21 in total (hits + misses, identical for every strategy). H/M/FA = hits / misses / false alarms.

| strategy | 2L H/M/FA | 3L H/M/FA | 4L H/M/FA | all depths H/M/FA |
|---|---|---|---|---|
| zero_net | 0/11/0 | 0/8/0 | 0/2/0 | 0/21/0 |
| trailing_mean_28 | 0/11/0 | 0/8/0 | 0/2/0 | 0/21/0 |
| naive_weekday | 0/11/10 | 0/8/7 | 0/2/4 | 0/21/21 |
| forecaster | 10/1/2 | 7/1/0 | 0/2/0 | 17/4/2 |
| forecaster_no_pipeline | 10/1/2 | 7/1/1 | 0/2/0 | 17/4/3 |
| forecaster_no_recurring | 0/11/0 | 0/8/0 | 0/2/0 | 0/21/0 |

## Which origins crossed (forecaster)

Every origin x depth that was a true event or a false alarm - the whole evidence behind the alert numbers, so the sample size is visible rather than implied. Threshold = opening - depth.

| seed | cutoff | depth | opening | true 14-day low | forecast low | outcome |
|---|---|---|---|---|---|---|
| 42 | 2025-08-30 | 2L | Rs 5,280,010 | Rs 4,955,653 | Rs 4,918,213 | hit |
| 42 | 2025-08-30 | 3L | Rs 5,280,010 | Rs 4,955,653 | Rs 4,918,213 | hit |
| 43 | 2025-07-25 | 2L | Rs 4,074,175 | Rs 3,988,796 | Rs 3,838,705 | false alarm |
| 43 | 2025-08-30 | 2L | Rs 5,035,456 | Rs 4,689,744 | Rs 4,723,171 | hit |
| 43 | 2025-08-30 | 3L | Rs 5,035,456 | Rs 4,689,744 | Rs 4,723,171 | hit |
| 44 | 2025-08-30 | 2L | Rs 5,212,651 | Rs 4,917,656 | Rs 4,935,745 | hit |
| 45 | 2025-07-25 | 2L | Rs 3,742,858 | Rs 3,634,014 | Rs 3,542,834 | false alarm |
| 45 | 2025-08-30 | 2L | Rs 4,814,018 | Rs 4,392,699 | Rs 4,484,125 | hit |
| 45 | 2025-08-30 | 3L | Rs 4,814,018 | Rs 4,392,699 | Rs 4,484,125 | hit |
| 45 | 2025-08-30 | 4L | Rs 4,814,018 | Rs 4,392,699 | Rs 4,484,125 | miss |
| 46 | 2025-08-30 | 2L | Rs 5,191,092 | Rs 4,802,861 | Rs 4,851,193 | hit |
| 46 | 2025-08-30 | 3L | Rs 5,191,092 | Rs 4,802,861 | Rs 4,851,193 | hit |
| 47 | 2025-08-30 | 2L | Rs 5,593,318 | Rs 5,234,774 | Rs 5,298,314 | hit |
| 47 | 2025-08-30 | 3L | Rs 5,593,318 | Rs 5,234,774 | Rs 5,298,314 | miss |
| 48 | 2025-08-30 | 2L | Rs 4,927,431 | Rs 4,584,080 | Rs 4,590,408 | hit |
| 48 | 2025-08-30 | 3L | Rs 4,927,431 | Rs 4,584,080 | Rs 4,590,408 | hit |
| 49 | 2025-08-30 | 2L | Rs 6,012,183 | Rs 5,596,023 | Rs 5,669,245 | hit |
| 49 | 2025-08-30 | 3L | Rs 6,012,183 | Rs 5,596,023 | Rs 5,669,245 | hit |
| 49 | 2025-08-30 | 4L | Rs 6,012,183 | Rs 5,596,023 | Rs 5,669,245 | miss |
| 50 | 2025-07-25 | 2L | Rs 4,133,652 | Rs 3,931,860 | Rs 3,948,316 | miss |
| 50 | 2025-08-30 | 2L | Rs 5,547,297 | Rs 5,239,409 | Rs 5,210,484 | hit |
| 50 | 2025-08-30 | 3L | Rs 5,547,297 | Rs 5,239,409 | Rs 5,210,484 | hit |
| 51 | 2025-08-30 | 2L | Rs 5,891,756 | Rs 5,632,064 | Rs 5,639,409 | hit |

## Balance error growth by horizon day (mean across seeds, Rs)

| h | zero_net | trailing_mean_28 | naive_weekday | forecaster | forecaster_no_pipeline | forecaster_no_recurring |
|---|---|---|---|---|---|---|
| 1 | 50,243 | 42,098 | 50,519 | 16,245 | 23,421 | 16,959 |
| 2 | 118,523 | 85,137 | 102,854 | 51,164 | 55,623 | 78,362 |
| 3 | 163,316 | 121,496 | 139,005 | 38,238 | 43,424 | 105,096 |
| 4 | 194,316 | 129,338 | 146,296 | 44,453 | 51,812 | 108,747 |
| 5 | 226,752 | 144,795 | 182,448 | 59,287 | 60,533 | 120,601 |
| 6 | 272,137 | 165,618 | 204,282 | 67,546 | 69,393 | 128,260 |
| 7 | 277,924 | 191,153 | 239,164 | 97,539 | 101,180 | 163,133 |
| 8 | 298,577 | 215,425 | 274,890 | 83,906 | 84,892 | 179,215 |
| 9 | 332,050 | 233,885 | 297,882 | 88,081 | 90,625 | 197,695 |
| 10 | 354,682 | 224,080 | 288,927 | 89,161 | 95,799 | 196,307 |
| 11 | 345,158 | 233,654 | 313,859 | 104,313 | 108,492 | 231,699 |
| 12 | 362,255 | 244,852 | 378,752 | 104,106 | 111,217 | 249,912 |
| 13 | 406,889 | 259,806 | 396,515 | 112,126 | 120,445 | 257,758 |
| 14 | 461,694 | 271,118 | 404,293 | 124,750 | 133,651 | 258,992 |

## Recurring-obligation detection quality (vs golden_obligations.csv)

| obligation | detected | period ok | anchor ok (+-2d) | amount ok (+-10%) |
|---|---|---|---|---|
| aws | 10/10 | 10/10 | 10/10 | 10/10 |
| gst | 10/10 | 10/10 | 10/10 | 6/10 |
| insurance | 10/10 | 10/10 | 10/10 | 10/10 |
| payroll | 10/10 | 10/10 | 10/10 | 10/10 |
| rent | 10/10 | 10/10 | 10/10 | 10/10 |
| tds | 10/10 | 10/10 | 10/10 | 10/10 |
| telecom | 10/10 | 10/10 | 10/10 | 10/10 |

False-positive recurring keys detected: 0

## Notes

- Ground truth = the generator's post-cutoff bank statement, minted at generation time; no forecaster can read past the cutoff (leakage test).
- Irreducible error is designed in: delayed/missing settlements, random noise debits, jittered obligations.
- Coverage80 is reported as measured; constants were frozen before the final multi-seed run.
- Read WAPE for what it is: error on the daily net-flow series, which is spiky by construction (settlement batches, the 1st-of-month obligation cluster, noise debits), so a 50-60% daily WAPE coexists with a balance path that sits within ~1-2% of the opening balance. Cash planning reads the balance path, the horizon low and its date, and the alerts - the daily line is not a promise about any one day.
- Alert evidence is a small sample by nature: only origins whose held-out fortnight really dropped by the depth count as true events. The sweep table states that count and the per-origin table lists every event - read hits and false alarms as 'on the events we had', not as a track record.
