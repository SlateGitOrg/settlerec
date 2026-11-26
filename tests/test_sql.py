from __future__ import annotations

import datetime as dt

import pytest

from settlerec.bizdays import BusinessCalendar
from settlerec.runner import parse_sql_file, run_pipeline, sql_files

FILES = sql_files()


def test_pipeline_files_are_the_expected_stages_in_order():
    assert [f.name for f in FILES] == [
        "10_staging.sql",
        "20_pos_to_batch.sql",
        "30_batch_to_deposit.sql",
        "40_deposit_to_bank.sql",
        "50_clearing.sql",
        "60_proof.sql",
        "70_fee_audit.sql",
    ]


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_every_sql_file_parses_with_sqlglot(path):
    statements = parse_sql_file(path)
    assert statements, "file holds no statements"


def test_business_day_arithmetic_matches_an_independent_python_calendar(con):
    python = BusinessCalendar()
    rows = con.execute(
        "SELECT d, bd_add(d, 1), bd_add(d, 2), bd_add(d, 3), is_bd FROM cal "
        "WHERE d BETWEEN DATE '2026-01-05' AND DATE '2026-06-30' ORDER BY d"
    ).fetchall()
    assert len(rows) == 177
    for day, one, two, three, is_bd in rows:
        assert one == python.add(day, 1)
        assert two == python.add(day, 2)
        assert three == python.add(day, 3)
        assert is_bd == python.is_business_day(day)


def test_holiday_and_weekend_are_skipped(con):
    # Friday 2026-02-13, Presidents Day on Monday 02-16: the next business day is Tuesday.
    assert con.execute("SELECT bd_add(DATE '2026-02-13', 1)").fetchone()[0] == dt.date(2026, 2, 17)
    assert con.execute("SELECT bd_add(DATE '2026-02-14', 1)").fetchone()[0] == dt.date(2026, 2, 17)
    assert con.execute("SELECT bd_between(DATE '2026-02-13', DATE '2026-02-17')").fetchone()[0] == 1
    assert con.execute("SELECT is_bd FROM cal WHERE d = DATE '2026-02-16'").fetchone()[0] is False
    assert con.execute("SELECT is_bd FROM cal WHERE d = DATE '2026-02-17'").fetchone()[0] is True


def test_runner_reports_a_missing_input_file(tmp_path):
    from settlerec.runner import InputError

    with pytest.raises(InputError, match="missing input file"):
        run_pipeline(tmp_path)


def test_runner_rejects_a_wrong_header(data_dir, tmp_path):
    from settlerec.runner import InputError

    for source in data_dir.iterdir():
        (tmp_path / source.name).write_bytes(source.read_bytes())
    bank = tmp_path / "bank.csv"
    bank.write_text(bank.read_text().replace("date,descriptor,amount", "day,descriptor,amount", 1))
    with pytest.raises(InputError, match="bank.csv: expected header"):
        run_pipeline(tmp_path)
