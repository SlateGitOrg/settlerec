"""The month-end close memo, as Markdown."""

from __future__ import annotations

import datetime as dt

import duckdb

from settlerec import report

ACTIONS = {
    "unsettled_batch": "Ask the store to close out the day on the terminal and resubmit the batch.",
    "missing_deposit": "Open a claim with the processor, quoting the deposit id; cash has not "
    "reached the bank.",
    "amount_mismatch": "Get the processor's batch detail and compare it with the POS journal.",
    "ambiguous": "Pair the deposit with its bank line by hand from the remittance detail.",
    "deposit_variance": "Ask the processor for a corrected remittance.",
    "unmatched_bank": "Find the remittance this bank line belongs to.",
}


def _month_title(month_start: dt.date) -> str:
    return month_start.strftime("%B %Y")


def build_memo(con: duckdb.DuckDBPyConnection, month: str) -> str:
    roll = report.rollforward(con, month)
    proved = report.proof(con, month)[0]
    items = report.exceptions(con, month)
    transit = report.in_transit_by_processor(con, month)
    fees = report.fee_audit(con, month)
    chargeback = report.chargebacks(con, month)
    m = report.money

    lines = [
        f"# Card clearing close memo: {_month_title(roll['month_start'])}",
        "",
        f"Month end {roll['month_end'].isoformat()}. All amounts in dollars; debits positive.",
        "",
        "## Roll-forward",
        "",
        report.markdown_table(
            ["Line", "Amount"],
            [
                [f"Opening balance ({roll['month_start'].isoformat()})", m(roll["opening"])],
                ["Debits: POS card sales", m(roll["debits"])],
                ["Credits: processor deposits at the bank", m(roll["credits"])],
                [f"Closing balance ({roll['month_end'].isoformat()})", m(roll["closing"])],
            ],
            right=[1],
        ),
        "",
        "## Proof",
        "",
        f"GL closing balance {m(proved['gl_closing'])} = in transit {m(proved['in_transit'])} "
        f"({proved['in_transit_batches']} batches) + open exceptions "
        f"{m(proved['open_exceptions'])} ({proved['open_exception_count']} items) + "
        f"unexplained {m(proved['unexplained'])}.",
        "",
        "Status: **proved**." if proved["status"] == "proved" else "Status: **UNEXPLAINED**.",
        "",
        "### In transit at month end",
        "",
        report.markdown_table(
            ["Processor", "Batches", "Deposits", "Amount"],
            [
                [t["processor"], str(t["batches"]), str(t["deposits"]), m(t["amount"])]
                for t in transit
            ],
            right=[1, 2, 3],
        ),
        "",
        "## Open exceptions",
        "",
    ]
    if items:
        lines += [
            report.markdown_table(
                report.EXCEPTION_HEADERS, report.exception_rows(items), right=[3, 4, 5]
            ),
            "",
        ]
    else:
        lines += ["None.", ""]

    tips = [e for e in items if e["exception_type"] == "tip_adjustment"]
    lines += ["### Proposed journal entries", ""]
    if tips:
        lines += [
            "Tips added after close are in the processor batch but not in the POS journal, so "
            "the ledger credit at settlement exceeded the debit from the POS. Book the tip to "
            "the liability account:",
            "",
        ]
        je_rows = []
        for n, e in enumerate(tips, start=1):
            label = f"TIP-{n} ({e['store']}, {e['exception_date'].isoformat()})"
            je_rows.append([label, "Dr Card clearing", m(e["amount"]), ""])
            je_rows.append([label, "Cr Tips payable", "", m(e["amount"])])
        lines += [
            report.markdown_table(["Entry", "Account", "Debit", "Credit"], je_rows, [2, 3]),
            "",
        ]
    else:
        lines += ["No tip adjustments are open.", ""]
    other = [e for e in items if e["exception_type"] in ACTIONS]
    if other:
        lines += ["### Follow-up", ""]
        for e in other:
            where = f"store {e['store']}" if e["store"] else f"deposit {e['reference']}"
            label = report.EXCEPTION_LABELS[e["exception_type"]]
            lines.append(
                f"- {label}, {where}, {e['exception_date'].isoformat()}, {m(e['amount'])}: "
                f"{ACTIONS[e['exception_type']]}"
            )
        lines.append("")

    lines += [
        "## Fee audit",
        "",
        report.markdown_table(report.FEE_HEADERS, report.fee_rows(fees), right=range(2, 9)),
        "",
    ]
    for f in fees:
        if f["status"] != "within contract":
            lines.append(
                f"Processor {f['processor']} charged {report.rate(f['actual_effective_rate'])} "
                f"against {report.rate(f['contract_effective_rate'])} under contract; "
                f"cumulative overcharge {m(f['cumulative_overcharge'])}."
            )
    lines += [
        "",
        "## Chargebacks",
        "",
        f"Deducted from deposits {m(chargeback['chargebacks'])}, reversed "
        f"{m(chargeback['reversals'])}, net {m(chargeback['net'])}.",
        "",
    ]
    return "\n".join(lines)
