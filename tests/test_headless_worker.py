"""Tests for HeadlessClaude service handler."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from swarm.services.registry import ServiceContext
from swarm.worker.headless import HeadlessClaude


@pytest.fixture()
def ctx() -> ServiceContext:
    return ServiceContext(pipeline_id="p1", step_id="s1")


class TestHeadlessClaude:
    async def test_missing_prompt(self, ctx: ServiceContext) -> None:
        result = await HeadlessClaude().execute({}, ctx)
        assert not result.success
        assert "prompt" in result.error.lower()

    async def test_success_json(self, ctx: ServiceContext) -> None:
        # ClaudeProvider expects {"result": "...", "session_id": "..."}
        import json as _json

        envelope = _json.dumps(
            {"type": "result", "result": '{"answer": 42}', "session_id": "sid1"}
        ).encode()
        mock_proc = _mock_process(stdout=envelope, returncode=0)
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ):
            result = await HeadlessClaude().execute(
                {
                    "prompt": "What is the answer?",
                    "output_format": "json",
                    "max_turns": 5,
                },
                ctx,
            )
        assert result.success
        assert result.data["result"] == '{"answer": 42}'
        assert result.data["session_id"] == "sid1"

    async def test_success_text(self, ctx: ServiceContext) -> None:
        mock_proc = _mock_process(stdout=b"Hello world", returncode=0)
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ):
            result = await HeadlessClaude().execute(
                {
                    "prompt": "Say hello",
                    "output_format": "text",
                },
                ctx,
            )
        assert result.success
        assert "Hello world" in result.data["result"]

    async def test_timeout(self, ctx: ServiceContext) -> None:
        mock_proc = _mock_process(stdout=b"", returncode=0, timeout=True)
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ):
            result = await HeadlessClaude().execute({"prompt": "slow task", "timeout": 1}, ctx)
        assert not result.success
        assert "timed out" in result.error.lower()
        mock_proc.kill.assert_called_once()

    async def test_nonzero_exit(self, ctx: ServiceContext) -> None:
        mock_proc = _mock_process(
            stdout=b"",
            stderr=b"something went wrong",
            returncode=1,
        )
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ):
            result = await HeadlessClaude().execute({"prompt": "fail"}, ctx)
        assert not result.success
        assert "code 1" in result.error

    async def test_cancellation(self, ctx: ServiceContext) -> None:
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(side_effect=asyncio.CancelledError)
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        with (
            patch(
                "asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await HeadlessClaude().execute({"prompt": "cancelled"}, ctx)
        mock_proc.kill.assert_called_once()

    async def test_custom_provider(self, ctx: ServiceContext) -> None:
        """Provider name is forwarded to get_provider."""
        mock_proc = _mock_process(stdout=b"result", returncode=0)
        with (
            patch(
                "asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ),
            patch("swarm.providers.get_provider") as mock_gp,
        ):
            mock_provider = MagicMock()
            mock_provider.headless_command.return_value = [
                "test-cmd",
                "-p",
                "hi",
            ]
            mock_provider.env_strip_prefixes.return_value = ()
            mock_provider.parse_headless_response.return_value = (
                "result",
                None,
            )
            mock_gp.return_value = mock_provider

            result = await HeadlessClaude().execute({"prompt": "hi", "provider": "gemini"}, ctx)

        mock_gp.assert_called_once_with("gemini")
        assert result.success

    async def test_headless_command_args(self, ctx: ServiceContext) -> None:
        """Verify correct args are passed to provider."""
        mock_proc = _mock_process(stdout=b"ok", returncode=0)

        with patch(
            "asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ) as mock_exec:
            await HeadlessClaude().execute(
                {
                    "prompt": "analyze this",
                    "output_format": "json",
                    "max_turns": 3,
                },
                ctx,
            )

        # ClaudeProvider builds: claude -p <prompt> --output-format json --max-turns 3
        args = mock_exec.call_args[0]
        assert args[0] == "claude"
        assert "-p" in args
        assert "analyze this" in args
        assert "--output-format" in args
        assert "json" in args
        assert "--max-turns" in args
        assert "3" in args


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_process(
    stdout: bytes = b"",
    stderr: bytes = b"",
    returncode: int = 0,
    timeout: bool = False,
) -> AsyncMock:
    """Build a mock asyncio.Process."""
    proc = AsyncMock()
    proc.returncode = returncode
    proc.kill = MagicMock()
    proc.wait = AsyncMock()

    if timeout:

        async def _communicate() -> tuple[bytes, bytes]:
            raise TimeoutError

        proc.communicate = _communicate
    else:
        proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc
