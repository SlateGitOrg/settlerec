-- 60_proof: at every month end the ledger balance must equal the itemised in-transit batches
-- plus the open exceptions. The residual is not a plug: it is what the ledger shows that the
-- bank, processor and POS records cannot explain, and it must be exactly zero.

CREATE OR REPLACE TABLE proof AS
SELECT
    r.month,
    r.month_end,
    r.closing AS gl_closing,
    coalesce(t.amount, 0) AS in_transit,
    coalesce(t.n_batches, 0) AS in_transit_batches,
    coalesce(e.gl_effect, 0) AS open_exceptions,
    coalesce(e.n_open, 0) AS open_exception_count,
    r.closing - coalesce(t.amount, 0) - coalesce(e.gl_effect, 0) AS unexplained,
    CASE
        WHEN r.closing - coalesce(t.amount, 0) - coalesce(e.gl_effect, 0) = 0 THEN 'proved'
        ELSE 'UNEXPLAINED'
    END AS status
FROM rollforward r
LEFT JOIN (
    SELECT month_end, sum(amount) AS amount, count(*) AS n_batches
    FROM in_transit GROUP BY month_end
) t ON t.month_end = r.month_end
LEFT JOIN (
    SELECT month_end, sum(gl_effect) AS gl_effect, count(*) AS n_open
    FROM open_exceptions GROUP BY month_end
) e ON e.month_end = r.month_end;
