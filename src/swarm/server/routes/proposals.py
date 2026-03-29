"""Proposal routes — list, approve, reject proposals and decision history."""

from __future__ import annotations

from aiohttp import web

from swarm.server.helpers import get_daemon, handle_errors, parse_limit


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
    return web.json_response({"status": "approved", "proposal_id": proposal_id})


@handle_errors
async def handle_reject_proposal(request: web.Request) -> web.Response:
    d = get_daemon(request)
    proposal_id = request.match_info["proposal_id"]
    d.reject_proposal(proposal_id)
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
    history = list(reversed(d.proposal_store.history[-limit:]))
    return web.json_response({"decisions": [d.proposal_dict(p) for p in history]})
