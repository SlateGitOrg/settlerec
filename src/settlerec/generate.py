"""Seeded generator for the synthetic card-settlement data set.

A 35-store retailer takes card payments through two processors for the first half of 2026:

* Processor A (Visa/Mastercard/Discover): batches settle the next business day and are deposited
  the same day; fees (1.85% + $0.10 per transaction under contract) are deducted from the deposit.
* Processor B (American Express): batches settle the next business day and are deposited one
  business day after that, gross; a 2.9% fee is debited from the bank monthly on the third
  business day of the following month.

Both processors send one deposit per business day covering every batch that settled since the
previous one, so weekend and holiday batches ride together. The general ledger debits the clearing
account from the POS and credits it from the bank, so every difference the analysis must find is
planted here and recorded in ``truth.json``.

Planted patterns:

* store S17's processor A batch for 2026-02-10 is never submitted (no batch, no deposit);
* one processor B deposit (2026-04-14) is reported paid but never reaches the bank;
* 12 chargebacks, 3 of which are later reversed;
* processor A charges 1.95% instead of the contracted 1.85% from 2026-04-01;
* 2 store-days where post-close tip adjustments make the batch exceed the POS;
* two processor A deposits of identical net amount on consecutive business days.

All money is handled in integer cents or ``Decimal``; nothing here is random once the seed is set.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import random
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import yaml

from settlerec.bizdays import HOLIDAYS_2026, BusinessCalendar

DEFAULT_SEED = 18
FIRST_DAY = dt.date(2026, 1, 1)
LAST_DAY = dt.date(2026, 6, 30)
CLOSED_DAYS = {dt.date(2026, 1, 1)}  # every store is closed on New Year's Day

CONTRACTS = {
    "tip_adjustment_max_pct": 0.25,
    "bank_window_bd": 2,
    "processors": {
        "A": {
            "name": "Processor A (Visa/Mastercard/Discover)",
            "bank_descriptor": "VISAMC SETTLE",
            "fee_mode": "deducted_at_deposit",
            "rate": 0.0185,
            "per_txn": 0.10,
        },
        "B": {
            "name": "Processor B (American Express)",
            "bank_descriptor": "AMEX SETTLE",
            "fee_mode": "monthly_debit",
            "fee_bank_descriptor": "AMEX FEE DEBIT",
            "rate": 0.029,
            "per_txn": 0.0,
        },
    },
}

A_CONTRACT_RATE = Decimal("0.0185")
A_CHARGED_RATE = Decimal("0.0195")
A_CHARGED_FROM = dt.date(2026, 4, 1)
A_PER_TXN = Decimal("0.10")
B_RATE = Decimal("0.029")

S17_DATE = dt.date(2026, 2, 10)
MISSING_B_DEPOSIT_DATE = dt.date(2026, 4, 14)
TIP_ADJUSTMENTS = (("S09", dt.date(2026, 3, 14)), ("S24", dt.date(2026, 5, 9)))
PAIR_WINDOW = (dt.date(2026, 5, 11), dt.date(2026, 5, 29))

STORE_NAMES = [
    "Harbor Street", "Maple Court", "Riverside Plaza", "Old Mill", "Union Square",
    "Cedar Point", "Lakeview", "Foundry Row", "Westgate", "Orchard Park",
    "Station Road", "Garden District", "Highland Mall", "Pier Forty", "Summit Ridge",
    "Canal Walk", "Birchwood", "Market Hall", "Eastfield", "Quarry Lane",
    "Stonebridge", "Elm Terrace", "Kingsway", "Granite Plaza", "Bayfront",
    "Prairie Commons", "Fairmount", "Ironworks", "Willow Creek", "Northgate",
    "Silver Lake", "Brookside", "Copper Hill", "Dockside", "Meadowbrook",
]  # fmt: skip
REGIONS = ("North", "South", "East", "West")

DOW_FACTOR = (0.80, 0.85, 0.90, 1.00, 1.25, 1.50, 1.00)  # Monday .. Sunday
MONTH_FACTOR = {1: 0.85, 2: 0.90, 3: 1.00, 4: 1.05, 5: 1.15, 6: 1.10}


@dataclass
class PosRow:
    store: str
    day: dt.date
    processor: str
    gross: int  # cents
    txns: int


@dataclass
class BatchRow:
    processor: str
    batch_id: str
    store: str
    day: dt.date
    gross: int
    refunds: int
    settle_date: dt.date
    pos: PosRow


@dataclass
class DepositRow:
    processor: str
    deposit_id: str
    day: dt.date
    batches: list[BatchRow] = field(default_factory=list)
    fees: int = 0
    chargebacks: int = 0
    reversals: int = 0
    net: int = 0
    bank_lag: int = 0
    missing: bool = False


class FeeCarry:
    """Cumulative fee rounding: the paid total always equals the exact total rounded to the cent."""

    def __init__(self) -> None:
        self.exact = Decimal(0)
        self.paid = 0

    def peek(self, increment: Decimal) -> int:
        return _cents(self.exact + increment) - self.paid

    def charge(self, increment: Decimal) -> int:
        fee = self.peek(increment)
        self.exact += increment
        self.paid += fee
        return fee


def _cents(dollars: Decimal) -> int:
    return int((dollars * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100}.{abs(cents) % 100:02d}"


def _fee_increment(rate: Decimal, batches: list[BatchRow]) -> Decimal:
    gross = Decimal(sum(b.gross for b in batches)) / 100
    txns = sum(b.pos.txns for b in batches)
    return gross * rate + A_PER_TXN * txns


def _month_end(month: int) -> dt.date:
    return (dt.date(2026, month, 1) + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1)


def _a_rate(day: dt.date) -> Decimal:
    return A_CHARGED_RATE if day >= A_CHARGED_FROM else A_CONTRACT_RATE


def _make_pos(rng: random.Random) -> list[PosRow]:
    stores = [f"S{i:02d}" for i in range(1, 36)]
    volume = {s: 0.6 + rng.random() * 1.2 for s in stores}
    b_share = {s: 0.08 + rng.random() * 0.06 for s in stores}
    rows: list[PosRow] = []
    day = FIRST_DAY
    while day <= LAST_DAY:
        if day not in CLOSED_DAYS:
            base_factor = DOW_FACTOR[day.weekday()] * MONTH_FACTOR[day.month]
            for store in stores:
                noise = rng.lognormvariate(0, 0.12)
                a_gross = int(round(300_000 * volume[store] * base_factor * noise))
                b_gross = int(round(a_gross * b_share[store] * rng.lognormvariate(0, 0.25)))
                a_tickets = max(1, int(round(a_gross / 100 / rng.uniform(48, 66))))
                b_tickets = max(1, int(round(b_gross / 100 / rng.uniform(85, 110))))
                rows.append(PosRow(store, day, "A", a_gross, a_tickets))
                rows.append(PosRow(store, day, "B", b_gross, b_tickets))
        day += dt.timedelta(days=1)
    return rows


def _make_batches(
    rng: random.Random, pos_rows: list[PosRow], cal: BusinessCalendar
) -> tuple[list[BatchRow], dict[str, int]]:
    tips: dict[tuple[str, dt.date], int] = {}
    pos_by_key = {(p.store, p.day, p.processor): p for p in pos_rows}
    for store, day in TIP_ADJUSTMENTS:
        pos = pos_by_key[(store, day, "A")]
        tips[(store, day)] = int(round(pos.gross * rng.uniform(0.05, 0.08)))
    batches: list[BatchRow] = []
    for pos in pos_rows:
        refund_odds, refund_cap = (0.30, 0.02) if pos.processor == "A" else (0.08, 0.015)
        refunds = 0
        if rng.random() < refund_odds:
            refunds = int(round(pos.gross * rng.uniform(0.005, refund_cap)))
        if (pos.store, pos.day, pos.processor) == ("S17", S17_DATE, "A"):
            continue  # the batch for this store-day is never submitted
        gross = pos.gross + tips.get((pos.store, pos.day), 0) if pos.processor == "A" else pos.gross
        batch_id = f"{pos.processor}-{pos.day:%Y%m%d}-{pos.store}"
        batches.append(
            BatchRow(
                pos.processor,
                batch_id,
                pos.store,
                pos.day,
                gross,
                refunds,
                cal.add(pos.day, 1),
                pos,
            )
        )
    return batches, {f"{s}:{d}": t for (s, d), t in tips.items()}


def _group_deposits(batches: list[BatchRow], cal: BusinessCalendar) -> list[DepositRow]:
    groups: dict[tuple[str, dt.date], DepositRow] = {}
    for batch in batches:
        deposit_day = batch.settle_date if batch.processor == "A" else cal.add(batch.settle_date, 1)
        key = (batch.processor, deposit_day)
        if key not in groups:
            groups[key] = DepositRow(
                batch.processor, f"DEP-{batch.processor}-{deposit_day:%Y%m%d}", deposit_day
            )
        groups[key].batches.append(batch)
    return sorted(groups.values(), key=lambda d: (d.day, d.processor))


def _assign_chargebacks(
    rng: random.Random, deposits: list[DepositRow], protected: set[str], cal: BusinessCalendar
) -> list[dict[str, str]]:
    window = [d for d in deposits if dt.date(2026, 1, 12) <= d.day <= dt.date(2026, 6, 10)]
    window = [d for d in window if d.deposit_id not in protected]
    a_pool = [d for d in window if d.processor == "A"]
    b_pool = [d for d in window if d.processor == "B"]
    chosen = sorted(
        rng.sample(a_pool, 9) + rng.sample(b_pool, 3), key=lambda d: (d.day, d.processor)
    )
    events: list[dict[str, str]] = []
    amounts: list[tuple[DepositRow, int]] = []
    for deposit in chosen:
        amount = int(rng.uniform(4_500, 62_000))
        deposit.chargebacks += amount
        amounts.append((deposit, amount))
        events.append(
            {"deposit": deposit.deposit_id, "type": "chargeback", "amount": money(amount)}
        )
    taken = {d.deposit_id for d in chosen} | protected
    for deposit, amount in [x for x in amounts if x[0].processor == "A"][1::3]:
        later = [
            d
            for d in deposits
            if d.processor == "A"
            and d.deposit_id not in taken
            and cal.add(deposit.day, 10) <= d.day <= cal.add(deposit.day, 25)
        ]
        target = later[0]
        taken.add(target.deposit_id)
        target.reversals += amount
        events.append({"deposit": target.deposit_id, "type": "reversal", "amount": money(amount)})
    return events


def _spread_adjustment(deposit: DepositRow, delta: int) -> None:
    """Move a deposit's batch grosses (and their POS rows) by ``delta`` cents, pro rata."""
    total = sum(b.gross for b in deposit.batches)
    applied = 0
    for batch in deposit.batches:
        share = int(round(delta * batch.gross / total))
        batch.gross += share
        batch.pos.gross += share
        applied += share
    batch = deposit.batches[-1]
    batch.gross += delta - applied
    batch.pos.gross += delta - applied


def _finish_deposits(deposits: list[DepositRow]) -> dict[str, str]:
    """Compute fees and net amounts in date order; force the identical-amount pair."""
    carry_a: dict[str, FeeCarry] = {}
    a_deposits = [d for d in deposits if d.processor == "A"]
    pair: tuple[DepositRow, DepositRow] | None = None
    best = None
    for first, second in zip(a_deposits, a_deposits[1:], strict=False):
        if not (PAIR_WINDOW[0] <= first.day and second.day <= PAIR_WINDOW[1]):
            continue
        if (second.day - first.day).days != 1 or first.day.weekday() > 2:
            continue
        gap = abs(
            sum(b.gross - b.refunds for b in first.batches)
            - sum(b.gross - b.refunds for b in second.batches)
        )
        if best is None or gap < best:
            best, pair = gap, (first, second)
    assert pair is not None
    first_net = 0
    for deposit in deposits:
        if deposit.processor == "B":
            deposit.net = (
                sum(b.gross - b.refunds for b in deposit.batches)
                - deposit.chargebacks
                + deposit.reversals
            )
            continue
        # Rounding is cumulative within a billing period: each month before the rate change, and
        # the whole stretch after it, so period totals equal the exact fee to the cent.
        key = "charged" if deposit.day >= A_CHARGED_FROM else f"{deposit.day:%Y-%m}"
        carry = carry_a.setdefault(key, FeeCarry())
        rate = _a_rate(deposit.day)

        def net_for(dep: DepositRow, carry: FeeCarry = carry, rate: Decimal = rate) -> int:
            fee = carry.peek(_fee_increment(rate, dep.batches))
            gross = sum(b.gross for b in dep.batches)
            refunds = sum(b.refunds for b in dep.batches)
            return gross - refunds - fee - dep.chargebacks + dep.reversals

        if deposit is pair[1]:
            gap = first_net - net_for(deposit)
            _spread_adjustment(deposit, int(round(gap / (1 - float(rate)))))
            step = 0
            while net_for(deposit) != first_net:
                step += 1
                sign = 1 if step % 2 else -1
                _spread_adjustment(deposit, sign * ((step + 1) // 2))
                if step > 4000:
                    raise RuntimeError("could not equalise the identical-amount pair")
        deposit.fees = carry.charge(_fee_increment(rate, deposit.batches))
        gross = sum(b.gross for b in deposit.batches)
        refunds = sum(b.refunds for b in deposit.batches)
        deposit.net = gross - refunds - deposit.fees - deposit.chargebacks + deposit.reversals
        if deposit is pair[0]:
            first_net = deposit.net
    assert pair[0].net == pair[1].net
    return {"first": pair[0].deposit_id, "second": pair[1].deposit_id}


def _bank_lines(
    rng: random.Random,
    deposits: list[DepositRow],
    fee_debits: list[tuple[dt.date, int]],
    cal: BusinessCalendar,
    pair_ids: set[str],
) -> list[tuple[dt.date, str, int]]:
    lines: list[tuple[dt.date, str, int]] = []
    for deposit in deposits:
        roll = rng.random()
        if deposit.processor == "A":
            lag = 0 if roll < 0.88 else 1
        else:
            lag = 0 if roll < 0.65 else (1 if roll < 0.95 else 2)
        if deposit.deposit_id in pair_ids:
            lag = 0
        deposit.bank_lag = lag
        if deposit.missing:
            continue
        bank_day = cal.add(deposit.day, lag) if lag else deposit.day
        name = CONTRACTS["processors"][deposit.processor]["bank_descriptor"]
        lines.append((bank_day, f"{name} {bank_day:%m%d}", deposit.net))
    for debit_day, amount in fee_debits:
        lines.append((debit_day, "AMEX FEE DEBIT", -amount))
    month = dt.date(2026, 1, 1)
    while month <= dt.date(2026, 7, 1):
        first_bd = cal.add(month - dt.timedelta(days=1), 1)
        lines.append((first_bd, "LEASE ACH", -rng.randrange(8_500_000, 9_500_000, 100)))
        lines.append(
            (cal.add(first_bd, 9), "PAYROLL ACH", -rng.randrange(21_000_000, 25_000_000, 100))
        )
        month = (month.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return sorted(lines, key=lambda x: (x[0], x[1], x[2]))


def generate(out_dir: Path, seed: int = DEFAULT_SEED) -> dict:
    """Write the nine input files and ``truth.json`` to ``out_dir``; return the truth dictionary."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    cal = BusinessCalendar()

    pos_rows = _make_pos(rng)
    batches, tip_cents = _make_batches(rng, pos_rows, cal)
    deposits = _group_deposits(batches, cal)

    missing = next(d for d in deposits if d.processor == "B" and d.day == MISSING_B_DEPOSIT_DATE)
    missing.missing = True
    a_deposits = [d for d in deposits if d.processor == "A"]
    pair_candidates = [d for d in a_deposits if PAIR_WINDOW[0] <= d.day <= PAIR_WINDOW[1]]
    protected = {missing.deposit_id} | {d.deposit_id for d in pair_candidates}
    events = _assign_chargebacks(rng, deposits, protected, cal)
    pair = _finish_deposits(deposits)
    pair_ids = {pair["first"], pair["second"]}

    fee_rows: list[tuple[str, str, dt.date, int]] = []
    fee_debits: list[tuple[dt.date, int]] = []
    for month in range(1, 7):
        gross = sum(
            b.gross
            for d in deposits
            if d.processor == "B" and d.day.month == month
            for b in d.batches
        )
        fee = _cents(Decimal(gross) / 100 * B_RATE)
        debit_day = cal.add(_month_end(month), 3)  # third business day of the next month
        fee_rows.append(("B", f"2026-{month:02d}", debit_day, fee))
        fee_debits.append((debit_day, fee))

    bank = _bank_lines(rng, deposits, fee_debits, cal, pair_ids)

    # General ledger: POS debits on the sales date; credits from the bank on the bank date.
    gl: list[tuple[dt.date, str, int, int]] = []
    by_day: dict[dt.date, int] = {}
    for pos in pos_rows:
        by_day[pos.day] = by_day.get(pos.day, 0) + pos.gross
    for day, total in sorted(by_day.items()):
        gl.append((day, f"POS-{day:%Y%m%d}", total, 0))
    for deposit in deposits:
        if deposit.missing:
            continue
        bank_day = cal.add(deposit.day, deposit.bank_lag) if deposit.bank_lag else deposit.day
        if bank_day <= LAST_DAY:
            gl.append((bank_day, deposit.deposit_id, 0, sum(b.gross for b in deposit.batches)))
    gl.sort(key=lambda x: (x[0], x[1]))

    _write_inputs(out_dir, pos_rows, batches, deposits, fee_rows, bank, gl)
    truth = _truth(seed, pos_rows, batches, deposits, missing, events, pair, tip_cents)
    (out_dir / "truth.json").write_text(json.dumps(truth, indent=2, sort_keys=True) + "\n")
    return truth


def _truth(seed, pos_rows, batches, deposits, missing, events, pair, tip_cents) -> dict:
    s17 = next(p for p in pos_rows if (p.store, p.day, p.processor) == ("S17", S17_DATE, "A"))
    chargebacks = sum(d.chargebacks for d in deposits)
    reversals = sum(d.reversals for d in deposits)
    charged_gross = sum(
        b.gross
        for d in deposits
        if d.processor == "A" and A_CHARGED_FROM <= d.day <= LAST_DAY
        for b in d.batches
    )
    overcharge = Decimal(charged_gross) / 100 * (A_CHARGED_RATE - A_CONTRACT_RATE)
    by_id = {d.deposit_id: d for d in deposits}
    return {
        "seed": seed,
        "s17_unsettled": {
            "store": "S17",
            "date": S17_DATE.isoformat(),
            "processor": "A",
            "amount": money(s17.gross),
        },
        "missing_deposit": {
            "deposit_id": missing.deposit_id,
            "processor": missing.processor,
            "date": missing.day.isoformat(),
            "net": money(missing.net),
            "gl_effect": money(sum(b.pos.gross for b in missing.batches)),
        },
        "chargebacks": {
            "events": events,
            "chargebacks_total": money(chargebacks),
            "reversals_total": money(reversals),
            "net": money(chargebacks - reversals),
            "chargeback_count": sum(1 for e in events if e["type"] == "chargeback"),
            "reversal_count": sum(1 for e in events if e["type"] == "reversal"),
        },
        "processor_a_fee_change": {
            "from": A_CHARGED_FROM.isoformat(),
            "contract_rate": str(A_CONTRACT_RATE),
            "charged_rate": str(A_CHARGED_RATE),
            "overcharge_exact": str(overcharge.quantize(Decimal("0.0001"))),
        },
        "tip_adjustments": [
            {
                "store": key.split(":")[0],
                "date": key.split(":")[1],
                "processor": "A",
                "amount": money(cents),
            }
            for key, cents in sorted(tip_cents.items())
        ],
        "identical_deposits": {
            "deposits": [pair["first"], pair["second"]],
            "net": money(by_id[pair["first"]].net),
        },
    }


def _write_csv(path: Path, header: list[str], rows) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def _write_inputs(out_dir, pos_rows, batches, deposits, fee_rows, bank, gl) -> None:
    _write_csv(
        out_dir / "stores.csv",
        ["store", "store_name", "region"],
        [(f"S{i + 1:02d}", STORE_NAMES[i], REGIONS[i % 4]) for i in range(35)],
    )
    _write_csv(
        out_dir / "pos_daily.csv",
        ["store", "business_date", "processor", "card_gross", "txn_count"],
        [(p.store, p.day.isoformat(), p.processor, money(p.gross), p.txns) for p in pos_rows],
    )
    _write_csv(
        out_dir / "processor_batches.csv",
        ["processor", "batch_id", "store", "batch_date", "gross", "refunds", "settle_date"],
        [
            (b.processor, b.batch_id, b.store, b.day.isoformat(), money(b.gross), money(b.refunds),
             b.settle_date.isoformat())
            for b in batches
        ],
    )  # fmt: skip
    _write_csv(
        out_dir / "processor_deposits.csv",
        ["processor", "deposit_id", "deposit_date", "batch_ids", "fees_deducted", "chargebacks",
         "reversals", "net"],
        [
            (d.processor, d.deposit_id, d.day.isoformat(),
             ";".join(sorted(b.batch_id for b in d.batches)), money(d.fees), money(d.chargebacks),
             money(d.reversals), money(d.net))
            for d in deposits
        ],
    )  # fmt: skip
    _write_csv(
        out_dir / "processor_fees_monthly.csv",
        ["processor", "fee_month", "debit_date", "amount"],
        [(p, m, d.isoformat(), money(a)) for p, m, d, a in fee_rows],
    )
    _write_csv(
        out_dir / "bank.csv",
        ["date", "descriptor", "amount"],
        [(d.isoformat(), desc, money(a)) for d, desc, a in bank],
    )
    _write_csv(
        out_dir / "gl_clearing.csv",
        ["date", "reference", "debit", "credit"],
        [(d.isoformat(), ref, money(dr), money(cr)) for d, ref, dr, cr in gl],
    )
    _write_csv(
        out_dir / "calendar.csv",
        ["date", "holiday"],
        [(d.isoformat(), name) for d, name in HOLIDAYS_2026],
    )
    (out_dir / "contracts.yaml").write_text(yaml.safe_dump(CONTRACTS, sort_keys=False))
