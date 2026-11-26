-- 30_batch_to_deposit: every deposit must equal
--     batch gross - refunds - fees - chargebacks + reversals
-- over the batches it lists.

CREATE OR REPLACE TABLE deposit_batches AS
SELECT deposit_id, unnest(string_split(batch_ids, ';')) AS batch_id
FROM deposits;

CREATE OR REPLACE TABLE batch_deposit AS
SELECT batch_id, min(deposit_id) AS deposit_id, count(*) AS n_deposits
FROM deposit_batches
GROUP BY batch_id;

CREATE OR REPLACE TABLE deposit_check AS
WITH listed AS (
    SELECT
        db.deposit_id,
        count(*) AS n_listed,
        count(b.batch_id) AS n_known,
        coalesce(sum(b.gross), 0) AS batch_gross,
        coalesce(sum(b.refunds), 0) AS batch_refunds
    FROM deposit_batches db
    LEFT JOIN batches b ON b.batch_id = db.batch_id
    GROUP BY db.deposit_id
)
SELECT
    d.deposit_id,
    d.processor,
    d.deposit_date,
    l.n_listed,
    l.n_known,
    l.batch_gross,
    l.batch_refunds,
    d.fees,
    d.chargebacks,
    d.reversals,
    l.batch_gross - l.batch_refunds - d.fees - d.chargebacks + d.reversals AS expected_net,
    d.net,
    d.net - (l.batch_gross - l.batch_refunds - d.fees - d.chargebacks + d.reversals) AS variance,
    CASE
        WHEN d.net = l.batch_gross - l.batch_refunds - d.fees - d.chargebacks + d.reversals
             AND l.n_listed = l.n_known THEN 'ok'
        ELSE 'variance'
    END AS status
FROM deposits d
JOIN listed l ON l.deposit_id = d.deposit_id;

CREATE OR REPLACE TABLE chargeback_summary AS
SELECT
    strftime(deposit_date, '%Y-%m') AS month,
    processor,
    sum(chargebacks) AS chargebacks,
    sum(reversals) AS reversals,
    sum(chargebacks) - sum(reversals) AS net_chargebacks
FROM deposits
GROUP BY 1, 2;
