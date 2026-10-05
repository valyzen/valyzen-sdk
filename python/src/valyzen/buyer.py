"""An LLM-driven buyer over the SDK, with the model layer injectable.

The model sees the public state (catalogue, offer on the table, the arbiter's
last verdict and suggested price, and the buyer's fair edge) and chooses a move
through tool calls. The buyer's private ceiling lives in :class:`Ceiling`, in
this process; it is never placed in a prompt, a tool argument, or a request.

The guard bids at the buyer's fair edge (FR-SHOP-E2E-060): the cheapest price
the arbiter still scores as fair, ``ceil(T_target / (1 + band)) + W``, never
above the ceiling. Any higher offer the model proposes is lowered to that bid,
a merchant offer is accepted only when it is at or below the bid, and offers
carry the inclusions on the table so the extras stay in the deal. The model
cannot overpay or spend past the limit even if it tries. (The arbiter's
suggested price is the F = 1.0 point, the top of the buyer's fair band: taking
it is what the 2026-09-22 spike buyer did.)

The chat layer is an OpenAI-compatible chat-completions client (xAI Grok, OpenAI,
or anything speaking that shape). Tests inject a scripted ``Model`` instead.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from valyzen.client import GatewayClient, GatewayClientError, Session
from valyzen.receipt import verify_receipt

__all__ = [
    "Ceiling",
    "FairEdgeModel",
    "Model",
    "OpenAICompatibleModel",
    "ToolCall",
    "bid_minor",
    "guard_move",
    "run_buyer",
]


@dataclass(frozen=True)
class Ceiling:
    """The buyer's private limit. Never serialised, never sent."""

    amount_minor: int


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]


class Model(Protocol):
    """One turn of the model: messages in, a tool call (or plain text) out."""

    def step(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ToolCall | str: ...


SYSTEM_PROMPT = """You are a buying agent negotiating one item through the Valyzen \
Negotiation Gateway, a neutral arbiter that scores every offer for fairness.
Rules you must follow:
- You are told the item, the merchant's standing offer, the arbiter's last \
verdict, the arbiter's suggested price (where the terms score exactly at the \
reference cost, the top of the fair band for the buyer) and \
fair_edge_price_minor (the lowest price the arbiter still scores as fair to the \
merchant: the buyer's edge of the fair band). You are NOT told the buyer's \
limit; a guard outside the model enforces it. Never ask for it and never \
invent one.
- Bid at fair_edge_price_minor. Accept the merchant's standing offer only when \
it is at or below fair_edge_price_minor; otherwise call offer with \
fair_edge_price_minor. Offers below it are rejected as unfair to the merchant \
and cost a corrective round; offers above it overpay, and the guard lowers \
them and refuses to accept a dearer standing offer.
- Make exactly one tool call per turn. When the session is agreed or closed, \
call get_receipt and then stop."""

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "catalogue",
            "description": "List the SKUs the merchant will negotiate over.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_session",
            "description": "Open the session over the chosen SKU.",
            "parameters": {
                "type": "object",
                "properties": {"sku": {"type": "string"}},
                "required": ["sku"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "offer",
            "description": "Propose or counter with a price in minor units.",
            "parameters": {
                "type": "object",
                "properties": {
                    "price_minor": {"type": "integer"},
                    "justification": {"type": "string"},
                },
                "required": ["price_minor"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "accept",
            "description": "Accept the merchant's standing offer.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reject",
            "description": "Walk away from the session.",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_session",
            "description": "Refresh the session state.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_receipt",
            "description": "Fetch the signed receipt once the session has ended.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


@dataclass
class BuyerRun:
    """What a run produced, for the caller and the tests."""

    session_id: str | None = None
    state: str = ""
    turns: int = 0
    moves: list[str] = field(default_factory=list)
    receipt: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None
    transcript: list[dict[str, Any]] = field(default_factory=list)


def public_view(session: Session | None) -> dict[str, Any]:
    """What the model may see. No private values, ever."""
    if session is None:
        return {"session": None}
    view = session.view
    return {
        "session_id": session.session_id,
        "state": view.get("state"),
        "round": view.get("round"),
        "max_rounds": view.get("max_rounds"),
        "your_turn": view.get("your_turn"),
        "corrective": view.get("corrective"),
        "merchant_offer_minor": session.merchant_offer_minor,
        "last_verdict": (view.get("last_verdict") or {}).get("verdict"),
        "suggested_price_minor": session.suggested_price_minor,
        "fair_edge_price_minor": session.fair_edge_minor,
        "close": view.get("close"),
    }


def bid_minor(session: Session, ceiling: Ceiling) -> int:
    """What the guard bids: the buyer's fair edge, never above the ceiling."""
    edge = session.fair_edge_minor
    if edge is None:
        return ceiling.amount_minor
    return min(edge, ceiling.amount_minor)


def guard_move(
    session: Session, ceiling: Ceiling, price_minor: int | None = None
) -> tuple[str, int]:
    """The guard's move for a buyer that would offer ``price_minor``.

    - A standing merchant offer at or below the bid the guard would make (the
      fair edge, never above the ceiling) is ACCEPTED, whatever the model
      proposed: the ask is already as good as the guard's own bid.
    - Otherwise the guard offers ``min(price_minor, bid, merchant's ask)``:
      never above the ceiling and never above what the merchant is asking, so
      clamping can never turn into a counter dearer than the ask. In a
      corrective round (nothing of the merchant's on the table) the ask is
      the merchant's latest fair offer.
    """
    bid = bid_minor(session, ceiling)
    standing = session.merchant_offer_minor
    if standing is not None and standing <= bid:
        return "accept", standing
    price = bid if price_minor is None else min(price_minor, bid)
    ask = session.merchant_ask_minor
    if ask is not None:
        price = min(price, ask)
    return "offer", price


def run_buyer(
    client: GatewayClient,
    model: Model,
    *,
    sku: str,
    ceiling: Ceiling,
    agent_id: str = "sdk-buyer",
    max_rounds: int = 5,
    max_turns: int = 20,
    inclusions: Sequence[str] = (),
    utilities: Mapping[str, str] | None = None,
    log: Callable[[str], None] | None = None,
) -> BuyerRun:
    """Drive one session to a terminal state with the model choosing moves.

    ``inclusions`` and ``utilities`` are the buyer's declared wants and what
    it would value them at (public, declared to the arbiter at open).
    """
    say = log or (lambda _line: None)
    run = BuyerRun()
    session: Session | None = None
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task": f"Buy SKU {sku}. Open the session, then negotiate.",
                    "sku": sku,
                }
            ),
        },
    ]

    for _ in range(max_turns):
        run.turns += 1
        step = model.step(messages, TOOLS)
        if isinstance(step, str):
            messages.append({"role": "assistant", "content": step})
            if session is not None and session.finished and run.receipt is not None:
                break
            messages.append({"role": "user", "content": "Use a tool. One tool call per turn."})
            continue

        call = step
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call_{run.turns}",
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments),
                        },
                    }
                ],
            }
        )
        result, session = _execute(
            client,
            session,
            call,
            sku=sku,
            ceiling=ceiling,
            agent_id=agent_id,
            max_rounds=max_rounds,
            inclusions=inclusions,
            utilities=utilities,
            run=run,
        )
        say(f"{call.name}({call.arguments}) -> {json.dumps(result)[:160]}")
        run.transcript.append({"tool": call.name, "arguments": call.arguments})
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"call_{run.turns}",
                "content": json.dumps(result),
            }
        )
        if run.receipt is not None:
            break

    if session is not None:
        run.session_id = session.session_id
        run.state = session.state
    return run


def _execute(
    client: GatewayClient,
    session: Session | None,
    call: ToolCall,
    *,
    sku: str,
    ceiling: Ceiling,
    agent_id: str,
    max_rounds: int,
    inclusions: Sequence[str] = (),
    utilities: Mapping[str, str] | None = None,
    run: BuyerRun,
) -> tuple[dict[str, Any], Session | None]:
    """Run one tool call under the ceiling guard; never raises to the model."""
    try:
        if call.name == "catalogue":
            return {"items": client.catalogue()}, session
        if call.name == "open_session":
            if session is not None:
                return {
                    "error": "a session is already open",
                    **public_view(session),
                }, session
            # The item is the caller's choice, never the model's: tool results
            # carry merchant text, and a prompt injection must not be able to
            # steer the buyer to a different item under the same ceiling.
            session = client.open_session(
                sku,
                agent_id=agent_id,
                max_rounds=max_rounds,
                inclusions=list(inclusions),
                utilities=utilities,
            )
            run.moves.append("open")
            return public_view(session), session
        if session is None:
            return {"error": "open a session first"}, None
        if call.name == "get_session":
            session.refresh()
            return public_view(session), session
        if call.name == "get_receipt":
            if not session.finished:
                return {
                    "error": "the session is still open",
                    **public_view(session),
                }, session
            receipt = session.receipt()
            run.receipt = receipt
            run.verification = verify_receipt(receipt)
            return {
                "state": receipt.get("state"),
                "final_terms": receipt.get("final_terms"),
                "signature": run.verification["signature"],
                "arbiter": receipt.get("arbiter"),
            }, session
        if session.finished:
            return {"error": "the session has ended", **public_view(session)}, session
        if call.name == "accept":
            standing = session.merchant_offer_minor
            if standing is None:
                return {"error": "nothing from the merchant to accept"}, session
            if standing > bid_minor(session, ceiling):
                # The guard, not the model, protects the limit and the edge.
                return {
                    "error": "that offer is above your bid; counter instead",
                    "fair_edge_price_minor": session.fair_edge_minor,
                }, session
            session.accept()
            run.moves.append("accept")
            return public_view(session), session
        if call.name == "offer":
            move, price = guard_move(session, ceiling, int(call.arguments.get("price_minor", 0)))
            if move == "accept":
                session.accept()
                run.moves.append("accept")
                return public_view(session), session
            session.offer(
                price,
                inclusions=session.table_inclusions or session.merchant_ask_inclusions or None,
                justification=str(call.arguments.get("justification") or "")[:2000] or None,
            )
            run.moves.append(f"offer:{price}")
            return public_view(session), session
        if call.name == "reject":
            session.reject(str(call.arguments.get("reason") or "buyer withdrew"))
            run.moves.append("reject")
            return public_view(session), session
        return {"error": f"unknown tool {call.name}"}, session
    except GatewayClientError as exc:
        return {
            "error": exc.code,
            "message": exc.message,
            **public_view(session),
        }, session


class FairEdgeModel:
    """A scripted model with no LLM: open, bid at the fair edge, take the receipt.

    Every move still goes through the ceiling guard in :func:`run_buyer`, so it
    behaves exactly as an LLM buyer that follows the system prompt would. This
    is what ``valyzen try`` runs.
    """

    def step(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ToolCall | str:
        last = _last_tool_result(messages)
        if last is None or last.get("session") is None and "session_id" not in last:
            return ToolCall("open_session", {})
        if "final_terms" in last and "signature" in last:
            return "done"
        if last.get("state") in ("agreed", "closed"):
            return ToolCall("get_receipt", {})
        edge = last.get("fair_edge_price_minor")
        standing = last.get("merchant_offer_minor")
        if edge is None:
            return ToolCall("get_session", {})
        if standing is not None and standing <= edge:
            return ToolCall("accept", {})
        return ToolCall("offer", {"price_minor": edge})


def _last_tool_result(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for message in reversed(messages):
        if message.get("role") == "tool":
            try:
                out = json.loads(message.get("content") or "{}")
            except ValueError:
                return {}
            return out if isinstance(out, dict) else {}
    return None


class OpenAICompatibleModel:
    """Chat completions with tool calling over any OpenAI-shaped endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        post: Callable[[str, Mapping[str, str], Mapping[str, Any]], Mapping[str, Any]]
        | None = None,
        temperature: float = 0.2,
    ) -> None:
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._headers = {
            "authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        }
        self._model = model
        self._post = post or _httpx_post
        self._temperature = temperature

    def step(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ToolCall | str:
        body = {
            "model": self._model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": self._temperature,
        }
        response = self._post(self._url, self._headers, body)
        choices: Sequence[Any] = response.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        calls = message.get("tool_calls") or []
        if calls:
            fn = calls[0].get("function") or {}
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except ValueError:
                args = {}
            return ToolCall(str(fn.get("name", "")), dict(args))
        return str(message.get("content") or "")


def _httpx_post(url: str, headers: Mapping[str, str], body: Mapping[str, Any]) -> Mapping[str, Any]:
    import httpx

    response = httpx.post(url, headers=dict(headers), json=dict(body), timeout=120)
    response.raise_for_status()
    data = response.json()
    return dict(data) if isinstance(data, dict) else {}
