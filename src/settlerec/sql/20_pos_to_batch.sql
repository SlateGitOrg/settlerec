-- 20_pos_to_batch: match POS card sales to processor batches on (store, date, processor).
--
-- matched          batch gross equals POS card gross
-- tip_adjustment   batch exceeds POS by no more than the contract's tip allowance: tips added
--                  after the POS closed the day
-- amount_mismatch  any other difference, including a batch with no POS row
-- unsettled_batch  a POS day with no batch at all

CREATE OR REPLACE TABLE pos_batch AS
SELECT
    coalesce(p.store, b.store) AS store,
    coalesce(p.business_date, b.batch_date) AS business_date,
    coalesce(p.processor, b.processor) AS processor,
    p.card_gross AS pos_gross,
    p.txn_count,
    b.batch_id,
    b.gross AS batch_gross,
    b.refunds,
    b.settle_date,
    coalesce(b.gross, 0) - coalesce(p.card_gross, 0) AS difference,
    CASE
        WHEN b.batch_id IS NULL THEN 'unsettled_batch'
        WHEN b.gross = coalesce(p.card_gross, 0) THEN 'matched'
        WHEN p.card_gross IS NOT NULL
             AND b.gross > p.card_gross
             AND b.gross - p.card_gross <= p.card_gross * (SELECT tip_adjustment_max_pct FROM settings)
            THEN 'tip_adjustment'
        ELSE 'amount_mismatch'
    END AS match_class
FROM pos p
FULL OUTER JOIN batches b
    ON b.store = p.store AND b.batch_date = p.business_date AND b.processor = p.processor;
