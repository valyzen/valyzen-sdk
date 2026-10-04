"""Prefilled verify links: ``<site>/verify#r=v1.<base64url(raw DEFLATE(JSON))>``.

The receipt rides in the URL fragment, which browsers never send to a server, so
the page verifies it offline and nothing lands in access logs. The same format
www.valyzen.ai/verify reads.
"""

from __future__ import annotations

import base64
import json
import zlib
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

__all__ = ["encode_receipt", "site_for", "verify_link"]

LINK_VERSION = "v1."
SITES = {
    "api.valyzen.ai": "https://www.valyzen.ai",
    "api-dev.valyzen.ai": "https://dev.valyzen.ai",
}


def encode_receipt(receipt: Mapping[str, Any]) -> str:
    data = json.dumps(receipt, separators=(",", ":"), ensure_ascii=False).encode()
    packer = zlib.compressobj(9, zlib.DEFLATED, -15)
    packed = packer.compress(data) + packer.flush()
    return LINK_VERSION + base64.urlsafe_b64encode(packed).rstrip(b"=").decode("ascii")


def site_for(base_url: str) -> str:
    """The website that verifies receipts from this gateway."""
    return SITES.get(urlsplit(base_url).hostname or "", "https://www.valyzen.ai")


def verify_link(receipt: Mapping[str, Any], *, base_url: str = "https://api.valyzen.ai") -> str:
    return f"{site_for(base_url)}/verify#r={encode_receipt(receipt)}"
