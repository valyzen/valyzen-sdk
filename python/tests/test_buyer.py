"""The buyer guard: the caller's item and ceiling hold whatever the model says."""

from __future__ import annotations

import json
from typing import Any

import httpx

from valyzen import GatewayClient
from valyzen.buyer import Ceiling, ToolCall, run_buyer


class Scripted:
    def __init__(self, *steps: ToolCall | str) -> None:
        self.steps = list(steps)

    def step(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ToolCall | str:
        return self.steps.pop(0) if self.steps else "done"


def gateway(opened: list[dict[str, Any]]) -> GatewayClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions" and request.method == "POST":
            body = json.loads(request.content)
            opened.append(body)
            view = {
                "session_id": "s1",
                "session_token": "vz_st_test_s1_t",
                "state": "negotiating",
                "your_turn": True,
                "offer_on_table": None,
                "events": [],
            }
            return httpx.Response(201, json=view)
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "no"}})

    return GatewayClient(
        "vz_pk_test_unit",
        base_url="https://api-dev.valyzen.ai",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_a_model_cannot_redirect_the_buyer_to_another_item() -> None:
    # A prompt injection in merchant text gets the model to ask for another SKU.
    opened: list[dict[str, Any]] = []
    model = Scripted(ToolCall("open_session", {"sku": "SOMETHING-EXPENSIVE"}))
    run_buyer(gateway(opened), model, sku="DEMO-MONITOR-27", ceiling=Ceiling(44900), max_turns=2)
    assert [b["sku"] for b in opened] == ["DEMO-MONITOR-27"]


def test_the_ceiling_never_appears_in_a_request() -> None:
    opened: list[dict[str, Any]] = []
    run_buyer(
        gateway(opened),
        Scripted(ToolCall("open_session", {})),
        sku="DEMO-MONITOR-27",
        ceiling=Ceiling(43217),
        max_turns=2,
    )
    assert "43217" not in json.dumps(opened)


# --- the 2026-10-04 api-dev transcript (VZ-MON-27-4K) -----------------------
# list 44900, T_target 41900, band 0.05; extras 2_yr_warranty and hdmi_cable.
# r1 the merchant asks $419 + extras (fair). The guard's first bid is the fair
# edge, $399.05; the arbiter weighs the buyer's extras at W = 3870 and rejects
# it as unfair to the merchant (corrective). The old guard then bid
# min(edge 437.75, ceiling 430) = $430, above the merchant's own ask, and the
# session closed. The guard must never counter above the ask.

EXTRAS = [
    {"kind": "2_yr_warranty", "value": "included"},
    {"kind": "hdmi_cable", "value": "included"},
]


def offer(role: str, price: int, kind: str = "offer.propose") -> dict[str, Any]:
    return {
        "type": kind,
        "sender": {"role": role},
        "payload": {
            "terms": {"price": {"amount_minor": price, "currency": "USD"}, "inclusions": EXTRAS}
        },
    }


def evaluate(verdict: str, w: int, price: int) -> dict[str, Any]:
    return {
        "type": "arbitration.evaluate",
        "payload": {
            "verdict": verdict,
            "w_minor": w,
            "suggested_adjustment": {"amount_minor": 41900 + w},
        },
    }


class Transcript:
    """The gateway as it scored that session, with symmetric W once fixed:
    a buyer offer at the merchant's ask scores like the ask and is accepted."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions":
            accept = {
                "type": "session.accept",
                "payload": {
                    "session_rules": {"fair_band": "0.05"},
                    "attested": {"t_target": {"amount_minor": 41900, "currency": "USD"}},
                },
            }
            view = self.view(
                "negotiating", [accept, offer("merchant", 41900), evaluate("fair", 0, 41900)], 41900
            )
            return httpx.Response(201, json={**view, "session_token": "vz_st_test_s1_t"})
        body = json.loads(request.content)
        self.sent.append(body)
        if body["type"] == "accept":
            return httpx.Response(200, json=self.view("agreed", [], None))
        price = body["price_minor"]
        if price >= 41900:  # at the ask: fair, and the merchant accepts it
            events = [offer("buyer", price, "offer.counter"), evaluate("fair", 0, price)]
            return httpx.Response(200, json=self.view("agreed", events, None))
        events = [
            offer("buyer", price, "offer.counter"),
            evaluate("unfair_to_merchant", 3870, price),
            {"type": "arbitration.reject", "payload": {"detail": "unfair to merchant"}},
        ]
        return httpx.Response(200, json=self.view("corrective", events, None))

    @staticmethod
    def view(state: str, events: list[dict[str, Any]], standing: int | None) -> dict[str, Any]:
        table = (
            {
                "proposer": "merchant",
                "terms": {
                    "price": {"amount_minor": standing, "currency": "USD"},
                    "inclusions": EXTRAS,
                },
            }
            if standing is not None
            else None
        )
        return {
            "session_id": "s1",
            "state": state,
            "your_turn": state not in ("agreed", "closed"),
            "corrective": state == "corrective",
            "offer_on_table": table,
            "events": events,
        }


def transcript_client(t: Transcript) -> GatewayClient:
    return GatewayClient(
        "vz_pk_test_unit",
        base_url="https://api-dev.valyzen.ai",
        client=httpx.Client(transport=httpx.MockTransport(t.handler)),
    )


def test_the_guard_never_counters_above_the_merchant_ask() -> None:
    """Regression (VZ-MON-27-4K): after the corrective, the guard re-proposes
    at the merchant's $419 ask with the extras, never $430, and the deal is
    made at $419."""
    t = Transcript()
    model = Scripted(
        ToolCall("open_session", {}),
        ToolCall("offer", {"price_minor": 39905}),
        ToolCall("offer", {"price_minor": 43000}),  # what the model asked for
    )
    run = run_buyer(
        transcript_client(t), model, sku="VZ-MON-27-4K", ceiling=Ceiling(43000), max_turns=4
    )
    assert run.moves == ["open", "offer:39905", "offer:41900"]
    assert run.state == "agreed"
    assert max(b["price_minor"] for b in t.sent if b["type"] == "offer") <= 41900
    last = t.sent[-1]
    assert [i["kind"] if isinstance(i, dict) else i for i in last["inclusions"]] == [
        "2_yr_warranty",
        "hdmi_cable",
    ]


def test_guard_move_rules() -> None:
    from valyzen import Session
    from valyzen.buyer import guard_move

    client = transcript_client(Transcript())
    accept = {
        "type": "session.accept",
        "payload": {
            "session_rules": {"fair_band": "0.05"},
            "attested": {"t_target": {"amount_minor": 41900}},
        },
    }
    base = Transcript.view(
        "negotiating", [accept, offer("merchant", 41900), evaluate("fair", 0, 41900)], 41900
    )
    s = Session(client, "s1", "t", base)
    # Edge 39905 < ask: counter at the model's price, never above the edge.
    assert guard_move(s, Ceiling(43000), 45000) == ("offer", 39905)
    assert guard_move(s, Ceiling(43000), 39000) == ("offer", 39000)
    # No fair edge known (bid = ceiling): the ask is within it, so accept.
    plain = Session(
        client,
        "s1",
        "t",
        Transcript.view(
            "negotiating", [offer("merchant", 41900), evaluate("fair", 0, 41900)], 41900
        ),
    )
    assert guard_move(plain, Ceiling(43000), 40000) == ("accept", 41900)
    assert guard_move(plain, Ceiling(41000), 50000) == ("offer", 41000)  # ceiling below ask
    # Corrective: nothing on the table; the ask still caps the counter.
    s.view = Transcript.view(
        "corrective",
        [offer("buyer", 39905, "offer.counter"), evaluate("unfair_to_merchant", 3870, 39905)],
        None,
    )
    s._learn(s.view)
    assert s.merchant_offer_minor is None and s.merchant_ask_minor == 41900
    assert guard_move(s, Ceiling(43000), 43000) == ("offer", 41900)
    assert guard_move(s, Ceiling(43000)) == ("offer", 41900)
    assert s.merchant_ask_inclusions == ["2_yr_warranty", "hdmi_cable"]
