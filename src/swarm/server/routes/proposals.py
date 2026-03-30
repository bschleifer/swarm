"""Proposal routes — list, approve, reject proposals and decision history."""

from __future__ import annotations

from aiohttp import web

from swarm.drones.log import LogCategory, SystemAction
from swarm.server.helpers import get_daemon, handle_errors, parse_limit, parse_offset


def register(app: web.Application) -> None:
    app.router.add_get("/api/proposals", handle_proposals)
    app.router.add_post("/api/proposals/{proposal_id}/approve", handle_approve_proposal)
    app.router.add_post("/api/proposals/{proposal_id}/reject", handle_reject_proposal)
    app.router.add_post("/api/proposals/reject-all", handle_reject_all_proposals)
    app.router.add_post("/api/proposals/approve-all", handle_approve_all_proposals)
    app.router.add_get("/api/decisions", handle_decisions)


@handle_errors
async def handle_proposals(request: web.Request) -> web.Response:
    d = get_daemon(request)
    pending = d.proposal_store.pending
    return web.json_response(
        {
            "proposals": [d.proposal_dict(p) for p in pending],
            "pending_count": len(pending),
        }
    )


@handle_errors
async def handle_approve_proposal(request: web.Request) -> web.Response:
    d = get_daemon(request)
    proposal_id = request.match_info["proposal_id"]
    body = await request.json() if request.can_read_body else {}
    draft_response = bool(body.get("draft_response")) if body else False
    await d.approve_proposal(proposal_id, draft_response=draft_response)
    proposal = d.proposal_store.get(proposal_id)
    if proposal:
        d.worker_svc._record_override(
            proposal.worker_name, "approved_after_skip", "proposal approved via API"
        )
        d.drone_log.add(
            SystemAction.USER_APPROVE,
            proposal.worker_name,
            f"user approved proposal: {proposal.task_title}",
            category=LogCategory.OPERATOR,
        )
    return web.json_response({"status": "approved", "proposal_id": proposal_id})


@handle_errors
async def handle_reject_proposal(request: web.Request) -> web.Response:
    d = get_daemon(request)
    proposal_id = request.match_info["proposal_id"]
    proposal = d.proposal_store.get(proposal_id)
    d.reject_proposal(proposal_id)
    if proposal:
        d.worker_svc._record_override(
            proposal.worker_name, "rejected_approval", "proposal rejected via API"
        )
        d.drone_log.add(
            SystemAction.USER_REJECT,
            proposal.worker_name,
            f"user rejected proposal: {proposal.task_title}",
            category=LogCategory.OPERATOR,
        )
    return web.json_response({"status": "rejected", "proposal_id": proposal_id})


@handle_errors
async def handle_reject_all_proposals(request: web.Request) -> web.Response:
    d = get_daemon(request)
    count = d.reject_all_proposals()
    return web.json_response({"status": "rejected_all", "count": count})


@handle_errors
async def handle_approve_all_proposals(request: web.Request) -> web.Response:
    d = get_daemon(request)
    count = await d.approve_all_proposals()
    return web.json_response({"status": "approved_all", "count": count})


@handle_errors
async def handle_decisions(request: web.Request) -> web.Response:
    d = get_daemon(request)
    limit = parse_limit(request)
    offset = parse_offset(request)
    all_history = list(reversed(d.proposal_store.history))
    total = len(all_history)
    page = all_history[offset : offset + limit]
    return web.json_response(
        {
            "decisions": [d.proposal_dict(p) for p in page],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )
