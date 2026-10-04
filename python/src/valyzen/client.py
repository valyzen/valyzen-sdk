"""HTTP client for the Valyzen Negotiation Gateway's REST API.

Transport is ``httpx``; a caller may inject an ``httpx.Client`` (the tests pass
one with a mock transport, so nothing touches the network). Server-side
failures (5xx) and connection errors are retried with a short backoff; every
gateway refusal is raised as :class:`GatewayClientError` carrying the
``{code, message}`` body and the HTTP status, so callers branch on ``code``.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from decimal import ROUND_CEILING, Decimal
from typing import Any

import httpx

from valyzen._version import __version__

__all__ = ["GatewayClient", "GatewayClientError", "Session", "suggested_price"]

DEFAULT_BASE_URL = "https://api.valyzen.ai"
KEY_ENV = "VALYZEN_API_KEY"
BASE_URL_ENV = "VALYZEN_BASE_URL"
USER_AGENT = f"valyzen-python/{__version__}"
RETRIABLE = frozenset({500, 502, 503, 504})


class GatewayClientError(Exception):
    """The gateway refused the request. ``code`` is stable; ``status`` is HTTP."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        # The rest of the error body, e.g. ``retry_after`` or a settlement
        # mismatch's ``expected`` / ``actual``.
        self.details: dict[str, Any] = dict(details or {})


class GatewayClient:
    """One tenant key (publishable or secret) against one gateway.

    ``key`` and ``base_url`` default to ``VALYZEN_API_KEY`` and
    ``VALYZEN_BASE_URL`` (else ``https://api.valyzen.ai``).
    """

    def __init__(
        self,
        key: str | None = None,
        *,
        base_url: str | None = None,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        key = key if key is not None else os.environ.get(KEY_ENV, "")
        if not key:
            raise GatewayClientError(
                0, "no_key", f"pass a key or set {KEY_ENV} (get one at www.valyzen.ai/keys)"
            )
        base_url = base_url or os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL
        if not base_url.startswith("https://") and not _is_local(base_url):
            # A key over plain HTTP is a key anyone on the path can read.
            raise GatewayClientError(0, "insecure_base_url", "base_url must be https://")
        self._key = key
        self._base = base_url.rstrip("/")
        self._http = client if client is not None else httpx.Client(timeout=timeout)
        self._retries = retries
        self._sleep = sleep

    # -- public, unauthenticated -------------------------------------------

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/healthz", auth=None)

    def jwks(self) -> dict[str, Any]:
        return self._request("GET", "/.well-known/jwks.json", auth=None)

    # -- tenant-scoped -----------------------------------------------------

    def me(self) -> dict[str, Any]:
        return self._request("GET", "/v1/me")

    def catalogue(self) -> list[dict[str, Any]]:
        items = self._request("GET", "/v1/catalogue")["items"]
        return list(items)

    def policies(self) -> list[dict[str, Any]]:
        return list(self._request("GET", "/v1/policies")["policies"])

    def put_policy(self, policy: Mapping[str, Any]) -> dict[str, Any]:
        sku = str(policy["sku"])
        return self._request("PUT", f"/v1/policies/{sku}", json=dict(policy))

    def sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        out = self._request("GET", "/v1/sessions", params={"limit": limit})
        return list(out["sessions"])

    def open_session(
        self,
        sku: str,
        *,
        agent_id: str,
        max_rounds: int = 5,
        currency: str | None = None,
        inclusions: list[str] | None = None,
        utilities: Mapping[str, str] | None = None,
        commitment: str | None = None,
        description: str | None = None,
    ) -> Session:
        """Open a session; the merchant moves first and the view says so."""
        declared: dict[str, Any] = {"max_rounds": max_rounds}
        if currency:
            declared["currency"] = currency
        if inclusions:
            declared["inclusions"] = list(inclusions)
        if utilities:
            declared["utilities"] = dict(utilities)
        buyer: dict[str, Any] = {"agent_id": agent_id, "declared": declared}
        if commitment:
            buyer["commitment"] = commitment
        body: dict[str, Any] = {"sku": sku, "buyer": buyer}
        if description:
            body["description"] = description
        view = self._request("POST", "/v1/sessions", json=body)
        token = str(view.pop("session_token"))
        return Session(self, view["session_id"], token, view)

    def session(self, session_id: str, token: str | None = None) -> Session:
        """Reattach to a session with its token (or this client's secret key)."""
        view = self._request("GET", f"/v1/sessions/{session_id}", auth=token or self._key)
        return Session(self, session_id, token or self._key, view)

    def claim_handoff(
        self, session_id: str, *, token: str | None = None, timeout: float = 60.0
    ) -> dict[str, Any]:
        """Collect the merchant's hand-off for an agreed session (FR-SHOP-E2E-021).

        Authenticates with the session token (``token``) or this client's key
        when it is a signed-in user token. While the merchant has not published
        yet (409 ``handoff_pending``) it waits ``retry_after`` seconds and asks
        again, giving up with that error after ``timeout`` seconds of waiting.
        Returns ``{kind, value, checkout_url, expires_at}``; ``value`` is a
        bearer secret (a discount code or invoice URL): do not log it.
        """
        waited = 0.0
        while True:
            try:
                return self._request(
                    "POST",
                    f"/v1/sessions/{session_id}/handoff/claim",
                    auth=token or self._key,
                )
            except GatewayClientError as exc:
                if exc.code != "handoff_pending":
                    raise
                pause = float(exc.details.get("retry_after") or 2)
                if waited + pause > timeout:
                    raise
                self._sleep(pause)
                waited += pause

    # -- transport ---------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        auth: str | None = "",
        json: Any = None,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers: dict[str, str] = {
            "accept": "application/json",
            "user-agent": USER_AGENT,
        }
        bearer = self._key if auth == "" else auth
        if bearer:
            headers["authorization"] = f"Bearer {bearer}"
        attempt = 0
        while True:
            try:
                response = self._http.request(
                    method,
                    self._base + path,
                    headers=headers,
                    json=json,
                    params=dict(params) if params else None,
                )
            except httpx.TransportError as exc:
                if attempt >= self._retries:
                    raise GatewayClientError(0, "transport", str(exc)) from exc
                attempt += 1
                self._sleep(0.25 * attempt)
                continue
            if response.status_code in RETRIABLE and attempt < self._retries:
                attempt += 1
                self._sleep(0.25 * attempt)
                continue
            return _parse(response)


def _is_local(url: str) -> bool:
    host = httpx.URL(url).host
    return host in ("localhost", "127.0.0.1", "::1", "testserver")


def _parse(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        body = None
    if response.is_success:
        if not isinstance(body, dict):
            raise GatewayClientError(response.status_code, "bad_response", "expected a JSON object")
        return body
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        raise GatewayClientError(
            response.status_code,
            str(error.get("code", "error")),
            str(error.get("message", response.reason_phrase)),
            {k: v for k, v in error.items() if k not in ("code", "message")},
        )
    raise GatewayClientError(
        response.status_code, "http_error", response.text[:200] or "request failed"
    )


class Session:
    """A buyer's handle on one session: moves, state, and the receipt.

    ``view`` is always the latest server view: ``state``, ``your_turn``,
    ``corrective``, ``offer_on_table``, ``last_verdict``, ``events``.

    The session also remembers what the arbiter published along the way: the
    attested target cost and fair band (``session.accept``) and the perceived
    inclusion value ``W`` it scored for each party's terms, which is what
    :attr:`fair_edge_minor` is computed from (FR-SHOP-E2E-060).
    """

    def __init__(
        self, client: GatewayClient, session_id: str, token: str, view: dict[str, Any]
    ) -> None:
        self._client = client
        self.session_id = session_id
        self.token = token
        self._t_target: int | None = None
        self._band: Decimal | None = None
        self._w: dict[tuple[str, frozenset[str]], int] = {}
        self._last_offer: tuple[str, frozenset[str]] | None = None
        self.view = view
        self._learn(view)

    # -- state -------------------------------------------------------------

    @property
    def state(self) -> str:
        return str(self.view.get("state", ""))

    @property
    def finished(self) -> bool:
        return self.state in ("agreed", "closed")

    @property
    def your_turn(self) -> bool:
        return bool(self.view.get("your_turn"))

    @property
    def corrective(self) -> bool:
        return bool(self.view.get("corrective"))

    @property
    def merchant_offer_minor(self) -> int | None:
        """The merchant's standing price, if the offer on the table is theirs."""
        on_table = self.view.get("offer_on_table")
        if not isinstance(on_table, dict) or on_table.get("proposer") != "merchant":
            return None
        return int(on_table["terms"]["price"]["amount_minor"])

    @property
    def suggested_price_minor(self) -> int | None:
        return suggested_price(self.view)

    @property
    def table_inclusions(self) -> list[str]:
        """The inclusion kinds in the terms on the table."""
        on_table = self.view.get("offer_on_table")
        if not isinstance(on_table, dict):
            return []
        items = (on_table.get("terms") or {}).get("inclusions") or []
        return [str(i["kind"]) for i in items if isinstance(i, dict) and "kind" in i]

    @property
    def fair_edge_minor(self) -> int | None:
        """The buyer's edge of the fair band, as a price for the terms on the
        table: ``ceil(T_target / (1 + band)) + W`` (FR-SHOP-E2E-060).

        The cheapest price the arbiter still scores as fair to the merchant.
        ``W`` is the perceived value of the inclusions as the arbiter scored
        it: for the buyer's own earlier offer of the same inclusions when there
        is one (the arbiter weighs a buyer's offer with the merchant's
        utilities), else for the merchant's terms. Public inputs only; it says
        nothing about any party's private limit.
        """
        if self._t_target is None or self._band is None:
            return None
        low = Decimal(self._t_target) / (Decimal(1) + self._band)
        edge = int(low.quantize(Decimal(1), rounding=ROUND_CEILING))
        kinds = frozenset(self.table_inclusions)
        w = self._w.get(("buyer", kinds), self._w.get(("merchant", kinds), 0))
        return edge + w

    def _learn(self, view: Mapping[str, Any]) -> None:
        """Note the arbiter's published inputs and scores from a view."""
        for key in ("log", "events"):
            entries = view.get(key)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                try:
                    self._learn_one(entry)
                except (KeyError, TypeError, ValueError, ArithmeticError):
                    continue

    def _learn_one(self, entry: Any) -> None:
        kind = entry.get("type")
        payload = entry.get("payload") or {}
        if kind == "session.accept":
            self._band = Decimal(str(payload["session_rules"]["fair_band"]))
            self._t_target = int(payload["attested"]["t_target"]["amount_minor"])
        elif kind in ("offer.propose", "offer.counter"):
            items = payload["terms"].get("inclusions") or []
            self._last_offer = (
                str(entry["sender"]["role"]),
                frozenset(str(i["kind"]) for i in items),
            )
        elif kind == "arbitration.evaluate" and self._last_offer is not None:
            self._w[self._last_offer] = int(payload["w_minor"])

    # -- moves -------------------------------------------------------------

    def offer(
        self,
        price_minor: int,
        *,
        inclusions: list[str] | None = None,
        justification: str | None = None,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"type": "offer", "price_minor": int(price_minor)}
        if inclusions:
            body["inclusions"] = list(inclusions)
        if justification:
            body["justification"] = justification
        return self._send(body, message_id)

    def accept(self, *, message_id: str | None = None) -> dict[str, Any]:
        return self._send({"type": "accept"}, message_id)

    def reject(self, reason: str | None = None, *, message_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"type": "reject"}
        if reason:
            body["reason"] = reason
        return self._send(body, message_id)

    def reveal(self, private: Mapping[str, Any]) -> dict[str, Any]:
        return self._send({"type": "reveal", "private": dict(private)}, None)

    def refresh(self) -> dict[str, Any]:
        self.view = self._client._request("GET", f"/v1/sessions/{self.session_id}", auth=self.token)
        self._learn(self.view)
        return self.view

    def claim_handoff(self, *, timeout: float = 60.0) -> dict[str, Any]:
        """:meth:`GatewayClient.claim_handoff` with this session's token."""
        return self._client.claim_handoff(self.session_id, token=self.token, timeout=timeout)

    def receipt(self) -> dict[str, Any]:
        return self._client._request(
            "GET", f"/v1/sessions/{self.session_id}/receipt", auth=self.token
        )

    def _send(self, body: dict[str, Any], message_id: str | None) -> dict[str, Any]:
        if message_id:
            body["message_id"] = message_id
        self.view = self._client._request(
            "POST",
            f"/v1/sessions/{self.session_id}/messages",
            auth=self.token,
            json=body,
        )
        self._learn(self.view)
        return self.view


def suggested_price(view: Mapping[str, Any]) -> int | None:
    """The arbiter's most recent suggested price for the buyer's next move.

    Every ``arbitration.evaluate`` and ``arbitration.reject`` the gateway returns
    carries ``suggested_adjustment``: the price at which the scored cost equals
    the attested target. The last one in ``events`` is the freshest.
    """
    events = view.get("events")
    if not isinstance(events, list):
        return None
    for event in reversed(events):
        if not isinstance(event, dict):
            continue
        if event.get("type") in ("arbitration.reject", "arbitration.evaluate"):
            payload = event.get("payload") or {}
            suggestion = payload.get("suggested_adjustment")
            if isinstance(suggestion, dict) and "amount_minor" in suggestion:
                return int(suggestion["amount_minor"])
    return None
