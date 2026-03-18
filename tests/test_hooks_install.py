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
    # Legacy hook removed, Bash hook preserved
    assert len(settings["hooks"]["PreToolUse"]) == 1
    assert settings["hooks"]["PreToolUse"][0]["matcher"] == "Bash"
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
    # Legacy PreToolUse removed
    assert "PreToolUse" not in settings.get("hooks", {})
    # Cross-task PostToolUse hook installed
    assert "PostToolUse" in settings.get("hooks", {})
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
