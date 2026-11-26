-- 10_staging: typed staging tables, contract terms and the business-day calendar.
-- Raw files are loaded by the runner as raw_* tables; everything downstream reads these.

CREATE OR REPLACE TABLE stores AS
SELECT store, store_name, region FROM raw_stores;

CREATE OR REPLACE TABLE pos AS
SELECT store, business_date, processor, card_gross, txn_count
FROM raw_pos;

CREATE OR REPLACE TABLE batches AS
SELECT processor, batch_id, store, batch_date, gross, refunds, settle_date
FROM raw_batches;

CREATE OR REPLACE TABLE deposits AS
SELECT processor, deposit_id, deposit_date, batch_ids, fees, chargebacks, reversals, net
FROM raw_deposits;

CREATE OR REPLACE TABLE fees_monthly AS
SELECT processor, fee_month, debit_date, amount
FROM raw_fees_monthly;

-- Identical bank lines stay distinguishable through their position in the file.
CREATE OR REPLACE TABLE bank AS
SELECT
    row_number() OVER (ORDER BY bank_date, descriptor, amount, file_row) AS bank_id,
    bank_date, descriptor, amount
FROM raw_bank;

CREATE OR REPLACE TABLE gl AS
SELECT gl_date, reference, debit, credit FROM raw_gl;

CREATE OR REPLACE TABLE contracts AS
SELECT processor, name, bank_descriptor, fee_bank_descriptor, fee_mode, rate, per_txn
FROM raw_contracts;

CREATE OR REPLACE TABLE settings AS
SELECT tip_adjustment_max_pct, bank_window_bd FROM raw_settings;

-- Calendar: every date from before the first sale to well after the last bank line.
-- bd_count is the number of business days up to and including the date, so the distance
-- between two dates in business days is a subtraction and "n business days after d" is a lookup.
CREATE OR REPLACE TABLE cal AS
WITH span AS (
    SELECT
        (SELECT min(business_date) FROM pos) - INTERVAL 14 DAY AS lo,
        greatest(
            (SELECT max(bank_date) FROM bank),
            (SELECT max(deposit_date) FROM deposits),
            (SELECT max(gl_date) FROM gl),
            (SELECT max(debit_date) FROM fees_monthly),
            (SELECT max(business_date) FROM pos)
        ) + INTERVAL 21 DAY AS hi
),
days AS (
    SELECT CAST(t.d AS DATE) AS d
    FROM span, generate_series(span.lo, span.hi, INTERVAL 1 DAY) AS t(d)
),
flagged AS (
    SELECT
        days.d,
        (isodow(days.d) < 6 AND h.holiday_date IS NULL) AS is_bd
    FROM days
    LEFT JOIN raw_calendar h ON h.holiday_date = days.d
)
SELECT
    d,
    is_bd,
    CAST(sum(CASE WHEN is_bd THEN 1 ELSE 0 END) OVER (ORDER BY d) AS INTEGER) AS bd_count
FROM flagged;

CREATE OR REPLACE TABLE bdays AS
SELECT bd_count, d FROM cal WHERE is_bd;

CREATE OR REPLACE MACRO bd_count_of(dt) AS (SELECT c.bd_count FROM cal c WHERE c.d = dt);
CREATE OR REPLACE MACRO bd_add(dt, n) AS (
    SELECT min(b.d) FROM bdays b WHERE b.bd_count = bd_count_of(dt) + n
);
CREATE OR REPLACE MACRO bd_between(d_from, d_to) AS bd_count_of(d_to) - bd_count_of(d_from);
