-- 40_deposit_to_bank: match processor deposits to bank lines.
--
-- A deposit and a bank line are candidates when they share the processor (by descriptor) and the
-- exact net amount, and the bank date falls 0..bank_window_bd business days after the deposit
-- date. Each deposit takes its nearest bank line and each bank line its nearest deposit; pairs
-- that choose each other are fixed, and the rounds repeat on what is left until nothing changes.
--
-- matched          one nearest line, nobody else equally near
-- ambiguous        two or more lines (or deposits) tie for nearest: the pairing is a coin toss,
--                  although the cash did arrive
-- missing_deposit  no bank line in the window

CREATE OR REPLACE TABLE bank_card AS
SELECT b.bank_id, b.bank_date, b.descriptor, b.amount, c.processor
FROM bank b
JOIN contracts c ON starts_with(b.descriptor, c.bank_descriptor);

CREATE OR REPLACE TABLE match_candidates AS
SELECT
    d.deposit_id,
    d.deposit_date,
    k.bank_id,
    k.bank_date,
    bd_between(d.deposit_date, k.bank_date) AS dist
FROM deposits d
JOIN bank_card k
    ON k.processor = d.processor
   AND k.amount = d.net
   AND k.bank_date >= d.deposit_date
WHERE bd_between(d.deposit_date, k.bank_date) <= (SELECT bank_window_bd FROM settings);

CREATE OR REPLACE TABLE assigned (
    deposit_id VARCHAR, bank_id BIGINT, dist INTEGER, round INTEGER
);

-- One round of mutual-nearest matching among deposits and bank lines not yet assigned.
CREATE OR REPLACE MACRO assign_round() AS TABLE
WITH open_pairs AS (
    SELECT c.*
    FROM match_candidates c
    WHERE c.deposit_id NOT IN (SELECT deposit_id FROM assigned)
      AND c.bank_id NOT IN (SELECT bank_id FROM assigned)
),
deposit_best AS (
    SELECT * FROM open_pairs
    QUALIFY row_number() OVER (PARTITION BY deposit_id ORDER BY dist, bank_date, bank_id) = 1
),
bank_best AS (
    SELECT * FROM open_pairs
    QUALIFY row_number() OVER (PARTITION BY bank_id ORDER BY dist, deposit_date, deposit_id) = 1
)
SELECT d.deposit_id, d.bank_id, d.dist
FROM deposit_best d
JOIN bank_best b ON b.deposit_id = d.deposit_id AND b.bank_id = d.bank_id;

INSERT INTO assigned SELECT deposit_id, bank_id, dist, 1 FROM assign_round();
INSERT INTO assigned SELECT deposit_id, bank_id, dist, 2 FROM assign_round();
INSERT INTO assigned SELECT deposit_id, bank_id, dist, 3 FROM assign_round();
INSERT INTO assigned SELECT deposit_id, bank_id, dist, 4 FROM assign_round();
INSERT INTO assigned SELECT deposit_id, bank_id, dist, 5 FROM assign_round();
INSERT INTO assigned SELECT deposit_id, bank_id, dist, 6 FROM assign_round();

-- Tie detection looks at the full candidate set, not just what was left in a round.
CREATE OR REPLACE TABLE deposit_ties AS
SELECT
    c.deposit_id,
    count(*) AS lines_at_nearest
FROM match_candidates c
JOIN (SELECT deposit_id, min(dist) AS dmin FROM match_candidates GROUP BY 1) m
    ON m.deposit_id = c.deposit_id AND c.dist = m.dmin
GROUP BY c.deposit_id;

CREATE OR REPLACE TABLE bank_ties AS
SELECT
    c.bank_id,
    count(*) AS deposits_at_nearest
FROM match_candidates c
JOIN (SELECT bank_id, min(dist) AS bmin FROM match_candidates GROUP BY 1) m
    ON m.bank_id = c.bank_id AND c.dist = m.bmin
GROUP BY c.bank_id;

CREATE OR REPLACE TABLE deposit_bank AS
SELECT
    d.deposit_id,
    d.processor,
    d.deposit_date,
    d.net,
    a.bank_id,
    k.bank_date,
    a.dist AS bd_to_bank,
    CASE
        WHEN a.deposit_id IS NULL THEN 'missing_deposit'
        WHEN dt.lines_at_nearest > 1 OR bt.deposits_at_nearest > 1 THEN 'ambiguous'
        ELSE 'matched'
    END AS status,
    bd_add(d.deposit_date, (SELECT bank_window_bd FROM settings)) AS window_end
FROM deposits d
LEFT JOIN assigned a ON a.deposit_id = d.deposit_id
LEFT JOIN bank_card k ON k.bank_id = a.bank_id
LEFT JOIN deposit_ties dt ON dt.deposit_id = d.deposit_id
LEFT JOIN bank_ties bt ON bt.bank_id = a.bank_id;

CREATE OR REPLACE TABLE bank_unmatched AS
SELECT k.bank_id, k.bank_date, k.descriptor, k.amount, k.processor
FROM bank_card k
WHERE k.bank_id NOT IN (SELECT bank_id FROM assigned);
