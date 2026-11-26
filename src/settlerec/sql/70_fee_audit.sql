-- 70_fee_audit: actual processor fees against the contract, per processor and deposit month.
--
-- Contract fee = rate x batch gross + per-transaction fee x transactions, over the batches in the
-- month's deposits. Actual fees are what the processor deducted from deposits (daily processors)
-- or debited from the bank for the month (monthly processors). Only the months being closed are
-- audited: deposits after the last month end belong to a month whose fees have not been billed.

CREATE OR REPLACE TABLE fee_base AS
SELECT
    d.processor,
    strftime(d.deposit_date, '%Y-%m') AS month,
    count(DISTINCT d.deposit_id) AS deposits,
    sum(b.gross) AS gross,
    sum(coalesce(p.txn_count, 0)) AS txns,
    sum(b.gross * c.rate + coalesce(p.txn_count, 0) * c.per_txn) AS contract_fee
FROM deposits d
JOIN deposit_batches db ON db.deposit_id = d.deposit_id
JOIN batches b ON b.batch_id = db.batch_id
LEFT JOIN pos p
    ON p.store = b.store AND p.business_date = b.batch_date AND p.processor = b.processor
JOIN contracts c ON c.processor = d.processor
WHERE strftime(d.deposit_date, '%Y-%m') IN (SELECT month FROM months)
GROUP BY d.processor, strftime(d.deposit_date, '%Y-%m');

CREATE OR REPLACE TABLE fee_actual AS
SELECT
    c.processor,
    fb.month,
    CASE c.fee_mode
        WHEN 'deducted_at_deposit' THEN (
            SELECT coalesce(sum(d.fees), 0) FROM deposits d
            WHERE d.processor = c.processor AND strftime(d.deposit_date, '%Y-%m') = fb.month
        )
        ELSE coalesce((
            SELECT sum(f.amount) FROM fees_monthly f
            WHERE f.processor = c.processor AND f.fee_month = fb.month
        ), 0)
    END AS actual_fee
FROM fee_base fb
JOIN contracts c ON c.processor = fb.processor;

-- A month within one cent of the contract is rounding, not an overcharge.
CREATE OR REPLACE TABLE fee_audit AS
WITH joined AS (
    SELECT
        fb.processor,
        fb.month,
        fb.deposits,
        fb.gross,
        fb.txns,
        c.rate AS contract_rate,
        c.per_txn AS contract_per_txn,
        fb.contract_fee AS expected_fee,
        fa.actual_fee,
        round(fa.actual_fee - fb.contract_fee, 2) AS overcharge
    FROM fee_base fb
    JOIN fee_actual fa ON fa.processor = fb.processor AND fa.month = fb.month
    JOIN contracts c ON c.processor = fb.processor
)
SELECT
    processor,
    month,
    deposits,
    gross,
    txns,
    contract_rate,
    contract_per_txn,
    CAST(expected_fee / gross AS DECIMAL(9, 6)) AS contract_effective_rate,
    CAST(actual_fee / gross AS DECIMAL(9, 6)) AS actual_effective_rate,
    round(expected_fee, 2) AS expected_fee,
    actual_fee,
    overcharge,
    sum(overcharge) OVER (PARTITION BY processor ORDER BY month) AS cumulative_overcharge,
    CASE
        WHEN overcharge > 0.01 THEN 'overcharged'
        WHEN overcharge < -0.01 THEN 'undercharged'
        ELSE 'within contract'
    END AS status
FROM joined;
