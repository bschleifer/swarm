"""Tests for per-worker persistent memory."""

from __future__ import annotations

from pathlib import Path

from swarm.worker.memory import (
    list_memory_files,
    load_memory,
    memory_dir,
    save_memory,
)


class TestMemoryDir:
    def test_creates_directory(self, tmp_path: Path, monkeypatch: object) -> None:
        import swarm.worker.memory as mod

        monkeypatch.setattr(mod, "_MEMORY_ROOT", tmp_path / "memory")
        d = memory_dir("api")
        assert d.is_dir()
        assert d.name == "api"

    def test_idempotent(self, tmp_path: Path, monkeypatch: object) -> None:
        import swarm.worker.memory as mod

        monkeypatch.setattr(mod, "_MEMORY_ROOT", tmp_path / "memory")
        d1 = memory_dir("api")
        d2 = memory_dir("api")
        assert d1 == d2


class TestLoadSaveMemory:
    def test_load_empty_returns_empty(self, tmp_path: Path, monkeypatch: object) -> None:
        import swarm.worker.memory as mod

        monkeypatch.setattr(mod, "_MEMORY_ROOT", tmp_path / "memory")
        result = load_memory("nonexistent")
        assert result == ""

    def test_save_and_load(self, tmp_path: Path, monkeypatch: object) -> None:
        import swarm.worker.memory as mod

        monkeypatch.setattr(mod, "_MEMORY_ROOT", tmp_path / "memory")
        save_memory("api", "# API Worker Memory\n- prefers REST over GraphQL")
        content = load_memory("api")
        assert "prefers REST over GraphQL" in content

    def test_overwrite(self, tmp_path: Path, monkeypatch: object) -> None:
        import swarm.worker.memory as mod

        monkeypatch.setattr(mod, "_MEMORY_ROOT", tmp_path / "memory")
        save_memory("api", "version 1")
        save_memory("api", "version 2")
        assert load_memory("api") == "version 2"


class TestListMemoryFiles:
    def test_empty_dir(self, tmp_path: Path, monkeypatch: object) -> None:
        import swarm.worker.memory as mod

        monkeypatch.setattr(mod, "_MEMORY_ROOT", tmp_path / "memory")
        files = list_memory_files("api")
        assert files == []

    def test_lists_md_files(self, tmp_path: Path, monkeypatch: object) -> None:
        import swarm.worker.memory as mod

        monkeypatch.setattr(mod, "_MEMORY_ROOT", tmp_path / "memory")
        save_memory("api", "main memory")
        # Add another md file
        d = memory_dir("api")
        (d / "notes.md").write_text("extra notes")
        (d / "not_md.txt").write_text("ignored")

        files = list_memory_files("api")
        assert "MEMORY.md" in files
        assert "notes.md" in files
        assert "not_md.txt" not in files
