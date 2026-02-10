"""Task panel — TUI widget for viewing and managing the task board."""

from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import (
    Button,
    Input,
    Label,
    ListItem,
    ListView,
    Select,
    TextArea,
)

from swarm.tasks.board import TaskBoard
from swarm.tasks.task import PRIORITY_MAP, STATUS_ICON, SwarmTask, TaskPriority


@dataclass
class TaskEditResult:
    """Result from the EditTaskModal."""

    task_id: str
    title: str
    description: str
    priority: TaskPriority
    tags: list[str]


# Rich markup variants for TUI display
_PRIORITY_LABEL = {
    TaskPriority.URGENT: "[bold red]!![/]",
    TaskPriority.HIGH: "[red]![/]",
    TaskPriority.NORMAL: " ",
    TaskPriority.LOW: "[dim]↓[/]",
}


class TaskSelected(Message):
    """Fired when a task is selected in the panel."""

    def __init__(self, task: SwarmTask) -> None:
        self.task = task
        super().__init__()


class TaskPanelWidget(Widget):
    """Task board panel showing all tasks with status."""

    def __init__(self, board: TaskBoard, **kwargs) -> None:
        self.board = board
        self._tasks: list[SwarmTask] = []
        self._last_labels: dict[str, str] = {}
        super().__init__(**kwargs)

    def compose(self) -> ComposeResult:
        yield ListView(id="tasks-lv")

    def on_mount(self) -> None:
        self.refresh_tasks()

    def refresh_tasks(self) -> None:
        self._tasks = self.board.all_tasks
        lv = self.query_one("#tasks-lv", ListView)

        # Build desired label for each task
        new_labels: dict[str, str] = {}
        for task in self._tasks:
            icon = STATUS_ICON.get(task.status, "?")
            pri = _PRIORITY_LABEL.get(task.priority, " ")
            worker = f" → {task.assigned_worker}" if task.assigned_worker else ""
            new_labels[task.id] = f"{icon} {pri} {task.title}{worker}"

        current_ids = [t.id for t in self._tasks]
        existing = list(lv.query(ListItem))
        existing_ids = [item.id.removeprefix("task-") for item in existing if item.id]

        if existing_ids != current_ids:
            # Task set/order changed — full rebuild
            lv.clear()
            for task in self._tasks:
                lv.append(ListItem(Label(new_labels[task.id], markup=True), id=f"task-{task.id}"))
        else:
            # Same tasks — update labels in-place only when changed
            for item in existing:
                task_id = item.id.removeprefix("task-") if item.id else ""
                new_text = new_labels.get(task_id, "")
                if new_text != self._last_labels.get(task_id, ""):
                    item.query_one(Label).update(new_text, layout=False)

        self._last_labels = new_labels
        self.border_subtitle = self.board.summary()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        idx = event.list_view.index
        if idx is not None and 0 <= idx < len(self._tasks):
            self.post_message(TaskSelected(self._tasks[idx]))


class CreateTaskModal(ModalScreen[SwarmTask | None]):
    """Modal for creating a new task."""

    BINDINGS = [("escape", "dismiss(None)", "Close")]

    def __init__(self) -> None:
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="create-task-dialog"):
            yield Label("Create Task", id="create-task-title")
            yield Input(placeholder="Task title", id="task-title-input")
            yield TextArea(id="task-desc-input")
            yield Select(
                [
                    ("Normal", "normal"),
                    ("High", "high"),
                    ("Urgent", "urgent"),
                    ("Low", "low"),
                ],
                value="normal",
                id="task-priority",
            )
            with Horizontal(id="create-task-buttons"):
                yield Button("Create", variant="primary", id="create-btn")
                yield Button("Cancel", id="create-cancel-btn")

    def on_mount(self) -> None:
        self.query_one("#task-title-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "create-btn":
            self._submit()
        elif event.button.id == "create-cancel-btn":
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "task-title-input":
            self.query_one("#task-desc-input", TextArea).focus()

    def _submit(self) -> None:
        title = self.query_one("#task-title-input", Input).value.strip()
        if not title:
            self.dismiss(None)
            return
        desc = self.query_one("#task-desc-input", TextArea).text.strip()
        pri_val = self.query_one("#task-priority", Select).value
        task = SwarmTask(
            title=title,
            description=desc,
            priority=PRIORITY_MAP.get(str(pri_val), TaskPriority.NORMAL),
        )
        self.dismiss(task)


class EditTaskModal(ModalScreen[TaskEditResult | None]):
    """Modal for editing an existing task."""

    BINDINGS = [("escape", "dismiss(None)", "Close")]

    def __init__(self, task: SwarmTask) -> None:
        self._task = task
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="edit-task-dialog"):
            yield Label("Edit Task", id="edit-task-title")
            yield Input(
                value=self._task.title,
                placeholder="Task title",
                id="edit-title-input",
            )
            yield TextArea(id="edit-desc-input")
            yield Select(
                [
                    ("Normal", "normal"),
                    ("High", "high"),
                    ("Urgent", "urgent"),
                    ("Low", "low"),
                ],
                value=self._task.priority.value,
                id="edit-priority",
            )
            yield Input(
                value=", ".join(self._task.tags),
                placeholder="Tags (comma-separated)",
                id="edit-tags-input",
            )
            if self._task.attachments:
                yield Label(
                    "Attachments: " + ", ".join(self._task.attachments),
                    id="edit-attachments-label",
                )
            with Horizontal(id="edit-task-buttons"):
                yield Button("Save", variant="primary", id="edit-save-btn")
                yield Button("Cancel", id="edit-cancel-btn")

    def on_mount(self) -> None:
        ta = self.query_one("#edit-desc-input", TextArea)
        ta.load_text(self._task.description)
        self.query_one("#edit-title-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "edit-save-btn":
            self._submit()
        elif event.button.id == "edit-cancel-btn":
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "edit-title-input":
            self.query_one("#edit-desc-input", TextArea).focus()

    def _submit(self) -> None:
        title = self.query_one("#edit-title-input", Input).value.strip()
        if not title:
            self.dismiss(None)
            return
        desc = self.query_one("#edit-desc-input", TextArea).text.strip()
        pri_val = self.query_one("#edit-priority", Select).value
        tags_raw = self.query_one("#edit-tags-input", Input).value.strip()
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else []
        self.dismiss(
            TaskEditResult(
                task_id=self._task.id,
                title=title,
                description=desc,
                priority=PRIORITY_MAP.get(str(pri_val), TaskPriority.NORMAL),
                tags=tags,
            )
        )
