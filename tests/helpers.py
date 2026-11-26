"""Build small hand-checkable data sets for the pipeline."""

from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import yaml

from settlerec.bizdays import HOLIDAYS_2026, BusinessCalendar
from settlerec.generate import CONTRACTS

CAL = BusinessCalendar()
CENT = Decimal("0.01")


def d(text: str) -> dt.date:
    return dt.date.fromisoformat(text)


def cents(value: Decimal | float | str) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class Sale:
    """One store-day on one processor."""

    store: str
    day: dt.date
    pos: Decimal
    txns: int
    proc: str = "A"
    batch: Decimal | None = None  # defaults to the POS amount
    refunds: Decimal = Decimal(0)
    submitted: bool = True

    @property
    def batch_id(self) -> str:
        return f"{self.proc}-{self.day:%Y%m%d}-{self.store}"

    @property
    def batch_gross(self) -> Decimal:
        return self.pos if self.batch is None else self.batch


@dataclass
class Deposit:
    proc: str
    day: dt.date
    sales: list[Sale]
    rate: Decimal = Decimal("0.0185")
    chargebacks: Decimal = Decimal(0)
    reversals: Decimal = Decimal(0)
    net_error: Decimal = Decimal(0)  # what the processor's net is off by
    bank_day: dt.date | None | str = "same"  # "same", a date, or None (never arrives)
    bank_credit: bool = True
    extra_bank_days: list[dt.date] = field(default_factory=list)

    @property
    def deposit_id(self) -> str:
        return f"DEP-{self.proc}-{self.day:%Y%m%d}"

    @property
    def gross(self) -> Decimal:
        return sum((s.batch_gross for s in self.sales), Decimal(0))

    @property
    def fees(self) -> Decimal:
        if self.proc != "A":
            return Decimal(0)
        txns = sum(s.txns for s in self.sales)
        return cents(self.gross * self.rate + Decimal("0.10") * txns)

    @property
    def net(self) -> Decimal:
        refunds = sum((s.refunds for s in self.sales), Decimal(0))
        return self.gross - refunds - self.fees - self.chargebacks + self.reversals + self.net_error

    @property
    def bank_date(self) -> dt.date | None:
        if self.bank_day == "same":
            return self.day
        return self.bank_day  # type: ignore[return-value]


def _write(path: Path, header: list[str], rows: list[list[object]]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def write_dataset(
    path: Path,
    sales: list[Sale],
    deposits: list[Deposit],
    *,
    fees_monthly: list[tuple[str, str, dt.date, Decimal]] = (),
    gl_extra: list[tuple[dt.date, str, Decimal, Decimal]] = (),
    bank_extra: list[tuple[dt.date, str, Decimal]] = (),
) -> Path:
    """Write a complete input directory; the ledger follows the accounting rule.

    The ledger debits the POS on the sales date and credits the gross of each deposit's batches on
    the date its bank line lands (unless ``bank_credit`` is false).
    """
    path.mkdir(parents=True, exist_ok=True)
    _write(
        path / "stores.csv",
        ["store", "store_name", "region"],
        [["S01", "One", "N"], ["S02", "Two", "S"]],
    )
    _write(
        path / "pos_daily.csv",
        ["store", "business_date", "processor", "card_gross", "txn_count"],
        [[s.store, s.day, s.proc, s.pos, s.txns] for s in sales],
    )
    _write(
        path / "processor_batches.csv",
        ["processor", "batch_id", "store", "batch_date", "gross", "refunds", "settle_date"],
        [
            [s.proc, s.batch_id, s.store, s.day, s.batch_gross, s.refunds, CAL.add(s.day, 1)]
            for s in sales
            if s.submitted
        ],
    )
    _write(
        path / "processor_deposits.csv",
        ["processor", "deposit_id", "deposit_date", "batch_ids", "fees_deducted", "chargebacks",
         "reversals", "net"],
        [
            [dep.proc, dep.deposit_id, dep.day, ";".join(s.batch_id for s in dep.sales), dep.fees,
             dep.chargebacks, dep.reversals, dep.net]
            for dep in deposits
        ],
    )  # fmt: skip
    _write(
        path / "processor_fees_monthly.csv",
        ["processor", "fee_month", "debit_date", "amount"],
        [list(row) for row in fees_monthly],
    )
    bank: list[tuple[dt.date, str, Decimal]] = list(bank_extra)
    gl: list[tuple[dt.date, str, Decimal, Decimal]] = list(gl_extra)
    by_day: dict[dt.date, Decimal] = {}
    for sale in sales:
        by_day[sale.day] = by_day.get(sale.day, Decimal(0)) + sale.pos
    gl += [(day, f"POS-{day:%Y%m%d}", total, Decimal(0)) for day, total in by_day.items()]
    for dep in deposits:
        descriptor = CONTRACTS["processors"][dep.proc]["bank_descriptor"]
        if dep.bank_date is not None:
            bank.append((dep.bank_date, f"{descriptor} {dep.bank_date:%m%d}", dep.net))
            if dep.bank_credit:
                gl.append((dep.bank_date, dep.deposit_id, Decimal(0), dep.gross))
        bank += [(day, f"{descriptor} {day:%m%d}", dep.net) for day in dep.extra_bank_days]
    _write(path / "bank.csv", ["date", "descriptor", "amount"], [list(r) for r in sorted(bank)])
    _write(
        path / "gl_clearing.csv",
        ["date", "reference", "debit", "credit"],
        [list(r) for r in sorted(gl, key=lambda r: (r[0], r[1]))],
    )
    _write(path / "calendar.csv", ["date", "holiday"], [[day, name] for day, name in HOLIDAYS_2026])
    (path / "contracts.yaml").write_text(yaml.safe_dump(CONTRACTS, sort_keys=False))
    return path


def week_of_february() -> tuple[list[Sale], list[Deposit]]:
    """Fri 02-27 and Sat 02-28 batches deposited Mon 03-02, a Mon 03-02 batch deposited Tue 03-03.

    Month end 2026-02-28 falls on a Saturday, so the first deposit is in transit at February's
    close. Deposit 1: gross 1700.00, 17 transactions, fees 31.45 + 1.70 = 33.15, net 1666.85.
    Deposit 2: gross 300.00, 3 transactions, fees 5.55 + 0.30 = 5.85, net 294.15.
    """
    sales = [
        Sale("S01", d("2026-02-27"), Decimal("1000.00"), 10),
        Sale("S02", d("2026-02-27"), Decimal("500.00"), 5),
        Sale("S01", d("2026-02-28"), Decimal("200.00"), 2),
        Sale("S01", d("2026-03-02"), Decimal("300.00"), 3),
    ]
    deposits = [
        Deposit("A", d("2026-03-02"), sales[:3]),
        Deposit("A", d("2026-03-03"), sales[3:]),
    ]
    return sales, deposits
