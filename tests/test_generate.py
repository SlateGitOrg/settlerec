from __future__ import annotations

import csv
import json

from settlerec.generate import generate

FILES = {
    "stores.csv",
    "pos_daily.csv",
    "processor_batches.csv",
    "processor_deposits.csv",
    "processor_fees_monthly.csv",
    "bank.csv",
    "gl_clearing.csv",
    "calendar.csv",
    "contracts.yaml",
    "truth.json",
}


def _rows(path):
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def test_generator_writes_every_input_file(data_dir):
    assert {p.name for p in data_dir.iterdir()} == FILES


def test_generator_is_deterministic_and_seed_sensitive(data_dir, tmp_path):
    again = tmp_path / "again"
    generate(again)
    for name in FILES:
        assert (again / name).read_bytes() == (data_dir / name).read_bytes(), name
    other = tmp_path / "other"
    generate(other, seed=3)
    assert (other / "pos_daily.csv").read_bytes() != (data_dir / "pos_daily.csv").read_bytes()


def test_data_set_has_the_documented_shape(data_dir):
    pos = _rows(data_dir / "pos_daily.csv")
    batches = _rows(data_dir / "processor_batches.csv")
    deposits = _rows(data_dir / "processor_deposits.csv")
    assert len(_rows(data_dir / "stores.csv")) == 35
    assert len(pos) == 12_600  # 35 stores x 180 trading days x 2 processors
    assert len(batches) == len(pos) - 1  # the unsettled S17 batch was never submitted
    assert {r["processor"] for r in pos} == {"A", "B"}
    assert 240 <= len(deposits) <= 260  # one per processor per business day
    assert min(r["business_date"] for r in pos) == "2026-01-02"
    assert max(r["business_date"] for r in pos) == "2026-06-30"


def test_data_set_stays_small(data_dir):
    assert sum(p.stat().st_size for p in data_dir.iterdir()) < 2_000_000


def test_calendar_holds_the_2026_bank_holidays(data_dir):
    holidays = {r["date"]: r["holiday"] for r in _rows(data_dir / "calendar.csv")}
    assert len(holidays) == 11
    assert "2026-02-16" in holidays and "2026-07-03" in holidays


def test_truth_file_records_every_planted_pattern(truth):
    assert set(truth) == {
        "seed",
        "s17_unsettled",
        "missing_deposit",
        "chargebacks",
        "processor_a_fee_change",
        "tip_adjustments",
        "identical_deposits",
    }
    assert truth["chargebacks"]["chargeback_count"] == 12
    assert truth["chargebacks"]["reversal_count"] == 3
    assert len(truth["tip_adjustments"]) == 2
    json.dumps(truth)  # serialisable
