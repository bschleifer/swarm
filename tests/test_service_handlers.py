"""Tests for built-in service handlers (YouTube scraper, file uploader)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from swarm.services.handlers import register_defaults
from swarm.services.handlers.file_uploader import FileUploader
from swarm.services.handlers.youtube_scraper import YouTubeScraper
from swarm.services.registry import ServiceContext, ServiceRegistry


@pytest.fixture()
def ctx() -> ServiceContext:
    return ServiceContext(
        pipeline_id="p1",
        step_id="s1",
    )


# ---------------------------------------------------------------------------
# register_defaults
# ---------------------------------------------------------------------------


class TestRegisterDefaults:
    def test_registers_all_handlers(self) -> None:
        reg = ServiceRegistry()
        register_defaults(reg)
        assert reg.has("youtube_scraper")
        assert reg.has("file_uploader")
        assert reg.has("headless_claude")


# ---------------------------------------------------------------------------
# YouTubeScraper
# ---------------------------------------------------------------------------


class TestYouTubeScraper:
    async def test_missing_api_key(self, ctx: ServiceContext) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("YOUTUBE_API_KEY", None)
            result = await YouTubeScraper().execute({}, ctx)
        assert not result.success
        assert "api_key" in result.error.lower() or "API" in result.error

    async def test_missing_channels(self, ctx: ServiceContext) -> None:
        result = await YouTubeScraper().execute({"api_key": "fake"}, ctx)
        assert not result.success
        assert "channels" in result.error.lower()

    async def test_api_key_from_env(self, ctx: ServiceContext) -> None:
        """API key falls back to YOUTUBE_API_KEY env var."""
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={"items": []})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("aiohttp.ClientSession", return_value=mock_session),
            patch.dict(os.environ, {"YOUTUBE_API_KEY": "env-key"}),
        ):
            result = await YouTubeScraper().execute({"channels": ["UC123"]}, ctx)
        assert result.success
        assert result.data["videos"] == []

    async def test_successful_fetch(self, ctx: ServiceContext) -> None:
        search_resp = _mock_response(
            {
                "items": [
                    {"id": {"videoId": "vid1"}},
                    {"id": {"videoId": "vid2"}},
                ]
            }
        )
        details_resp = _mock_response(
            {
                "items": [
                    {
                        "id": "vid1",
                        "snippet": {
                            "title": "Title 1",
                            "description": "Desc",
                            "tags": ["a"],
                            "publishedAt": "2026-01-01T00:00:00Z",
                            "thumbnails": {"high": {"url": "http://img/1"}},
                        },
                        "statistics": {"viewCount": "100"},
                    },
                    {
                        "id": "vid2",
                        "snippet": {
                            "title": "Title 2",
                            "description": "",
                            "tags": [],
                            "publishedAt": "2026-01-02T00:00:00Z",
                            "thumbnails": {},
                        },
                        "statistics": {},
                    },
                ]
            }
        )

        call_count = 0

        def _get_side_effect(*_a: Any, **_kw: Any) -> Any:
            nonlocal call_count
            call_count += 1
            return search_resp if call_count == 1 else details_resp

        mock_session = AsyncMock()
        mock_session.get = MagicMock(side_effect=_get_side_effect)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await YouTubeScraper().execute(
                {"api_key": "k", "channels": ["UC1"], "max_results": 5},
                ctx,
            )

        assert result.success
        assert len(result.data["videos"]) == 2
        v1 = result.data["videos"][0]
        assert v1["video_id"] == "vid1"
        assert v1["title"] == "Title 1"
        assert v1["view_count"] == 100
        assert v1["thumbnail_url"] == "http://img/1"

    async def test_http_error(self, ctx: ServiceContext) -> None:
        import aiohttp

        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock(
            side_effect=aiohttp.ClientResponseError(
                request_info=MagicMock(),
                history=(),
                status=403,
                message="Forbidden",
            )
        )
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await YouTubeScraper().execute({"api_key": "k", "channels": ["UC1"]}, ctx)
        assert not result.success
        assert "HTTP error" in result.error

    async def test_empty_results(self, ctx: ServiceContext) -> None:
        mock_resp = _mock_response({"items": []})

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await YouTubeScraper().execute({"api_key": "k", "channels": ["UC1"]}, ctx)
        assert result.success
        assert result.data["videos"] == []


# ---------------------------------------------------------------------------
# FileUploader
# ---------------------------------------------------------------------------


class TestFileUploader:
    async def test_missing_credentials(self, ctx: ServiceContext) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
            result = await FileUploader().execute({}, ctx)
        assert not result.success
        assert "credentials" in result.error.lower()

    async def test_file_not_found(self, ctx: ServiceContext) -> None:
        result = await FileUploader().execute(
            {
                "credentials_path": "/tmp/fake.json",
                "file_path": "/nonexistent/file.txt",
            },
            ctx,
        )
        assert not result.success
        assert "not found" in result.error.lower()

    async def test_invalid_credentials_json(
        self,
        ctx: ServiceContext,
        tmp_path: Path,
    ) -> None:
        bad_creds = tmp_path / "bad.json"
        bad_creds.write_text("not json")
        upload_file = tmp_path / "data.txt"
        upload_file.write_text("hello")
        result = await FileUploader().execute(
            {
                "credentials_path": str(bad_creds),
                "file_path": str(upload_file),
            },
            ctx,
        )
        assert not result.success
        assert "invalid credentials" in result.error.lower()

    async def test_credentials_from_env(
        self,
        ctx: ServiceContext,
        tmp_path: Path,
    ) -> None:
        """Falls back to GOOGLE_APPLICATION_CREDENTIALS env var."""
        creds, upload_file = _make_creds_and_file(tmp_path)

        mock_token_resp = _mock_response({"access_token": "tok123"})
        mock_upload_resp = _mock_response(
            {
                "id": "fid",
                "webViewLink": "https://link",
                "webContentLink": "https://dl",
            }
        )

        call_count = 0

        def _post_side(*_a: Any, **_kw: Any) -> Any:
            nonlocal call_count
            call_count += 1
            return mock_token_resp if call_count == 1 else mock_upload_resp

        mock_session = AsyncMock()
        mock_session.post = MagicMock(side_effect=_post_side)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("aiohttp.ClientSession", return_value=mock_session),
            patch.dict(
                os.environ,
                {"GOOGLE_APPLICATION_CREDENTIALS": str(creds)},
            ),
        ):
            result = await FileUploader().execute({"file_path": str(upload_file)}, ctx)
        assert result.success

    async def test_successful_upload(
        self,
        ctx: ServiceContext,
        tmp_path: Path,
    ) -> None:
        creds, upload_file = _make_creds_and_file(tmp_path)

        mock_token_resp = _mock_response({"access_token": "tok123"})
        mock_upload_resp = _mock_response(
            {
                "id": "fid",
                "webViewLink": "https://link",
                "webContentLink": "https://dl",
            }
        )

        call_count = 0

        def _post_side(*_a: Any, **_kw: Any) -> Any:
            nonlocal call_count
            call_count += 1
            return mock_token_resp if call_count == 1 else mock_upload_resp

        mock_session = AsyncMock()
        mock_session.post = MagicMock(side_effect=_post_side)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await FileUploader().execute(
                {
                    "credentials_path": str(creds),
                    "file_path": str(upload_file),
                    "folder_id": "folder1",
                },
                ctx,
            )

        assert result.success
        assert result.data["file_id"] == "fid"
        assert result.data["web_view_link"] == "https://link"
        assert result.data["web_content_link"] == "https://dl"

    async def test_upload_http_error(
        self,
        ctx: ServiceContext,
        tmp_path: Path,
    ) -> None:
        import aiohttp

        creds, upload_file = _make_creds_and_file(tmp_path)

        mock_token_resp = _mock_response({"access_token": "tok123"})
        mock_err_resp = AsyncMock()
        mock_err_resp.raise_for_status = MagicMock(
            side_effect=aiohttp.ClientResponseError(
                request_info=MagicMock(),
                history=(),
                status=500,
                message="Server Error",
            )
        )
        mock_err_resp.__aenter__ = AsyncMock(return_value=mock_err_resp)
        mock_err_resp.__aexit__ = AsyncMock(return_value=False)

        call_count = 0

        def _post_side(*_a: Any, **_kw: Any) -> Any:
            nonlocal call_count
            call_count += 1
            return mock_token_resp if call_count == 1 else mock_err_resp

        mock_session = AsyncMock()
        mock_session.post = MagicMock(side_effect=_post_side)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await FileUploader().execute(
                {
                    "credentials_path": str(creds),
                    "file_path": str(upload_file),
                },
                ctx,
            )
        assert not result.success
        assert "HTTP error" in result.error


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(data: dict[str, Any]) -> AsyncMock:
    """Create a mock aiohttp response that returns *data* from .json()."""
    resp = AsyncMock()
    resp.raise_for_status = MagicMock()
    resp.json = AsyncMock(return_value=data)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _make_creds_and_file(
    tmp_path: Path,
) -> tuple[Path, Path]:
    """Generate a real RSA key pair and write a service account JSON."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()

    sa_info = {
        "type": "service_account",
        "client_email": "test@test.iam.gserviceaccount.com",
        "private_key": pem,
        "private_key_id": "key1",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps(sa_info))

    upload_file = tmp_path / "data.txt"
    upload_file.write_text("hello world")

    return creds, upload_file
