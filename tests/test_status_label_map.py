"""Coverage test for ``STATUS_LABEL`` — every TaskStatus member must have an entry.

The label map drives every UI surface (filter chips, status badge text, icon
hover titles, edit-modal dropdown). A new TaskStatus added without a matching
label entry would render the raw enum value to operators (e.g. "in_review"
instead of "In Review"), so this test catches the omission at unit-test time
rather than after a release.
"""

from __future__ import annotations

from swarm.tasks.task import STATUS_ICON, STATUS_LABEL, TaskStatus


def test_every_status_has_a_label() -> None:
    missing = [s for s in TaskStatus if s not in STATUS_LABEL]
    assert not missing, f"TaskStatus members missing from STATUS_LABEL: {missing}"


def test_every_status_has_an_icon() -> None:
    missing = [s for s in TaskStatus if s not in STATUS_ICON]
    assert not missing, f"TaskStatus members missing from STATUS_ICON: {missing}"


def test_label_values_are_capitalised_human_strings() -> None:
    """No raw enum spellings leaked into labels (e.g. 'in_progress' vs 'Active')."""
    for status, label in STATUS_LABEL.items():
        assert label, f"empty label for {status.name}"
        assert "_" not in label, f"label for {status.name} looks like a raw enum value: {label!r}"
        assert label[0].isupper(), f"label for {status.name} not capitalised: {label!r}"


def test_v9_canonical_vocabulary() -> None:
    """The v9 vocabulary cleanup locks in these six labels — operator-facing."""
    assert STATUS_LABEL[TaskStatus.BACKLOG] == "Backlog"
    assert STATUS_LABEL[TaskStatus.UNASSIGNED] == "Unassigned"
    assert STATUS_LABEL[TaskStatus.ASSIGNED] == "Assigned"
    assert STATUS_LABEL[TaskStatus.ACTIVE] == "In Progress"
    assert STATUS_LABEL[TaskStatus.DONE] == "Done"
    assert STATUS_LABEL[TaskStatus.FAILED] == "Failed"
