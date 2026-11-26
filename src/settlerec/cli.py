"""Command line interface: generate, run, exceptions, fees, memo."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import duckdb
import typer

from settlerec import generate as generator
from settlerec import memo as memo_builder
from settlerec import report
from settlerec.runner import InputError, run_pipeline

app = typer.Typer(
    help="Prove the card clearing balance equals in-transit batches plus open exceptions.",
    no_args_is_help=True,
    add_completion=False,
)

DataOption = Annotated[Path, typer.Option("--data", help="Directory holding the input files.")]
MonthOption = Annotated[
    str | None, typer.Option("--month", help="Month as YYYY-MM; defaults to the last month.")
]


def _open(data: Path, month: str | None, database: Path | None = None):
    try:
        con = run_pipeline(data, database)
        return con, report.resolve_month(con, month)
    except (InputError, ValueError, duckdb.Error) as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(2) from None


@app.command()
def generate(
    out: Annotated[Path, typer.Option("--out", help="Output directory.")] = Path("data"),
    seed: Annotated[int, typer.Option("--seed", help="Random seed.")] = generator.DEFAULT_SEED,
) -> None:
    """Write the synthetic input files and truth.json."""
    generator.generate(out, seed)
    typer.echo(f"wrote {len(list(out.iterdir()))} files to {out}")


@app.command()
def run(
    data: DataOption = Path("data"),
    month: MonthOption = None,
    db: Annotated[Path | None, typer.Option("--db", help="Keep the DuckDB file here.")] = None,
) -> None:
    """Run the pipeline; print the proof for every month and the roll-forward for one."""
    con, month = _open(data, month, db)
    proofs = report.proof(con)
    typer.echo(
        report.render_table(
            ["Month", "GL closing", "In transit", "Open exceptions", "Unexplained", "Status"],
            [
                [
                    p["month"],
                    report.money(p["gl_closing"]),
                    report.money(p["in_transit"]),
                    report.money(p["open_exceptions"]),
                    report.money(p["unexplained"]),
                    p["status"],
                ]
                for p in proofs
            ],
            right=[1, 2, 3, 4],
        )
    )
    roll = report.rollforward(con, month)
    chosen = report.proof(con, month)[0]
    m = report.money
    width = max(
        len(m(v)) for v in (roll["opening"], roll["debits"], roll["credits"], roll["closing"])
    )
    typer.echo(f"\nRoll-forward {month}")
    typer.echo(f"  Opening balance       {m(roll['opening']).rjust(width)}")
    typer.echo(f"  + POS debits          {m(roll['debits']).rjust(width)}")
    typer.echo(f"  - Bank credits        {m(roll['credits']).rjust(width)}")
    typer.echo(f"  Closing balance       {m(roll['closing']).rjust(width)}")
    typer.echo(
        f"\nProof {month}: {m(chosen['gl_closing'])} = in transit {m(chosen['in_transit'])} "
        f"+ open exceptions {m(chosen['open_exceptions'])} + unexplained "
        f"{m(chosen['unexplained'])}  [{chosen['status']}]"
    )
    if any(p["unexplained"] != 0 or p["status"] != "proved" for p in proofs):
        raise typer.Exit(1)
    if any(r["rollforward_diff"] != 0 for r in report.query(con, "SELECT * FROM rollforward")):
        raise typer.Exit(1)


@app.command()
def exceptions(data: DataOption = Path("data"), month: MonthOption = None) -> None:
    """List the exceptions open at a month end, with age in business days."""
    con, month = _open(data, month)
    items = report.exceptions(con, month)
    typer.echo(f"Open exceptions at the end of {month}: {len(items)}")
    if items:
        typer.echo(
            report.render_table(
                report.EXCEPTION_HEADERS, report.exception_rows(items), right=[3, 4, 5]
            )
        )


@app.command()
def fees(data: DataOption = Path("data")) -> None:
    """Effective fee rate by processor-month against the contract, with cumulative overcharge."""
    con, _ = _open(data, None)
    typer.echo(
        report.render_table(
            report.FEE_HEADERS, report.fee_rows(report.fee_audit(con)), right=range(2, 9)
        )
    )


@app.command()
def memo(
    data: DataOption = Path("data"),
    month: MonthOption = None,
    out: Annotated[Path | None, typer.Option("--out", help="Where to write the memo.")] = None,
) -> None:
    """Write the month-end close memo as Markdown."""
    con, month = _open(data, month)
    target = out or Path("out") / f"close-memo-{month}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(memo_builder.build_memo(con, month))
    typer.echo(f"wrote {target}")


if __name__ == "__main__":
    app()
