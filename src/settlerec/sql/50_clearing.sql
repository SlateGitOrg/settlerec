-- 50_clearing: the clearing account roll-forward and, for every month end, the itemised
-- in-transit list and the open exceptions.
--
-- The ledger posts a debit from the POS on the sales date and a credit equal to the gross of the
-- batches in a deposit on the date the bank shows that deposit. Looking at each POS row as of a
-- month end M, exactly one of these holds:
--   settled          bank date <= M and batch equals POS: nets to zero
--   in_transit       batch dated <= M whose bank deposit lands after M: POS debit still open
--   unsettled_batch  no batch, or a batch that never reached a deposit: POS debit open
--   missing_deposit  deposit past its bank window with no bank line: POS debits open
--   tip_adjustment / amount_mismatch
--                    bank date <= M but batch differs from POS: credit exceeds debit by the
--                    difference

CREATE OR REPLACE TABLE months AS
SELECT
    strftime(CAST(m.d AS DATE), '%Y-%m') AS month,
    CAST(m.d AS DATE) AS month_start,
    last_day(CAST(m.d AS DATE)) AS month_end
FROM (SELECT min(gl_date) AS lo, max(gl_date) AS hi FROM gl) g,
     generate_series(date_trunc('month', g.lo), date_trunc('month', g.hi), INTERVAL 1 MONTH) AS m(d);

-- Opening balance is the previous row's closing; the ledger is summed independently so a ledger
-- line outside the month ranges (or a broken chain) shows up as rollforward_diff.
CREATE OR REPLACE TABLE rollforward AS
WITH per_month AS (
    SELECT
        m.month, m.month_start, m.month_end,
        coalesce(sum(g.debit) FILTER (WHERE g.gl_date BETWEEN m.month_start AND m.month_end), 0)
            AS debits,
        coalesce(sum(g.credit) FILTER (WHERE g.gl_date BETWEEN m.month_start AND m.month_end), 0)
            AS credits,
        coalesce(sum(g.debit - g.credit) FILTER (WHERE g.gl_date < m.month_start), 0)
            AS ledger_opening,
        coalesce(sum(g.debit - g.credit) FILTER (WHERE g.gl_date <= m.month_end), 0)
            AS ledger_closing
    FROM months m
    CROSS JOIN gl g
    GROUP BY m.month, m.month_start, m.month_end
),
chained AS (
    SELECT
        *,
        CASE
            WHEN row_number() OVER (ORDER BY month_start) = 1 THEN ledger_opening
            ELSE lag(ledger_opening + debits - credits) OVER (ORDER BY month_start)
        END AS opening
    FROM per_month
)
SELECT
    month, month_start, month_end, opening, debits, credits,
    opening + debits - credits AS closing,
    ledger_closing,
    opening + debits - credits - ledger_closing AS rollforward_diff
FROM chained;

CREATE OR REPLACE TABLE row_facts AS
SELECT
    pb.store, pb.business_date, pb.processor, pb.pos_gross, pb.batch_id, pb.batch_gross,
    pb.match_class,
    bd.deposit_id,
    db.deposit_date,
    db.status AS deposit_status,
    db.bank_date,
    db.window_end
FROM pos_batch pb
LEFT JOIN batch_deposit bd ON bd.batch_id = pb.batch_id
LEFT JOIN deposit_bank db ON db.deposit_id = bd.deposit_id;

CREATE OR REPLACE TABLE clearing_items AS
WITH classified AS (
    SELECT
        m.month_end,
        f.*,
        CASE
            WHEN f.batch_id IS NULL OR f.deposit_id IS NULL THEN 'unsettled_batch'
            WHEN f.deposit_status = 'missing_deposit' THEN
                CASE WHEN m.month_end >= f.window_end THEN 'missing_deposit' ELSE 'in_transit' END
            WHEN f.bank_date <= m.month_end THEN
                CASE WHEN f.match_class = 'matched' THEN 'settled' ELSE f.match_class END
            ELSE 'in_transit'
        END AS state
    FROM months m
    JOIN row_facts f ON f.business_date <= m.month_end
)
SELECT
    *,
    CASE
        WHEN state IN ('tip_adjustment', 'amount_mismatch')
            THEN coalesce(pos_gross, 0) - batch_gross
        WHEN state = 'settled' THEN 0
        ELSE coalesce(pos_gross, 0)
    END AS gl_effect
FROM classified;

CREATE OR REPLACE TABLE in_transit AS
SELECT
    month_end,
    processor,
    store,
    business_date AS batch_date,
    batch_id,
    deposit_id,
    deposit_date,
    bank_date,
    batch_gross,
    gl_effect AS amount,
    CASE
        WHEN deposit_status = 'missing_deposit' THEN 'bank window still open'
        WHEN deposit_date > month_end THEN 'processor deposit after month end'
        ELSE 'bank line after month end'
    END AS reason
FROM clearing_items
WHERE state = 'in_transit';

-- Open exceptions as of each month end. amount is the document amount (what the person chasing
-- it would quote); gl_effect is the signed amount it contributes to the clearing balance.
CREATE OR REPLACE TABLE open_exceptions AS
WITH item_level AS (
    SELECT
        month_end,
        state AS exception_type,
        store,
        business_date AS exception_date,
        CASE WHEN state = 'unsettled_batch' THEN coalesce(pos_gross, batch_gross)
             ELSE batch_gross - coalesce(pos_gross, 0) END AS amount,
        gl_effect,
        coalesce(batch_id, 'POS ' || store || ' ' || strftime(business_date, '%Y-%m-%d'))
            AS reference,
        processor
    FROM clearing_items
    WHERE state IN ('unsettled_batch', 'tip_adjustment', 'amount_mismatch')
),
missing AS (
    SELECT
        ci.month_end,
        'missing_deposit' AS exception_type,
        CAST(NULL AS VARCHAR) AS store,
        db.deposit_date AS exception_date,
        db.net AS amount,
        sum(ci.gl_effect) AS gl_effect,
        ci.deposit_id AS reference,
        db.processor
    FROM clearing_items ci
    JOIN deposit_bank db ON db.deposit_id = ci.deposit_id
    WHERE ci.state = 'missing_deposit'
    GROUP BY ci.month_end, db.deposit_date, db.net, ci.deposit_id, db.processor
),
ambiguous AS (
    SELECT
        m.month_end, 'ambiguous' AS exception_type, CAST(NULL AS VARCHAR) AS store,
        db.deposit_date AS exception_date, db.net AS amount, CAST(0 AS DECIMAL(18, 2)) AS gl_effect,
        db.deposit_id AS reference, db.processor
    FROM months m
    JOIN deposit_bank db ON db.status = 'ambiguous' AND db.bank_date <= m.month_end
),
footing AS (
    SELECT
        m.month_end, 'deposit_variance' AS exception_type, CAST(NULL AS VARCHAR) AS store,
        dc.deposit_date AS exception_date, dc.variance AS amount,
        CAST(0 AS DECIMAL(18, 2)) AS gl_effect, dc.deposit_id AS reference, dc.processor
    FROM months m
    JOIN deposit_check dc ON dc.status = 'variance' AND dc.deposit_date <= m.month_end
),
stray_bank AS (
    SELECT
        m.month_end, 'unmatched_bank' AS exception_type, CAST(NULL AS VARCHAR) AS store,
        u.bank_date AS exception_date, u.amount, CAST(0 AS DECIMAL(18, 2)) AS gl_effect,
        'BANK-' || u.bank_id AS reference, u.processor
    FROM months m
    JOIN bank_unmatched u ON u.bank_date <= m.month_end
),
combined AS (
    SELECT * FROM item_level
    UNION ALL SELECT * FROM missing
    UNION ALL SELECT * FROM ambiguous
    UNION ALL SELECT * FROM footing
    UNION ALL SELECT * FROM stray_bank
)
SELECT
    month_end, exception_type, store, exception_date, processor, amount, gl_effect, reference,
    bd_between(exception_date, month_end) AS age_bd
FROM combined;
