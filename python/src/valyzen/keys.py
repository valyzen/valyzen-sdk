"""Valyzen's arbiter signing keys, pinned by kid AND public key.

A receipt carries the key that signed it, and anyone can embed a key under any
kid, including ours. A valid signature therefore proves only that the embedded
key signed; a receipt is a Valyzen receipt when the embedded key for its kid is
byte-for-byte one of these. Source: each gateway's ``/.well-known/jwks.json``.
The same list is pinned on www.valyzen.ai/verify.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

__all__ = ["ARBITER_KEYS", "ArbiterKey", "Signer", "signer_of"]


@dataclass(frozen=True)
class ArbiterKey:
    x: str
    label: str
    mode: Literal["live", "test"]


ARBITER_KEYS: Mapping[str, ArbiterKey] = MappingProxyType(
    {
        "ed25519-28860223a6941457": ArbiterKey(
            "wd270_PgIwLChz_kk9_wHgG26cb70_muCjnbN8WrkeE", "the Valyzen arbiter", "live"
        ),
        "ed25519-0190292b036517a6": ArbiterKey(
            "4xj3LRerKGkc3sRyYORRE3mJRiyNIEePDEp25XMfkjE", "the Valyzen arbiter", "test"
        ),
        "ed25519-2f3286feb69fb953": ArbiterKey(
            "Er_WT_nxhGzA8212gQp8dCBwvQRC4TmT1s8n185Znyg",
            "the Valyzen development arbiter",
            "test",
        ),
    }
)


@dataclass(frozen=True)
class Signer:
    """Who signed: ``arbiter`` (a pinned Valyzen key) or ``unknown``."""

    kind: Literal["arbiter", "unknown"]
    label: str | None = None
    mode: Literal["live", "test"] | None = None


def signer_of(
    kid: str | None,
    jwks: Any,
    trusted: Mapping[str, ArbiterKey] = ARBITER_KEYS,
) -> Signer:
    if not isinstance(kid, str):
        return Signer("unknown")
    keys = jwks.get("keys") if isinstance(jwks, dict) else None
    embedded = (
        [k for k in keys if isinstance(k, dict) and k.get("kid") == kid]
        if isinstance(keys, list)
        else []
    )
    pinned = trusted.get(kid)
    if (
        pinned is not None
        and len(embedded) == 1
        and embedded[0].get("kty") == "OKP"
        and embedded[0].get("crv") == "Ed25519"
        and embedded[0].get("x") == pinned.x
    ):
        return Signer("arbiter", pinned.label, pinned.mode)
    return Signer("unknown")
