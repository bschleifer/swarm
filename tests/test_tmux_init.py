from __future__ import annotations

from unittest.mock import patch

import pytest

from swarm.tmux import init


@pytest.fixture
def tmp_tmux_conf(tmp_path, monkeypatch):
    conf_file = tmp_path / ".tmux.conf"
    monkeypatch.setattr(init, "_TMUX_CONF", conf_file)
    return conf_file


def test_fresh_install_no_existing_file(tmp_tmux_conf, capsys):
    result = init.write_tmux_config()

    assert result is True
    assert tmp_tmux_conf.exists()
    content = tmp_tmux_conf.read_text()
    assert init._MARKER_START in content
    assert init._MARKER_END in content
    assert "# Truecolor (24-bit RGB)" in content

    captured = capsys.readouterr()
    assert "Wrote ~/.tmux.conf with swarm configuration" in captured.out


def test_empty_existing_file(tmp_tmux_conf, capsys):
    tmp_tmux_conf.write_text("")

    result = init.write_tmux_config()

    assert result is True
    content = tmp_tmux_conf.read_text()
    assert init._MARKER_START in content
    assert init._MARKER_END in content

    captured = capsys.readouterr()
    assert "Wrote ~/.tmux.conf with swarm configuration" in captured.out


def test_replace_existing_swarm_block(tmp_tmux_conf, capsys):
    original_content = f"""# User config before
set -g status-left "custom"

{init._MARKER_START}
# Old swarm config
set -g mouse off
{init._MARKER_END}

# User config after
bind r source-file ~/.tmux.conf
"""
    tmp_tmux_conf.write_text(original_content)

    result = init.write_tmux_config()

    assert result is True
    content = tmp_tmux_conf.read_text()

    assert "# User config before" in content
    assert "# User config after" in content
    assert init._MARKER_START in content
    assert init._MARKER_END in content
    assert "# Old swarm config" not in content
    assert "set -g mouse off" not in content
    assert "set -g mouse on" in content

    captured = capsys.readouterr()
    assert "Updated swarm block in ~/.tmux.conf" in captured.out


def test_replace_when_end_marker_missing(tmp_tmux_conf, capsys):
    original_content = f"""# User config before
{init._MARKER_START}
# Incomplete old swarm config
set -g mouse off
# Missing end marker
# User config after
"""
    tmp_tmux_conf.write_text(original_content)

    result = init.write_tmux_config()

    assert result is True
    content = tmp_tmux_conf.read_text()

    assert "# User config before" in content
    assert init._MARKER_START in content
    assert init._MARKER_END in content
    assert "set -g mouse on" in content
    assert "# Incomplete old swarm config" not in content

    captured = capsys.readouterr()
    assert "Updated swarm block in ~/.tmux.conf" in captured.out


def test_append_to_existing_non_swarm_config_user_confirms(tmp_tmux_conf, capsys):
    original_content = """# My custom tmux config
set -g status-left "custom"
bind r source-file ~/.tmux.conf
"""
    tmp_tmux_conf.write_text(original_content)

    with patch("click.confirm", return_value=True):
        result = init.write_tmux_config()

    assert result is True
    content = tmp_tmux_conf.read_text()

    assert "# My custom tmux config" in content
    assert 'set -g status-left "custom"' in content
    assert init._MARKER_START in content
    assert init._MARKER_END in content
    assert content.index("# My custom tmux config") < content.index(init._MARKER_START)

    captured = capsys.readouterr()
    assert "Found existing ~/.tmux.conf" in captured.out
    assert "Appended swarm block to ~/.tmux.conf" in captured.out


def test_skip_append_when_user_declines(tmp_tmux_conf, capsys):
    original_content = """# My custom tmux config
set -g status-left "custom"
"""
    tmp_tmux_conf.write_text(original_content)

    with patch("click.confirm", return_value=False):
        result = init.write_tmux_config()

    assert result is False
    content = tmp_tmux_conf.read_text()

    assert content == original_content
    assert init._MARKER_START not in content

    captured = capsys.readouterr()
    assert "Found existing ~/.tmux.conf" in captured.out
    assert "Skipped — no changes to ~/.tmux.conf" in captured.out


def test_preserve_user_config_around_swarm_block(tmp_tmux_conf):
    original_content = f"""# Before section
set -g status-left "before"

{init._MARKER_START}
# Old swarm config
{init._MARKER_END}

# After section
set -g status-right "after"
"""
    tmp_tmux_conf.write_text(original_content)

    init.write_tmux_config()

    content = tmp_tmux_conf.read_text()
    lines = content.split("\n")

    before_idx = next(i for i, line in enumerate(lines) if "# Before section" in line)
    start_idx = next(i for i, line in enumerate(lines) if init._MARKER_START in line)
    end_idx = next(i for i, line in enumerate(lines) if init._MARKER_END in line)
    after_idx = next(i for i, line in enumerate(lines) if "# After section" in line)

    assert before_idx < start_idx < end_idx < after_idx
    assert 'set -g status-left "before"' in content
    assert 'set -g status-right "after"' in content


def test_whitespace_only_file_treated_as_empty(tmp_tmux_conf, capsys):
    tmp_tmux_conf.write_text("   \n\n  \t  \n")

    result = init.write_tmux_config()

    assert result is True
    content = tmp_tmux_conf.read_text()
    assert init._MARKER_START in content

    captured = capsys.readouterr()
    assert "Wrote ~/.tmux.conf with swarm configuration" in captured.out


def test_swarm_block_content_is_valid(tmp_tmux_conf):
    init.write_tmux_config()

    content = tmp_tmux_conf.read_text()

    assert "set -ag terminal-features" in content
    assert "set -g mouse on" in content
    assert "set -g history-limit 50000" in content
    assert "set -g monitor-activity on" in content
    assert "set -g pane-border-status top" in content
    assert "bind 0 select-pane -t 0" in content
    assert "bind S set-window-option synchronize-panes" in content


def test_swarm_block_enables_clipboard_passthrough(tmp_tmux_conf):
    """Paste (Ctrl-V, images, attachments) requires allow-passthrough and set-clipboard."""
    init.write_tmux_config()
    content = tmp_tmux_conf.read_text()
    assert "allow-passthrough on" in content, "swarm tmux config must include allow-passthrough on"
    assert "set-clipboard on" in content, "swarm tmux config must include set-clipboard on"


def test_swarm_block_disables_mouse_drag_copy_mode(tmp_tmux_conf):
    """Mouse drag must not auto-enter copy-mode."""
    init.write_tmux_config()
    content = tmp_tmux_conf.read_text()
    assert "bind -T root MouseDrag1Pane send-keys -M" in content


def test_swarm_block_overrides_mouse_drag_end(tmp_tmux_conf):
    """Mouse drag end in copy-mode must stop selection (not auto-copy)."""
    init.write_tmux_config()
    content = tmp_tmux_conf.read_text()
    assert "bind -T copy-mode    MouseDragEnd1Pane send-keys -X stop-selection" in content
    assert "bind -T copy-mode-vi MouseDragEnd1Pane send-keys -X stop-selection" in content


def test_swarm_block_ctrl_c_copies(tmp_tmux_conf):
    """Ctrl+C in copy-mode must copy selection to clipboard."""
    init.write_tmux_config()
    content = tmp_tmux_conf.read_text()
    assert "bind -T copy-mode    C-c send-keys -X copy-selection-and-cancel" in content
    assert "bind -T copy-mode-vi C-c send-keys -X copy-selection-and-cancel" in content


def test_multiple_updates_preserve_user_content(tmp_tmux_conf):
    user_config = "# My config\nset -g status-left 'user'\n"
    tmp_tmux_conf.write_text(user_config)

    with patch("click.confirm", return_value=True):
        init.write_tmux_config()

    first_content = tmp_tmux_conf.read_text()
    assert "# My config" in first_content
    assert init._MARKER_START in first_content

    init.write_tmux_config()

    second_content = tmp_tmux_conf.read_text()
    assert "# My config" in second_content
    assert init._MARKER_START in second_content
    assert second_content.count(init._MARKER_START) == 1
    assert second_content.count(init._MARKER_END) == 1
