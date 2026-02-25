"""CLI entry point for the swarm command."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import click

from swarm.config import HiveConfig, load_config
from swarm.logging import setup_logging

# Default daemon API port
_DEFAULT_PORT = 9090


async def _api_get(port: int, path: str) -> dict:
    """Make a GET request to the daemon API. Returns parsed JSON dict."""
    import aiohttp

    url = f"http://localhost:{port}{path}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise click.ClickException(f"API error ({resp.status}): {text}")
                return await resp.json()
    except aiohttp.ClientConnectorError:
        raise click.ClickException(f"Cannot connect to daemon on port {port}. Is swarm running?")


async def _api_post(port: int, path: str, json: dict | None = None) -> dict:
    """Make a POST request to the daemon API. Returns parsed JSON dict."""
    import aiohttp

    url = f"http://localhost:{port}{path}"
    headers = {"X-Requested-With": "swarm-cli"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=json, headers=headers) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise click.ClickException(f"API error ({resp.status}): {text}")
                return await resp.json()
    except aiohttp.ClientConnectorError:
        raise click.ClickException(f"Cannot connect to daemon on port {port}. Is swarm running?")


class SwarmCLI(click.Group):
    """Route unknown subcommands as targets to ``start``."""

    def resolve_command(self, ctx: click.Context, args: list[str]) -> tuple:
        try:
            cmd_name, cmd, remaining = super().resolve_command(ctx, args)
            if cmd is not None:
                return cmd_name, cmd, remaining
        except click.UsageError:
            pass
        # Unknown command -- treat first arg as a target for 'start'
        start_cmd = self.get_command(ctx, "start")
        if start_cmd is not None:
            return "start", start_cmd, args
        raise click.UsageError(f"No such command '{args[0]}'.")


@click.group(cls=SwarmCLI, invoke_without_command=True)
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
    """Swarm -- a hive-mind for Claude Code agents.

    \b
    Run with a target name to launch directly:
        swarm rcg-v6           # launch 'rcg-v6' group + web dashboard
        swarm start default    # explicit 'start' subcommand
        swarm                  # start daemon + open web UI
    """
    # Defer full logging setup -- store CLI overrides on context so
    # subcommands (start, serve, etc.) can configure with the right mode.
    ctx.ensure_object(dict)
    ctx.obj["log_level"] = log_level
    ctx.obj["log_file"] = log_file

    # No subcommand -> open the dashboard
    if ctx.invoked_subcommand is None:
        ctx.invoke(start_cmd)
    else:
        # Non-start commands: stderr + file (serve reconfigures with config values)
        setup_logging(level=log_level, log_file=log_file, stderr=True)


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
@click.option("--skip-hooks", is_flag=True, help="Skip Claude Code hooks installation")
@click.option("--skip-config", is_flag=True, help="Skip swarm.yaml generation")
def init(  # noqa: C901
    projects_dir: str | None,
    output_path: str,
    skip_hooks: bool,
    skip_config: bool,
) -> None:
    """Set up swarm: Claude Code hooks and swarm.yaml.

    On a fresh install, this ensures everything is ready to go.
    """
    import shutil

    checks: list[tuple[str, bool]] = []

    # --- Step 1: Install Claude Code hooks ---
    if not skip_hooks:
        from swarm.hooks.install import install

        install(global_install=True)
        click.echo("  Claude Code hooks installed globally")
        checks.append(("Claude Code hooks", True))
    else:
        click.echo("  Skipping hooks (--skip-hooks)")
        checks.append(("Claude Code hooks", None))

    # --- Step 2: Generate swarm.yaml ---
    if not skip_config:
        from swarm.config import discover_projects, write_config

        out_file = Path(output_path)
        ported_settings: dict | None = None

        # Check for existing config
        if out_file.exists():
            click.echo(f"\n  Existing config found: {output_path}")
            choice = (
                click.prompt(
                    "  [k]eep existing / [p]ort settings / [f]resh start",
                    default="p",
                    show_default=False,
                )
                .strip()
                .lower()
            )

            if choice.startswith("k"):
                click.echo("  Keeping existing config.")
                checks.append(("swarm.yaml generated", True))
                skip_config = True
            else:
                # Back up before any changes
                backup_path = out_file.with_suffix(".yaml.bak")
                shutil.copy2(str(out_file), str(backup_path))
                click.echo(f"  Backed up existing config to {backup_path}")

                if choice.startswith("p"):
                    import yaml as _yaml

                    try:
                        old_data = _yaml.safe_load(out_file.read_text()) or {}
                        ported_settings = {
                            k: v
                            for k, v in old_data.items()
                            if k not in {"workers", "groups", "projects_dir"}
                        }
                        click.echo("  Porting settings from existing config.")
                    except Exception:
                        click.echo("  Could not parse existing config -- starting fresh.")
                        ported_settings = None

        if not skip_config:
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
                    if len(workers) > 1 and click.confirm(
                        "\n  Define custom groups?", default=False
                    ):
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

                    # Ask for API password (skip if ported from existing config)
                    api_password: str | None = None
                    if not (ported_settings and ported_settings.get("api_password")):
                        click.echo("\n  API password protects config changes in the web dashboard.")
                        pw = click.prompt(
                            "  Set API password (Enter to skip)",
                            default="",
                            show_default=False,
                            hide_input=True,
                        ).strip()
                        if pw:
                            api_password = pw

                    write_config(
                        output_path,
                        workers,
                        groups,
                        str(scan_dir),
                        api_password=api_password,
                        ported_settings=ported_settings,
                    )
                    click.echo(f"\n  Wrote {output_path} with {len(workers)} workers")
                    if ported_settings:
                        click.echo("  Settings ported from previous config.")
                    checks.append(("swarm.yaml generated", True))
                else:
                    click.echo("  No workers selected")
                    checks.append(("swarm.yaml generated", False))
    else:
        click.echo("  Skipping swarm.yaml (--skip-config)")
        checks.append(("swarm.yaml generated", None))

    # --- Step 3: Install systemd service ---
    from swarm.service import (
        _PLIST_PATH,
        _SERVICE_PATH,
        _check_systemd,
        enable_wsl_systemd,
        install_launchd,
        install_service as _install_svc,
        is_macos,
        is_wsl,
    )

    systemd_err = _check_systemd()
    if systemd_err and is_wsl():
        click.echo("\n  systemd is not enabled in WSL.")
        if click.confirm("  Enable systemd in /etc/wsl.conf? (requires sudo)", default=True):
            try:
                enable_wsl_systemd()
                click.echo("  Enabled! Restart WSL to activate: wsl --shutdown")
                checks.append(("systemd service", "RESTART"))
            except Exception as e:
                click.echo(f"  Failed: {e}", err=True)
                checks.append(("systemd service", False))
        else:
            checks.append(("systemd service", None))
    elif systemd_err and is_macos():
        if _PLIST_PATH.exists():
            checks.append(("launchd service", True))
        else:
            try:
                install_launchd(output_path if not skip_config else None)
                checks.append(("launchd service", True))
            except Exception:
                checks.append(("launchd service", False))
    elif systemd_err:
        click.echo(f"  {systemd_err}")
        checks.append(("background service", None))
    elif _SERVICE_PATH.exists():
        checks.append(("systemd service", True))
    else:
        try:
            _install_svc(output_path if not skip_config else None)
            checks.append(("systemd service", True))
        except Exception:
            checks.append(("systemd service", False))

    # --- Step 4: WSL auto-start on Windows boot ---
    from swarm.service import install_wsl_startup, wsl_startup_installed

    if is_wsl():
        if wsl_startup_installed():
            checks.append(("WSL auto-start", True))
        else:
            try:
                vbs = install_wsl_startup()
                checks.append(("WSL auto-start", vbs is not None))
            except Exception:
                checks.append(("WSL auto-start", False))
    else:
        checks.append(("WSL auto-start", None))

    # --- Summary ---
    click.echo("\n  System readiness:")
    needs_restart = False
    for label, status in checks:
        if status is True:
            indicator = "OK"
        elif status is False:
            indicator = "FAIL"
        elif status == "RESTART":
            indicator = "PEND"
            needs_restart = True
        else:
            indicator = "SKIP"
        click.echo(f"    [{indicator:4s}] {label}")

    if needs_restart:
        click.echo("\n  Restart WSL (wsl --shutdown) then re-run: swarm init")
    all_ok = all(s is not False for _, s in checks)
    if all_ok and not needs_restart:
        click.echo("\n  Ready! Next: swarm start all")
    elif not all_ok:
        click.echo("\n  Some checks failed -- see above.", err=True)


def _show_available(cfg: HiveConfig) -> None:
    """Print available groups and workers for interactive selection."""
    num_groups = len(cfg.groups)
    click.echo("Groups:")
    for i, g in enumerate(cfg.groups):
        members = ", ".join(g.workers)
        click.echo(f"  [{i + 1:2d}] {g.name:20s} {members}")
    click.echo("\nIndividual workers:")
    for i, w in enumerate(cfg.workers):
        click.echo(f"  [{num_groups + i + 1:2d}] {w.name}")
    click.echo("\nUsage: swarm <name|number> or swarm start -a")


def _resolve_launch_workers(
    cfg: HiveConfig, group: str | None, launch_all: bool
) -> list[str] | None:
    """Resolve which workers to launch. Returns names or None (show help)."""
    if launch_all:
        return [w.name for w in cfg.workers]
    target = group or cfg.default_group
    if not target:
        return None
    _name, resolved = _resolve_target(cfg, target)
    if resolved is None:
        label = "default_group" if not group else "group or worker"
        click.echo(f"Unknown {label}: '{target}'\n")
        _show_available(cfg)
        return None
    return [w.name for w in resolved]


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
@click.option("--port", default=None, type=int, help="Daemon API port (default: config or 9090)")
def launch(group: str | None, config_path: str | None, launch_all: bool, port: int | None) -> None:
    """Launch workers via the running daemon."""
    cfg = load_config(config_path)
    errors = cfg.validate()
    if errors:
        for e in errors:
            click.echo(f"Config error: {e}", err=True)
        raise SystemExit(1)

    worker_names = _resolve_launch_workers(cfg, group, launch_all)
    if worker_names is None:
        _show_available(cfg)
        return

    api_port = port or cfg.port

    async def _launch() -> None:
        try:
            data = await _api_post(api_port, "/api/workers/launch", {"workers": worker_names})
            launched = data.get("launched", [])
            if launched:
                click.echo(f"Launched {len(launched)} worker(s): {', '.join(launched)}")
            else:
                click.echo("No new workers to launch (already running).")
        except Exception as e:
            click.echo(f"Cannot reach daemon at localhost:{api_port}: {e}", err=True)
            click.echo("Is the daemon running? Start it with: swarm start", err=True)
            raise SystemExit(1)

    asyncio.run(_launch())


def _resolve_target(cfg: HiveConfig, target: str) -> tuple[str, list | None]:
    """Resolve a target as group name, worker name, or number.

    Returns (session_name, workers) if resolved, or (target, None) if not found.
    """
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
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=True),
    help="Path to swarm.yaml",
)
@click.option("--host", default="localhost", help="Host to bind to")
@click.option("--port", default=None, type=int, help="Port to serve on (default: config or 9090)")
@click.pass_context
def serve(ctx: click.Context, config_path: str | None, host: str, port: int | None) -> None:
    """Serve the Bee Hive web dashboard."""
    from swarm.server.daemon import run_daemon

    cfg = load_config(config_path)

    if os.environ.get("SWARM_DEV"):
        from swarm.update import get_local_source_path

        source = get_local_source_path()
        if source:
            click.echo(f"SWARM_DEV detected — switching to dev mode from {source}")
            os.chdir(source)
            os.execvp("uv", ["uv", "run", "swarm"] + sys.argv[1:])

    port = port or cfg.port

    # Re-configure logging with config values (stderr stays on for serve)
    cli_obj = ctx.obj or {}
    log_level = cli_obj.get("log_level") or cfg.log_level
    log_file = cli_obj.get("log_file") or cfg.log_file
    setup_logging(level=log_level, log_file=log_file, stderr=True)

    asyncio.run(run_daemon(cfg, host=host, port=port))


@main.command("start")
@click.argument("target", required=False)
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=True),
    help="Path to swarm.yaml",
)
@click.option("--host", default="0.0.0.0", help="Host to bind to (default: all interfaces)")
@click.option("--port", default=None, type=int, help="Port to serve on (default: config or 9090)")
@click.option("--no-browser", is_flag=True, help="Don't auto-open the browser")
@click.option("--test", "test_mode", is_flag=True, help="Run in test mode with synthetic project")
@click.pass_context
def start_cmd(  # noqa: C901
    ctx: click.Context,
    target: str | None,
    config_path: str | None,
    host: str,
    port: int | None,
    no_browser: bool,
    test_mode: bool,
) -> None:
    """Launch workers and open the web dashboard.

    TARGET can be a group name, worker name, or number.
    Workers are launched by the daemon automatically.

    \b
    Examples:
        swarm                  # start daemon, open web UI
        swarm rcg-v6           # launch 'rcg-v6' group, open web UI
        swarm start default    # explicit 'start' subcommand
    """
    import webbrowser

    from swarm.server.daemon import run_daemon

    cfg = load_config(config_path)

    if os.environ.get("SWARM_DEV"):
        from swarm.update import get_local_source_path

        source = get_local_source_path()
        if source:
            click.echo(f"SWARM_DEV detected — switching to dev mode from {source}")
            os.chdir(source)
            os.execvp("uv", ["uv", "run", "swarm"] + sys.argv[1:])

    port = port or cfg.port

    cli_obj = ctx.obj or {}
    log_level = cli_obj.get("log_level") or cfg.log_level
    log_file = cli_obj.get("log_file") or cfg.log_file
    setup_logging(level=log_level, log_file=log_file, stderr=True)

    if target:
        session_name, workers = _resolve_target(cfg, target)
        if workers is not None:
            errors = cfg.validate()
            if errors:
                for e in errors:
                    click.echo(f"Config error: {e}", err=True)
                raise SystemExit(1)
            cfg.session_name = session_name
        else:
            # Unresolved target -- use it as session name (daemon will handle)
            cfg.session_name = target
    elif cfg.default_group:
        session_name, workers = _resolve_target(cfg, cfg.default_group)
        if workers is not None:
            cfg.session_name = session_name
        else:
            cfg.session_name = cfg.default_group

    # --- Test mode setup ---
    test_project_mgr = None
    if test_mode:
        test_project_mgr, project_dir = _setup_test_config(cfg)
        click.echo(f"TEST MODE: synthetic project at {project_dir}")

    url = f"http://{host}:{port}"
    if not no_browser:

        def _open_browser() -> None:
            import subprocess
            import time

            time.sleep(0.8)  # let the server start
            try:
                subprocess.Popen(
                    ["xdg-open", url],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except FileNotFoundError:
                webbrowser.open(url)

        import threading

        threading.Thread(target=_open_browser, daemon=True).start()

    try:
        asyncio.run(run_daemon(cfg, host=host, port=port, test_mode=test_mode))
    finally:
        if test_project_mgr:
            test_project_mgr.cleanup()


def _setup_test_config(cfg: HiveConfig) -> tuple:
    """Set up test mode: create synthetic project, override config for single test worker.

    Returns (TestProjectManager, project_dir).
    """
    from swarm.config import GroupConfig, WorkerConfig
    from swarm.testing.project import TestProjectManager

    mgr = TestProjectManager()
    try:
        project_dir = mgr.setup()
    except FileNotFoundError as e:
        click.echo(f"Test mode error: {e}", err=True)
        raise SystemExit(1)

    cfg.workers = [WorkerConfig(name="test-worker", path=str(project_dir))]
    cfg.groups = [GroupConfig(name="all", workers=["test-worker"])]
    cfg.test.enabled = True
    cfg.drones.auto_stop_on_complete = True
    return mgr, project_dir


@main.command("test")
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=True),
    help="Path to swarm.yaml",
)
@click.option(
    "--port",
    default=None,
    type=int,
    help="Port for test dashboard (default: config test.port or 9091)",
)
@click.option(
    "--timeout", default=300, type=int, help="Max seconds before auto-shutdown (default: 300)"
)
@click.option("--no-cleanup", is_flag=True, help="Keep temp dir after test")
@click.pass_context
def test_cmd(
    ctx: click.Context,
    config_path: str | None,
    port: int | None,
    timeout: int,
    no_cleanup: bool,
) -> None:
    """Run orchestration tests on a dedicated port with auto-shutdown.

    Launches a synthetic test project, runs the daemon on a side port (default 9091),
    monitors progress, generates a report, and exits.

    \b
    Examples:
        swarm test                    # run on :9091 with 5min timeout
        swarm test --port 9092        # custom port
        swarm test --timeout 120      # 2min timeout
        swarm test --no-cleanup       # keep temp dir after test
    """
    import uuid

    from swarm.server.daemon import run_test_daemon

    cfg = load_config(config_path)
    port = port or cfg.test.port

    cli_obj = ctx.obj or {}
    log_level = cli_obj.get("log_level") or cfg.log_level
    log_file = cli_obj.get("log_file") or cfg.log_file
    setup_logging(level=log_level, log_file=log_file, stderr=True)

    session_name = f"swarm-test-{uuid.uuid4().hex[:8]}"
    cfg.session_name = session_name

    test_project_mgr, project_dir = _setup_test_config(cfg)
    click.echo(f"TEST: synthetic project at {project_dir}")
    click.echo(f"TEST: session={session_name}, port={port}, timeout={timeout}s")

    report_path = None
    exit_code = 0
    try:
        report_path = asyncio.run(run_test_daemon(cfg, host="0.0.0.0", port=port, timeout=timeout))
    except KeyboardInterrupt:
        click.echo("\nTest interrupted by user")
        exit_code = 1
    except TimeoutError:
        click.echo(f"\nTest timed out after {timeout}s")
        exit_code = 2

    if report_path:
        _print_report_summary(report_path)
    else:
        click.echo("No test report generated.")

    if not no_cleanup:
        _cleanup_test(test_project_mgr)
    else:
        click.echo(f"Skipping cleanup (--no-cleanup). Session: {session_name}")

    raise SystemExit(exit_code)


def _cleanup_test(mgr: object) -> None:
    """Clean up temp directory and test tasks."""
    if hasattr(mgr, "cleanup"):
        mgr.cleanup()
    # Remove isolated test task board so it doesn't accumulate stale tasks
    test_tasks_path = Path.home() / ".swarm" / "test-tasks.json"
    test_tasks_path.unlink(missing_ok=True)


def _print_report_summary(path: Path) -> None:
    """Print the Summary section from a markdown test report."""
    click.echo(f"\nReport: {path}")
    try:
        text = path.read_text()
    except OSError:
        return
    # Extract ## Summary section
    in_summary = False
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("## Summary"):
            in_summary = True
            continue
        if in_summary:
            if line.startswith("## "):
                break
            lines.append(line)
    if lines:
        click.echo("\n".join(lines).strip())


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
@click.option("--host", default="0.0.0.0", help="Host to bind to (default: all interfaces)")
@click.option("--port", default=None, type=int, help="Port to serve on (default: config or 9090)")
def start(config_path: str | None, host: str, port: int | None) -> None:
    """Start the web dashboard in the background."""
    from swarm.server.webctl import web_start

    cfg = load_config(config_path)
    port = port or cfg.port
    ok, msg = web_start(host=host, port=port, config_path=config_path)
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
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=True),
    help="Path to swarm.yaml",
)
@click.option("--port", default=None, type=int, help="Daemon API port (default: config or 9090)")
def status(config_path: str | None, port: int | None) -> None:
    """One-shot status check of all workers via daemon API."""
    cfg = load_config(config_path)
    api_port = port or cfg.port

    async def _status() -> None:
        try:
            data = await _api_get(api_port, "/api/workers")
        except Exception as e:
            click.echo(f"Cannot reach daemon at localhost:{api_port}: {e}", err=True)
            click.echo("Is the daemon running? Start it with: swarm start", err=True)
            raise SystemExit(1)

        workers = data.get("workers", [])
        if not workers:
            click.echo("No workers registered with the daemon.")
            return

        # State indicators matching WorkerState
        indicators = {
            "buzzing": ".",
            "waiting": "?",
            "resting": "~",
            "sleeping": "z",
            "stung": "!",
        }

        for w in workers:
            name = w.get("name", "?")
            state = w.get("state", "unknown").lower()
            indicator = indicators.get(state, " ")
            click.echo(f"  {indicator} {name:20s} [{state}]")

    asyncio.run(_status())


@main.command("check-states")
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=True),
    help="Path to swarm.yaml",
)
@click.option("--port", default=None, type=int, help="Daemon API port (default: config or 9090)")
def check_states(config_path: str | None, port: int | None) -> None:
    """Show current worker states from the daemon.

    With PTY-based process management, state is always fresh (read from the
    ring buffer), so this command simply displays the current states.
    """
    cfg = load_config(config_path)
    api_port = port or cfg.port

    async def _check_states() -> None:
        try:
            data = await _api_get(api_port, "/api/workers")
        except Exception as e:
            click.echo(f"Cannot reach daemon at localhost:{api_port}: {e}", err=True)
            click.echo("Is the daemon running? Start it with: swarm start", err=True)
            raise SystemExit(1)

        workers = data.get("workers", [])
        if not workers:
            click.echo("No workers registered with the daemon.")
            return

        # Print table header
        click.echo(f"{'Worker':<20s} {'State':<12s} {'Duration'}")
        for w in workers:
            name = w.get("name", "?")
            state = w.get("state", "unknown")
            duration = w.get("state_duration", 0)
            click.echo(f"{name:<20s} {state:<12s} {duration:.1f}s")

    asyncio.run(_check_states())


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
            click.echo(f"  x {e}", err=True)
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
@click.option("--port", default=None, type=int, help="Daemon API port (default: config or 9090)")
def send(target: str, message: str, config_path: str | None, port: int | None) -> None:
    """Send a message to a worker, group, or all.

    TARGET is a worker name, group name, or 'all'.
    MESSAGE is the text to send.
    """
    cfg = load_config(config_path)
    api_port = port or cfg.port

    async def _send() -> None:
        try:
            data = await _api_get(api_port, "/api/workers")
        except Exception as e:
            click.echo(f"Cannot reach daemon at localhost:{api_port}: {e}", err=True)
            click.echo("Is the daemon running? Start it with: swarm start", err=True)
            raise SystemExit(1)

        workers = data.get("workers", [])
        if not workers:
            click.echo("No workers registered with the daemon.")
            return

        worker_names = [w.get("name", "") for w in workers]

        # Resolve targets
        if target.lower() == "all":
            targets = worker_names
        else:
            # Try as group name first
            try:
                group_workers = cfg.get_group(target)
                group_names = {w.name.lower() for w in group_workers}
                targets = [n for n in worker_names if n.lower() in group_names]
            except ValueError:
                # Try as worker name
                targets = [n for n in worker_names if n.lower() == target.lower()]

        if not targets:
            click.echo(f"No matching workers for '{target}'")
            return

        for name in targets:
            try:
                await _api_post(api_port, f"/api/workers/{name}/send", {"message": message})
                click.echo(f"  Sent to {name}")
            except Exception as e:
                click.echo(f"  Failed to send to {name}: {e}", err=True)

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
@click.option("--port", default=None, type=int, help="Daemon API port (default: config or 9090)")
def kill(worker_name: str, config_path: str | None, port: int | None) -> None:
    """Kill a worker process."""
    cfg = load_config(config_path)
    api_port = port or cfg.port

    async def _kill() -> None:
        try:
            await _api_post(api_port, f"/api/workers/{worker_name}/kill")
            click.echo(f"Killed worker: {worker_name}")
        except Exception as e:
            click.echo(f"Failed to kill worker '{worker_name}': {e}", err=True)
            raise SystemExit(1)

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
                " -- create from the web dashboard or use 'swarm tasks create')"
            )
            return
        for t in all_tasks:
            assigned = f" -> {t.assigned_worker}" if t.assigned_worker else ""
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
            click.echo(f"Assigned task [{task_id}] -> {worker}")
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
@click.option(
    "--port", default=None, type=int, help="Port for the API server (default: config or 9090)"
)
def daemon(config_path: str | None, host: str, port: int | None) -> None:
    """Run the swarm as a background daemon with REST + WebSocket API."""
    from swarm.server.daemon import run_daemon

    cfg = load_config(config_path)
    port = port or cfg.port

    asyncio.run(run_daemon(cfg, host=host, port=port))


@main.command()
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=True),
    help="Path to swarm.yaml",
)
@click.option(
    "--port", default=None, type=int, help="Local port to tunnel (default: config or 9090)"
)
def tunnel(config_path: str | None, port: int | None) -> None:
    """Start a Cloudflare Tunnel for remote access.

    Prints the public HTTPS URL for accessing the dashboard from a phone.
    Press Ctrl-C to stop.
    """
    import shutil

    if not shutil.which("cloudflared"):
        click.echo(
            "cloudflared is not installed.\n"
            "Install: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/",
            err=True,
        )
        raise SystemExit(1)

    cfg = load_config(config_path)
    port = port or cfg.port

    async def _run_tunnel() -> None:
        from swarm.tunnel import TunnelManager

        mgr = TunnelManager(port=port)
        try:
            url = await mgr.start()
        except RuntimeError as e:
            click.echo(f"Error: {e}", err=True)
            raise SystemExit(1)

        click.echo(f"\n  Tunnel active: {url}")
        click.echo(f"  Proxying to:   http://localhost:{port}")
        click.echo("  Press Ctrl-C to stop\n")

        # Wait until interrupted
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        finally:
            await mgr.stop()
            click.echo("Tunnel stopped")

    try:
        asyncio.run(_run_tunnel())
    except KeyboardInterrupt:
        pass


@main.command("install-hooks")
@click.option("--global", "global_install", is_flag=True, help="Install hooks globally")
def install_hooks(global_install: bool) -> None:
    """Install auto-approval hooks for Claude Code."""
    from swarm.hooks.install import install

    install(global_install=global_install)
    scope = "globally" if global_install else "for this project"
    click.echo(f"Hooks installed {scope}")


@main.command("install-service")
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=True),
    help="Path to swarm.yaml",
)
@click.option("--uninstall", is_flag=True, help="Remove the background service")
def install_service_cmd(config_path: str | None, uninstall: bool) -> None:
    """Install (or remove) a background service so swarm starts on login.

    Uses systemd on Linux/WSL and launchd on macOS.
    """
    from swarm.service import is_macos

    if is_macos():
        from swarm.service import install_launchd, launchd_status, uninstall_launchd

        if uninstall:
            removed = uninstall_launchd()
            if removed:
                click.echo("Swarm Launch Agent removed.")
            else:
                click.echo("No plist file found -- nothing to remove.")
            return

        try:
            path = install_launchd(config_path)
        except (RuntimeError, FileNotFoundError) as e:
            click.echo(str(e), err=True)
            raise SystemExit(1)

        click.echo(f"Service installed: {path}")
        click.echo(launchd_status())
        click.echo("\nThe swarm dashboard will now start automatically on login.")
        click.echo("  Status:    launchctl list com.swarm.dashboard")
        click.echo("  Logs:      tail -f ~/.swarm/launchd-stderr.log")
        click.echo("  Uninstall: swarm install-service --uninstall")
    else:
        from swarm.service import install_service, service_status, uninstall_service

        if uninstall:
            removed = uninstall_service()
            if removed:
                click.echo("Swarm service removed.")
            else:
                click.echo("No service file found -- nothing to remove.")
            return

        try:
            path = install_service(config_path)
        except (RuntimeError, FileNotFoundError) as e:
            click.echo(str(e), err=True)
            raise SystemExit(1)

        click.echo(f"Service installed: {path}")
        click.echo(service_status())
        click.echo("\nThe swarm dashboard will now start automatically on login.")
        click.echo("  Status:    systemctl --user status swarm")
        click.echo("  Logs:      journalctl --user -u swarm -f")
        click.echo("  Uninstall: swarm install-service --uninstall")


@main.command()
@click.option("--check", "check_only", is_flag=True, help="Check only, don't install")
def update(check_only: bool) -> None:
    """Check for and install updates from GitHub."""
    from swarm.update import check_for_update, perform_update

    result = asyncio.run(check_for_update(force=True))
    if result.error:
        click.echo(f"Update check failed: {result.error}", err=True)
        raise SystemExit(1)

    click.echo(f"  Installed: {result.current_version}")
    click.echo(f"  Latest:    {result.remote_version}")
    if result.commit_sha:
        click.echo(f"  Commit:    {result.commit_sha} -- {result.commit_message}")

    if not result.available:
        click.echo("\n  Already up to date.")
        return

    click.echo(f"\n  Update available: {result.current_version} -> {result.remote_version}")

    if check_only:
        return

    if not click.confirm("  Install update?", default=True):
        return

    click.echo("  Updating...")
    success, output = asyncio.run(perform_update())
    if success:
        click.echo("  Update installed successfully.")
        click.echo("  Restart any running swarm processes to use the new version.")
    else:
        click.echo(f"  Update failed:\n{output}", err=True)
        raise SystemExit(1)
