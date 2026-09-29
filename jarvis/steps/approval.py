"""Print the plan and block on synchronous human approval."""
from __future__ import annotations

from rich.console import Console
from rich.table import Table

from jarvis.models.plan import Plan

console = Console()

_HIGH_RISK_CONFIRMATION = "yes I confirm"


def print_plan(plan: Plan, ticket_id: str) -> None:
    console.rule(f"[bold]Plan for {ticket_id}[/bold]")
    console.print(f"[bold]Approach:[/bold] {plan.approach}\n")

    files_table = Table(title="Files to change")
    files_table.add_column("Path")
    files_table.add_column("Reason")
    for fc in plan.files_to_change:
        files_table.add_row(fc.path, fc.reason)
    console.print(files_table)

    console.print("\n[bold]Test plan:[/bold]")
    for test in plan.test_plan:
        console.print(f"  - {test}")

    risk_style = {"low": "green", "medium": "yellow", "high": "bold red"}[plan.risk_class]
    console.print(f"\n[bold]Risk class:[/bold] [{risk_style}]{plan.risk_class}[/{risk_style}]")
    console.print(f"[bold]Estimated tokens:[/bold] {plan.estimated_tokens}\n")


def request_approval(plan: Plan) -> tuple[bool, str | None]:
    """Blocks on synchronous human input (never skipped). Returns (approved, response)."""
    if plan.risk_class == "high":
        console.print(
            "[bold red]WARNING:[/bold red] this is a HIGH risk change. "
            f'Type exactly "{_HIGH_RISK_CONFIRMATION}" to proceed.'
        )
        response = input("> ").strip()
        return response == _HIGH_RISK_CONFIRMATION, response

    response = input("Approve this plan? [y/N] ").strip().lower()
    return response in ("y", "yes"), response
