"""Offline receipt verification: what makes a receipt a Valyzen receipt."""

from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from valyzen import commit, verify_receipt
from valyzen.keys import ARBITER_KEYS, ArbiterKey
from valyzen.receipt import canonical_bytes

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def sign(receipt: dict[str, Any], key: Ed25519PrivateKey, kid: str) -> dict[str, Any]:
    """Re-sign ``signed_payload`` exactly as the arbiter does, with ``key``."""
    header = {"alg": "EdDSA", "b64": False, "crit": ["b64"], "kid": kid}
    protected = b64u(canonical_bytes(header))
    signature = key.sign(protected.encode() + b"." + canonical_bytes(receipt["signed_payload"]))
    public = key.public_key().public_bytes_raw()
    out = copy.deepcopy(receipt)
    out["artifact"] = {"protected": protected, "signature": b64u(signature)}
    out["jwks"] = {"keys": [{"kty": "OKP", "crv": "Ed25519", "kid": kid, "x": b64u(public)}]}
    return out


def check(result: Any, check_id: str) -> Any:
    return next(c for c in result.checks if c.id == check_id)


# --- positive -------------------------------------------------------------


def test_a_real_dev_receipt_verifies_and_names_the_dev_arbiter() -> None:
    result = verify_receipt(load("dev-receipt.json"))
    assert result.ok and result.status == "valid"
    assert result.signer.kind == "arbiter" and result.signer.mode == "test"
    assert result.kid == "ed25519-2f3286feb69fb953"
    assert result.signed_terms == load("dev-receipt.json")["signed_payload"]["final_terms"]
    # Without the log the chain is skipped, not failed.
    assert check(result, "chain").skipped


def test_a_key_pinned_by_the_caller_is_trusted() -> None:
    key = Ed25519PrivateKey.generate()
    receipt = sign(load("dev-receipt.json"), key, "ed25519-mine")
    x = receipt["jwks"]["keys"][0]["x"]
    trusted = {"ed25519-mine": ArbiterKey(x, "my staging arbiter", "test")}
    assert verify_receipt(receipt, trusted_keys=trusted).ok
    assert not verify_receipt(receipt).ok  # not a Valyzen key by default


def chained(n: int) -> list[dict[str, Any]]:
    log: list[dict[str, Any]] = []
    for i in range(n):
        envelope: dict[str, Any] = {"type": f"step.{i}", "message_id": str(i), "payload": {"i": i}}
        if log:
            envelope["prev_hash"] = commit(log[-1])
        log.append(envelope)
    log[-1]["type"] = "session.agree"
    return log


def with_chain(receipt: dict[str, Any], log: list[dict[str, Any]]) -> dict[str, Any]:
    out = copy.deepcopy(receipt)
    out["chain"] = {"head": commit(log[-1]), "length": len(log), "retention_days": 90}
    out["signed_payload"]["session"]["chain_head"] = commit(log[-2])
    return out


def test_a_session_log_that_links_end_to_end_verifies_the_chain() -> None:
    log = chained(5)
    receipt = with_chain(load("dev-receipt.json"), log)
    result = verify_receipt(receipt, log=log)
    assert check(result, "chain").ok and not check(result, "chain").skipped


# --- validation -----------------------------------------------------------


def test_a_closed_session_is_unsigned_not_forged() -> None:
    receipt = load("dev-receipt.json")
    receipt.update(state="closed", artifact=None, signed_payload=None, final_terms=None)
    result = verify_receipt(receipt)
    assert result.status == "unsigned" and not result.ok
    assert check(result, "signature").skipped


def test_canonicalisation_ignores_key_order_and_whitespace() -> None:
    receipt = load("dev-receipt.json")
    terms = receipt["final_terms"]
    receipt["final_terms"] = json.loads(json.dumps(dict(reversed(list(terms.items())))))
    assert verify_receipt(receipt).ok


# --- negative -------------------------------------------------------------


def test_tampered_final_terms_fail_even_with_a_valid_signature() -> None:
    receipt = load("dev-receipt.json")
    receipt["final_terms"]["price"]["amount_minor"] = 100
    result = verify_receipt(receipt)
    assert check(result, "signature").ok  # the signed payload is untouched
    assert not check(result, "terms").ok and not result.ok
    assert result.signed_terms["price"]["amount_minor"] == 41900  # signed truth survives


def test_a_tampered_signed_payload_breaks_the_signature() -> None:
    receipt = load("dev-receipt.json")
    receipt["signed_payload"]["final_terms"]["price"]["amount_minor"] = 100
    receipt["final_terms"]["price"]["amount_minor"] = 100
    result = verify_receipt(receipt)
    assert result.status == "invalid" and not result.ok


def test_agreed_without_an_artifact_is_unverifiable() -> None:
    receipt = load("dev-receipt.json")
    receipt["artifact"] = None
    result = verify_receipt(receipt)
    assert result.status == "unverifiable" and not result.ok
    assert not check(result, "header").skipped


@pytest.mark.parametrize(
    "header",
    [
        {"alg": "HS256", "b64": False, "crit": ["b64"], "kid": "k"},
        {"alg": "EdDSA", "kid": "k"},  # attached payload
        {"alg": "EdDSA", "b64": False, "crit": ["b64"]},  # no kid
    ],
)
def test_headers_other_than_detached_eddsa_with_a_kid_are_refused(header: dict) -> None:
    receipt = load("dev-receipt.json")
    receipt["artifact"]["protected"] = b64u(json.dumps(header).encode())
    assert verify_receipt(receipt).status == "unverifiable"


@pytest.mark.parametrize(
    ("mutate", "expect"),
    [
        (lambda log: log.__setitem__(2, {**log[2], "payload": {"i": 99}}), "does not link"),
        (lambda log: log.pop(), "head the receipt names"),
        (lambda log: log.reverse(), "does not link"),
    ],
)
def test_an_edited_reordered_or_truncated_log_fails_the_chain(mutate: Any, expect: str) -> None:
    log = chained(5)
    receipt = with_chain(load("dev-receipt.json"), log)
    mutate(log)
    result = verify_receipt(receipt, log=log)
    assert not check(result, "chain").ok and expect in check(result, "chain").detail
    assert not result.ok


def test_a_signed_head_that_is_not_before_agree_fails() -> None:
    log = chained(5)
    receipt = with_chain(load("dev-receipt.json"), log)
    receipt["signed_payload"]["session"]["chain_head"] = commit(log[0])
    assert "before session.agree" in check(verify_receipt(receipt, log=log), "chain").detail


# --- security -------------------------------------------------------------


def test_a_receipt_signed_with_someone_elses_key_is_not_a_valyzen_receipt() -> None:
    result = verify_receipt(load("forged-receipt.json"))
    assert check(result, "signature").ok  # the forger's own signature is fine…
    assert result.signer.kind == "unknown" and not result.ok  # …but it is not ours


def test_our_kid_with_a_different_key_is_refused() -> None:
    result = verify_receipt(load("forged-kid-receipt.json"))
    assert result.kid in ARBITER_KEYS
    assert result.signer.kind == "unknown" and not result.ok


def test_two_keys_under_one_kid_are_refused() -> None:
    receipt = load("dev-receipt.json")
    receipt["jwks"]["keys"].append(dict(receipt["jwks"]["keys"][0]))
    result = verify_receipt(receipt)
    assert not check(result, "key").ok and not result.ok


def test_non_base64url_signature_is_rejected_not_lenient() -> None:
    receipt = load("dev-receipt.json")
    sig = receipt["artifact"]["signature"]
    receipt["artifact"]["signature"] = sig.replace("-", "+").replace("_", "/") + "=="
    assert verify_receipt(receipt).status == "invalid"


def test_verification_never_touches_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def refuse(*_a: Any, **_k: Any) -> None:
        raise AssertionError("network access during offline verification")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    assert verify_receipt(load("dev-receipt.json")).ok


def test_the_pinned_list_cannot_be_modified_at_runtime() -> None:
    with pytest.raises(TypeError):
        ARBITER_KEYS["ed25519-evil"] = ArbiterKey("x", "evil", "live")  # type: ignore[index]
