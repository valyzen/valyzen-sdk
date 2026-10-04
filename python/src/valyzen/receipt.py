"""Offline receipt verification. Never contacts the gateway.

A receipt is self-contained: ``artifact`` is a detached Ed25519 JWS (RFC 7797,
``b64: false``) over the RFC 8785 canonical bytes of ``signed_payload``, and
``jwks`` carries the key by ``kid``. The checks, in order:

* ``header``    the JWS header is EdDSA, detached, with a kid
* ``key``       an Ed25519 key with that kid is embedded in the receipt
* ``signature`` it verifies over the canonical signed payload
* ``signer``    that embedded key is a pinned Valyzen arbiter key (:mod:`valyzen.keys`)
* ``terms``     ``final_terms`` and ``session_id`` equal what was signed
* ``chain``     with the session log: every envelope links to the one before
                (``prev_hash``), the last is the head the receipt names, and the
                signed chain head is the envelope before ``session.agree``

Only signed fields are evidence: ``final_terms`` is read from
``signed_payload``. Verdicts, invariants and the rest are the gateway's report.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import rfc8785
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from valyzen.keys import ARBITER_KEYS, ArbiterKey, Signer, signer_of

__all__ = ["Check", "Verification", "canonical_bytes", "commit", "verify_receipt"]

Status = Literal["valid", "invalid", "unverifiable", "unsigned"]


@dataclass(frozen=True)
class Check:
    id: str
    ok: bool
    detail: str
    skipped: bool = False


@dataclass(frozen=True)
class Verification:
    """``ok`` is True only for a receipt signed by a pinned Valyzen arbiter key
    whose terms match what was signed (and, given a log, whose chain holds)."""

    ok: bool
    status: Status
    kid: str | None
    signer: Signer
    checks: list[Check] = field(default_factory=list)
    #: The terms the arbiter signed: the only terms to act on.
    signed_terms: dict[str, Any] | None = None

    def __getitem__(self, key: str) -> Any:
        # Dict-style access for callers of the 0.0 gateway SDK.
        if key == "signature":
            return {"status": self.status, "kid": self.kid}
        if key == "checks":
            return [c.__dict__ for c in self.checks]
        return getattr(self, key)


def canonical_bytes(value: Any) -> bytes:
    """RFC 8785 (JCS) canonical UTF-8 bytes."""
    return rfc8785.dumps(value)


def commit(value: Any) -> str:
    """``sha256:<hex>`` over the canonical bytes: the form the chain uses."""
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def _unb64url(text: str) -> bytes:
    if not isinstance(text, str) or any(c in text for c in "+/="):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _header(protected: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(_unb64url(protected))
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def verify_receipt(
    receipt: Mapping[str, Any],
    *,
    log: Sequence[Mapping[str, Any]] | None = None,
    trusted_keys: Mapping[str, ArbiterKey] = ARBITER_KEYS,
) -> Verification:
    """Verify a receipt (and its session log, if given) offline.

    Every check that can run does, so a caller can show all of them; a check
    that cannot run says why and is marked ``skipped``.
    """
    checks: list[Check] = []
    state = receipt.get("state")
    artifact = receipt.get("artifact")
    payload = receipt.get("signed_payload")
    status: Status = "unsigned"
    kid: str | None = None
    sig_ok = False
    signer = Signer("unknown")
    signed_terms: dict[str, Any] | None = None

    if not isinstance(artifact, dict) or not isinstance(payload, dict):
        agreed = state == "agreed"
        why = (
            "the receipt says agreed but carries no signed artifact"
            if agreed
            else "no agreement was signed; the session closed without one"
        )
        checks.append(Check("header", False, why, skipped=not agreed))
        checks.append(Check("signature", False, "nothing to verify", skipped=not agreed))
        if agreed:
            status = "unverifiable"
    else:
        protected = artifact.get("protected")
        header = _header(protected) if isinstance(protected, str) else None
        kid = header.get("kid") if header and isinstance(header.get("kid"), str) else None
        header_ok = (
            header is not None
            and header.get("alg") == "EdDSA"
            and header.get("b64") is False
            and header.get("crit") == ["b64"]
            and kid is not None
        )
        checks.append(
            Check(
                "header",
                header_ok,
                f"EdDSA, detached payload, key {kid}."
                if header_ok
                else "The protected header is not an EdDSA detached JWS with a kid.",
            )
        )
        if not header_ok:
            status = "unverifiable"
            checks.append(Check("signature", False, "Cannot verify without a well-formed header."))
        else:
            keys = (
                (receipt.get("jwks") or {}).get("keys")
                if isinstance(receipt.get("jwks"), dict)
                else None
            )
            embedded = (
                [k for k in keys if isinstance(k, dict) and k.get("kid") == kid]
                if isinstance(keys, list)
                else []
            )
            jwk = embedded[0] if len(embedded) == 1 else None
            key_ok = bool(
                jwk
                and jwk.get("kty") == "OKP"
                and jwk.get("crv") == "Ed25519"
                and isinstance(jwk.get("x"), str)
            )
            checks.append(
                Check(
                    "key",
                    key_ok,
                    f"Ed25519 key {kid} is embedded in the receipt."
                    if key_ok
                    else f"No single Ed25519 key with kid {kid} in the receipt's JWKS.",
                )
            )
            if not key_ok or jwk is None:
                status = "unverifiable"
                checks.append(Check("signature", False, "Cannot verify without the key."))
            else:
                try:
                    public_key = Ed25519PublicKey.from_public_bytes(_unb64url(jwk["x"]))
                    signing_input = protected.encode("ascii") + b"." + canonical_bytes(payload)
                    public_key.verify(_unb64url(str(artifact.get("signature"))), signing_input)
                    sig_ok = True
                except (InvalidSignature, ValueError, TypeError, rfc8785.CanonicalizationError):
                    sig_ok = False
                status = "valid" if sig_ok else "invalid"
                checks.append(
                    Check(
                        "signature",
                        sig_ok,
                        "The signature verifies over the canonical signed payload."
                        if sig_ok
                        else "The signature does not verify: the signed payload, the "
                        "signature, or the key is not what was signed.",
                    )
                )
                signer = signer_of(kid, receipt.get("jwks"), trusted_keys)
                pinned = signer.kind == "arbiter"
                checks.append(
                    Check(
                        "signer",
                        pinned,
                        f"Signed by {signer.label} ({signer.mode} key {kid})."
                        if pinned
                        else f"Key {kid} is not a Valyzen arbiter key: the signature "
                        "proves only that whoever made this receipt signed it.",
                    )
                )

        session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
        signed = payload.get("final_terms")
        signed_terms = signed if isinstance(signed, dict) else None
        try:
            terms_ok = canonical_bytes(receipt.get("final_terms")) == canonical_bytes(signed)
        except rfc8785.CanonicalizationError:
            terms_ok = False
        id_ok = session.get("session_id") == receipt.get("session_id")
        checks.append(
            Check(
                "terms",
                terms_ok and id_ok,
                "final_terms and session_id match what was signed."
                if terms_ok and id_ok
                else "The receipt's final_terms differ from the signed terms."
                if not terms_ok
                else "The signed session id differs from the receipt.",
            )
        )

    chain = receipt.get("chain") if isinstance(receipt.get("chain"), dict) else None
    if log is None:
        hint = (
            f"Pass the session log to verify the chain; the receipt names head "
            f"{str(chain.get('head'))[:23]}… over {chain.get('length')} envelopes."
            if chain
            else "Pass the session log to verify the chain."
        )
        checks.append(Check("chain", False, hint, skipped=True))
    else:
        ok, detail = _verify_chain(list(log), chain, payload if isinstance(payload, dict) else None)
        checks.append(Check("chain", ok, detail))

    ok = all(c.ok for c in checks if not c.skipped) and sig_ok and signer.kind == "arbiter"
    return Verification(
        ok=ok, status=status, kid=kid, signer=signer, checks=checks, signed_terms=signed_terms
    )


def _verify_chain(
    log: list[Mapping[str, Any]],
    chain: Mapping[str, Any] | None,
    payload: Mapping[str, Any] | None,
) -> tuple[bool, str]:
    if not log or not all(isinstance(e, Mapping) for e in log):
        return False, "The log must be a non-empty list of envelopes."
    try:
        for i in range(1, len(log)):
            if log[i].get("prev_hash") != commit(log[i - 1]):
                return (
                    False,
                    f"Envelope {i} ({log[i].get('type')}) does not link to the one before it.",
                )
        head = commit(log[-1])
        before_agree = commit(log[-2]) if len(log) >= 2 else None
    except rfc8785.CanonicalizationError:
        return False, "An envelope in the log is not canonicalisable."
    if chain and chain.get("head") != head:
        return False, "Every link holds, but the last envelope is not the head the receipt names."
    if chain and chain.get("length") != len(log):
        return (
            False,
            f"The receipt names {chain.get('length')} envelopes; the log holds {len(log)}.",
        )
    session = payload.get("session") if payload else None
    signed_head = session.get("chain_head") if isinstance(session, Mapping) else None
    if isinstance(signed_head, str) and before_agree is not None and signed_head != before_agree:
        return False, "The signed chain head is not the hash of the envelope before session.agree."
    return (
        True,
        f"{len(log)} envelopes, each linked to its predecessor; the last is the head the receipt names.",
    )
