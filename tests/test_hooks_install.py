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
    assert "hooks" in settings
    assert "PreToolUse" in settings["hooks"]
    assert len(settings["hooks"]["PreToolUse"]) == 1
    hook = settings["hooks"]["PreToolUse"][0]
    assert hook["matcher"] == "Read|Edit|Write|Glob|Grep|WebSearch|WebFetch"
    assert hook["hooks"][0]["type"] == "command"
    assert hook["hooks"][0]["command"] == 'echo \'{"decision": "allow"}\''


def test_install_global_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    install(global_install=True)
    settings_path = tmp_path / ".claude" / "settings.json"
    assert settings_path.exists()
    settings = json.loads(settings_path.read_text())
    assert "hooks" in settings
    assert "PreToolUse" in settings["hooks"]


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
    assert "PreToolUse" in settings["hooks"]
    assert len(settings["hooks"]["PostToolUse"]) == 1


def test_install_avoids_duplicate_matcher(tmp_path, monkeypatch):
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
    assert len(settings["hooks"]["PreToolUse"]) == 1


def test_install_twice_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    install(global_install=False)
    install(global_install=False)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text())
    assert len(settings["hooks"]["PreToolUse"]) == 1


def test_install_merges_with_different_matcher(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    existing = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo before"}]}
            ]
        }
    }
    settings_path.write_text(json.dumps(existing, indent=2))
    install(global_install=False)
    settings = json.loads(settings_path.read_text())
    assert len(settings["hooks"]["PreToolUse"]) == 2
    matchers = {h["matcher"] for h in settings["hooks"]["PreToolUse"]}
    assert "Bash" in matchers
    assert "Read|Edit|Write|Glob|Grep|WebSearch|WebFetch" in matchers


def test_install_json_format(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    install(global_install=False)
    settings_path = tmp_path / ".claude" / "settings.json"
    content = settings_path.read_text()
    assert content.endswith("\n")
    parsed = json.loads(content)
    assert isinstance(parsed, dict)


def test_install_empty_existing_hooks(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    existing = {"hooks": {}}
    settings_path.write_text(json.dumps(existing, indent=2))
    install(global_install=False)
    settings = json.loads(settings_path.read_text())
    assert "PreToolUse" in settings["hooks"]
    assert len(settings["hooks"]["PreToolUse"]) == 1


# --- uninstall tests ---


def test_uninstall_removes_swarm_hooks(tmp_path, monkeypatch):
    """uninstall() removes swarm-installed hooks and cleans up empty event keys."""
    from swarm.hooks.install import uninstall

    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    install(global_install=False)
    settings_path = tmp_path / ".claude" / "settings.json"
    # Verify hooks exist before uninstall
    assert "PreToolUse" in json.loads(settings_path.read_text())["hooks"]

    uninstall(global_install=False)
    settings = json.loads(settings_path.read_text())
    # hooks key removed entirely when no hooks remain
    assert "hooks" not in settings


def test_uninstall_no_settings_file(tmp_path, monkeypatch):
    """uninstall() is a no-op when settings file doesn't exist."""
    from swarm.hooks.install import uninstall

    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    # No settings file — should not raise
    uninstall(global_install=False)
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_uninstall_preserves_other_hooks(tmp_path, monkeypatch):
    """uninstall() keeps non-swarm hooks intact."""
    from swarm.hooks.install import uninstall

    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    existing = {
        "editor": "vim",
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Read|Edit|Write|Glob|Grep|WebSearch|WebFetch",
                    "hooks": [{"type": "command", "command": 'echo \'{"decision": "allow"}\''}],
                },
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]},
            ],
            "PostToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo done"}]}
            ],
        },
    }
    settings_path.write_text(json.dumps(existing, indent=2))
    uninstall(global_install=False)
    settings = json.loads(settings_path.read_text())
    assert settings["editor"] == "vim"
    # Swarm matcher removed, Bash matcher preserved
    assert len(settings["hooks"]["PreToolUse"]) == 1
    assert settings["hooks"]["PreToolUse"][0]["matcher"] == "Bash"
    # PostToolUse untouched
    assert "PostToolUse" in settings["hooks"]


def test_uninstall_corrupt_json(tmp_path, monkeypatch):
    """uninstall() silently returns on corrupt JSON."""
    from swarm.hooks.install import uninstall

    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text("{corrupt json!!!")
    # Should not raise
    uninstall(global_install=False)
    # File left unchanged
    assert settings_path.read_text() == "{corrupt json!!!"


def test_uninstall_empty_hooks_key(tmp_path, monkeypatch):
    """uninstall() handles settings with empty hooks dict."""
    from swarm.hooks.install import uninstall

    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"hooks": {}}))
    uninstall(global_install=False)
    # No change needed — file should remain the same (no changed flag)
    settings = json.loads(settings_path.read_text())
    assert settings == {"hooks": {}}
