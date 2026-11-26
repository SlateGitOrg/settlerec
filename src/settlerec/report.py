"""Read the pipeline's result tables and format them for the terminal and the close memo."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import duckdb

Row = dict[str, Any]


def query(con: duckdb.DuckDBPyConnection, sql: str, params: Sequence[Any] = ()) -> list[Row]:
    cursor = con.execute(sql, list(params))
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, values, strict=True)) for values in cursor.fetchall()]


def money(value: Decimal | None) -> str:
    """Thousands separators and two decimals, negatives with a leading minus."""
    if value is None:
        return ""
    return f"{value:,.2f}"


def rate(value: Decimal | None) -> str:
    """A fraction as a percentage with three decimals (0.021265 -> 2.127%)."""
    if value is None:
        return ""
    return f"{value * 100:.3f}%"


def render_table(
    headers: Sequence[str], rows: Sequence[Sequence[str]], right: Sequence[int]
) -> str:
    """A plain-text table; columns whose index is in ``right`` are right-aligned."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def line(cells: Sequence[str]) -> str:
        parts = [
            cell.rjust(widths[i]) if i in right else cell.ljust(widths[i])
            for i, cell in enumerate(cells)
        ]
        return "  ".join(parts).rstrip()

    rule = "  ".join("-" * w for w in widths)
    return "\n".join([line(headers), rule, *(line(r) for r in rows)])


def markdown_table(
    headers: Sequence[str], rows: Sequence[Sequence[str]], right: Sequence[int]
) -> str:
    head = "| " + " | ".join(headers) + " |"
    rule = "| " + " | ".join("---:" if i in right else "---" for i in range(len(headers))) + " |"
    body = ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join([head, rule, *body])


def months(con: duckdb.DuckDBPyConnection) -> list[str]:
    return [r["month"] for r in query(con, "SELECT month FROM months ORDER BY month_start")]


def resolve_month(con: duckdb.DuckDBPyConnection, month: str | None) -> str:
    """``month`` as YYYY-MM, defaulting to the last month in the ledger."""
    available = months(con)
    if month is None:
        return available[-1]
    if month not in available:
        raise ValueError(f"month {month} is not in the ledger ({available[0]} to {available[-1]})")
    return month


def rollforward(con: duckdb.DuckDBPyConnection, month: str) -> Row:
    return query(con, "SELECT * FROM rollforward WHERE month = ?", [month])[0]


def proof(con: duckdb.DuckDBPyConnection, month: str | None = None) -> list[Row]:
    if month is None:
        return query(con, "SELECT * FROM proof ORDER BY month")
    return query(con, "SELECT * FROM proof WHERE month = ?", [month])


def exceptions(con: duckdb.DuckDBPyConnection, month: str) -> list[Row]:
    return query(
        con,
        "SELECT e.* FROM open_exceptions e JOIN months m ON m.month_end = e.month_end "
        "WHERE m.month = ? ORDER BY e.exception_date, e.exception_type, e.store, e.reference",
        [month],
    )


def in_transit_by_processor(con: duckdb.DuckDBPyConnection, month: str) -> list[Row]:
    return query(
        con,
        "SELECT t.processor, count(*) AS batches, count(DISTINCT t.deposit_id) AS deposits, "
        "sum(t.amount) AS amount FROM in_transit t JOIN months m ON m.month_end = t.month_end "
        "WHERE m.month = ? GROUP BY t.processor ORDER BY t.processor",
        [month],
    )


def fee_audit(con: duckdb.DuckDBPyConnection, month: str | None = None) -> list[Row]:
    if month is None:
        return query(con, "SELECT * FROM fee_audit ORDER BY processor, month")
    return query(con, "SELECT * FROM fee_audit WHERE month = ? ORDER BY processor", [month])


def chargebacks(con: duckdb.DuckDBPyConnection, month: str) -> Row:
    return query(
        con,
        "SELECT coalesce(sum(chargebacks), 0) AS chargebacks, coalesce(sum(reversals), 0) AS "
        "reversals, coalesce(sum(net_chargebacks), 0) AS net FROM chargeback_summary "
        "WHERE month = ?",
        [month],
    )[0]


EXCEPTION_LABELS = {
    "unsettled_batch": "unsettled batch",
    "missing_deposit": "missing deposit",
    "tip_adjustment": "tip adjustment",
    "amount_mismatch": "amount mismatch",
    "ambiguous": "ambiguous match",
    "deposit_variance": "deposit variance",
    "unmatched_bank": "unmatched bank line",
}


def exception_rows(items: Sequence[Row]) -> list[list[str]]:
    return [
        [
            EXCEPTION_LABELS.get(e["exception_type"], e["exception_type"]),
            e["store"] or "all",
            e["exception_date"].isoformat(),
            money(e["amount"]),
            money(e["gl_effect"]),
            str(e["age_bd"]),
            e["reference"],
        ]
        for e in items
    ]


EXCEPTION_HEADERS = [
    "Type",
    "Store",
    "Date",
    "Amount",
    "Effect on clearing",
    "Age (bd)",
    "Reference",
]


def fee_rows(items: Sequence[Row]) -> list[list[str]]:
    return [
        [
            r["processor"],
            r["month"],
            money(r["gross"]),
            rate(r["contract_effective_rate"]),
            rate(r["actual_effective_rate"]),
            money(r["expected_fee"]),
            money(r["actual_fee"]),
            money(r["overcharge"]),
            money(r["cumulative_overcharge"]),
        ]
        for r in items
    ]


FEE_HEADERS = [
    "Proc",
    "Month",
    "Gross",
    "Contract eff. rate",
    "Actual eff. rate",
    "Contract fee",
    "Actual fee",
    "Overcharge",
    "Cumulative",
]
