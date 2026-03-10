"""Queen action routes: ask-queen, ask-queen-question, ask-queen-worker."""

from __future__ import annotations

from aiohttp import web

from swarm.server.daemon import console_log
from swarm.server.helpers import get_daemon, json_error
from swarm.tasks.proposal import QueenAction
from swarm.tasks.task import TaskStatus
from swarm.web.app import _require_queen, handle_swarm_errors


@handle_swarm_errors
async def handle_action_ask_queen(request: web.Request) -> web.Response:
    d = get_daemon(request)
    queen = _require_queen(d)

    console_log("Queen coordinating hive...")
    result = await d.coordinate_hive(force=True)
    n = len(result.get("directives", []))
    console_log(f"Queen done — {n} directive(s)")
    result["cooldown"] = queen.cooldown_remaining
    return web.json_response(result)


@handle_swarm_errors
async def handle_action_ask_queen_question(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    question = data.get("question", "").strip()
    if not question:
        return json_error("question required")

    queen = _require_queen(d)

    console_log(f"Queen asked: {question[:60]}...")
    hive_ctx = await d.gather_hive_context()
    prompt = (
        f"You are the Queen of a swarm of {queen.provider_display_name} agents.\n\n"
        f"{hive_ctx}\n\n"
        f"The operator asks: {question}\n\n"
        f"Respond with a JSON object:\n"
        f'{{\n  "assessment": "your analysis",\n'
        f'  "directives": ['
        f'{{"worker": "name", '
        f'"action": "continue|send_message|restart|wait", '
        f'"message": "if applicable", "reason": "why"}}],\n'
        f'  "suggestions": ["any high-level suggestions"]\n}}'
    )
    result = await queen.ask(prompt, force=True)
    console_log("Queen answered operator question")
    result["cooldown"] = queen.cooldown_remaining
    return web.json_response(result)


@handle_swarm_errors
async def handle_action_ask_queen_worker(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]

    _require_queen(d)

    console_log(f'Queen analyzing "{name}"...')
    result = await d.analyze_worker(name, force=True)
    console_log(f'Queen analysis of "{name}" complete')

    # If Queen recommends complete_task, create a proper completion proposal
    # so the user gets the full completion dialog (with draft email option).
    if result.get("action") == QueenAction.COMPLETE_TASK and d.task_board:
        from swarm.tasks.proposal import AssignmentProposal

        active = [
            t
            for t in d.task_board.tasks_for_worker(name)
            if t.status in (TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS)
        ]
        if active:
            task = active[0]
            proposal = AssignmentProposal.completion(
                worker_name=name,
                task_id=task.id,
                task_title=task.title,
                assessment=result.get("assessment", ""),
                reasoning=result.get("reasoning", ""),
                confidence=result.get("confidence", 0.8),
            )
            d.queue_proposal(proposal)
            console_log(f'Created completion proposal for "{task.title}"')

    result["cooldown"] = d.queen.cooldown_remaining
    return web.json_response(result)


def register(app: web.Application) -> None:
    """Register queen action routes."""
    app.router.add_post("/action/ask-queen", handle_action_ask_queen)
    app.router.add_post("/action/ask-queen-question", handle_action_ask_queen_question)
    app.router.add_post("/action/ask-queen/{name}", handle_action_ask_queen_worker)
