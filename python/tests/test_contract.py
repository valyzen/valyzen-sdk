"""Contract tests against a live gateway's sandbox store.

Opt-in: ``VALYZEN_API_KEY=vz_pk_test_… VALYZEN_BASE_URL=https://api-dev.valyzen.ai
uv run pytest -m contract``. Never runs in public CI (no key there).
"""

from __future__ import annotations

import os

import pytest

from valyzen import GatewayClient, verify_receipt
from valyzen.buyer import Ceiling, FairEdgeModel, run_buyer
from valyzen.cli import main

pytestmark = [
    pytest.mark.contract,
    pytest.mark.skipif(not os.environ.get("VALYZEN_API_KEY"), reason="needs VALYZEN_API_KEY"),
]


def test_the_sandbox_lists_the_demo_monitor() -> None:
    items = {i["sku"]: i for i in GatewayClient().catalogue()}
    monitor = items["DEMO-MONITOR-27"]
    assert monitor["list_price_minor"] == 44900 and set(monitor["inclusions"]) == {
        "warranty",
        "cable",
    }


def test_the_fair_edge_buyer_reaches_a_verified_agreement_within_the_ceiling() -> None:
    run = run_buyer(GatewayClient(), FairEdgeModel(), sku="DEMO-MONITOR-27", ceiling=Ceiling(44900))
    assert run.state == "agreed" and run.receipt is not None
    result = verify_receipt(run.receipt)
    assert result.ok and result.signer.kind == "arbiter"
    assert result.signed_terms["price"]["amount_minor"] <= 44900


def test_a_ceiling_below_the_fair_band_ends_without_overpaying() -> None:
    run = run_buyer(GatewayClient(), FairEdgeModel(), sku="DEMO-MONITOR-27", ceiling=Ceiling(30000))
    if run.receipt and run.receipt.get("state") == "agreed":
        assert run.receipt["signed_payload"]["final_terms"]["price"]["amount_minor"] <= 30000


def test_valyzen_try_end_to_end() -> None:
    lines: list[str] = []
    assert main(["try"], out=lines.append) == 0
    text = "\n".join(lines)
    assert "Verified offline" in text and "/verify#r=v1." in text
    assert os.environ["VALYZEN_API_KEY"] not in text  # the key is never printed
