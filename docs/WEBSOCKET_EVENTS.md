# Nexus WebSocket Events (Component 13)

Live notifications for the authenticated frontend. Events are
**hints, not truth**: every state change is persisted first and
emitted second; clients recover after any disconnect by querying the
REST API, then consume future events. There is no durable event log
and no replay: a process restart loses everything undelivered.

## Connecting

1. Call `POST /api/v1/events/ticket` with the REST bearer token.
   The response carries one cryptographically random ticket plus its
   UTC expiry (60 seconds by default, `NEXUS_API_TICKET_TTL_S`).
2. Open `/api/v1/events/ws` within that window, presenting the
   ticket as a `Sec-WebSocket-Protocol` entry
   `nexus-ticket.<ticket>` — the only accepted slot. In a browser:

   ```js
   const {ticket} = await (await fetch("/api/v1/events/ticket",
     {method: "POST", headers: {Authorization: "Bearer " + token}}
   )).json();
   const ws = new WebSocket("ws://127.0.0.1:8000/api/v1/events/ws",
     ["nexus-ticket." + ticket]);
   ```
   Query-string tickets are not honored: server access logs record
   request paths including query strings, which would persist a
   bearer-equivalent credential outside the application's control.
3. The ticket is consumed exactly once: expired, unknown, reused, or
   foreign-owner tickets close the socket with code `4401`. Simultaneous
   redemptions race safely — exactly one wins.

The long-lived REST bearer token never belongs in a URL. Tickets are
never logged and never appear in error bodies.

## Origin policy

Same-origin browser pages (matching `Origin`/`Host`) and clients
sending no `Origin` are accepted; anything else must be listed in
`NEXUS_API_ALLOWED_ORIGINS` (comma-separated exact origins).
Disallowed origins get HTTP `403` before any WebSocket handshake.
There is intentionally no wildcard mode.

## Event envelope

```json
{"event_id": "evt_<hex>", "type": "<dotted name>",
 "timestamp": "<UTC ISO-8601>",
 "data": {"request_id": "req_…", "status": "COMPLETED"}}
```

`event_id` values are unique per emission; `timestamp` values are
timezone-aware UTC. `data` carries identifiers and terminal statuses
only — never tokens, tickets, keys, signatures, system instructions,
traces, credentials, or conversation/query contents. Fetch full
records through the authenticated REST API.

## Event types and sources

| Type | Emitted after |
|---|---|
| `conversation.turn.completed` | A chat turn's user and assistant rows commit (`user/assistant` ids, model label) |
| `remote.request.created` | An outbound request commits (`QUEUED`) |
| `remote.request.approval_required` | Component 9 registers an inbound query (`WAITING_APPROVAL`) |
| `remote.request.updated` | Approval, rejection, or a terminal execution/receiver outcome commits |
| `remote.response.received` | Component 11 persists a response and completes/fails the request |
| `execution.updated` | `RemoteExecutor` returns an outcome for an approved request |

Events fire only for committed transitions: rolled-back operations,
duplicate deliveries (no new transition), and rejected responses emit
nothing. Execution outcomes report persisted results (`COMPLETED`,
`FAILED`, or live-claim `IN_PROGRESS`) — never forecasts.

## Backpressure and reconnect

Each connection drains a bounded queue (default 100). A subscriber
that cannot keep up is closed with code `1013` ("try again later")
instead of stalling publishers or losing mail silently. Publishers
never block on readers: database work and inbound dispatch proceed
regardless of who is listening.

Reconnect flow: obtain a fresh ticket, open a new socket,
re-query REST state (`GET /requests/{id}`, conversation history),
then consume new events. Tickets are never reused and in-memory
queues do not survive restarts.

Peer-gone disconnects attempt `1001`; overload and shutdown
sweeps close with `1013` ("try again later" — the hub marks
overloaded subscriptions closed and drops them, and shutdown clears
every subscription, so pumps observing the close signal terminate
with `1013`); authentication failures close with `4401`. Cancelled
server tasks close no frame at all: the transport simply goes away,
and the client must reconnect with a fresh ticket.

## Scaling limits

The hub is in-process and single-owner: one hub per API process,
one subscription set per deployment owner. Two API worker processes
would have separate hubs (no shared delivery). A distributed broker
is a future scaling requirement, not a current guarantee.
