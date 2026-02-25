"""EmailService — handles email processing, attachments, and draft replies."""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from swarm.drones.log import LogCategory, SystemAction
from swarm.logging import get_logger
from swarm.tasks.task import auto_classify_type, smart_title

if TYPE_CHECKING:
    from collections.abc import Callable

    from swarm.auth.graph import GraphTokenManager
    from swarm.drones.log import DroneLog
    from swarm.queen.queen import Queen

_log = get_logger("server.email")

_FETCH_TIMEOUT = 10  # seconds


def _html_to_text(html: str) -> str:
    """Convert HTML email body to readable plain text preserving structure."""
    import html as _html

    text = html
    # Block elements → newlines
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</div>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</tr>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</li>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<li[^>]*>", "  • ", text, flags=re.IGNORECASE)
    text = re.sub(r"<h[1-6][^>]*>", "\n## ", text, flags=re.IGNORECASE)
    text = re.sub(r"</h[1-6]>", "\n", text, flags=re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode HTML entities
    text = _html.unescape(text)
    # Normalize whitespace within lines but preserve newlines
    lines = text.split("\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in lines]
    # Collapse 3+ blank lines → 2
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class EmailService:
    """Handles email-related operations: attachments, processing, and draft replies."""

    def __init__(
        self,
        drone_log: DroneLog,
        queen: Queen,
        graph_mgr: GraphTokenManager | None,
        broadcast_ws: Callable[[dict[str, Any]], None],
        uploads_dir: Path | None = None,
    ) -> None:
        self._drone_log = drone_log
        self._queen = queen
        self._graph_mgr = graph_mgr
        self._broadcast_ws = broadcast_ws
        self._uploads_dir = uploads_dir or (Path.home() / ".swarm" / "uploads")

    def save_attachment(self, filename: str, data: bytes) -> str:
        """Save an uploaded file to uploads dir and return the absolute path."""
        self._uploads_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(data).hexdigest()[:12]
        safe_name = Path(filename).name  # strip directory components
        dest = self._uploads_dir / f"{digest}_{safe_name}"
        dest.write_bytes(data)
        return str(dest)

    async def fetch_and_save_image(self, url: str) -> str:
        """Fetch an image from a URL and save as attachment. Returns saved path."""
        import aiohttp as _aiohttp

        from swarm.server.daemon import SwarmOperationError

        if url.startswith("blob:"):
            raise ValueError("blob: URLs cannot be fetched")
        if not url.startswith(("http://", "https://", "file:///")):
            raise ValueError("unsupported URL scheme")

        if url.startswith("file:///"):
            from urllib.parse import unquote as _unquote

            local_path = _unquote(url[8:])  # strip file:///
            # Convert Windows path to WSL if needed
            if len(local_path) > 1 and local_path[1] == ":":
                drive = local_path[0].lower()
                local_path = f"/mnt/{drive}{local_path[2:].replace(chr(92), '/')}"
            fp = Path(local_path).resolve()
            # Restrict file:// reads to the uploads directory (prevent path traversal)
            uploads_resolved = self._uploads_dir.resolve()
            if not str(fp).startswith(str(uploads_resolved) + "/") and fp != uploads_resolved:
                raise ValueError(f"file:/// access denied outside uploads dir: {fp}")
            if not fp.exists():
                raise FileNotFoundError(f"file not found: {local_path}")
            img_data = fp.read_bytes()
            filename = fp.name
        else:
            async with _aiohttp.ClientSession() as sess:
                timeout = _aiohttp.ClientTimeout(total=_FETCH_TIMEOUT)
                async with sess.get(url, timeout=timeout) as resp:
                    if resp.status != 200:
                        raise SwarmOperationError(f"HTTP {resp.status}")
                    img_data = await resp.read()
                    filename = url.split("/")[-1].split("?")[0] or "image.png"

        if not img_data:
            raise SwarmOperationError("empty response from URL")

        return self.save_attachment(filename, img_data)

    async def process_email_data(
        self,
        subject: str,
        body_content: str,
        body_type: str,
        attachment_dicts: list[dict[str, Any]],
        effective_id: str,
        *,
        graph_token: str = "",
    ) -> dict[str, Any]:
        """Process fetched email data into task fields.

        Converts HTML to text, saves attachments, generates title, classifies type.
        Returns a dict ready for the frontend task-creation form.
        """
        if body_type.lower() == "html":
            body_text = _html_to_text(body_content)
        else:
            body_text = body_content.strip()

        description = f"Subject: {subject}\n\n{body_text}" if subject else body_text

        paths: list[str] = []
        for att in attachment_dicts:
            if att.get("@odata.type") != "#microsoft.graph.fileAttachment":
                continue
            name = att.get("name", "attachment")
            raw_b64 = att.get("contentBytes", "")
            if raw_b64:
                content_bytes = base64.b64decode(raw_b64)
                if content_bytes:
                    paths.append(self.save_attachment(name, content_bytes))

        title = await smart_title(description) or subject
        task_type = auto_classify_type(title, description)

        return {
            "title": title,
            "description": description,
            "task_type": task_type.value,
            "attachments": paths,
            "message_id": effective_id,
        }

    def _notify_draft_failed(self, task_title: str, task_id: str, error: str) -> None:
        """Broadcast draft-reply failure to WS clients and log it."""
        self._broadcast_ws(
            {
                "type": "draft_reply_failed",
                "task_title": task_title,
                "task_id": task_id,
                "error": error,
            }
        )
        self._drone_log.add(
            SystemAction.DRAFT_FAILED,
            "system",
            f"{task_title[:60]}: {error[:80]}" if error else task_title[:80],
            category=LogCategory.SYSTEM,
            is_notification=True,
        )

    async def send_completion_reply(
        self,
        message_id: str,
        task_title: str,
        task_type: str,
        resolution: str,
        task_id: str = "",
    ) -> None:
        """Draft a reply via Queen and create as draft in Outlook."""
        try:
            # Resolve RFC 822 Message-ID (<...@...>) to Graph message ID
            graph_id = message_id
            if "<" in message_id and "@" in message_id:
                resolved = await self._graph_mgr.resolve_message_id(message_id)
                if not resolved:
                    reason = f"Could not resolve RFC 822 ID '{message_id[:60]}'"
                    _log.warning(reason)
                    self._notify_draft_failed(task_title, task_id, reason)
                    return
                graph_id = resolved

            reply_text = await self._queen.draft_email_reply(task_title, task_type, resolution)
            ok = await self._graph_mgr.create_reply_draft(graph_id, reply_text)
            if ok:
                _log.info("Draft reply created for task '%s'", task_title[:50])
                self._broadcast_ws({"type": "draft_reply_ok", "task_title": task_title})
                self._drone_log.add(
                    SystemAction.DRAFT_OK,
                    "system",
                    task_title[:80],
                    category=LogCategory.SYSTEM,
                )
            else:
                _log.warning("Draft reply failed for task '%s'", task_title[:50])
                self._notify_draft_failed(task_title, task_id, "Graph API returned failure")
        except Exception as exc:  # broad catch: Graph/Queen errors are unpredictable
            _log.warning("Draft reply error for '%s'", task_title[:50], exc_info=True)
            self._notify_draft_failed(task_title, task_id, str(exc)[:200])
