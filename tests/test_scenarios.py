"""Hand-computed fixtures: each builds a tiny data set and checks the pipeline by arithmetic."""

from __future__ import annotations

from decimal import Decimal

from helpers import Deposit, Sale, d, week_of_february, write_dataset
from settlerec.report import exceptions, fee_audit, proof, query
from settlerec.runner import run_pipeline

D = Decimal


def run(tmp_path, sales, deposits, **extra):
    return run_pipeline(write_dataset(tmp_path / "data", sales, deposits, **extra))


def types(con, month):
    return sorted(e["exception_type"] for e in exceptions(con, month))


def test_clean_week_is_proved_with_an_itemised_in_transit_list(tmp_path):
    sales, deposits = week_of_february()
    assert deposits[0].net == D("1666.85") and deposits[1].net == D("294.15")
    con = run(tmp_path, sales, deposits)
    feb, mar = proof(con, "2026-02")[0], proof(con, "2026-03")[0]
    assert (feb["gl_closing"], feb["in_transit"], feb["open_exceptions"]) == (
        D("1700.00"),
        D("1700.00"),
        0,
    )
    assert feb["in_transit_batches"] == 3 and feb["unexplained"] == 0
    assert (mar["gl_closing"], mar["in_transit"], mar["unexplained"]) == (D(0), D(0), D(0))
    items = query(
        con, "SELECT batch_id, amount, reason FROM in_transit WHERE month_end = DATE '2026-02-28'"
    )
    assert sorted((i["batch_id"], i["amount"]) for i in items) == [
        ("A-20260227-S01", D("1000.00")),
        ("A-20260227-S02", D("500.00")),
        ("A-20260228-S01", D("200.00")),
    ]
    assert {i["reason"] for i in items} == {"processor deposit after month end"}
    assert types(con, "2026-03") == []


def test_a_tip_adjustment_is_classified_and_only_hits_the_proof_once_banked(tmp_path):
    sales, deposits = week_of_february()
    sales[0].batch = D("1060.00")  # 60.00 of tips added after close
    con = run(tmp_path, sales, deposits)
    row = query(
        con,
        "SELECT match_class, difference FROM pos_batch "
        "WHERE store = 'S01' AND business_date = DATE '2026-02-27'",
    )[0]
    assert row == {"match_class": "tip_adjustment", "difference": D("60.00")}
    # In transit at February's close at the POS amount: no exception yet.
    assert types(con, "2026-02") == []
    assert proof(con, "2026-02")[0]["unexplained"] == 0
    # Banked in March: the credit exceeds the debit by the tip.
    (tip,) = exceptions(con, "2026-03")
    assert (tip["exception_type"], tip["amount"], tip["gl_effect"]) == (
        "tip_adjustment",
        D("60.00"),
        D("-60.00"),
    )
    mar = proof(con, "2026-03")[0]
    assert (mar["gl_closing"], mar["open_exceptions"], mar["unexplained"]) == (
        D("-60.00"),
        D("-60.00"),
        0,
    )


def test_a_batch_below_pos_or_far_above_it_is_an_amount_mismatch(tmp_path):
    sales, deposits = week_of_february()
    sales[1].batch = D("490.00")  # 10.00 short
    sales[3].batch = D("700.00")  # 133% above POS: not a tip
    con = run(tmp_path, sales, deposits)
    rows = query(
        con,
        "SELECT store, difference, match_class FROM pos_batch "
        "WHERE match_class <> 'matched' ORDER BY business_date",
    )
    assert [(r["difference"], r["match_class"]) for r in rows] == [
        (D("-10.00"), "amount_mismatch"),
        (D("400.00"), "amount_mismatch"),
    ]
    mar = proof(con, "2026-03")[0]
    assert mar["unexplained"] == 0
    assert mar["open_exceptions"] == D("10.00") - D("400.00")


def test_a_tie_between_equal_deposits_is_flagged_ambiguous(tmp_path):
    friday = Sale("S01", d("2026-02-27"), D("1000.00"), 10)
    monday = Sale("S01", d("2026-03-02"), D("1000.00"), 10)
    first = Deposit("A", d("2026-03-02"), [friday], bank_day=d("2026-03-03"))
    second = Deposit("A", d("2026-03-03"), [monday], bank_day=d("2026-03-03"))
    assert first.net == second.net == D("980.50")
    con = run(tmp_path, [friday, monday], [first, second])
    rows = query(con, "SELECT deposit_id, status FROM deposit_bank ORDER BY deposit_id")
    assert [r["status"] for r in rows] == ["ambiguous", "ambiguous"]
    assert types(con, "2026-03") == ["ambiguous", "ambiguous"]
    assert all(proof(con, m)[0]["unexplained"] == 0 for m in ("2026-02", "2026-03"))


def test_the_same_equal_deposits_with_distinct_bank_dates_are_not_ambiguous(tmp_path):
    friday = Sale("S01", d("2026-02-27"), D("1000.00"), 10)
    monday = Sale("S01", d("2026-03-02"), D("1000.00"), 10)
    first = Deposit("A", d("2026-03-02"), [friday])
    second = Deposit("A", d("2026-03-03"), [monday])
    con = run(tmp_path, [friday, monday], [first, second])
    rows = query(con, "SELECT deposit_id, bank_date, status FROM deposit_bank ORDER BY deposit_id")
    assert [(r["bank_date"], r["status"]) for r in rows] == [
        (d("2026-03-02"), "matched"),
        (d("2026-03-03"), "matched"),
    ]


def test_a_deposit_with_no_bank_line_is_timing_until_its_window_closes(tmp_path):
    sales, deposits = week_of_february()
    deposits[0].bank_day = None  # the 03-02 deposit never reaches the bank
    con = run(tmp_path, sales, deposits)
    assert types(con, "2026-02") == []  # deposit date is after February's close
    assert proof(con, "2026-02")[0]["in_transit"] == D("1700.00")
    (missing,) = exceptions(con, "2026-03")
    assert missing["exception_type"] == "missing_deposit"
    assert (missing["exception_date"], missing["amount"], missing["gl_effect"]) == (
        d("2026-03-02"), D("1666.85"), D("1700.00"),
    )  # fmt: skip
    mar = proof(con, "2026-03")[0]
    assert (mar["gl_closing"], mar["unexplained"]) == (D("1700.00"), D(0))


def test_a_bank_line_beyond_the_window_is_missing_not_timing(tmp_path):
    sales, deposits = week_of_february()
    deposits[0].bank_day = d("2026-03-06")  # four business days after the deposit
    con = run(tmp_path, sales, deposits)
    assert types(con, "2026-03") == ["missing_deposit", "unmatched_bank"]
    mar = proof(con, "2026-03")[0]
    # The ledger booked the cash; the matching says it never arrived inside the window.
    assert mar["status"] == "UNEXPLAINED" and mar["unexplained"] == D("-1700.00")


def test_a_stray_ledger_entry_is_the_unexplained_residual(tmp_path):
    sales, deposits = week_of_february()
    con = run(tmp_path, sales, deposits, gl_extra=[(d("2026-03-15"), "JE-9", D("100.00"), D(0))])
    mar = proof(con, "2026-03")[0]
    assert (mar["unexplained"], mar["status"]) == (D("100.00"), "UNEXPLAINED")
    assert proof(con, "2026-02")[0]["unexplained"] == 0


def test_a_deposit_that_does_not_foot_is_a_variance_exception(tmp_path):
    sales, deposits = week_of_february()
    deposits[1].net_error = D("1.00")
    con = run(tmp_path, sales, deposits)
    (variance,) = exceptions(con, "2026-03")
    assert (variance["exception_type"], variance["amount"], variance["gl_effect"]) == (
        "deposit_variance", D("1.00"), D(0),
    )  # fmt: skip
    assert proof(con, "2026-03")[0]["unexplained"] == 0


def test_a_batch_missing_from_every_deposit_is_unsettled(tmp_path):
    sales, deposits = week_of_february()
    sales.append(Sale("S02", d("2026-03-02"), D("400.00"), 4))  # batched, never deposited
    con = run(tmp_path, sales, deposits)
    (item,) = exceptions(con, "2026-03")
    assert (item["exception_type"], item["amount"], item["reference"]) == (
        "unsettled_batch", D("400.00"), "A-20260302-S02",
    )  # fmt: skip
    assert proof(con, "2026-03")[0]["unexplained"] == 0


def test_chargebacks_and_refunds_foot_into_the_deposit(tmp_path):
    sales, deposits = week_of_february()
    sales[0].refunds = D("25.00")
    deposits[0].chargebacks = D("40.00")
    deposits[1].reversals = D("15.00")
    con = run(tmp_path, sales, deposits)
    assert query(con, "SELECT count(*) AS n FROM deposit_check WHERE status <> 'ok'")[0]["n"] == 0
    row = query(
        con, "SELECT net, expected_net FROM deposit_check WHERE deposit_id = 'DEP-A-20260302'"
    )[0]
    assert row["net"] == row["expected_net"] == D("1700.00") - D("25.00") - D("33.15") - D("40.00")
    summary = query(con, "SELECT chargebacks, reversals, net_chargebacks FROM chargeback_summary")[
        0
    ]
    assert summary == {
        "chargebacks": D("40.00"),
        "reversals": D("15.00"),
        "net_chargebacks": D("25.00"),
    }
    assert proof(con, "2026-03")[0]["unexplained"] == 0


def test_fee_audit_at_contract_terms_has_no_overcharge(tmp_path):
    sales, deposits = week_of_february()
    con = run(tmp_path, sales, deposits)
    (row,) = [r for r in fee_audit(con) if r["month"] == "2026-03"]
    # 1.85% x 2000.00 + 0.10 x 20 transactions
    assert (row["expected_fee"], row["actual_fee"], row["overcharge"]) == (
        D("39.00"),
        D("39.00"),
        D("0.00"),
    )
    assert row["status"] == "within contract"


def test_fee_audit_catches_a_rate_above_contract(tmp_path):
    sales, deposits = week_of_february()
    for deposit in deposits:
        deposit.rate = D("0.0195")
    con = run(tmp_path, sales, deposits)
    (row,) = [r for r in fee_audit(con) if r["month"] == "2026-03"]
    assert (row["actual_fee"], row["overcharge"]) == (D("41.00"), D("2.00"))  # 0.10% of 2000.00
    assert row["status"] == "overcharged"
    assert row["actual_effective_rate"] == D("0.020500")


def test_monthly_debit_fee_is_audited_against_the_contract(tmp_path):
    sale = Sale("S01", d("2026-02-27"), D("1000.00"), 10, proc="B")
    deposit = Deposit("B", d("2026-03-03"), [sale])  # settles Monday, paid a business day later
    debit = (("B", "2026-03", d("2026-04-03"), D("31.00")),)
    con = run(tmp_path, [sale], [deposit], fees_monthly=list(debit))
    (row,) = [r for r in fee_audit(con) if r["processor"] == "B"]
    assert (row["expected_fee"], row["actual_fee"], row["overcharge"]) == (
        D("29.00"),
        D("31.00"),
        D("2.00"),
    )
    assert row["status"] == "overcharged"
