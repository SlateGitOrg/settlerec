from __future__ import annotations

from decimal import Decimal

from typer.testing import CliRunner

from helpers import d, week_of_february, write_dataset
from settlerec.cli import app

runner = CliRunner()


def test_generate_command_writes_the_input_files(tmp_path):
    result = runner.invoke(app, ["generate", "--out", str(tmp_path / "out"), "--seed", "5"])
    assert result.exit_code == 0
    assert "wrote 10 files" in result.output
    assert (tmp_path / "out" / "gl_clearing.csv").is_file()


def test_run_prints_the_proof_for_every_month_and_exits_zero(data_dir):
    result = runner.invoke(app, ["run", "--data", str(data_dir), "--month", "2026-06"])
    assert result.exit_code == 0, result.output
    assert result.output.count("proved") >= 7  # six table rows plus the chosen month's line
    assert "Roll-forward 2026-06" in result.output
    assert "unexplained 0.00  [proved]" in result.output


def test_run_exits_one_when_the_residual_is_not_zero(tmp_path):
    sales, deposits = week_of_february()
    path = write_dataset(
        tmp_path / "data",
        sales,
        deposits,
        gl_extra=[(d("2026-03-15"), "JE-9", Decimal("100.00"), 0)],
    )
    result = runner.invoke(app, ["run", "--data", str(path)])
    assert result.exit_code == 1
    assert "UNEXPLAINED" in result.output


def test_run_can_keep_the_database(data_dir, tmp_path):
    import duckdb

    db = tmp_path / "settlerec.duckdb"
    result = runner.invoke(app, ["run", "--data", str(data_dir), "--db", str(db)])
    assert result.exit_code == 0
    kept = duckdb.connect(str(db), read_only=True)
    assert kept.execute("SELECT count(*) FROM proof WHERE status = 'proved'").fetchone()[0] == 6
    kept.close()


def test_exceptions_lists_s17_and_the_missing_deposit(data_dir, truth):
    result = runner.invoke(app, ["exceptions", "--data", str(data_dir), "--month", "2026-06"])
    assert result.exit_code == 0
    assert "Open exceptions at the end of 2026-06: 4" in result.output
    s17 = truth["s17_unsettled"]
    line = next(x for x in result.output.splitlines() if "unsettled batch" in x)
    assert "S17" in line and s17["date"] in line and f"{Decimal(s17['amount']):,.2f}" in line
    assert truth["missing_deposit"]["deposit_id"] in result.output


def test_fees_prints_the_cumulative_overcharge(data_dir):
    result = runner.invoke(app, ["fees", "--data", str(data_dir)])
    assert result.exit_code == 0
    line = next(x for x in result.output.splitlines() if x.startswith("A") and "2026-06" in x)
    assert line.split()[-1].replace(",", "").replace(".", "").isdigit()
    assert "2.126%" in line


def test_memo_writes_markdown_with_the_proof_line_and_journal_entries(data_dir, tmp_path, truth):
    out = tmp_path / "memo.md"
    result = runner.invoke(
        app, ["memo", "--data", str(data_dir), "--month", "2026-06", "--out", str(out)]
    )
    assert result.exit_code == 0
    text = out.read_text()
    assert text.startswith("# Card clearing close memo: June 2026")
    for heading in ("## Roll-forward", "## Proof", "## Open exceptions", "## Fee audit"):
        assert heading in text
    assert "unexplained 0.00." in text and "Status: **proved**." in text
    assert "Proposed journal entries" in text
    for tip in truth["tip_adjustments"]:
        assert f"Dr Card clearing | {Decimal(tip['amount']):,.2f}" in text
        assert f"Cr Tips payable |  | {Decimal(tip['amount']):,.2f}" in text
    assert "Processor A charged" in text and "Processor B charged" not in text


def test_bad_inputs_and_months_exit_with_a_clear_error(tmp_path, data_dir):
    missing = runner.invoke(app, ["run", "--data", str(tmp_path / "nowhere")])
    assert missing.exit_code == 2
    bad_month = runner.invoke(app, ["exceptions", "--data", str(data_dir), "--month", "2027-01"])
    assert bad_month.exit_code == 2
    assert "not in the ledger" in bad_month.output
