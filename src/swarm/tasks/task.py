"""SwarmTask — internal task model for agent coordination."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TypedDict

_log = logging.getLogger("swarm.tasks.task")


class TaskStatus(Enum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskPriority(Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class TaskType(Enum):
    BUG = "bug"
    VERIFY = "verify"
    FEATURE = "feature"
    CHORE = "chore"


@dataclass
class SwarmTask:
    """A unit of work that can be assigned to a worker."""

    title: str
    description: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: TaskStatus = TaskStatus.PENDING
    priority: TaskPriority = TaskPriority.NORMAL
    task_type: TaskType = TaskType.CHORE
    assigned_worker: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    depends_on: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    attachments: list[str] = field(default_factory=list)  # file paths
    resolution: str = ""  # explanation of what was done (filled on completion)
    source_email_id: str = ""  # Graph message ID if created from email
    jira_key: str = ""  # Jira ticket key (e.g. "PROJ-123") if synced from Jira
    number: int = 0  # auto-incrementing display number (set by TaskBoard)

    def assign(self, worker_name: str) -> None:
        self.assigned_worker = worker_name
        self.status = TaskStatus.ASSIGNED
        self.updated_at = time.time()

    def unassign(self) -> None:
        self.assigned_worker = None
        self.status = TaskStatus.PENDING
        self.updated_at = time.time()

    def start(self) -> None:
        self.status = TaskStatus.IN_PROGRESS
        self.updated_at = time.time()

    def complete(self, resolution: str = "") -> None:
        self.status = TaskStatus.COMPLETED
        self.completed_at = time.time()
        self.updated_at = time.time()
        if resolution:
            self.resolution = resolution

    def fail(self) -> None:
        self.status = TaskStatus.FAILED
        self.updated_at = time.time()

    def reopen(self) -> None:
        self.status = TaskStatus.PENDING
        self.assigned_worker = None
        self.completed_at = None
        self.resolution = ""
        self.updated_at = time.time()

    @property
    def is_available(self) -> bool:
        """True when task is pending."""
        return self.status == TaskStatus.PENDING

    @property
    def age(self) -> float:
        return time.time() - self.created_at


class TaskDict(TypedDict):
    """Typed shape of task serialization for API/WebSocket responses."""

    id: str
    title: str
    description: str
    status: str
    priority: str
    task_type: str
    assigned_worker: str | None
    tags: list[str]
    attachments: list[str]
    depends_on: list[str]
    resolution: str
    source_email_id: str
    number: int


# Canonical display constants — single source of truth for all UIs
STATUS_ICON = {
    TaskStatus.PENDING: "○",
    TaskStatus.ASSIGNED: "◐",
    TaskStatus.IN_PROGRESS: "●",
    TaskStatus.COMPLETED: "✓",
    TaskStatus.FAILED: "✗",
}

PRIORITY_LABEL = {
    TaskPriority.URGENT: "!!",
    TaskPriority.HIGH: "!",
    TaskPriority.NORMAL: "",
    TaskPriority.LOW: "↓",
}

PRIORITY_MAP: dict[str, TaskPriority] = {
    "low": TaskPriority.LOW,
    "normal": TaskPriority.NORMAL,
    "high": TaskPriority.HIGH,
    "urgent": TaskPriority.URGENT,
}

TYPE_MAP: dict[str, TaskType] = {
    "bug": TaskType.BUG,
    "verify": TaskType.VERIFY,
    "feature": TaskType.FEATURE,
    "chore": TaskType.CHORE,
}


def validate_priority(raw: str) -> TaskPriority:
    """Parse and validate a priority string.

    Raises ``ValueError`` on invalid input.
    """
    if raw not in PRIORITY_MAP:
        opts = ", ".join(sorted(PRIORITY_MAP))
        raise ValueError(f"priority must be one of: {opts}")
    return PRIORITY_MAP[raw]


def validate_task_type(raw: str) -> TaskType:
    """Parse and validate a task_type string.

    Raises ``ValueError`` on invalid input.
    """
    if raw not in TYPE_MAP:
        opts = ", ".join(sorted(TYPE_MAP))
        raise ValueError(f"task_type must be one of: {opts}")
    return TYPE_MAP[raw]


TASK_TYPE_LABEL: dict[TaskType, str] = {
    TaskType.BUG: "Bug Fix",
    TaskType.VERIFY: "Verification",
    TaskType.FEATURE: "Feature",
    TaskType.CHORE: "Chore",
}

# Keywords for auto-classification (checked against title + description, case-insensitive)
_BUG_KEYWORDS = (
    "bug",
    "fix",
    "broken",
    "crash",
    "error",
    "fail",
    "issue",
    "defect",
    "regression",
    "wrong",
    "incorrect",
    "not working",
    "doesn't work",
)
_VERIFY_KEYWORDS = (
    "verify",
    "check",
    "confirm",
    "test",
    "validate",
    "qa",
    "review",
    "ensure",
    "audit",
    "inspect",
)
_FEATURE_KEYWORDS = (
    "add",
    "new",
    "feature",
    "implement",
    "create",
    "build",
    "introduce",
    "support",
    "enable",
    "extend",
)


def auto_classify_type(title: str, description: str = "") -> TaskType:
    """Classify task type from title and description using keyword matching.

    Returns the best-match TaskType, defaulting to CHORE if ambiguous.
    """
    text = f"{title} {description}".lower()

    bug_score = sum(1 for kw in _BUG_KEYWORDS if kw in text)
    verify_score = sum(1 for kw in _VERIFY_KEYWORDS if kw in text)
    feature_score = sum(1 for kw in _FEATURE_KEYWORDS if kw in text)

    best = max(bug_score, verify_score, feature_score)
    if best == 0:
        return TaskType.CHORE

    # Require clear winner (no ties with another category)
    scores = [bug_score, verify_score, feature_score]
    if scores.count(best) > 1:
        return TaskType.CHORE

    if bug_score == best:
        return TaskType.BUG
    if verify_score == best:
        return TaskType.VERIFY
    return TaskType.FEATURE


def _decode_payload(part, *, strip_html: bool = False) -> str:
    """Decode a MIME part payload to a string."""
    import re as _re

    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        text = payload.decode(charset)
    except (UnicodeDecodeError, LookupError):
        text = payload.decode("latin-1", errors="replace")
    if strip_html:
        text = _re.sub(r"<[^>]+>", "", text).strip()
    return text


def parse_email(raw_bytes: bytes, *, filename: str = "") -> dict[str, Any]:
    """Parse a .eml or .msg file and extract subject, body, and attachments.

    Returns ``{"subject": str, "body": str, "attachments": [{"filename": str, "data": bytes}]}``.
    """
    if filename.lower().endswith(".msg") or _looks_like_msg(raw_bytes):
        return _parse_msg(raw_bytes)
    return _parse_eml(raw_bytes)


def _looks_like_msg(data: bytes) -> bool:
    """Check for OLE2 magic bytes (Outlook .msg files)."""
    return data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _parse_eml(raw_bytes: bytes) -> dict[str, Any]:
    """Parse an RFC 822 .eml file."""
    import email
    import email.policy

    msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
    subject = str(msg.get("subject", "")).strip()

    body = ""
    attachments: list[dict] = []

    if msg.is_multipart():
        for part in msg.walk():
            disposition = str(part.get("Content-Disposition", ""))
            if "attachment" in disposition:
                fname = part.get_filename() or "attachment"
                data = part.get_payload(decode=True) or b""
                attachments.append({"filename": fname, "data": data})
            elif part.get_content_type() == "text/plain" and not body:
                body = _decode_payload(part)
            elif part.get_content_type() == "text/html" and not body:
                body = _decode_payload(part, strip_html=True)
    else:
        is_html = msg.get_content_type() == "text/html"
        body = _decode_payload(msg, strip_html=is_html)

    message_id = str(msg.get("Message-ID", "")).strip()
    return {
        "subject": subject,
        "body": body.strip(),
        "attachments": attachments,
        "message_id": message_id,
    }


def _parse_msg(raw_bytes: bytes) -> dict[str, Any]:
    """Parse an Outlook .msg file using extract-msg."""
    import re as _re
    import tempfile

    try:
        import extract_msg
    except ImportError:
        _log.warning("extract-msg not installed — cannot parse .msg files")
        return {"subject": "", "body": "", "attachments": []}

    with tempfile.NamedTemporaryFile(suffix=".msg", delete=True) as tmp:
        tmp.write(raw_bytes)
        tmp.flush()
        msg = extract_msg.openMsg(tmp.name)

    subject = (msg.subject or "").strip()
    body = (msg.body or "").strip()
    if not body:
        html = msg.htmlBody
        if html:
            text = html.decode("utf-8", errors="replace") if isinstance(html, bytes) else html
            body = _re.sub(r"<[^>]+>", " ", text).strip()
            body = _re.sub(r"\s+", " ", body).strip()

    attachments: list[dict] = []
    for att in msg.attachments or []:
        fname = getattr(att, "longFilename", None) or getattr(att, "shortFilename", "attachment")
        data = getattr(att, "data", b"") or b""
        if fname and data:
            attachments.append({"filename": fname, "data": data})

    message_id = getattr(msg, "messageId", "") or ""
    msg.close()
    return {"subject": subject, "body": body, "attachments": attachments, "message_id": message_id}


async def smart_title(description: str, max_len: int = 80) -> str:
    """Generate a concise task title using a headless LLM call.

    Uses the configured provider's headless command with a 15-second timeout.
    Falls back to :func:`auto_title` on any failure.
    """
    if not description or not description.strip():
        return ""
    truncated = description[:2000]  # limit prompt size
    prompt = (
        f"Generate a concise task title (max {max_len} chars) for this task. "
        f"Return ONLY the title, no quotes or extra text.\n\n{truncated}"
    )
    try:
        from swarm.providers import get_provider

        provider = get_provider()
        args = provider.headless_command(prompt, output_format="text", max_turns=1)
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            err_msg = stderr.decode(errors="replace").strip()[:200]
            _log.warning("smart_title: LLM exited %d: %s", proc.returncode, err_msg)
            return auto_title(description, max_len)
        title = stdout.decode().strip().strip('"').strip("'").strip()
        if not title:
            _log.warning("smart_title: LLM returned empty output")
            return auto_title(description, max_len)
        # Truncate if too long
        if len(title) > max_len:
            title = title[: max_len - 1] + "\u2026"
        _log.debug("smart_title: generated %r", title)
        return title
    except TimeoutError:
        _log.warning("smart_title: LLM timed out after 15s")
    except FileNotFoundError:
        _log.warning("smart_title: LLM binary not found")
    except OSError as e:
        _log.warning("smart_title: OS error spawning LLM: %s", e)
    return auto_title(description, max_len)


def auto_title(description: str, max_len: int = 80) -> str:
    """Generate a title from the first line/sentence of a description.

    Returns the first non-empty line, truncated to *max_len* characters.
    Returns ``""`` when *description* is blank.
    """
    if not description or not description.strip():
        return ""
    first_line = description.strip().splitlines()[0].strip()
    if len(first_line) <= max_len:
        return first_line
    return first_line[: max_len - 1] + "\u2026"
