"""Component 13 event endpoints: ticket issuance and WebSocket fan-out.

``POST /api/v1/events/ticket`` spends the REST bearer token for a
short-lived, single-use, owner-bound ticket. ``/api/v1/events/ws``
redeems exactly one ticket per connection and then pumps hub events
until disconnect, overload, cancellation, or shutdown. Ticket values
never appear in logs or error bodies.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from backend.api import deps
from backend.api.events import (
    CLOSE_GOING_AWAY,
    CLOSE_OVERLOADED,
    CLOSE_UNAUTHORIZED,
    AppEvent,
    EventHub,
)

logger = logging.getLogger("nexus.api.events")

router = APIRouter(tags=["events"])

#: Subprotocol slot carrying the ticket (preferred over URL query,
#: which intermediaries may log).
_TICKET_SUBPROTOCOL_PREFIX = "nexus-ticket."


class TicketBody(BaseModel):
    """Issued ticket (transported once, then redeemed and forgotten)."""

    model_config = ConfigDict(extra="forbid")

    ticket: str
    expires_at: str


#: Fixed subprotocol the server selects when the client offered it.
#: Browsers (per the WHATWG WebSocket standard) fail the connection when
#: they offered subprotocols but the server selects none, so accepting
#: without a selection would make real browsers unable to connect at all.
#: The ticket itself is never echoed here: only this fixed, ticket-free
#: name is negotiated. Clients must still present the ticket in the
#: ``nexus-ticket.<ticket>`` request protocol; that is the only slot the
#: server reads it from.
NEGOTIATED_SUBPROTOCOL = "nexus-events"


def _ticket_from_headers(websocket: WebSocket) -> str | None:
    """Extract the ticket from the subprotocol header, if present."""
    raw = websocket.headers.get("sec-websocket-protocol", "")
    for offered in raw.split(","):
        candidate = offered.strip()
        if candidate.startswith(_TICKET_SUBPROTOCOL_PREFIX):
            value = candidate[len(_TICKET_SUBPROTOCOL_PREFIX):]
            if value:
                return value
    return None


def _origin_allowed(request: Any, settings: Any) -> bool:
    """Same-origin (or absent Origin) plus explicit allow-list only."""
    origin = request.headers.get("origin")
    if not origin:
        return True
    try:
        host = request.headers.get("host", "")
        if urlsplit(origin).netloc.lower() == host.lower() and host:
            return True
    except ValueError:
        return False
    allowed = getattr(settings, "allowed_origins", []) or []
    return origin in allowed


@router.post("/events/ticket", response_model=TicketBody)
async def issue_ticket(
    request: Request, settings=Depends(deps.authenticate),
) -> TicketBody:
    """Spend REST authentication for one single-use WebSocket ticket."""
    state = deps.state_of(request)
    ticket, expires_at = state.tickets.issue(settings.owner_user_id)
    return TicketBody(ticket=ticket, expires_at=expires_at.isoformat())


@router.websocket("/events/ws")
async def events_socket(websocket: WebSocket) -> None:
    """Authenticated event stream for the ticket's owner."""
    settings = websocket.app.state.settings
    if not _origin_allowed(websocket, settings):
        # Deny the upgrade before accepting: Starlette translates a
        # WebSocketException raised pre-accept into a 403 handshake
        # rejection (no socket, no ticket consumption).
        raise WebSocketException(code=4403)
    # Select the fixed subprotocol (never the ticket) when the client
    # offered it: browsers fail the handshake when they offered protocols
    # but the server selects none, and equally when the server selects one
    # they did not offer. Non-browser clients that offer nothing still get
    # a plain accept.
    offered = {
        entry.strip()
        for entry in websocket.headers.get(
            "sec-websocket-protocol", "").split(",")
    }
    selected = (
        NEGOTIATED_SUBPROTOCOL if NEGOTIATED_SUBPROTOCOL in offered else None
    )
    await websocket.accept(subprotocol=selected)
    ticket = _ticket_from_headers(websocket)
    state = websocket.app.state.api
    if (
        state is None
        or not ticket
        or not state.tickets.redeem(ticket, settings.owner_user_id)
    ):
        await websocket.close(code=CLOSE_UNAUTHORIZED)
        return
    subscription_id = state.events.subscribe()
    try:
        await _pump_connection(websocket, state.events, subscription_id)
    finally:
        state.events.unsubscribe(subscription_id)


async def _pump_connection(
    websocket: WebSocket, hub: EventHub, subscription_id: str
) -> None:
    """Deliver queued events until close, overload, or cancellation.

    A hub-side overload closes the socket with ``CLOSE_OVERLOADED``
    instead of dropping mail silently; anything else ends the pump
    quietly after unsubscribing. Cancellation always propagates.
    """
    queue = hub.queue_of(subscription_id)
    closed = hub.close_of(subscription_id)
    if queue is None or closed is None:
        # Only reachable when the hub already dropped this entry
        # (overload between subscribe and pump start).
        try:
            await websocket.close(code=CLOSE_OVERLOADED)
        except Exception:  # noqa: BLE001 - already gone
            pass
        return
    closed_task = asyncio.ensure_future(closed.wait())
    get_task: asyncio.Task | None = None
    try:
        while True:
            if hub.is_closed(subscription_id):
                break
            get_task = asyncio.ensure_future(queue.get())
            done, pending = await asyncio.wait(
                {get_task, closed_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if closed_task in done:
                for task in pending:
                    task.cancel()
                break
            event = get_task.result()
            get_task = None
            if not isinstance(event, AppEvent):
                continue
            try:
                await websocket.send_json(event.to_dict())
            except Exception:  # noqa: BLE001 - peer gone; just stop
                break
    except asyncio.CancelledError:
        raise
    finally:
        closed_task.cancel()
        if get_task is not None and not get_task.done():
            get_task.cancel()
        # Decided here (not on the exit path): the hub marks overload
        # by removing the subscription, so a closed-or-gone entry at
        # teardown means overload even if the exit itself was a send
        # failure or a wake-up race.
        overloaded = hub.is_closed(subscription_id)
        hub.unsubscribe(subscription_id)
        try:
            await websocket.close(
                code=CLOSE_OVERLOADED if overloaded else CLOSE_GOING_AWAY
            )
        except Exception:  # noqa: BLE001 - already gone
            pass
