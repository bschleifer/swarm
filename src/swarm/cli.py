"""CLI entry point — swarm launch, tui, serve, status, install-hooks, init."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import click

from swarm.config import load_config
from swarm.logging import setup_logging

_MIN_TMUX_VERSION = 3.2


def _require_tmux() -> None:
    """Check that tmux is installed and meets minimum version. Exit with guidance if not."""
    tmux_path = shutil.which("tmux")
    if not tmux_path:
        click.echo("tmux is not installed.", err=True)
        click.echo("Install it (e.g. 'sudo apt install tmux') then run 'swarm init'.", err=True)
        raise SystemExit(1)

    import subprocess

    result = subprocess.run([tmux_path, "-V"], capture_output=True, text=True)
    version_str = result.stdout.strip()  # e.g. "tmux 3.4"
    try:
        ver = float(version_str.split()[-1])
        if ver < _MIN_TMUX_VERSION:
            click.echo(f"tmux {ver} found but swarm requires >= {_MIN_TMUX_VERSION}.", err=True)
            raise SystemExit(1)
    except (ValueError, IndexError):
        pass  # can't parse version — proceed optimistically


@click.group(invoke_without_command=True)
@click.option(
    "--log-level",
    default="WARNING",
    envvar="SWARM_LOG_LEVEL",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    help="Logging verbosity",
)
@click.option(
    "--log-file", default=None, envvar="SWARM_LOG_FILE", type=click.Path(), help="Log to file"
)
@click.version_option(package_name="swarm-ai")
@click.pass_context
def main(ctx: click.Context, log_level: str, log_file: str | None) -> None:
    """Swarm — a hive-mind for Claude Code agents."""
    setup_logging(level=log_level, log_file=log_file)

    # No subcommand → open the TUI
    if ctx.invoked_subcommand is None:
        ctx.invoke(tui)


@main.command()
@click.option(
    "-d",
    "--dir",
    "projects_dir",
    type=click.Path(exists=True),
    help="Directory to scan for projects",
)
@click.option(
    "-o",
    "--output",
    "output_path",
    default=str(Path.home() / ".config" / "swarm" / "config.yaml"),
    help="Output config path (default: ~/.config/swarm/config.yaml)",
)
@click.option("--skip-tmux", is_flag=True, help="Skip tmux configuration")
@click.option("--skip-hooks", is_flag=True, help="Skip Claude Code hooks installation")
@click.option("--skip-config", is_flag=True, help="Skip swarm.yaml generation")
def init(  # noqa: C901
    projects_dir: str | None,
    output_path: str,
    skip_tmux: bool,
    skip_hooks: bool,
    skip_config: bool,
) -> None:
    """Set up swarm: tmux config, Claude Code hooks, and swarm.yaml.

    On a fresh install, this ensures everything is ready to go.
    """
    checks: list[tuple[str, bool]] = []

    # --- Step 1: Check tmux ---
    tmux_path = shutil.which("tmux")
    if not tmux_path:
        click.echo("tmux is not installed. Please install tmux >= 3.2 first.", err=True)
        checks.append(("tmux installed", False))
    else:
        import subprocess

        result = subprocess.run([tmux_path, "-V"], capture_output=True, text=True)
        version_str = result.stdout.strip()  # e.g. "tmux 3.4"
        checks.append(("tmux installed", True))
        try:
            ver = float(version_str.split()[-1])
            if ver < 3.2:
                click.echo(
                    f"tmux {ver} detected. swarm requires >= 3.2 for border features.",
                    err=True,
                )
                checks.append(("tmux >= 3.2", False))
            else:
                click.echo(f"  {version_str}")
                checks.append(("tmux >= 3.2", True))
        except (ValueError, IndexError):
            click.echo(f"  {version_str} (could not parse version)")
            checks.append(("tmux >= 3.2", True))

    # --- Step 2: Write tmux config ---
    if not skip_tmux:
        from swarm.tmux.init import write_tmux_config

        wrote = write_tmux_config()
        checks.append(("tmux config written", wrote))
    else:
        click.echo("  Skipping tmux config (--skip-tmux)")
        checks.append(("tmux config written", None))

    # --- Step 3: Install Claude Code hooks ---
    if not skip_hooks:
        from swarm.hooks.install import install

        install(global_install=True)
        click.echo("  Claude Code hooks installed globally")
        checks.append(("Claude Code hooks", True))
    else:
        click.echo("  Skipping hooks (--skip-hooks)")
        checks.append(("Claude Code hooks", None))

    # --- Step 4: Generate swarm.yaml ---
    if not skip_config:
        from swarm.config import discover_projects, write_config

        scan_dir = Path(projects_dir) if projects_dir else Path.home() / "projects"
        projects = discover_projects(scan_dir)

        if not projects:
            click.echo(f"\n  No git repos found in {scan_dir}")
            checks.append(("swarm.yaml generated", False))
        else:
            click.echo(f"\n  Found {len(projects)} projects in {scan_dir}:\n")
            for i, (name, path) in enumerate(projects):
                click.echo(f"    [{i + 1:2d}] {name:30s} {path}")

            click.echo("\n  Select workers (comma-separated numbers, 'a' for all):")
            selection = click.prompt("  ", default="a", show_default=False).strip()

            if selection.lower() == "a":
                selected = list(range(len(projects)))
            else:
                try:
                    selected = [int(x.strip()) - 1 for x in selection.split(",")]
                    selected = [i for i in selected if 0 <= i < len(projects)]
                except ValueError:
                    click.echo("  Invalid selection")
                    selected = []

            if selected:
                workers = [(projects[i][0], projects[i][1]) for i in selected]

                # Ask about groups
                groups: dict[str, list[str]] = {}
                if len(workers) > 1 and click.confirm("\n  Define custom groups?", default=False):
                    while True:
                        gname = click.prompt(
                            "    Group name (or Enter to finish)",
                            default="",
                            show_default=False,
                        ).strip()
                        if not gname:
                            break
                        click.echo("    Available:")
                        for i, (n, _) in enumerate(workers):
                            click.echo(f"      [{i + 1:2d}] {n}")
                        raw = click.prompt(
                            "    Members (numbers or names, comma-separated)"
                        ).strip()
                        member_names = []
                        for token in raw.split(","):
                            token = token.strip()
                            if not token:
                                continue
                            try:
                                idx = int(token) - 1
                                if 0 <= idx < len(workers):
                                    member_names.append(workers[idx][0])
                            except ValueError:
                                member_names.append(token)
                        groups[gname] = member_names
                        click.echo(f"    -> {gname}: {', '.join(member_names)}")

                groups["all"] = [n for n, _ in workers]
                write_config(output_path, workers, groups, str(scan_dir))
                click.echo(f"\n  Wrote {output_path} with {len(workers)} workers")
                checks.append(("swarm.yaml generated", True))
            else:
                click.echo("  No workers selected")
                checks.append(("swarm.yaml generated", False))
    else:
        click.echo("  Skipping swarm.yaml (--skip-config)")
        checks.append(("swarm.yaml generated", None))

    # --- Summary ---
    click.echo("\n  System readiness:")
    for label, status in checks:
        if status is True:
            indicator = "OK"
        elif status is False:
            indicator = "FAIL"
        else:
            indicator = "SKIP"
        click.echo(f"    [{indicator:4s}] {label}")

    all_ok = all(s is not False for _, s in checks)
    if all_ok:
        click.echo("\n  Ready! Next: swarm launch all")
    else:
        click.echo("\n  Some checks failed — see above.", err=True)


@main.command()
@click.argument("group", required=False)
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=True),
    help="Path to swarm.yaml",
)
@click.option("-a", "--all", "launch_all", is_flag=True, help="Launch all workers")
def launch(group: str | None, config_path: str | None, launch_all: bool) -> None:  # noqa: C901
    """Start workers in the hive."""
    _require_tmux()
    from swarm.worker.manager import launch_hive

    cfg = load_config(config_path)
    errors = cfg.validate()
    if errors:
        for e in errors:
            click.echo(f"Config error: {e}", err=True)
        raise SystemExit(1)

    num_groups = len(cfg.groups)

    def _show_available() -> None:
        click.echo("Groups:")
        for i, g in enumerate(cfg.groups):
            members = ", ".join(g.workers)
            click.echo(f"  [{i + 1:2d}] {g.name:20s} {members}")
        click.echo("\nIndividual workers:")
        for i, w in enumerate(cfg.workers):
            click.echo(f"  [{num_groups + i + 1:2d}] {w.name}")
        click.echo("\nUsage: swarm launch <name|number> or swarm launch -a")

    session_name = cfg.session_name
    if launch_all:
        workers = cfg.workers
    elif group:
        # Try as a number first
        try:
            idx = int(group) - 1
            if 0 <= idx < num_groups:
                group_name = cfg.groups[idx].name
                workers = cfg.get_group(group_name)
                session_name = group_name
            elif num_groups <= idx < num_groups + len(cfg.workers):
                w = cfg.workers[idx - num_groups]
                workers = [w]
                session_name = w.name
            else:
                click.echo(f"Number {group} out of range\n")
                _show_available()
                return
        except ValueError:
            # Try as a group name, then worker name (case-insensitive)
            try:
                workers = cfg.get_group(group)
                session_name = group
            except ValueError:
                w = cfg.get_worker(group)
                if w:
                    workers = [w]
                    session_name = w.name
                else:
                    click.echo(f"Unknown group or worker: '{group}'\n")
                    _show_available()
                    return
    else:
        _show_available()
        return

    asyncio.run(launch_hive(session_name, workers, panes_per_window=cfg.panes_per_window))
    click.echo(f"Hive launched: {len(workers)} workers in session '{session_name}'")
    click.echo(f"Attach with: tmux attach -t {session_name}")
    click.echo(f"Or run: swarm tui {session_name}")


def _resolve_target(cfg: object, target: str) -> tuple[str, list | None]:
    """Resolve a target as group name, worker name, or number.

    Returns (session_name, workers) if resolved, or (target, None) if not found.
    """
    from swarm.config import HiveConfig

    assert isinstance(cfg, HiveConfig)
    num_groups = len(cfg.groups)

    # Try as a number first
    try:
        idx = int(target) - 1
        if 0 <= idx < num_groups:
            group_name = cfg.groups[idx].name
            return group_name, cfg.get_group(group_name)
        elif num_groups <= idx < num_groups + len(cfg.workers):
            w = cfg.workers[idx - num_groups]
            return w.name, [w]
    except ValueError:
        pass

    # Try as a group name (case-insensitive)
    try:
        workers = cfg.get_group(target)
        return target, workers
    except ValueError:
        pass

    # Try as a worker name (case-insensitive)
    w = cfg.get_worker(target)
    if w:
        return w.name, [w]

    return target, None


@main.command()
@click.argument("target", required=False)
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=True),
    help="Path to swarm.yaml",
)
def tui(target: str | None, config_path: str | None) -> None:  # noqa: C901
    """Open the Bee Hive dashboard.

    TARGET can be a group name, worker name, number, or tmux session name.
    If the workers aren't already running, they will be launched automatically.
    """
    from swarm.tmux.hive import find_swarm_session, session_exists
    from swarm.ui.app import BeeHiveApp
    from swarm.worker.manager import launch_hive

    cfg = load_config(config_path)

    if target:
        # If a tmux session with this name already exists, just attach
        if asyncio.run(session_exists(target)):
            cfg.session_name = target
        else:
            # Resolve target as group/worker name and auto-launch
            session_name, workers = _resolve_target(cfg, target)
            if workers is not None:
                _require_tmux()
                errors = cfg.validate()
                if errors:
                    for e in errors:
                        click.echo(f"Config error: {e}", err=True)
                    raise SystemExit(1)
                asyncio.run(
                    launch_hive(session_name, workers, panes_per_window=cfg.panes_per_window)
                )
                click.echo(f"Launched {len(workers)} workers in session '{session_name}'")
                cfg.session_name = session_name
            else:
                # Not a known group/worker — treat as literal session name
                cfg.session_name = target
    else:
        found = asyncio.run(find_swarm_session())
        if found and found != cfg.session_name:
            cfg.session_name = found

    app = BeeHiveApp(cfg)
    app.run()


@main.command()
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=True),
    help="Path to swarm.yaml",
)
@click.option("--host", default="localhost", help="Host to bind to")
@click.option("--port", default=8080, type=int, help="Port to serve on")
@click.option("-s", "--session", default=None, help="tmux session name")
def serve(config_path: str | None, host: str, port: int, session: str | None) -> None:
    """Serve the Bee Hive web dashboard."""
    from swarm.server.daemon import run_daemon

    cfg = load_config(config_path)
    if session:
        cfg.session_name = session

    asyncio.run(run_daemon(cfg, host=host, port=port))


@main.group()
def web() -> None:
    """Manage the web dashboard (background process)."""


@web.command()
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=True),
    help="Path to swarm.yaml",
)
@click.option("--host", default="localhost", help="Host to bind to")
@click.option("--port", default=8080, type=int, help="Port to serve on")
@click.option("-s", "--session", default=None, help="tmux session name")
def start(config_path: str | None, host: str, port: int, session: str | None) -> None:
    """Start the web dashboard in the background."""
    from swarm.server.webctl import web_start

    ok, msg = web_start(host=host, port=port, config_path=config_path, session=session)
    click.echo(msg)
    if ok:
        from swarm.server.webctl import _WEB_LOG_FILE

        click.echo(f"  Logs: {_WEB_LOG_FILE}")
        click.echo("  Stop with: swarm web stop")


@web.command()
def stop() -> None:
    """Stop the background web dashboard."""
    from swarm.server.webctl import web_stop

    _ok, msg = web_stop()
    click.echo(msg)


@web.command("status")
def web_status() -> None:
    """Check if the web dashboard is running."""
    from swarm.server.webctl import web_is_running

    pid = web_is_running()
    if pid:
        click.echo(f"Web dashboard is running (PID {pid})")
    else:
        click.echo("Web dashboard is not running")


@main.command()
@click.argument("session", required=False)
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=True),
    help="Path to swarm.yaml",
)
def status(session: str | None, config_path: str | None) -> None:
    """One-shot status check of all workers."""
    from swarm.tmux.cell import capture_pane, get_pane_command
    from swarm.worker.state import classify_pane_content

    cfg = load_config(config_path)

    async def _status() -> None:
        from swarm.tmux.hive import discover_workers, find_swarm_session

        target = session or cfg.session_name
        # Auto-discover if configured session doesn't exist
        if not session:
            found = await find_swarm_session()
            if found:
                target = found

        workers = await discover_workers(target)
        if not workers:
            click.echo(f"No active hive found for session '{target}'")
            return
        for w in workers:
            cmd = await get_pane_command(w.pane_id)
            content = await capture_pane(w.pane_id)
            state = classify_pane_content(cmd, content)
            click.echo(f"  {state.indicator} {w.name:20s} [{state.display}]")

    asyncio.run(_status())


@main.command()
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=True),
    help="Path to swarm.yaml",
)
def validate(config_path: str | None) -> None:
    """Validate the swarm.yaml configuration."""
    cfg = load_config(config_path)
    errors = cfg.validate()
    if errors:
        click.echo(f"Found {len(errors)} error(s):", err=True)
        for e in errors:
            click.echo(f"  ✗ {e}", err=True)
        raise SystemExit(1)
    click.echo(f"Config OK: {len(cfg.workers)} workers, {len(cfg.groups)} groups")


@main.command()
@click.argument("target")
@click.argument("message")
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=True),
    help="Path to swarm.yaml",
)
@click.option("-s", "--session", default=None, help="tmux session name")
def send(target: str, message: str, config_path: str | None, session: str | None) -> None:
    """Send a message to a worker, group, or all.

    TARGET is a worker name, group name, or 'all'.
    MESSAGE is the text to send.
    """
    from swarm.tmux.cell import send_keys

    cfg = load_config(config_path)

    async def _send() -> None:
        from swarm.tmux.hive import discover_workers, find_swarm_session

        target_session = session or cfg.session_name
        if not session:
            found = await find_swarm_session()
            if found:
                target_session = found

        workers = await discover_workers(target_session)
        if not workers:
            click.echo(f"No active hive found for session '{target_session}'")
            return

        # Resolve targets
        if target.lower() == "all":
            targets = workers
        else:
            # Try as group name first
            try:
                group_workers = cfg.get_group(target)
                group_names = {w.name.lower() for w in group_workers}
                targets = [w for w in workers if w.name.lower() in group_names]
            except ValueError:
                # Try as worker name
                targets = [w for w in workers if w.name.lower() == target.lower()]

        if not targets:
            click.echo(f"No matching workers for '{target}'")
            return

        for w in targets:
            await send_keys(w.pane_id, message)
            click.echo(f"  Sent to {w.name}")

        click.echo(f"Message sent to {len(targets)} worker(s)")

    asyncio.run(_send())


@main.command()
@click.argument("worker_name")
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=True),
    help="Path to swarm.yaml",
)
@click.option("-s", "--session", default=None, help="tmux session name")
def kill(worker_name: str, config_path: str | None, session: str | None) -> None:
    """Kill a worker's tmux pane."""
    from swarm.worker.manager import kill_worker

    cfg = load_config(config_path)

    async def _kill() -> None:
        from swarm.tmux.hive import discover_workers, find_swarm_session

        target_session = session or cfg.session_name
        if not session:
            found = await find_swarm_session()
            if found:
                target_session = found

        workers = await discover_workers(target_session)
        if not workers:
            click.echo(f"No active hive found for session '{target_session}'")
            return

        worker = next((w for w in workers if w.name.lower() == worker_name.lower()), None)
        if not worker:
            names = ", ".join(w.name for w in workers)
            click.echo(f"Worker '{worker_name}' not found. Available: {names}")
            return

        await kill_worker(worker)
        click.echo(f"Killed worker: {worker.name}")

    asyncio.run(_kill())


@main.command()
@click.argument(
    "action",
    type=click.Choice(["list", "create", "assign", "complete"]),
    default="list",
)
@click.option("--title", help="Task title (for create)")
@click.option("--desc", default="", help="Task description (for create)")
@click.option(
    "--priority",
    type=click.Choice(["low", "normal", "high", "urgent"]),
    default="normal",
    help="Task priority (for create)",
)
@click.option("--task-id", help="Task ID (for assign/complete)")
@click.option("--worker", help="Worker name (for assign)")
def tasks(  # noqa: C901
    action: str,
    title: str | None,
    desc: str,
    priority: str,
    task_id: str | None,
    worker: str | None,
) -> None:
    """Manage the task board.

    Actions: list, create, assign, complete.
    """
    from swarm.tasks.board import TaskBoard
    from swarm.tasks.store import FileTaskStore
    from swarm.tasks.task import PRIORITY_MAP

    # Persist tasks to disk so they survive between CLI invocations.
    board = TaskBoard(store=FileTaskStore())

    if action == "list":
        all_tasks = board.all_tasks
        if not all_tasks:
            click.echo(
                "No tasks on the board. (Tasks are session-scoped"
                " — create from TUI or use 'swarm tasks create')"
            )
            return
        for t in all_tasks:
            assigned = f" → {t.assigned_worker}" if t.assigned_worker else ""
            click.echo(f"  {t.status.value:12s} [{t.id}] {t.title}{assigned}")
        click.echo(f"\n{board.summary()}")

    elif action == "create":
        if not title:
            click.echo("--title is required for create", err=True)
            raise SystemExit(1)
        task = board.create(title, description=desc, priority=PRIORITY_MAP[priority])
        click.echo(f"Created task [{task.id}]: {task.title} (priority={priority})")

    elif action == "assign":
        if not task_id or not worker:
            click.echo("--task-id and --worker are required for assign", err=True)
            raise SystemExit(1)
        if board.assign(task_id, worker):
            click.echo(f"Assigned task [{task_id}] → {worker}")
        else:
            click.echo(f"Task [{task_id}] not found", err=True)
            raise SystemExit(1)

    elif action == "complete":
        if not task_id:
            click.echo("--task-id is required for complete", err=True)
            raise SystemExit(1)
        if board.complete(task_id):
            click.echo(f"Task [{task_id}] marked complete")
        else:
            click.echo(f"Task [{task_id}] not found", err=True)
            raise SystemExit(1)


@main.command()
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=True),
    help="Path to swarm.yaml",
)
@click.option("--host", default="localhost", help="Host to bind to")
@click.option("--port", default=8081, type=int, help="Port for the API server")
@click.option("-s", "--session", default=None, help="tmux session name")
def daemon(config_path: str | None, host: str, port: int, session: str | None) -> None:
    """Run the swarm as a background daemon with REST + WebSocket API."""
    from swarm.server.daemon import run_daemon

    cfg = load_config(config_path)
    if session:
        cfg.session_name = session

    asyncio.run(run_daemon(cfg, host=host, port=port))


@main.command("install-hooks")
@click.option("--global", "global_install", is_flag=True, help="Install hooks globally")
def install_hooks(global_install: bool) -> None:
    """Install auto-approval hooks for Claude Code."""
    from swarm.hooks.install import install

    install(global_install=global_install)
    scope = "globally" if global_install else "for this project"
    click.echo(f"Hooks installed {scope}")
