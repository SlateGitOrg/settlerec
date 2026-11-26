"""The brief's acceptance tests, run on the seeded data set."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from settlerec.bizdays import BusinessCalendar
from settlerec.report import exceptions, fee_audit, months, proof, query, rollforward

MONTHS = ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]


def dec(text: str) -> Decimal:
    return Decimal(text)


def test_ledger_months_are_january_to_june(con):
    assert months(con) == MONTHS


@pytest.mark.parametrize("month", MONTHS)
def test_unexplained_residual_is_exactly_zero_at_each_month_end(con, month):
    row = proof(con, month)[0]
    assert row["unexplained"] == Decimal("0.00")
    assert row["status"] == "proved"
    assert row["gl_closing"] == row["in_transit"] + row["open_exceptions"]


def test_every_month_has_in_transit_items(con):
    assert all(p["in_transit_batches"] > 0 for p in proof(con))


def test_roll_forward_closes_and_chains(con):
    rows = [rollforward(con, m) for m in MONTHS]
    for row in rows:
        assert row["opening"] + row["debits"] - row["credits"] == row["closing"]
        assert row["rollforward_diff"] == 0
    for previous, following in zip(rows, rows[1:], strict=False):
        assert previous["closing"] == following["opening"]
    assert rows[0]["opening"] == 0


def test_s17_unsettled_batch_is_reported_exactly_and_stays_open(con, truth):
    expected = truth["s17_unsettled"]
    assert expected["store"] == "S17"
    for month in MONTHS[1:]:  # February onwards
        hits = [e for e in exceptions(con, month) if e["exception_type"] == "unsettled_batch"]
        assert len(hits) == 1, month
        assert hits[0]["store"] == expected["store"]
        assert hits[0]["exception_date"] == dt.date.fromisoformat(expected["date"])
        assert hits[0]["amount"] == dec(expected["amount"])
        assert hits[0]["gl_effect"] == dec(expected["amount"])
    assert not [e for e in exceptions(con, "2026-01") if e["exception_type"] == "unsettled_batch"]


def test_exception_age_counts_business_days_on_the_calendar(con, truth):
    hit = next(e for e in exceptions(con, "2026-06") if e["exception_type"] == "unsettled_batch")
    expected = BusinessCalendar().between(dt.date(2026, 2, 10), dt.date(2026, 6, 30))
    assert hit["age_bd"] == expected == 97


def test_missing_processor_b_deposit_is_reported_with_date_and_amount(con, truth):
    expected = truth["missing_deposit"]
    hits = [e for e in exceptions(con, "2026-04") if e["exception_type"] == "missing_deposit"]
    assert len(hits) == 1
    assert hits[0]["reference"] == expected["deposit_id"]
    assert hits[0]["exception_date"] == dt.date.fromisoformat(expected["date"])
    assert hits[0]["amount"] == dec(expected["net"])
    assert hits[0]["gl_effect"] == dec(expected["gl_effect"])
    assert hits[0]["processor"] == "B"
    assert not [e for e in exceptions(con, "2026-03") if e["exception_type"] == "missing_deposit"]
    assert (
        len([e for e in exceptions(con, "2026-06") if e["exception_type"] == "missing_deposit"])
        == 1
    )


def test_friday_batch_before_presidents_day_matches_the_tuesday_deposit(con):
    batches = query(
        con,
        "SELECT pb.batch_id, bd.deposit_id, db.deposit_date, db.status, db.bd_to_bank "
        "FROM pos_batch pb JOIN batch_deposit bd ON bd.batch_id = pb.batch_id "
        "JOIN deposit_bank db ON db.deposit_id = bd.deposit_id "
        "WHERE pb.business_date = DATE '2026-02-13' AND pb.processor = 'A'",
    )
    assert len(batches) == 35
    assert {b["deposit_id"] for b in batches} == {"DEP-A-20260217"}
    assert {b["deposit_date"] for b in batches} == {dt.date(2026, 2, 17)}
    assert {b["status"] for b in batches} == {"matched"}
    flagged = query(
        con,
        "SELECT * FROM open_exceptions WHERE exception_date IN "
        "(DATE '2026-02-13', DATE '2026-02-17') AND exception_type <> 'unsettled_batch'",
    )
    assert flagged == []


def test_chargebacks_net_of_reversals_equal_truth(con, truth):
    expected = truth["chargebacks"]
    totals = query(
        con,
        "SELECT sum(chargebacks) AS cb, sum(reversals) AS rev, sum(net_chargebacks) AS net "
        "FROM chargeback_summary",
    )[0]
    assert totals["cb"] == dec(expected["chargebacks_total"])
    assert totals["rev"] == dec(expected["reversals_total"])
    assert totals["net"] == dec(expected["net"])
    counts = query(
        con,
        "SELECT count(*) FILTER (WHERE chargebacks > 0) AS cb, "
        "count(*) FILTER (WHERE reversals > 0) AS rev FROM deposits",
    )[0]
    assert counts == {"cb": 12, "rev": 3}


def test_every_deposit_foots_in_the_seeded_data(con):
    assert query(con, "SELECT count(*) AS n FROM deposit_check WHERE status <> 'ok'")[0]["n"] == 0


def test_processor_a_overcharge_is_a_tenth_of_a_percent_of_gross(con, truth):
    audit = {(r["processor"], r["month"]): r for r in fee_audit(con)}
    total_gross = Decimal(0)
    total_over = Decimal(0)
    for month in ("2026-04", "2026-05", "2026-06"):
        row = audit[("A", month)]
        assert abs(row["overcharge"] - row["gross"] * Decimal("0.001")) <= Decimal("0.01")
        assert row["status"] == "overcharged"
        total_gross += row["gross"]
        total_over += row["overcharge"]
    assert abs(total_over - total_gross * Decimal("0.001")) <= Decimal("0.01")
    assert audit[("A", "2026-06")]["cumulative_overcharge"] == total_over
    assert abs(total_over - dec(truth["processor_a_fee_change"]["overcharge_exact"])) <= Decimal(
        "0.01"
    )
    for month in ("2026-01", "2026-02", "2026-03"):
        assert audit[("A", month)]["overcharge"] == Decimal("0.00")
        assert audit[("A", month)]["status"] == "within contract"


def test_processor_a_gross_in_the_audit_matches_the_raw_deposits(con):
    row = query(
        con,
        "SELECT sum(b.gross) AS gross FROM deposits d "
        "JOIN deposit_batches db USING (deposit_id) JOIN batches b ON b.batch_id = db.batch_id "
        "WHERE d.processor = 'A' "
        "AND d.deposit_date BETWEEN DATE '2026-04-01' AND DATE '2026-04-30'",
    )[0]
    audit = {(r["processor"], r["month"]): r for r in fee_audit(con)}
    assert audit[("A", "2026-04")]["gross"] == row["gross"]


def test_processor_b_shows_no_overcharge(con):
    rows = [r for r in fee_audit(con) if r["processor"] == "B"]
    assert len(rows) == 6
    for row in rows:
        assert row["status"] == "within contract"
        assert row["overcharge"] == Decimal("0.00")
        assert row["actual_effective_rate"] == Decimal("0.029000")
    assert rows[-1]["cumulative_overcharge"] == Decimal("0.00")


def test_tip_adjustments_are_classified_with_exact_amounts(con, truth):
    found = query(
        con,
        "SELECT store, business_date, difference, match_class FROM pos_batch "
        "WHERE match_class IN ('tip_adjustment', 'amount_mismatch') ORDER BY business_date",
    )
    assert [r["match_class"] for r in found] == ["tip_adjustment", "tip_adjustment"]
    expected = truth["tip_adjustments"]
    assert [(r["store"], r["business_date"].isoformat(), r["difference"]) for r in found] == [
        (t["store"], t["date"], dec(t["amount"])) for t in expected
    ]
    assert not [e for e in exceptions(con, "2026-06") if e["exception_type"] == "amount_mismatch"]


def test_identical_amount_deposits_match_by_nearest_date_without_ambiguity(con, truth):
    first, second = truth["identical_deposits"]["deposits"]
    rows = query(
        con,
        "SELECT deposit_id, deposit_date, bank_date, net, status, bd_to_bank FROM deposit_bank "
        "WHERE deposit_id IN (?, ?) ORDER BY deposit_date",
        [first, second],
    )
    assert [r["deposit_id"] for r in rows] == [first, second]
    assert rows[0]["net"] == rows[1]["net"] == dec(truth["identical_deposits"]["net"])
    assert (rows[1]["deposit_date"] - rows[0]["deposit_date"]).days == 1
    assert [r["status"] for r in rows] == ["matched", "matched"]
    assert [r["bank_date"] for r in rows] == [r["deposit_date"] for r in rows]
    assert [r["bd_to_bank"] for r in rows] == [0, 0]
    assert (
        query(con, "SELECT count(*) AS n FROM deposit_bank WHERE status = 'ambiguous'")[0]["n"] == 0
    )


def test_every_bank_card_line_is_used_exactly_once(con):
    assert query(con, "SELECT count(*) AS n FROM bank_unmatched")[0]["n"] == 0
    row = query(con, "SELECT count(*) AS lines, count(DISTINCT bank_id) AS used FROM assigned")[0]
    assert row["lines"] == row["used"]
    deposits = query(con, "SELECT count(*) AS n FROM deposit_bank WHERE status = 'missing_deposit'")
    assert deposits[0]["n"] == 1


def test_in_transit_at_june_end_is_batches_whose_bank_line_lands_in_july(con):
    june = query(
        con,
        "SELECT t.batch_id, t.batch_date, t.bank_date, t.amount FROM in_transit t "
        "WHERE t.month_end = DATE '2026-06-30'",
    )
    assert june
    assert all(r["batch_date"] <= dt.date(2026, 6, 30) for r in june)
    assert all(r["bank_date"] is None or r["bank_date"] > dt.date(2026, 6, 30) for r in june)
    # Cross-check against the raw bank file: the A batches of the last trading days.
    a_dates = {r["batch_date"] for r in june if r["batch_id"].startswith("A-")}
    # Tuesday 06-30 is deposited Wednesday 07-01; Monday 06-29 only if its bank line slipped a day.
    assert dt.date(2026, 6, 30) in a_dates
    assert a_dates <= {dt.date(2026, 6, 29), dt.date(2026, 6, 30)}
    b_dates = {r["batch_date"] for r in june if r["batch_id"].startswith("B-")}
    assert dt.date(2026, 6, 30) in b_dates and dt.date(2026, 6, 29) in b_dates


def test_two_runs_produce_identical_outputs(con, data_dir):
    from settlerec.runner import run_pipeline

    other = run_pipeline(data_dir)
    for table in (
        "proof",
        "rollforward",
        "open_exceptions",
        "in_transit",
        "deposit_bank",
        "fee_audit",
        "pos_batch",
    ):
        order = "ALL" if table != "x" else ""
        first = con.execute(f"SELECT * FROM {table} ORDER BY ALL").fetchall()  # noqa: S608
        second = other.execute(f"SELECT * FROM {table} ORDER BY ALL").fetchall()  # noqa: S608
        assert first == second, table
        assert order
    other.close()
