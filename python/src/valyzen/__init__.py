"""Python client for the Valyzen Negotiation Gateway.

    from valyzen import GatewayClient, verify_receipt

    client = GatewayClient()                    # VALYZEN_API_KEY, VALYZEN_BASE_URL
    session = client.open_session("DEMO-MONITOR-27", agent_id="my-buyer")
    session.offer(41500, inclusions=session.table_inclusions)
    receipt = session.receipt()
    result = verify_receipt(receipt)            # offline; pinned Valyzen keys
    if result.ok:
        act_on(result.signed_terms)

A publishable key opens sessions; the session token the gateway returns drives
that one session. Nothing here holds a buyer's private limit: keep it in your
own process (:class:`valyzen.buyer.Ceiling`).
"""

from valyzen._version import __version__
from valyzen.client import GatewayClient, GatewayClientError, Session, suggested_price
from valyzen.keys import ARBITER_KEYS, signer_of
from valyzen.links import verify_link
from valyzen.receipt import Check, Verification, commit, verify_receipt

__all__ = [
    "ARBITER_KEYS",
    "Check",
    "GatewayClient",
    "GatewayClientError",
    "Session",
    "Verification",
    "__version__",
    "commit",
    "signer_of",
    "suggested_price",
    "verify_link",
    "verify_receipt",
]
