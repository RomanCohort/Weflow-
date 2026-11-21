"""Local approval and management commands for generated tools."""

from __future__ import annotations

import json

import typer

from .agent.dynamic_tools import (
    disable_tool,
    list_approved,
    list_proposals,
    load_proposal,
    promote_proposal,
    rollback_tool,
    run_approved,
)

tool_app = typer.Typer(
    no_args_is_help=True,
    help="Review, approve, disable, roll back, or run agent-authored tools.",
)


@tool_app.command("proposals")
def proposals() -> None:
    """List sandbox-tested proposals awaiting approval."""
    rows = list_proposals()
    if not rows:
        typer.echo("(no pending proposals)")
        return
    for row in rows:
        typer.echo(
            f"{row['proposal_id']}  {row['name']}  {row['description']}"
        )


@tool_app.command("show")
def show(proposal_id: str = typer.Argument(...)) -> None:
    """Show a proposal's manifest, source, and selftest."""
    proposal = load_proposal(proposal_id)
    typer.echo(json.dumps(
        {key: value for key, value in proposal.items()
         if key not in {"code", "selftest"}},
        ensure_ascii=False,
        indent=2,
    ))
    typer.echo("\n--- tool.py ---\n" + proposal["code"])
    typer.echo("\n--- selftest.py ---\n" + proposal["selftest"])


@tool_app.command("promote")
def promote(proposal_id: str = typer.Argument(...)) -> None:
    """Approve a proposal and make it callable by the agent."""
    manifest = promote_proposal(proposal_id)
    typer.echo(
        f"approved {manifest['name']} v{manifest['version']} "
        f"from {manifest['proposal_id']}"
    )


@tool_app.command("list")
def list_() -> None:
    """List approved tools."""
    rows = list_approved()
    if not rows:
        typer.echo("(no approved dynamic tools)")
        return
    for row in rows:
        typer.echo(
            f"{row['name']} v{row['version']} "
            f"enabled={str(row['enabled']).lower()} :: {row['description']}"
        )


@tool_app.command("run")
def run(
    name: str = typer.Argument(...),
    args_json: str = typer.Argument("{}", help="JSON object passed to tool.run"),
    timeout_s: int = typer.Option(15, "--timeout", min=1, max=60),
) -> None:
    """Run an approved tool once in the sandbox."""
    try:
        args = json.loads(args_json)
    except json.JSONDecodeError as exc:
        typer.echo(f"[ERR] invalid JSON args: {exc}")
        raise typer.Exit(2)
    if not isinstance(args, dict):
        typer.echo("[ERR] args JSON must be an object")
        raise typer.Exit(2)
    typer.echo(run_approved(name, args, timeout_s=timeout_s))


@tool_app.command("disable")
def disable(name: str = typer.Argument(...)) -> None:
    """Disable an approved tool without deleting its versions."""
    disable_tool(name)
    typer.echo(f"disabled {name}")


@tool_app.command("rollback")
def rollback(
    name: str = typer.Argument(...),
    version: int | None = typer.Option(None, "--version", min=1),
) -> None:
    """Roll an approved tool back to an earlier version."""
    selected = rollback_tool(name, version)
    typer.echo(f"rolled back {name} to v{selected}")
