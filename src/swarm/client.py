"""SwarmClient — connects to the daemon API for remote control."""

from __future__ import annotations

import asyncio
import json
from typing import Callable

import aiohttp

from swarm.logging import get_logger

_log = get_logger("client")


_DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=10)


class SwarmClient:
    """Client for the swarm daemon REST + WebSocket API."""

    def __init__(self, base_url: str = "http://localhost:8081") -> None:
        self.base_url = base_url.rstrip("/")
        self.ws_url = self.base_url.replace("http", "ws", 1) + "/ws"
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._on_message: list[Callable] = []

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=_DEFAULT_TIMEOUT)
        return self._session

    async def close(self) -> None:
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()

    # --- REST API ---

    async def health(self) -> dict:
        session = await self._get_session()
        async with session.get(f"{self.base_url}/api/health") as resp:
            return await resp.json()

    async def workers(self) -> list[dict]:
        session = await self._get_session()
        async with session.get(f"{self.base_url}/api/workers") as resp:
            data = await resp.json()
            return data.get("workers", [])

    async def worker_detail(self, name: str) -> dict:
        session = await self._get_session()
        async with session.get(f"{self.base_url}/api/workers/{name}") as resp:
            return await resp.json()

    async def send_message(self, worker_name: str, message: str) -> dict:
        session = await self._get_session()
        async with session.post(
            f"{self.base_url}/api/workers/{worker_name}/send",
            json={"message": message},
        ) as resp:
            return await resp.json()

    async def continue_worker(self, worker_name: str) -> dict:
        session = await self._get_session()
        async with session.post(f"{self.base_url}/api/workers/{worker_name}/continue") as resp:
            return await resp.json()

    async def kill_worker(self, worker_name: str) -> dict:
        session = await self._get_session()
        async with session.post(f"{self.base_url}/api/workers/{worker_name}/kill") as resp:
            return await resp.json()

    async def buzz_log(self, limit: int = 50) -> list[dict]:
        session = await self._get_session()
        async with session.get(f"{self.base_url}/api/buzz/log", params={"limit": limit}) as resp:
            data = await resp.json()
            return data.get("entries", [])

    async def toggle_buzz(self) -> dict:
        session = await self._get_session()
        async with session.post(f"{self.base_url}/api/buzz/toggle") as resp:
            return await resp.json()

    async def get_tasks(self) -> list[dict]:
        session = await self._get_session()
        async with session.get(f"{self.base_url}/api/tasks") as resp:
            data = await resp.json()
            return data.get("tasks", [])

    async def create_task(self, title: str, description: str = "", priority: str = "normal") -> dict:
        session = await self._get_session()
        async with session.post(
            f"{self.base_url}/api/tasks",
            json={"title": title, "description": description, "priority": priority},
        ) as resp:
            return await resp.json()

    async def assign_task(self, task_id: str, worker: str) -> dict:
        session = await self._get_session()
        async with session.post(
            f"{self.base_url}/api/tasks/{task_id}/assign",
            json={"worker": worker},
        ) as resp:
            return await resp.json()

    # --- WebSocket ---

    def on_message(self, callback: Callable[[dict], None]) -> None:
        self._on_message.append(callback)

    async def connect_ws(self) -> None:
        """Connect to the WebSocket and listen for events."""
        session = await self._get_session()
        self._ws = await session.ws_connect(self.ws_url)
        _log.info("WebSocket connected to %s", self.ws_url)

        async for msg in self._ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    for cb in self._on_message:
                        cb(data)
                except json.JSONDecodeError:
                    _log.warning("invalid JSON from WebSocket: %s", msg.data[:100])
            elif msg.type == aiohttp.WSMsgType.ERROR:
                _log.warning("WebSocket error: %s", self._ws.exception())
                break

    async def is_daemon_running(self) -> bool:
        """Check if the daemon is reachable."""
        try:
            await self.health()
            return True
        except Exception:
            return False
