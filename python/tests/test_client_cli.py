"""Client transport, verify links and the CLI, against a scripted gateway."""

from __future__ import annotations

import base64
import json
import zlib
from pathlib import Path
from typing import Any

import httpx
import pytest

from valyzen import GatewayClient, GatewayClientError, verify_link
from valyzen.cli import main, money
from valyzen.links import encode_receipt, site_for

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
KEY = "vz_pk_test_unit_secretvalue"


def client_for(handler: Any, **kw: Any) -> GatewayClient:
    return GatewayClient(
        KEY,
        base_url="https://api-dev.valyzen.ai",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _s: None,
        **kw,
    )


# --- client -----------------------------------------------------------------


def test_requests_carry_the_key_and_a_user_agent() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"items": []})

    assert client_for(handler).catalogue() == []
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"
    assert seen[0].headers["user-agent"].startswith("valyzen-python/")


def test_server_errors_are_retried_then_refusals_raised_with_their_code() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(
            429, json={"error": {"code": "rate_limited", "message": "slow down", "retry_after": 30}}
        )

    with pytest.raises(GatewayClientError) as err:
        client_for(handler).catalogue()
    assert (err.value.status, err.value.code) == (429, "rate_limited")
    assert err.value.details == {"retry_after": 30}
    assert calls["n"] == 3


def test_key_and_base_url_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VALYZEN_API_KEY", KEY)
    monkeypatch.setenv("VALYZEN_BASE_URL", "https://api-dev.valyzen.ai/")
    client = GatewayClient()
    assert client._key == KEY and client._base == "https://api-dev.valyzen.ai"


def test_no_key_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VALYZEN_API_KEY", raising=False)
    with pytest.raises(GatewayClientError) as err:
        GatewayClient()
    assert err.value.code == "no_key" and "valyzen.ai/keys" in err.value.message


def test_plain_http_is_refused_except_locally() -> None:
    with pytest.raises(GatewayClientError) as err:
        GatewayClient(KEY, base_url="http://api.valyzen.ai")
    assert err.value.code == "insecure_base_url"
    GatewayClient(KEY, base_url="http://localhost:8000")


# --- links ------------------------------------------------------------------


def decode(value: str) -> Any:
    assert value.startswith("v1.")
    raw = base64.urlsafe_b64decode(value[3:] + "=" * (-len(value[3:]) % 4))
    return json.loads(zlib.decompress(raw, -15))


def test_verify_links_round_trip_and_point_at_the_matching_site() -> None:
    receipt = json.loads((FIXTURES / "dev-receipt.json").read_text())
    link = verify_link(receipt, base_url="https://api-dev.valyzen.ai")
    assert link.startswith("https://dev.valyzen.ai/verify#r=v1.")
    assert decode(link.split("#r=", 1)[1]) == receipt
    assert site_for("https://api.valyzen.ai") == "https://www.valyzen.ai"
    assert site_for("https://gateway.example") == "https://www.valyzen.ai"
    assert "=" not in encode_receipt(receipt)[3:]  # base64url without padding


# --- CLI --------------------------------------------------------------------


def run_cli(argv: list[str]) -> tuple[int, str]:
    lines: list[str] = []
    code = main(argv, out=lines.append)
    return code, "\n".join(lines)


def test_try_without_a_key_says_where_to_get_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VALYZEN_API_KEY", raising=False)
    code, out = run_cli(["try", "--base-url", "https://api-dev.valyzen.ai"])
    assert code == 2 and "https://dev.valyzen.ai/keys" in out


def test_try_refuses_a_live_key() -> None:
    code, out = run_cli(["try", "--key", "vz_pk_live_abc"])
    assert code == 2 and "test key" in out


@pytest.mark.parametrize("bad", ["0", "-5", "12.345", "abc"])
def test_a_bad_ceiling_is_rejected_by_the_parser(bad: str) -> None:
    with pytest.raises(SystemExit):
        main(["try", "--ceiling", bad], out=lambda _l: None)


def test_verify_command_passes_a_real_receipt_and_fails_a_forged_one() -> None:
    code, out = run_cli(["verify", str(FIXTURES / "dev-receipt.json")])
    assert code == 0 and "Verified offline" in out
    code, out = run_cli(["verify", str(FIXTURES / "forged-receipt.json")])
    assert code == 1 and "NOT verified" in out and "not a Valyzen arbiter key" in out


def test_verify_command_rejects_unreadable_input(tmp_path: Path) -> None:
    bad = tmp_path / "r.json"
    bad.write_text("[1, 2]")
    assert run_cli(["verify", str(bad)])[0] == 2
    bad.write_text("not json")
    assert run_cli(["verify", str(bad)])[0] == 2


def test_money_formats_minor_units() -> None:
    assert money(41525) == "$415.25"
    assert money(123456, "INR") == "₹1,234.56"
    assert money(None) == "—"
