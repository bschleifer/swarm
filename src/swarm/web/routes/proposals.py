"""Proposal action routes: approve, approve-always, add-rule, reject."""

from __future__ import annotations

from aiohttp import web

from swarm.server.daemon import console_log
from swarm.server.helpers import get_daemon, json_error
from swarm.web.app import handle_swarm_errors


@handle_swarm_errors
async def handle_action_approve_proposal(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    proposal_id = data.get("proposal_id", "")
    if not proposal_id:
        return json_error("proposal_id required")
    draft_response = data.get("draft_response") == "true"
    await d.approve_proposal(proposal_id, draft_response=draft_response)
    console_log(f"Proposal approved: {proposal_id[:8]}")
    return web.json_response({"status": "approved", "proposal_id": proposal_id})


@handle_swarm_errors
async def handle_action_approve_always(request: web.Request) -> web.Response:
    """Approve a proposal AND add a permanent drone approval rule."""
    import re as _re

    from swarm.config import DroneApprovalRule

    d = get_daemon(request)
    data = await request.post()
    proposal_id = data.get("proposal_id", "")
    pattern = data.get("pattern", "")
    if not proposal_id:
        return json_error("proposal_id required")
    if not pattern:
        return json_error("pattern required")
    try:
        _re.compile(pattern)
    except _re.error as exc:
        return json_error(f"invalid regex: {exc}", status=400)

    # Check proposal exists before mutating config
    p = d.proposal_store.get(proposal_id)
    if not p:
        return json_error("proposal not found", status=404)

    from swarm.drones.log import DroneAction, LogCategory

    # Append rule to config
    d.config.drones.approval_rules.append(DroneApprovalRule(pattern=pattern, action="approve"))

    # Approve the proposal
    await d.approve_proposal(proposal_id)

    # Hot-reload + save to disk.  sync_rules=True because we just
    # appended to approval_rules and need the DB to reflect it.
    d.config_mgr.hot_apply()
    d.config_mgr.save(sync_rules=True)

    d.drone_log.add(
        DroneAction.OPERATOR,
        p.worker_name,
        f"approval rule added: {pattern}",
        category=LogCategory.OPERATOR,
    )
    console_log(f"Proposal approved + rule added: {pattern}")
    return web.json_response(
        {"status": "approved", "proposal_id": proposal_id, "rule_added": pattern}
    )


@handle_swarm_errors
async def handle_action_add_approval_rule(request: web.Request) -> web.Response:
    """Add a drone approval rule (no proposal required)."""
    import re as _re

    from swarm.config import DroneApprovalRule

    d = get_daemon(request)
    data = await request.post()
    pattern = data.get("pattern", "")
    if not pattern:
        return json_error("pattern required")
    try:
        _re.compile(pattern)
    except _re.error as exc:
        return json_error(f"invalid regex: {exc}", status=400)

    from swarm.drones.log import DroneAction, LogCategory

    d.config.drones.approval_rules.append(DroneApprovalRule(pattern=pattern, action="approve"))
    d.config_mgr.hot_apply()
    # sync_rules=True: this route's sole purpose is to add an approval
    # rule, so the in-memory list is authoritative for this save.
    d.config_mgr.save(sync_rules=True)

    d.drone_log.add(
        DroneAction.OPERATOR,
        "system",
        f"approval rule added: {pattern}",
        category=LogCategory.OPERATOR,
    )
    console_log(f"Approval rule added: {pattern}")
    return web.json_response({"status": "ok", "rule_added": pattern})


@handle_swarm_errors
async def handle_action_reject_proposal(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    proposal_id = data.get("proposal_id", "")
    if not proposal_id:
        return json_error("proposal_id required")
    d.reject_proposal(proposal_id)
    console_log(f"Proposal rejected: {proposal_id[:8]}")
    return web.json_response({"status": "rejected", "proposal_id": proposal_id})


@handle_swarm_errors
async def handle_action_reject_all_proposals(request: web.Request) -> web.Response:
    d = get_daemon(request)
    count = d.reject_all_proposals()
    console_log(f"All proposals rejected ({count})")
    return web.json_response({"status": "rejected_all", "count": count})


def register(app: web.Application) -> None:
    """Register proposal action routes."""
    app.router.add_post("/action/proposal/approve", handle_action_approve_proposal)
    app.router.add_post("/action/proposal/approve-always", handle_action_approve_always)
    app.router.add_post("/action/add-approval-rule", handle_action_add_approval_rule)
    app.router.add_post("/action/proposal/reject", handle_action_reject_proposal)
    app.router.add_post("/action/proposal/reject-all", handle_action_reject_all_proposals)
