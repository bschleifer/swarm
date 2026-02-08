"""Task panel — TUI widget for viewing and managing the task board."""

from __future__ import annotations

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
    Static,
    TextArea,
)

from swarm.tasks.board import TaskBoard
from swarm.tasks.task import STATUS_ICON, SwarmTask, TaskPriority

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
        super().__init__(**kwargs)

    def compose(self) -> ComposeResult:
        yield Static("", id="task-summary")
        yield ListView(id="tasks-lv")

    def on_mount(self) -> None:
        self.refresh_tasks()

    def refresh_tasks(self) -> None:
        self._tasks = self.board.all_tasks
        lv = self.query_one("#tasks-lv", ListView)
        lv.clear()
        for task in self._tasks:
            icon = STATUS_ICON.get(task.status, "?")
            pri = _PRIORITY_LABEL.get(task.priority, " ")
            worker = f" → {task.assigned_worker}" if task.assigned_worker else ""
            label = f"{icon} {pri} {task.title}{worker}"
            lv.append(ListItem(Label(label, markup=True), id=f"task-{task.id}"))
        self.query_one("#task-summary", Static).update(self.board.summary())

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        idx = event.list_view.index
        if idx is not None and 0 <= idx < len(self._tasks):
            self.post_message(TaskSelected(self._tasks[idx]))


class CreateTaskModal(ModalScreen[SwarmTask | None]):
    """Modal for creating a new task."""

    BINDINGS = [("escape", "dismiss(None)", "Close")]

    def __init__(self, workers: list[str] | None = None) -> None:
        self._worker_names = workers or []
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
        pri_map = {
            "low": TaskPriority.LOW,
            "normal": TaskPriority.NORMAL,
            "high": TaskPriority.HIGH,
            "urgent": TaskPriority.URGENT,
        }
        task = SwarmTask(
            title=title,
            description=desc,
            priority=pri_map.get(str(pri_val), TaskPriority.NORMAL),
        )
        self.dismiss(task)
