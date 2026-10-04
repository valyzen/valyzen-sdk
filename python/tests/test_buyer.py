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
