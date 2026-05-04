"""Task message builder — pure functions for formatting task messages to workers."""

from __future__ import annotations

from pathlib import Path

from swarm.tasks.task import SwarmTask

_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"})
# Plain-text-ish formats Read handles natively.
_TEXT_EXTENSIONS = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".rst",
        ".json",
        ".yaml",
        ".yml",
        ".csv",
        ".tsv",
        ".log",
        ".xml",
        ".html",
        ".htm",
        ".ini",
        ".toml",
        ".eml",
    }
)
_DOCX_EXTENSIONS = frozenset({".docx", ".doc"})
_PDF_EXTENSIONS = frozenset({".pdf"})
_XLSX_EXTENSIONS = frozenset({".xlsx", ".xls", ".ods"})
_PPTX_EXTENSIONS = frozenset({".pptx", ".ppt", ".odp"})

_COMPLETION_INSTRUCTIONS = """\

When done, use the swarm_complete_task MCP tool with a brief resolution summary.
If the task originated from another worker, send them a swarm_send_message with your findings."""


def _attachment_hint(path: str) -> str:
    """Return a one-line instruction telling the worker how to read this file.

    Read handles plain-text and images natively. Office formats are zipped
    XML / proprietary binary — Read returns garbled bytes — so we name the
    common conversion command (pandoc / pdftotext / docx2txt / openpyxl)
    so the worker doesn't have to guess.
    """
    ext = Path(path).suffix.lower()
    if ext in _IMAGE_EXTENSIONS:
        return f"IMAGE: {path} — Use the Read tool to view this image file."
    if ext in _TEXT_EXTENSIONS:
        return f"TEXT: {path} — Use the Read tool for context."
    if ext in _DOCX_EXTENSIONS:
        return (
            f"WORD DOC: {path} — Read returns binary garbage on .docx; convert first. "
            f"Try: `pandoc {path!r} -t plain` "
            f'or `python -c "import docx2txt; print(docx2txt.process({path!r}))"`.'
        )
    if ext in _PDF_EXTENSIONS:
        return (
            f"PDF: {path} — Convert to text first. "
            f"Try: `pdftotext {path!r} -` "
            f'or `python -c "import pypdf; r=pypdf.PdfReader({path!r}); '
            f"print('\\n'.join(p.extract_text() for p in r.pages))\"`."
        )
    if ext in _XLSX_EXTENSIONS:
        return (
            f"SPREADSHEET: {path} — Read won't help; load with openpyxl or convert to CSV. "
            f'Try: `python -c "import openpyxl; wb=openpyxl.load_workbook({path!r}); '
            f'[print(s.title, list(s.values)) for s in wb]"`.'
        )
    if ext in _PPTX_EXTENSIONS:
        return (
            f"PRESENTATION: {path} — Convert to text. "
            f"Try: `pandoc {path!r} -t plain` "
            f'or `python -c "import pptx; p=pptx.Presentation({path!r}); ..."`.'
        )
    return (
        f"{path} — Try the Read tool first. If it returns binary, run "
        f"`file {path!r}` to identify the format, then pick a converter."
    )


def task_detail_parts(task: SwarmTask) -> list[str]:
    """Collect title, description, tags, and source metadata into a parts list."""
    parts: list[str] = [f"#{task.number}: {task.title}" if task.number else task.title]
    if task.description:
        parts.append(task.description)
    if task.tags:
        parts.append(f"Tags: {', '.join(task.tags)}")
    # Source metadata — lets the worker know the task's external origin
    source_parts: list[str] = []
    if task.jira_key:
        source_parts.append(f"Jira: {task.jira_key}")
    if task.source_email_id:
        source_parts.append(f"Email: {task.source_email_id}")
    if source_parts:
        parts.append(f"Source: {', '.join(source_parts)}")
    return parts


def attachment_lines(task: SwarmTask) -> str:
    """Format attachment paths as separate lines for the worker.

    Per-format hints tell the worker which tool to use — Read for images
    and plain text, conversion commands (pandoc / pdftotext / openpyxl)
    for Office binary formats where Read would just return zip bytes.
    """
    if not task.attachments:
        return ""
    lines = ["\nAttachments:"]
    for a in task.attachments:
        lines.append(f"  - {_attachment_hint(a)}")
    return "\n".join(lines)


def build_task_message(task: SwarmTask, *, supports_slash_commands: bool = True) -> str:
    """Build a message string describing a task for a worker.

    If the task type has a dedicated Claude Code skill (e.g. ``/feature``),
    the message is formatted as a skill invocation so the worker's Claude
    session handles the full pipeline.  Otherwise, inline workflow steps
    are appended as before.

    When *supports_slash_commands* is False (non-Claude providers), skill
    invocations are skipped and inline workflow instructions are used instead.

    Attachments are always listed on separate lines (never squished into
    the skill command's quoted argument) so the worker can see and read them.
    """
    from swarm.tasks.workflows import get_skill_command, get_workflow_instructions

    completion = _COMPLETION_INSTRUCTIONS.format(task_id=task.id)

    skill = get_skill_command(task.task_type) if supports_slash_commands else None
    if skill:
        desc = " ".join(task_detail_parts(task))
        msg = f'{skill} "{desc}"'
        atts = attachment_lines(task)
        if atts:
            msg = f"{msg}{atts}"
        return msg + completion

    # Fallback: inline workflow instructions (CHORE, unknown types).
    prefix = f"Task #{task.number}: " if task.number else "Task: "
    parts = [f"{prefix}{task.title}"]
    if task.description:
        parts.append(f"\n{task.description}")
    atts = attachment_lines(task)
    if atts:
        parts.append(atts)
    if task.tags:
        parts.append(f"\nTags: {', '.join(task.tags)}")
    # Source metadata (inline path)
    source_parts: list[str] = []
    if task.jira_key:
        source_parts.append(f"Jira: {task.jira_key}")
    if task.source_email_id:
        source_parts.append(f"Email: {task.source_email_id}")
    if source_parts:
        parts.append(f"\nSource: {', '.join(source_parts)}")
    workflow = get_workflow_instructions(task.task_type)
    if workflow:
        parts.append(f"\n{workflow}")
    parts.append(completion)
    return "\n".join(parts)
