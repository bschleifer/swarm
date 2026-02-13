from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import aiohttp

from swarm.client import SwarmClient


class TestSwarmClientConstruction:
    def test_init_without_password(self):
        client = SwarmClient(base_url="http://localhost:9090")
        assert client.base_url == "http://localhost:9090"
        assert client._password is None
        assert client.ws_url == "ws://localhost:9090/ws"
        assert client._session is None
        assert client._ws is None
        assert client._on_message == []

    def test_init_with_password(self):
        client = SwarmClient(base_url="http://localhost:9090", password="secret")
        assert client.base_url == "http://localhost:9090"
        assert client._password == "secret"
        assert client.ws_url == "ws://localhost:9090/ws?token=secret"

    def test_init_strips_trailing_slash(self):
        client = SwarmClient(base_url="http://localhost:9090/")
        assert client.base_url == "http://localhost:9090"

    def test_init_https_becomes_wss(self):
        client = SwarmClient(base_url="https://example.com")
        assert client.ws_url == "wss://example.com/ws"

    def test_headers_without_password(self):
        client = SwarmClient()
        headers = client._headers()
        assert headers == {"X-Requested-With": "SwarmClient"}

    def test_headers_with_password(self):
        client = SwarmClient(password="secret")
        headers = client._headers()
        assert headers == {
            "X-Requested-With": "SwarmClient",
            "Authorization": "Bearer secret",
        }


@pytest.mark.asyncio
class TestSwarmClientSession:
    async def test_get_session_creates_new_session(self):
        client = SwarmClient(base_url="http://localhost:9090", password="secret")
        session = await client._get_session()
        assert isinstance(session, aiohttp.ClientSession)
        assert session is client._session
        await client.close()

    async def test_get_session_reuses_session(self):
        client = SwarmClient()
        session1 = await client._get_session()
        session2 = await client._get_session()
        assert session1 is session2
        await client.close()

    async def test_get_session_recreates_after_close(self):
        client = SwarmClient()
        session1 = await client._get_session()
        await client.close()
        session2 = await client._get_session()
        assert session1 is not session2
        await client.close()

    async def test_close_closes_ws_and_session(self):
        client = SwarmClient()

        mock_ws = AsyncMock()
        mock_ws.closed = False
        client._ws = mock_ws

        await client._get_session()

        await client.close()

        mock_ws.close.assert_called_once()
        assert client._ws is None
        assert client._session is None

    async def test_close_handles_already_closed_ws(self):
        client = SwarmClient()

        mock_ws = AsyncMock()
        mock_ws.closed = True
        client._ws = mock_ws

        await client.close()

        mock_ws.close.assert_not_called()

    async def test_close_handles_none_ws_and_session(self):
        client = SwarmClient()
        await client.close()
        assert client._ws is None
        assert client._session is None


@pytest.mark.asyncio
class TestSwarmClientREST:
    async def test_health(self):
        client = SwarmClient()
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={"status": "ok"})
        mock_resp.raise_for_status = MagicMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock()

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        with patch.object(client, "_get_session", AsyncMock(return_value=mock_session)):
            result = await client.health()

        assert result == {"status": "ok"}
        mock_session.get.assert_called_once_with("http://localhost:9090/api/health")

    async def test_workers(self):
        client = SwarmClient()
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={"workers": [{"name": "w1"}, {"name": "w2"}]})
        mock_resp.raise_for_status = MagicMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock()

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        with patch.object(client, "_get_session", AsyncMock(return_value=mock_session)):
            result = await client.workers()

        assert result == [{"name": "w1"}, {"name": "w2"}]
        mock_session.get.assert_called_once_with("http://localhost:9090/api/workers")

    async def test_workers_empty_list_when_no_workers_key(self):
        client = SwarmClient()
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={})
        mock_resp.raise_for_status = MagicMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock()

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        with patch.object(client, "_get_session", AsyncMock(return_value=mock_session)):
            result = await client.workers()

        assert result == []

    async def test_worker_detail(self):
        client = SwarmClient()
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={"name": "worker1", "state": "BUZZING"})
        mock_resp.raise_for_status = MagicMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock()

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        with patch.object(client, "_get_session", AsyncMock(return_value=mock_session)):
            result = await client.worker_detail("worker1")

        assert result == {"name": "worker1", "state": "BUZZING"}
        mock_session.get.assert_called_once_with("http://localhost:9090/api/workers/worker1")

    async def test_worker_detail_encodes_name(self):
        client = SwarmClient()
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={"name": "worker 1"})
        mock_resp.raise_for_status = MagicMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock()

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        with patch.object(client, "_get_session", AsyncMock(return_value=mock_session)):
            await client.worker_detail("worker 1")

        mock_session.get.assert_called_once_with("http://localhost:9090/api/workers/worker%201")

    async def test_send_message(self):
        client = SwarmClient()
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={"success": True})
        mock_resp.raise_for_status = MagicMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock()

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)

        with patch.object(client, "_get_session", AsyncMock(return_value=mock_session)):
            result = await client.send_message("worker1", "hello")

        assert result == {"success": True}
        mock_session.post.assert_called_once_with(
            "http://localhost:9090/api/workers/worker1/send",
            json={"message": "hello"},
        )

    async def test_continue_worker(self):
        client = SwarmClient()
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={"success": True})
        mock_resp.raise_for_status = MagicMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock()

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)

        with patch.object(client, "_get_session", AsyncMock(return_value=mock_session)):
            result = await client.continue_worker("worker1")

        assert result == {"success": True}
        mock_session.post.assert_called_once_with(
            "http://localhost:9090/api/workers/worker1/continue"
        )

    async def test_kill_worker(self):
        client = SwarmClient()
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={"success": True})
        mock_resp.raise_for_status = MagicMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock()

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)

        with patch.object(client, "_get_session", AsyncMock(return_value=mock_session)):
            result = await client.kill_worker("worker1")

        assert result == {"success": True}
        mock_session.post.assert_called_once_with("http://localhost:9090/api/workers/worker1/kill")

    async def test_drone_log_default_limit(self):
        client = SwarmClient()
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={"entries": [{"msg": "log1"}]})
        mock_resp.raise_for_status = MagicMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock()

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        with patch.object(client, "_get_session", AsyncMock(return_value=mock_session)):
            result = await client.drone_log()

        assert result == [{"msg": "log1"}]
        mock_session.get.assert_called_once_with(
            "http://localhost:9090/api/drones/log",
            params={"limit": 50},
        )

    async def test_drone_log_custom_limit(self):
        client = SwarmClient()
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={"entries": []})
        mock_resp.raise_for_status = MagicMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock()

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        with patch.object(client, "_get_session", AsyncMock(return_value=mock_session)):
            await client.drone_log(limit=100)

        mock_session.get.assert_called_once_with(
            "http://localhost:9090/api/drones/log",
            params={"limit": 100},
        )

    async def test_toggle_drones(self):
        client = SwarmClient()
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={"enabled": False})
        mock_resp.raise_for_status = MagicMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock()

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)

        with patch.object(client, "_get_session", AsyncMock(return_value=mock_session)):
            result = await client.toggle_drones()

        assert result == {"enabled": False}
        mock_session.post.assert_called_once_with("http://localhost:9090/api/drones/toggle")

    async def test_get_tasks(self):
        client = SwarmClient()
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={"tasks": [{"id": "t1"}, {"id": "t2"}]})
        mock_resp.raise_for_status = MagicMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock()

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        with patch.object(client, "_get_session", AsyncMock(return_value=mock_session)):
            result = await client.get_tasks()

        assert result == [{"id": "t1"}, {"id": "t2"}]
        mock_session.get.assert_called_once_with("http://localhost:9090/api/tasks")

    async def test_get_tasks_empty_when_no_tasks_key(self):
        client = SwarmClient()
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={})
        mock_resp.raise_for_status = MagicMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock()

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        with patch.object(client, "_get_session", AsyncMock(return_value=mock_session)):
            result = await client.get_tasks()

        assert result == []

    async def test_create_task_with_defaults(self):
        client = SwarmClient()
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={"id": "task1", "title": "Test"})
        mock_resp.raise_for_status = MagicMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock()

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)

        with patch.object(client, "_get_session", AsyncMock(return_value=mock_session)):
            result = await client.create_task("Test")

        assert result == {"id": "task1", "title": "Test"}
        mock_session.post.assert_called_once_with(
            "http://localhost:9090/api/tasks",
            json={"title": "Test", "description": "", "priority": "normal"},
        )

    async def test_create_task_with_all_params(self):
        client = SwarmClient()
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={"id": "task1"})
        mock_resp.raise_for_status = MagicMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock()

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)

        with patch.object(client, "_get_session", AsyncMock(return_value=mock_session)):
            await client.create_task(
                title="Urgent task",
                description="Fix the bug",
                priority="high",
            )

        mock_session.post.assert_called_once_with(
            "http://localhost:9090/api/tasks",
            json={"title": "Urgent task", "description": "Fix the bug", "priority": "high"},
        )

    async def test_assign_task(self):
        client = SwarmClient()
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={"success": True})
        mock_resp.raise_for_status = MagicMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock()

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)

        with patch.object(client, "_get_session", AsyncMock(return_value=mock_session)):
            result = await client.assign_task("task1", "worker1")

        assert result == {"success": True}
        mock_session.post.assert_called_once_with(
            "http://localhost:9090/api/tasks/task1/assign",
            json={"worker": "worker1"},
        )


class TestSwarmClientWebSocket:
    def test_on_message_registers_callback(self):
        client = SwarmClient()

        def callback(data):
            pass

        client.on_message(callback)
        assert callback in client._on_message

    def test_on_message_registers_multiple_callbacks(self):
        client = SwarmClient()

        def cb1(data):
            pass

        def cb2(data):
            pass

        client.on_message(cb1)
        client.on_message(cb2)
        assert cb1 in client._on_message
        assert cb2 in client._on_message
        assert len(client._on_message) == 2


@pytest.mark.asyncio
class TestSwarmClientWebSocketConnection:
    async def test_connect_ws_calls_ws_connect(self):
        client = SwarmClient()

        async def async_iter_empty():
            return
            yield

        mock_ws = AsyncMock()
        mock_ws.__aiter__ = lambda self: async_iter_empty()

        mock_session = MagicMock()
        mock_session.ws_connect = AsyncMock(return_value=mock_ws)

        with patch.object(client, "_get_session", AsyncMock(return_value=mock_session)):
            await client.connect_ws()

        mock_session.ws_connect.assert_called_once_with("ws://localhost:9090/ws")
        assert client._ws is mock_ws

    async def test_connect_ws_processes_text_messages(self):
        client = SwarmClient()
        received = []
        client.on_message(lambda data: received.append(data))

        msg = MagicMock()
        msg.type = aiohttp.WSMsgType.TEXT
        msg.data = json.dumps({"event": "test", "value": 42})

        async def async_iter_msgs():
            yield msg

        mock_ws = AsyncMock()
        mock_ws.__aiter__ = lambda self: async_iter_msgs()

        mock_session = MagicMock()
        mock_session.ws_connect = AsyncMock(return_value=mock_ws)

        with patch.object(client, "_get_session", AsyncMock(return_value=mock_session)):
            await client.connect_ws()

        assert received == [{"event": "test", "value": 42}]

    async def test_connect_ws_calls_async_callbacks(self):
        client = SwarmClient()
        received = []

        async def async_callback(data):
            received.append(data)

        client.on_message(async_callback)

        msg = MagicMock()
        msg.type = aiohttp.WSMsgType.TEXT
        msg.data = json.dumps({"event": "test"})

        async def async_iter_msgs():
            yield msg

        mock_ws = AsyncMock()
        mock_ws.__aiter__ = lambda self: async_iter_msgs()

        mock_session = MagicMock()
        mock_session.ws_connect = AsyncMock(return_value=mock_ws)

        with patch.object(client, "_get_session", AsyncMock(return_value=mock_session)):
            await client.connect_ws()

        assert received == [{"event": "test"}]

    async def test_connect_ws_handles_invalid_json(self):
        client = SwarmClient()
        received = []
        client.on_message(lambda data: received.append(data))

        msg = MagicMock()
        msg.type = aiohttp.WSMsgType.TEXT
        msg.data = "not json"

        async def async_iter_msgs():
            yield msg

        mock_ws = AsyncMock()
        mock_ws.__aiter__ = lambda self: async_iter_msgs()

        mock_session = MagicMock()
        mock_session.ws_connect = AsyncMock(return_value=mock_ws)

        with patch.object(client, "_get_session", AsyncMock(return_value=mock_session)):
            await client.connect_ws()

        assert received == []

    async def test_connect_ws_handles_error_messages(self):
        client = SwarmClient()

        msg = MagicMock()
        msg.type = aiohttp.WSMsgType.ERROR

        async def async_iter_msgs():
            yield msg

        mock_ws = AsyncMock()
        mock_ws.exception = MagicMock(return_value=Exception("test error"))
        mock_ws.__aiter__ = lambda self: async_iter_msgs()

        mock_session = MagicMock()
        mock_session.ws_connect = AsyncMock(return_value=mock_ws)

        with patch.object(client, "_get_session", AsyncMock(return_value=mock_session)):
            await client.connect_ws()


@pytest.mark.asyncio
class TestSwarmClientDaemonCheck:
    async def test_is_daemon_running_returns_true_on_success(self):
        client = SwarmClient()

        with patch.object(client, "health", AsyncMock(return_value={"status": "ok"})):
            result = await client.is_daemon_running()

        assert result is True

    async def test_is_daemon_running_returns_false_on_exception(self):
        client = SwarmClient()

        with patch.object(client, "health", AsyncMock(side_effect=Exception("connection refused"))):
            result = await client.is_daemon_running()

        assert result is False

    async def test_is_daemon_running_returns_false_on_http_error(self):
        client = SwarmClient()

        with patch.object(client, "health", AsyncMock(side_effect=aiohttp.ClientError())):
            result = await client.is_daemon_running()

        assert result is False
