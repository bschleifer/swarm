from __future__ import annotations

import json
from pathlib import Path

from swarm.hooks.install import install


def test_install_local_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    install(global_install=False)
    settings_path = tmp_path / ".claude" / "settings.json"
    assert settings_path.exists()
    settings = json.loads(settings_path.read_text())
    assert "permissions" in settings
    allow = settings["permissions"]["allow"]
    assert "Edit" in allow
    assert "Write" in allow
    assert "WebFetch" in allow
    assert "WebSearch" in allow


def test_install_global_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    install(global_install=True)
    settings_path = tmp_path / ".claude" / "settings.json"
    assert settings_path.exists()
    settings = json.loads(settings_path.read_text())
    assert "permissions" in settings
    assert "Edit" in settings["permissions"]["allow"]


def test_install_creates_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    assert not (tmp_path / ".claude").exists()
    install(global_install=False)
    assert (tmp_path / ".claude").exists()
    assert (tmp_path / ".claude" / "settings.json").exists()


def test_install_preserves_existing_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    existing = {
        "editor": "vim",
        "theme": "dark",
        "hooks": {
            "PostToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo done"}]}
            ]
        },
    }
    settings_path.write_text(json.dumps(existing, indent=2))
    install(global_install=False)
    settings = json.loads(settings_path.read_text())
    assert settings["editor"] == "vim"
    assert settings["theme"] == "dark"
    assert "PostToolUse" in settings["hooks"]
    assert "Edit" in settings["permissions"]["allow"]


def test_install_avoids_duplicate_permissions(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    existing = {
        "permissions": {
            "allow": ["Edit", "Write", "WebFetch", "WebSearch"],
        }
    }
    settings_path.write_text(json.dumps(existing, indent=2))
    install(global_install=False)
    settings = json.loads(settings_path.read_text())
    assert settings["permissions"]["allow"].count("Edit") == 1
    assert len(settings["permissions"]["allow"]) == 4


def test_install_twice_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    install(global_install=False)
    install(global_install=False)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text())
    assert settings["permissions"]["allow"].count("Edit") == 1
    assert len(settings["permissions"]["allow"]) == 4


def test_install_merges_with_existing_permissions(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    existing = {
        "permissions": {
            "allow": ["Bash(git status:*)"],
        }
    }
    settings_path.write_text(json.dumps(existing, indent=2))
    install(global_install=False)
    settings = json.loads(settings_path.read_text())
    allow = settings["permissions"]["allow"]
    assert "Bash(git status:*)" in allow
    assert "Edit" in allow
    assert "Write" in allow
    assert len(allow) == 5


def test_install_json_format(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    install(global_install=False)
    settings_path = tmp_path / ".claude" / "settings.json"
    content = settings_path.read_text()
    assert content.endswith("\n")
    parsed = json.loads(content)
    assert isinstance(parsed, dict)


def test_install_removes_legacy_hook(tmp_path, monkeypatch):
    """install() removes the old broken PreToolUse auto-allow hook."""
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    existing = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Read|Edit|Write|Glob|Grep|WebSearch|WebFetch",
                    "hooks": [{"type": "command", "command": 'echo \'{"decision": "allow"}\''}],
                },
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]},
            ]
        }
    }
    settings_path.write_text(json.dumps(existing, indent=2))
    install(global_install=False)
    settings = json.loads(settings_path.read_text())
    # Legacy hook removed, Bash hook preserved, new approval hook added
    pre_tool = settings["hooks"]["PreToolUse"]
    matchers = [m.get("matcher") for m in pre_tool]
    assert "Bash" in matchers
    assert "Read|Edit|Write|Glob|Grep|WebSearch|WebFetch" not in matchers
    # Approval hook present (no matcher)
    assert any(
        any(h.get("command", "").endswith("approval-hook.sh") for h in m.get("hooks", []))
        for m in pre_tool
    )
    # Permissions added
    assert "Edit" in settings["permissions"]["allow"]


def test_install_removes_legacy_hook_cleans_empty(tmp_path, monkeypatch):
    """install() cleans up legacy PreToolUse hook; PostToolUse cross-task hook remains."""
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    existing = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Read|Edit|Write|Glob|Grep|WebSearch|WebFetch",
                    "hooks": [{"type": "command", "command": 'echo \'{"decision": "allow"}\''}],
                }
            ]
        }
    }
    settings_path.write_text(json.dumps(existing, indent=2))
    install(global_install=False)
    settings = json.loads(settings_path.read_text())
    # Legacy PreToolUse removed but new approval hook added
    pre_tool = settings["hooks"].get("PreToolUse", [])
    assert not any(
        m.get("matcher") == "Read|Edit|Write|Glob|Grep|WebSearch|WebFetch" for m in pre_tool
    )
    # Approval hook is present
    assert any(
        any(h.get("command", "").endswith("approval-hook.sh") for h in m.get("hooks", []))
        for m in pre_tool
    )
    # PostToolUse hooks removed (replaced by MCP tools)
    assert "PostToolUse" not in settings.get("hooks", {})
    assert "Edit" in settings["permissions"]["allow"]


# --- uninstall tests ---


def test_uninstall_removes_swarm_permissions(tmp_path, monkeypatch):
    """uninstall() removes swarm-installed permissions."""
    from swarm.hooks.install import uninstall

    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    install(global_install=False)
    settings_path = tmp_path / ".claude" / "settings.json"
    assert "Edit" in json.loads(settings_path.read_text())["permissions"]["allow"]

    uninstall(global_install=False)
    settings = json.loads(settings_path.read_text())
    assert "permissions" not in settings


def test_uninstall_no_settings_file(tmp_path, monkeypatch):
    """uninstall() is a no-op when settings file doesn't exist."""
    from swarm.hooks.install import uninstall

    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    uninstall(global_install=False)
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_uninstall_preserves_other_permissions(tmp_path, monkeypatch):
    """uninstall() keeps non-swarm permissions intact."""
    from swarm.hooks.install import uninstall

    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    existing = {
        "editor": "vim",
        "permissions": {
            "allow": ["Edit", "Write", "WebFetch", "WebSearch", "Bash(git status:*)"],
        },
    }
    settings_path.write_text(json.dumps(existing, indent=2))
    uninstall(global_install=False)
    settings = json.loads(settings_path.read_text())
    assert settings["editor"] == "vim"
    assert settings["permissions"]["allow"] == ["Bash(git status:*)"]


def test_uninstall_corrupt_json(tmp_path, monkeypatch):
    """uninstall() silently returns on corrupt JSON."""
    from swarm.hooks.install import uninstall

    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text("{corrupt json!!!")
    uninstall(global_install=False)
    assert settings_path.read_text() == "{corrupt json!!!"


def test_uninstall_empty_permissions(tmp_path, monkeypatch):
    """uninstall() handles settings with empty permissions."""
    from swarm.hooks.install import uninstall

    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"permissions": {}}))
    uninstall(global_install=False)
    settings = json.loads(settings_path.read_text())
    assert settings == {"permissions": {}}


# ---------------------------------------------------------------------------
# Sandbox opt-in
# ---------------------------------------------------------------------------


class _FakeSandbox:
    def __init__(self, enabled=True, min_version="2.0", overrides=None):
        self.enabled = enabled
        self.min_claude_version = min_version
        self.settings_overrides = overrides or {}


def _patch_claude_version(monkeypatch, version_str):
    """Stub subprocess.run so _claude_version_at_least sees *version_str*."""
    from subprocess import CompletedProcess

    def fake_run(cmd, **kwargs):
        if cmd and cmd[0] == "claude":
            return CompletedProcess(cmd, 0, stdout=version_str, stderr="")
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr("swarm.hooks.install.subprocess.run", fake_run)


def test_install_sandbox_disabled_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    sandbox = _FakeSandbox(enabled=False, overrides={"allow_network": True})
    install(global_install=False, sandbox=sandbox)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert "sandbox" not in settings


def test_install_sandbox_enabled_with_supported_cc_writes_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    _patch_claude_version(monkeypatch, "2.1.3 (Claude Code)")
    sandbox = _FakeSandbox(
        enabled=True,
        min_version="2.0",
        overrides={"allow_network": False, "denied_tools": ["Bash"]},
    )
    install(global_install=False, sandbox=sandbox)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert settings["sandbox"] == {"allow_network": False, "denied_tools": ["Bash"]}


def test_install_sandbox_skipped_when_cc_too_old(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    _patch_claude_version(monkeypatch, "1.8.0 (Claude Code)")
    sandbox = _FakeSandbox(
        enabled=True,
        min_version="2.0",
        overrides={"allow_network": True},
    )
    install(global_install=False, sandbox=sandbox)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert "sandbox" not in settings


def test_install_sandbox_skipped_when_claude_not_installed(tmp_path, monkeypatch):
    """If ``claude`` isn't on PATH, stay on legacy flow instead of crashing."""
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)

    def not_found(*args, **kwargs):
        raise FileNotFoundError("claude")

    monkeypatch.setattr("swarm.hooks.install.subprocess.run", not_found)
    sandbox = _FakeSandbox(enabled=True, overrides={"allow_network": True})
    install(global_install=False, sandbox=sandbox)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert "sandbox" not in settings


def test_install_sandbox_merges_with_existing_sandbox_block(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    _patch_claude_version(monkeypatch, "2.0.0 (Claude Code)")
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"sandbox": {"allow_network": True, "legacy": "keep"}}))
    sandbox = _FakeSandbox(
        enabled=True, overrides={"allow_network": False, "denied_tools": ["Bash"]}
    )
    install(global_install=False, sandbox=sandbox)
    settings = json.loads(settings_path.read_text())
    # Merged: legacy preserved, allow_network overridden, denied_tools added
    assert settings["sandbox"]["legacy"] == "keep"
    assert settings["sandbox"]["allow_network"] is False
    assert settings["sandbox"]["denied_tools"] == ["Bash"]


def test_install_sandbox_empty_overrides_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    _patch_claude_version(monkeypatch, "2.1.0 (Claude Code)")
    sandbox = _FakeSandbox(enabled=True, overrides={})
    install(global_install=False, sandbox=sandbox)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert "sandbox" not in settings


class TestClaudeVersionParse:
    def test_parses_triple(self):
        from swarm.hooks.install import _parse_version

        assert _parse_version("1.2.3") == (1, 2, 3)

    def test_parses_pair_as_patch_zero(self):
        from swarm.hooks.install import _parse_version

        assert _parse_version("2.5") == (2, 5, 0)

    def test_ignores_prerelease(self):
        from swarm.hooks.install import _parse_version

        assert _parse_version("2.0.0-beta.3") == (2, 0, 0)

    def test_extracts_version_from_noise(self):
        from swarm.hooks.install import _parse_version

        assert _parse_version("1.4.1 (Claude Code)") == (1, 4, 1)

    def test_no_match_returns_none(self):
        from swarm.hooks.install import _parse_version

        assert _parse_version("no digits here") is None


class TestSandboxConfig:
    def test_defaults(self):
        from swarm.config import SandboxConfig

        c = SandboxConfig()
        assert c.enabled is False
        assert c.min_claude_version == "2.0"
        assert c.settings_overrides == {}

    def test_carries_overrides(self):
        from swarm.config import SandboxConfig

        c = SandboxConfig(enabled=True, settings_overrides={"allow_network": False})
        assert c.enabled is True
        assert c.settings_overrides == {"allow_network": False}


# ---------------------------------------------------------------------------
# Worker slash commands installer
# ---------------------------------------------------------------------------


def test_install_worker_commands_writes_all_six(tmp_path):
    """All six bundled command files land in <workdir>/.claude/commands/."""
    from swarm.hooks.install import WORKER_COMMAND_FILES, install_worker_commands

    n = install_worker_commands(tmp_path)

    commands_dir = tmp_path / ".claude" / "commands"
    assert commands_dir.is_dir()
    assert n == len(WORKER_COMMAND_FILES) == 6
    for fname in WORKER_COMMAND_FILES:
        assert (commands_dir / fname).is_file()


def test_install_worker_commands_creates_parent_dirs(tmp_path):
    """install_worker_commands creates the .claude/commands/ tree if missing."""
    from swarm.hooks.install import install_worker_commands

    assert not (tmp_path / ".claude").exists()
    install_worker_commands(tmp_path)
    assert (tmp_path / ".claude" / "commands").is_dir()


def test_install_worker_commands_idempotent(tmp_path):
    """Running twice does not duplicate or corrupt the installed files."""
    from swarm.hooks.install import WORKER_COMMAND_FILES, install_worker_commands

    install_worker_commands(tmp_path)
    install_worker_commands(tmp_path)

    commands_dir = tmp_path / ".claude" / "commands"
    files = sorted(p.name for p in commands_dir.iterdir())
    assert files == sorted(WORKER_COMMAND_FILES)


def test_install_worker_commands_overwrites_existing(tmp_path):
    """Re-running replaces hand-edited content so updates propagate."""
    from swarm.hooks.install import install_worker_commands

    commands_dir = tmp_path / ".claude" / "commands"
    commands_dir.mkdir(parents=True)
    stale = commands_dir / "swarm-status.md"
    stale.write_text("STALE CONTENT")

    install_worker_commands(tmp_path)

    assert stale.read_text() != "STALE CONTENT"
    assert "swarm_task_status" in stale.read_text()


def test_install_worker_commands_returns_zero_on_unreachable_dir(tmp_path, monkeypatch):
    """A workdir whose .claude/ cannot be created returns 0 instead of raising."""
    from swarm.hooks import install as install_module

    def boom(self, parents=False, exist_ok=False):
        raise OSError("read-only fs")

    monkeypatch.setattr(Path, "mkdir", boom)
    n = install_module.install_worker_commands(tmp_path)
    assert n == 0


def test_command_files_have_frontmatter():
    """Every bundled command starts with YAML frontmatter and a description."""
    from swarm.hooks.install import _COMMANDS_SRC_DIR, WORKER_COMMAND_FILES

    for fname in WORKER_COMMAND_FILES:
        body = (_COMMANDS_SRC_DIR / fname).read_text()
        assert body.startswith("---\n"), f"{fname} missing frontmatter open"
        assert "description:" in body.split("---", 2)[1], f"{fname} missing description"
