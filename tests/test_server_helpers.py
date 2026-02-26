"""Tests for swarm.server.helpers — shared HTTP utilities."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.server.helpers import (
    MAX_QUERY_LIMIT,
    get_daemon,
    json_error,
    parse_limit,
    read_file_field,
    validate_worker_name,
)

# --- json_error ---


def test_json_error_default_status():
    resp = json_error("oops")
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body == {"error": "oops"}


def test_json_error_custom_status():
    resp = json_error("not found", 404)
    assert resp.status == 404
    body = json.loads(resp.body)
    assert body == {"error": "not found"}


def test_json_error_content_type():
    resp = json_error("bad")
    assert resp.content_type == "application/json"


# --- get_daemon ---


def test_get_daemon_extracts_from_app():
    daemon = MagicMock()
    request = MagicMock()
    request.app = {"daemon": daemon}
    assert get_daemon(request) is daemon


# --- parse_limit ---


def _make_request_with_query(query: dict[str, str]) -> MagicMock:
    request = MagicMock()
    request.query = query
    return request


def test_parse_limit_default():
    request = _make_request_with_query({})
    assert parse_limit(request) == 50


def test_parse_limit_valid_int():
    request = _make_request_with_query({"limit": "25"})
    assert parse_limit(request) == 25


def test_parse_limit_capped_at_max():
    request = _make_request_with_query({"limit": "9999"})
    assert parse_limit(request) == MAX_QUERY_LIMIT


def test_parse_limit_invalid_string_fallback():
    request = _make_request_with_query({"limit": "abc"})
    assert parse_limit(request) == 50


def test_parse_limit_custom_default():
    request = _make_request_with_query({})
    assert parse_limit(request, default=100) == 100


def test_parse_limit_custom_default_on_invalid():
    request = _make_request_with_query({"limit": "xyz"})
    assert parse_limit(request, default=10) == 10


# --- validate_worker_name ---


def test_validate_worker_name_valid():
    assert validate_worker_name("api-worker") is None
    assert validate_worker_name("worker_1") is None
    assert validate_worker_name("ABC123") is None


def test_validate_worker_name_empty():
    result = validate_worker_name("")
    assert result is not None
    assert "Invalid" in result


def test_validate_worker_name_special_chars():
    result = validate_worker_name("bad name!")
    assert result is not None
    assert "Invalid" in result


def test_validate_worker_name_spaces():
    result = validate_worker_name("has space")
    assert result is not None


# --- read_file_field ---


@pytest.mark.asyncio
async def test_read_file_field_missing_field():
    """Missing multipart field should raise ValueError."""
    field = AsyncMock()
    field.name = "other"
    reader = AsyncMock()
    reader.next = AsyncMock(return_value=field)
    request = MagicMock()
    request.multipart = AsyncMock(return_value=reader)

    with pytest.raises(ValueError, match="file field required"):
        await read_file_field(request)


@pytest.mark.asyncio
async def test_read_file_field_no_field_at_all():
    """No multipart field should raise ValueError."""
    reader = AsyncMock()
    reader.next = AsyncMock(return_value=None)
    request = MagicMock()
    request.multipart = AsyncMock(return_value=reader)

    with pytest.raises(ValueError, match="file field required"):
        await read_file_field(request)


@pytest.mark.asyncio
async def test_read_file_field_empty_data():
    """Empty file data should raise ValueError."""
    field = AsyncMock()
    field.name = "file"
    field.filename = "test.txt"
    field.read = AsyncMock(return_value=b"")
    reader = AsyncMock()
    reader.next = AsyncMock(return_value=field)
    request = MagicMock()
    request.multipart = AsyncMock(return_value=reader)

    with pytest.raises(ValueError, match="empty file"):
        await read_file_field(request)


@pytest.mark.asyncio
async def test_read_file_field_success():
    """Successful read returns (filename, data)."""
    field = AsyncMock()
    field.name = "file"
    field.filename = "report.pdf"
    field.read = AsyncMock(return_value=b"PDF content")
    reader = AsyncMock()
    reader.next = AsyncMock(return_value=field)
    request = MagicMock()
    request.multipart = AsyncMock(return_value=reader)

    filename, data = await read_file_field(request)
    assert filename == "report.pdf"
    assert data == b"PDF content"


@pytest.mark.asyncio
async def test_read_file_field_default_filename():
    """Missing filename defaults to 'upload'."""
    field = AsyncMock()
    field.name = "file"
    field.filename = None
    field.read = AsyncMock(return_value=b"data")
    reader = AsyncMock()
    reader.next = AsyncMock(return_value=field)
    request = MagicMock()
    request.multipart = AsyncMock(return_value=reader)

    filename, data = await read_file_field(request)
    assert filename == "upload"


@pytest.mark.asyncio
async def test_read_file_field_custom_field_name():
    """Custom field_name should be checked."""
    field = AsyncMock()
    field.name = "file"
    reader = AsyncMock()
    reader.next = AsyncMock(return_value=field)
    request = MagicMock()
    request.multipart = AsyncMock(return_value=reader)

    with pytest.raises(ValueError, match="attachment field required"):
        await read_file_field(request, field_name="attachment")
