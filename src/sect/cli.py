"""The ``sect`` command -- what "me, manually" means in the architecture.

Installed with the CLI extra::

    pip install "the-sect[cli]"

It is an ordinary HTTP client: it needs ``SECT_URL`` and ``SECT_MASTER_KEY`` and has no
database access and no server-side code. Anything it can do, the SDK can do too.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from rich.console import Console
from rich.table import Table

from sect import __version__
from sect.client import SectMaster
from sect.env import ensure_loaded
from sect.errors import SectError
from sect.models import Mission
from sect.realms import REALMS

console = Console()
errs = Console(stderr=True)

# Built from the ladder itself so `--realm` choices can never drift from sect.realms.
RealmChoice = Enum("RealmChoice", {realm.replace("-", "_"): realm for realm in REALMS}, type=str)

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Talk to the Sect: post missions, read results, manage disciples.",
)
disciple_app = typer.Typer(no_args_is_help=True, help="Act on a single disciple.")
mission_app = typer.Typer(no_args_is_help=True, help="Act on a single mission.")
app.add_typer(disciple_app, name="disciple")
app.add_typer(mission_app, name="mission")


def _die(message: str) -> NoReturn:
    errs.print(f"[bold red]error[/bold red] {message}")
    raise typer.Exit(1)


@contextmanager
def _sect() -> Iterator[SectMaster]:
    """A master client, with wire errors turned into clean CLI failures."""
    try:
        master = SectMaster()
    except SectError as exc:
        _die(str(exc))
    try:
        yield master
    except SectError as exc:
        _die(str(exc))
    finally:
        master.close()


def _load_json(value: str | None) -> dict[str, Any]:
    """Inline JSON, or ``@path/to/file.json``."""
    if not value:
        return {}
    try:
        raw = Path(value[1:]).read_text(encoding="utf-8") if value.startswith("@") else value
        parsed = json.loads(raw)
    except (OSError, ValueError) as exc:
        _die(f"could not read JSON from {value!r}: {exc}")
    if not isinstance(parsed, dict):
        _die("payload must be a JSON object")
    return parsed


def _when(value: Any) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "-"


def _emit(data: Any, as_json: bool) -> bool:
    """Print machine-readable output if asked. Returns True if it handled the output."""
    if as_json:
        console.print_json(json.dumps(data, default=str))
    return as_json


JsonFlag = Annotated[bool, typer.Option("--json", help="Emit JSON instead of a table.")]


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    version: Annotated[bool, typer.Option("--version", help="Show the version and exit.")] = False,
) -> None:
    if version:
        console.print(__version__)
        raise typer.Exit(0)
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        raise typer.Exit(0)


# --------------------------------------------------------------------------- #
# Disciples
# --------------------------------------------------------------------------- #


@app.command("disciples")
def list_disciples(
    art: Annotated[str | None, typer.Option(help="Only disciples with this art.")] = None,
    realm: Annotated[RealmChoice | None, typer.Option(help="Only this realm.")] = None,
    active: Annotated[bool | None, typer.Option(help="Filter by active flag.")] = None,
    as_json: JsonFlag = False,
) -> None:
    """List the disciples and how they are doing."""
    with _sect() as master:
        records = master.disciples(art=art, realm=realm.value if realm else None, active=active)

    if _emit([r.model_dump(mode="json") for r in records], as_json):
        return

    table = Table(title=f"{len(records)} disciple(s)", header_style="bold")
    for column in ("name", "arts", "realm", "done", "failed", "last seen", "version"):
        table.add_column(column)
    for record in records:
        table.add_row(
            record.name if record.active else f"[dim]{record.name} (inactive)[/dim]",
            ", ".join(record.arts),
            record.realm,
            str(record.stats.completed),
            str(record.stats.failed),
            _when(record.last_seen_at),
            record.agent_version or "-",
        )
    console.print(table)


@disciple_app.command("create")
def create_disciple(
    name: Annotated[str, typer.Argument(help="Lowercase slug, unique in the Sect.")],
    art: Annotated[list[str], typer.Option("--art", help="Repeatable. At least one.")],
    repo: Annotated[str | None, typer.Option(help="Where this disciple's code lives.")] = None,
    display_name: Annotated[str | None, typer.Option(help="Human-facing name.")] = None,
    description: Annotated[str | None, typer.Option(help="What it does.")] = None,
) -> None:
    """Admit a disciple and print its token. The token is shown exactly once."""
    with _sect() as master:
        record, token = master.register_disciple(
            name, art, repo_url=repo, display_name=display_name, description=description
        )
    console.print(f"[green]{record.name}[/green] admitted at realm [cyan]{record.realm}[/cyan]")
    console.print(f"\n  SECT_TOKEN={token}\n")
    errs.print(
        "[yellow]This token is shown once and stored only as a hash.[/yellow] "
        "Put it in the disciple repo's secrets now; if you lose it, rotate."
    )


@disciple_app.command("show")
def show_disciple(name: str, as_json: JsonFlag = False) -> None:
    """Show one disciple."""
    with _sect() as master:
        record = master.disciple(name)
    if _emit(record.model_dump(mode="json"), as_json):
        return
    console.print(record)


@disciple_app.command("grant")
def grant_realm(
    name: str,
    realm: Annotated[RealmChoice, typer.Argument(help="The realm to elevate them to.")],
) -> None:
    """Elevate a disciple to a higher cultivation realm."""
    with _sect() as master:
        record = master.grant_realm(name, realm.value)
    console.print(f"[green]{record.name}[/green] now stands at [cyan]{record.realm}[/cyan]")


@disciple_app.command("rotate")
def rotate_token(name: str) -> None:
    """Issue a new token. The previous one stops working immediately."""
    with _sect() as master:
        token = master.rotate_token(name)
    console.print(f"\n  SECT_TOKEN={token}\n")


@disciple_app.command("deactivate")
def deactivate(name: str) -> None:
    """Revoke a disciple's access without deleting its history."""
    with _sect() as master:
        master.set_active(name, False)
    console.print(f"[yellow]{name}[/yellow] is no longer admitted")


@disciple_app.command("reinstate")
def reinstate(name: str) -> None:
    """Let a deactivated disciple back in."""
    with _sect() as master:
        master.set_active(name, True)
    console.print(f"[green]{name}[/green] is admitted once more")


# --------------------------------------------------------------------------- #
# Missions
# --------------------------------------------------------------------------- #


def _mission_table(missions: list[Mission], title: str) -> Table:
    table = Table(title=title, header_style="bold")
    for column in ("id", "status", "art", "pri", "title", "holder", "created"):
        table.add_column(column)
    colours = {
        "open": "cyan",
        "claimed": "yellow",
        "completed": "green",
        "failed": "red",
        "cancelled": "dim",
    }
    for mission in missions:
        table.add_row(
            str(mission.id),
            f"[{colours[mission.status]}]{mission.status}[/{colours[mission.status]}]",
            mission.required_art,
            str(mission.priority),
            mission.title,
            mission.claimed_by or "-",
            _when(mission.created_at),
        )
    return table


@app.command("missions")
def list_missions(
    status: Annotated[
        str | None, typer.Option(help="open|claimed|completed|failed|cancelled")
    ] = None,
    art: Annotated[str | None, typer.Option(help="Only this required art.")] = None,
    claimed_by: Annotated[str | None, typer.Option(help="Only this disciple's missions.")] = None,
    limit: Annotated[int, typer.Option(help="How many to show.")] = 20,
    as_json: JsonFlag = False,
) -> None:
    """Browse the Mission Hall."""
    with _sect() as master:
        page = master.missions(status=status, art=art, claimed_by=claimed_by, limit=limit)

    if _emit([m.model_dump(mode="json") for m in page.missions], as_json):
        return

    console.print(_mission_table(page.missions, f"{page.count} mission(s)"))
    if page.next_cursor:
        console.print("[dim]more available; raise --limit to see them[/dim]")


@mission_app.command("post")
def post_mission(
    title: Annotated[str, typer.Argument(help="What needs doing.")],
    art: Annotated[str, typer.Option("--art", help="The art required to do it.")],
    payload: Annotated[
        str | None, typer.Option(help="Inline JSON object, or @path/to/file.json")
    ] = None,
    description: Annotated[str | None, typer.Option(help="Longer brief.")] = None,
    priority: Annotated[int, typer.Option(help="Higher runs sooner.")] = 0,
    lease_seconds: Annotated[int | None, typer.Option(help="Override the lease.")] = None,
    max_attempts: Annotated[int | None, typer.Option(help="Override the retry budget.")] = None,
    idempotency_key: Annotated[
        str | None, typer.Option(help="Posting twice with this key is a no-op.")
    ] = None,
) -> None:
    """Post a mission to the Mission Hall."""
    body = _load_json(payload)
    with _sect() as master:
        mission = master.post_mission(
            title,
            art,
            payload=body,
            description=description,
            priority=priority,
            lease_seconds=lease_seconds,
            max_attempts=max_attempts,
            idempotency_key=idempotency_key,
        )
    console.print(f"[green]posted[/green] {mission.id}  ({mission.required_art}, {mission.status})")


@mission_app.command("show")
def show_mission(mission_id: str, as_json: JsonFlag = False) -> None:
    """Show one mission, including its result."""
    with _sect() as master:
        mission = master.mission(mission_id)

    if _emit(mission.model_dump(mode="json"), as_json):
        return

    console.print(_mission_table([mission], mission.title))
    if mission.payload:
        console.print("\n[bold]payload[/bold]")
        console.print_json(json.dumps(mission.payload, default=str))
    if mission.result is not None:
        console.print("\n[bold]result[/bold]")
        console.print_json(json.dumps(mission.result, default=str))
    if mission.error:
        console.print("\n[bold red]error[/bold red]")
        console.print(mission.error)


@mission_app.command("cancel")
def cancel_mission(mission_id: str) -> None:
    """Pull a mission off the board."""
    with _sect() as master:
        mission = master.cancel(mission_id)
    console.print(f"[yellow]cancelled[/yellow] {mission.id}")


# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #


@app.command("stats")
def show_stats(as_json: JsonFlag = False) -> None:
    """Counts by status and by art."""
    with _sect() as master:
        stats = master.stats()

    if _emit(stats.model_dump(mode="json"), as_json):
        return

    table = Table(title="the Mission Hall", header_style="bold")
    table.add_column("art")
    for column in ("open", "claimed", "completed", "failed", "cancelled"):
        table.add_column(column, justify="right")
    for art, counts in sorted(stats.by_art.items()):
        table.add_row(
            art,
            str(counts.open),
            str(counts.claimed),
            str(counts.completed),
            str(counts.failed),
            str(counts.cancelled),
        )
    totals = stats.missions
    table.add_section()
    table.add_row(
        "[bold]all[/bold]",
        str(totals.open),
        str(totals.claimed),
        str(totals.completed),
        str(totals.failed),
        str(totals.cancelled),
    )
    console.print(table)
    console.print(
        f"{stats.disciples.active} active disciple(s) of {stats.disciples.total} admitted"
    )


@app.command("sweep")
def sweep() -> None:
    """Mark zombie missions failed: leases expired with no attempts left."""
    with _sect() as master:
        swept = master.sweep()
    console.print(f"swept [bold]{swept}[/bold] mission(s)")


def main() -> None:
    # Before anything reads SECT_URL or SECT_MASTER_KEY.
    ensure_loaded()
    app()


if __name__ == "__main__":
    main()
