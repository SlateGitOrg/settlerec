"""Load the input files into DuckDB and run the SQL pipeline in order.

The pipeline is the ``sql/*.sql`` files, executed in file-name order. Python only loads the raw
files (typed, with their headers checked) and the contract terms; every matching, classification
and proof step is SQL.
"""

from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

import duckdb
import sqlglot
import yaml
from sqlglot import exp

SQL_DIR = Path(__file__).parent / "sql"
MONEY = "DECIMAL(18,2)"

# table -> (file, [(csv header, column name, type)])
INPUTS: dict[str, tuple[str, list[tuple[str, str, str]]]] = {
    "raw_stores": (
        "stores.csv",
        [
            ("store", "store", "VARCHAR"),
            ("store_name", "store_name", "VARCHAR"),
            ("region", "region", "VARCHAR"),
        ],
    ),
    "raw_pos": (
        "pos_daily.csv",
        [
            ("store", "store", "VARCHAR"),
            ("business_date", "business_date", "DATE"),
            ("processor", "processor", "VARCHAR"),
            ("card_gross", "card_gross", MONEY),
            ("txn_count", "txn_count", "INTEGER"),
        ],
    ),
    "raw_batches": (
        "processor_batches.csv",
        [
            ("processor", "processor", "VARCHAR"),
            ("batch_id", "batch_id", "VARCHAR"),
            ("store", "store", "VARCHAR"),
            ("batch_date", "batch_date", "DATE"),
            ("gross", "gross", MONEY),
            ("refunds", "refunds", MONEY),
            ("settle_date", "settle_date", "DATE"),
        ],
    ),
    "raw_deposits": (
        "processor_deposits.csv",
        [
            ("processor", "processor", "VARCHAR"),
            ("deposit_id", "deposit_id", "VARCHAR"),
            ("deposit_date", "deposit_date", "DATE"),
            ("batch_ids", "batch_ids", "VARCHAR"),
            ("fees_deducted", "fees", MONEY),
            ("chargebacks", "chargebacks", MONEY),
            ("reversals", "reversals", MONEY),
            ("net", "net", MONEY),
        ],
    ),
    "raw_fees_monthly": (
        "processor_fees_monthly.csv",
        [
            ("processor", "processor", "VARCHAR"),
            ("fee_month", "fee_month", "VARCHAR"),
            ("debit_date", "debit_date", "DATE"),
            ("amount", "amount", MONEY),
        ],
    ),
    "raw_bank": (
        "bank.csv",
        [
            ("date", "bank_date", "DATE"),
            ("descriptor", "descriptor", "VARCHAR"),
            ("amount", "amount", MONEY),
        ],
    ),
    "raw_gl": (
        "gl_clearing.csv",
        [
            ("date", "gl_date", "DATE"),
            ("reference", "reference", "VARCHAR"),
            ("debit", "debit", MONEY),
            ("credit", "credit", MONEY),
        ],
    ),
    "raw_calendar": (
        "calendar.csv",
        [("date", "holiday_date", "DATE"), ("holiday", "holiday_name", "VARCHAR")],
    ),
}

FEE_MODES = {"deducted_at_deposit", "monthly_debit"}


class InputError(ValueError):
    """An input file is missing, has the wrong header or invalid contract terms."""


def sql_files() -> list[Path]:
    """The pipeline files in execution order."""
    return sorted(SQL_DIR.glob("*.sql"))


def parse_sql_file(path: Path) -> list[exp.Expression]:
    """Parse a pipeline file with sqlglot's DuckDB dialect; raises on anything it cannot read."""
    statements = sqlglot.parse(path.read_text(), read="duckdb")
    parsed = [s for s in statements if s is not None]
    opaque = [s for s in parsed if isinstance(s, exp.Command)]
    if opaque:
        raise sqlglot.errors.ParseError(f"{path.name}: unparsed statement {opaque[0].sql()[:60]!r}")
    return parsed


def _load_csv(con: duckdb.DuckDBPyConnection, data_dir: Path, table: str) -> None:
    file_name, columns = INPUTS[table]
    path = data_dir / file_name
    if not path.is_file():
        raise InputError(f"missing input file: {path}")
    with path.open(newline="") as handle:
        header = next(csv.reader(handle), [])
    expected = [c[0] for c in columns]
    if header != expected:
        raise InputError(f"{file_name}: expected header {expected}, found {header}")
    spec = ", ".join(f"'{name}': '{typ}'" for _, name, typ in columns)
    con.execute(
        f"CREATE OR REPLACE TABLE {table} AS "
        f"SELECT row_number() OVER () AS file_row, * "
        f"FROM read_csv(?, header = true, columns = {{{spec}}})",
        [str(path)],
    )


def _load_contracts(con: duckdb.DuckDBPyConnection, data_dir: Path) -> None:
    path = data_dir / "contracts.yaml"
    if not path.is_file():
        raise InputError(f"missing input file: {path}")
    terms = yaml.safe_load(path.read_text())
    try:
        processors = terms["processors"]
        con.execute(
            "CREATE OR REPLACE TABLE raw_contracts (processor VARCHAR, name VARCHAR, "
            "bank_descriptor VARCHAR, fee_bank_descriptor VARCHAR, fee_mode VARCHAR, "
            "rate DECIMAL(9,6), per_txn DECIMAL(9,4))"
        )
        for code, spec in processors.items():
            if spec["fee_mode"] not in FEE_MODES:
                raise InputError(f"contracts.yaml: unknown fee_mode {spec['fee_mode']!r}")
            con.execute(
                "INSERT INTO raw_contracts VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    code,
                    spec["name"],
                    spec["bank_descriptor"],
                    spec.get("fee_bank_descriptor"),
                    spec["fee_mode"],
                    Decimal(str(spec["rate"])),
                    Decimal(str(spec.get("per_txn", 0))),
                ],
            )
        con.execute(
            "CREATE OR REPLACE TABLE raw_settings AS "
            "SELECT CAST(? AS DECIMAL(5,4)) AS tip_adjustment_max_pct, "
            "CAST(? AS INTEGER) AS bank_window_bd",
            [Decimal(str(terms["tip_adjustment_max_pct"])), int(terms["bank_window_bd"])],
        )
    except KeyError as missing:
        raise InputError(f"contracts.yaml: missing key {missing}") from None


def run_pipeline(
    data_dir: Path | str, database: Path | str | None = None
) -> duckdb.DuckDBPyConnection:
    """Run every SQL stage over ``data_dir`` and return the open connection.

    The database is in memory unless ``database`` names a file. A single thread keeps row order,
    and therefore every output, identical from run to run.
    """
    data_dir = Path(data_dir)
    files = sql_files()
    for path in files:
        parse_sql_file(path)
    con = duckdb.connect(str(database) if database else ":memory:")
    con.execute("SET threads TO 1")
    for table in INPUTS:
        _load_csv(con, data_dir, table)
    _load_contracts(con, data_dir)
    for path in files:
        con.execute(path.read_text())
    return con
