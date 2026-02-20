"""CLI entry point for the swarm command."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import click

from swarm.config import HiveConfig, load_config
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


class SwarmCLI(click.Group):
    """Route unknown subcommands as targets to ``start``."""

    def resolve_command(self, ctx: click.Context, args: list[str]) -> tuple:
        try:
            cmd_name, cmd, remaining = super().resolve_command(ctx, args)
            if cmd is not None:
                return cmd_name, cmd, remaining
        except click.UsageError:
            pass
        # Unknown command — treat first arg as a target for 'start'
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
    """Swarm — a hive-mind for Claude Code agents.

    \b
    Run with a target name to launch directly:
        swarm rcg-v6           # launch 'rcg-v6' group + web dashboard
        swarm start default    # explicit 'start' subcommand
        swarm                  # auto-detect session, open web UI
    """
    # Defer full logging setup — store CLI overrides on context so
    # subcommands (start, serve, etc.) can configure with the right mode.
    ctx.ensure_object(dict)
    ctx.obj["log_level"] = log_level
    ctx.obj["log_file"] = log_file

    # No subcommand → open the dashboard
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
                        click.echo("  Could not parse existing config — starting fresh.")
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

    # --- Step 5: Install systemd service ---
    from swarm.service import (
        _SERVICE_PATH,
        _check_systemd,
        enable_wsl_systemd,
        install_service as _install_svc,
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
    elif systemd_err:
        click.echo(f"  {systemd_err}")
        checks.append(("systemd service", None))
    elif _SERVICE_PATH.exists():
        checks.append(("systemd service", True))
    else:
        try:
            _install_svc(output_path if not skip_config else None)
            checks.append(("systemd service", True))
        except Exception:
            checks.append(("systemd service", False))

    # --- Step 6: WSL auto-start on Windows boot ---
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
        click.echo("\n  Ready! Next: swarm launch all")
    elif not all_ok:
        click.echo("\n  Some checks failed — see above.", err=True)


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
    click.echo("\nUsage: swarm <name|number> or swarm launch -a")


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
def launch(group: str | None, config_path: str | None, launch_all: bool) -> None:
    """Start workers in the hive."""
    _require_tmux()
    from swarm.config import WorkerConfig
    from swarm.worker.manager import launch_hive

    cfg = load_config(config_path)
    errors = cfg.validate()
    if errors:
        for e in errors:
            click.echo(f"Config error: {e}", err=True)
        raise SystemExit(1)

    session_name: str
    workers: list[WorkerConfig]

    if launch_all:
        session_name = cfg.session_name
        workers = cfg.workers
    elif group:
        session_name, resolved = _resolve_target(cfg, group)
        if resolved is None:
            click.echo(f"Unknown group or worker: '{group}'\n")
            _show_available(cfg)
            return
        workers = resolved
    elif cfg.default_group:
        session_name, resolved = _resolve_target(cfg, cfg.default_group)
        if resolved is None:
            click.echo(f"default_group '{cfg.default_group}' not found\n")
            _show_available(cfg)
            return
        workers = resolved
    else:
        _show_available(cfg)
        return

    asyncio.run(launch_hive(session_name, workers, panes_per_window=cfg.panes_per_window))
    click.echo(f"Hive launched: {len(workers)} workers in session '{session_name}'")
    click.echo(f"Attach with: tmux attach -t {session_name}")
    click.echo(f"Or run: swarm {session_name}")


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
@click.option("-s", "--session", default=None, help="tmux session name")
@click.pass_context
def serve(
    ctx: click.Context, config_path: str | None, host: str, port: int | None, session: str | None
) -> None:
    """Serve the Bee Hive web dashboard."""
    from swarm.server.daemon import run_daemon

    cfg = load_config(config_path)
    port = port or cfg.port

    # Re-configure logging with config values (stderr stays on for serve)
    cli_obj = ctx.obj or {}
    log_level = cli_obj.get("log_level") or cfg.log_level
    log_file = cli_obj.get("log_file") or cfg.log_file
    setup_logging(level=log_level, log_file=log_file, stderr=True)

    if session:
        cfg.session_name = session

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

    TARGET can be a group name, worker name, number, or tmux session name.
    If the workers aren't already running, they will be launched automatically.

    \b
    Examples:
        swarm                  # auto-detect session, open web UI
        swarm rcg-v6           # launch 'rcg-v6' group, open web UI
        swarm start default    # explicit subcommand form
        swarm start my-session # attach to existing tmux session
    """
    import webbrowser

    from swarm.server.daemon import run_daemon
    from swarm.tmux.hive import find_swarm_session, session_exists
    from swarm.worker.manager import launch_hive

    cfg = load_config(config_path)
    port = port or cfg.port

    cli_obj = ctx.obj or {}
    log_level = cli_obj.get("log_level") or cfg.log_level
    log_file = cli_obj.get("log_file") or cfg.log_file
    setup_logging(level=log_level, log_file=log_file, stderr=True)

    if target:
        if asyncio.run(session_exists(target)):
            cfg.session_name = target
        else:
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
                cfg.session_name = target
    elif cfg.default_group:
        session_name, workers = _resolve_target(cfg, cfg.default_group)
        if workers is not None and not asyncio.run(session_exists(session_name)):
            _require_tmux()
            errors = cfg.validate()
            if errors:
                for e in errors:
                    click.echo(f"Config error: {e}", err=True)
                raise SystemExit(1)
            asyncio.run(launch_hive(session_name, workers, panes_per_window=cfg.panes_per_window))
            click.echo(f"Launched {len(workers)} workers in session '{session_name}'")
            cfg.session_name = session_name
        else:
            cfg.session_name = session_name or cfg.default_group
    else:
        found = asyncio.run(find_swarm_session())
        if found and found != cfg.session_name:
            cfg.session_name = found

    # --- Test mode setup ---
    test_project_mgr = None
    if test_mode:
        test_project_mgr, project_dir = _setup_test_config(cfg)
        click.echo(f"TEST MODE: synthetic project at {project_dir}")

        # Launch workers for the test project
        _require_tmux()
        asyncio.run(
            launch_hive(cfg.session_name, cfg.workers, panes_per_window=cfg.panes_per_window)
        )

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
@click.option("--no-cleanup", is_flag=True, help="Keep tmux session and temp dir after test")
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
        swarm test --no-cleanup       # keep tmux session after test
    """
    import uuid

    from swarm.server.daemon import run_test_daemon
    from swarm.worker.manager import launch_hive

    _require_tmux()

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

    asyncio.run(launch_hive(session_name, cfg.workers, panes_per_window=cfg.panes_per_window))

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
        _cleanup_test(session_name, test_project_mgr)
    else:
        click.echo(f"Skipping cleanup (--no-cleanup). Session: {session_name}")

    raise SystemExit(exit_code)


def _cleanup_test(session: str, mgr: object) -> None:
    """Kill tmux test session and clean up temp directory + test tasks."""
    import subprocess

    try:
        subprocess.run(
            ["tmux", "kill-session", "-t", session],
            capture_output=True,
        )
    except FileNotFoundError:
        pass
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
@click.option("-s", "--session", default=None, help="tmux session name")
def start(config_path: str | None, host: str, port: int | None, session: str | None) -> None:
    """Start the web dashboard in the background."""
    from swarm.server.webctl import web_start

    cfg = load_config(config_path)
    port = port or cfg.port
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


@main.command("check-states")
@click.argument("session", required=False)
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=True),
    help="Path to swarm.yaml",
)
def check_states(session: str | None, config_path: str | None) -> None:
    """Compare stored tmux state vs fresh classifier output for each worker."""
    from swarm.tmux.cell import capture_pane, get_pane_command
    from swarm.worker.state import classify_pane_content

    cfg = load_config(config_path)

    async def _check_states() -> None:
        from swarm.tmux.hive import discover_workers, find_swarm_session

        target = session or cfg.session_name
        if not session:
            found = await find_swarm_session()
            if found:
                target = found

        workers = await discover_workers(target)
        if not workers:
            click.echo(f"No active hive found for session '{target}'")
            return

        # Gather data for each worker
        rows: list[tuple[str, str, str, bool, str]] = []
        for w in workers:
            stored = w.state.value
            cmd = await get_pane_command(w.pane_id)
            content = await capture_pane(w.pane_id)
            fresh = classify_pane_content(cmd, content).value
            match = stored == fresh
            rows.append((w.name, stored, fresh, match, content))

        # Print table header
        click.echo(f"{'Worker':<20s} {'Stored':<10s} {'Fresh':<10s} Match")
        for name, stored, fresh, match, _content in rows:
            mark = "\u2713" if match else "\u2717"
            click.echo(f"{name:<20s} {stored:<10s} {fresh:<10s} {mark}")

        # Print mismatch details
        mismatches = [(n, s, f, c) for n, s, f, m, c in rows if not m]
        if mismatches:
            click.echo()
            for name, stored, fresh, content in mismatches:
                click.echo(f"\u2717 {name}: stored={stored}, fresh={fresh}")
                click.echo("  Last 5 lines:")
                for line in content.strip().splitlines()[-5:]:
                    click.echo(f"  | {line}")

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
                " — create from the web dashboard or use 'swarm tasks create')"
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
@click.option(
    "--port", default=None, type=int, help="Port for the API server (default: config or 9090)"
)
@click.option("-s", "--session", default=None, help="tmux session name")
def daemon(config_path: str | None, host: str, port: int | None, session: str | None) -> None:
    """Run the swarm as a background daemon with REST + WebSocket API."""
    from swarm.server.daemon import run_daemon

    cfg = load_config(config_path)
    port = port or cfg.port
    if session:
        cfg.session_name = session

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
@click.option("--uninstall", is_flag=True, help="Remove the systemd service")
def install_service_cmd(config_path: str | None, uninstall: bool) -> None:
    """Install (or remove) a systemd user service so swarm starts on login."""
    from swarm.service import install_service, service_status, uninstall_service

    if uninstall:
        removed = uninstall_service()
        if removed:
            click.echo("Swarm service removed.")
        else:
            click.echo("No service file found — nothing to remove.")
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
        click.echo(f"  Commit:    {result.commit_sha} — {result.commit_message}")

    if not result.available:
        click.echo("\n  Already up to date.")
        return

    click.echo(f"\n  Update available: {result.current_version} → {result.remote_version}")

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
