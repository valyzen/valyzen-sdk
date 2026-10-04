"""``valyzen``: try a negotiation in the sandbox, or verify a receipt offline.

export VALYZEN_API_KEY=vz_pk_test_…     # www.valyzen.ai/keys
valyzen try                             # negotiate the Demo Monitor, verify, link
valyzen verify receipt.json [--log log.json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from valyzen._version import __version__
from valyzen.buyer import Ceiling, bid_minor
from valyzen.client import (
    BASE_URL_ENV,
    DEFAULT_BASE_URL,
    KEY_ENV,
    GatewayClient,
    GatewayClientError,
    Session,
)
from valyzen.links import site_for, verify_link
from valyzen.receipt import Verification, verify_receipt

DEMO_SKU = "DEMO-MONITOR-27"
MAX_MOVES = 12

Out = Callable[[str], None]


def money(minor: int | None, currency: str = "USD") -> str:
    if minor is None:
        return "—"
    symbol = {"USD": "$", "EUR": "€", "GBP": "£", "INR": "₹"}.get(currency, "")
    amount = f"{Decimal(minor) / 100:,.2f}"
    return f"{symbol}{amount}" if symbol else f"{amount} {currency}"


def _price(value: str) -> int:
    """'420' or '420.00' (major units) to minor units."""
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"not a price: {value!r}") from None
    if amount <= 0 or amount != amount.quantize(Decimal("0.01")):
        raise argparse.ArgumentTypeError("a positive price with at most two decimals")
    return int(amount * 100)


def describe(event: dict[str, Any], currency: str) -> str | None:
    """One line per event a person cares about; the rest are skipped."""
    kind = event.get("type")
    role = (event.get("sender") or {}).get("role")
    p = event.get("payload") or {}
    if kind in ("offer.propose", "offer.counter"):
        terms = p.get("terms") or {}
        price = (terms.get("price") or {}).get("amount_minor")
        extras = [i.get("kind") for i in terms.get("inclusions") or [] if isinstance(i, dict)]
        with_ = f" with {', '.join(extras)}" if extras else ""
        who = "merchant offers" if role == "merchant" else "you offer"
        return f"  {who:<16}{money(price, currency)}{with_}"
    if kind == "arbitration.evaluate":
        verdict = str(p.get("verdict", "")).replace("_", " ")
        return f"  {'arbiter':<16}{verdict} (fairness {p.get('fairness_score', '?')})"
    if kind == "arbitration.reject":
        return f"  {'':<16}→ corrective round: the offer must move into the fair band"
    if kind == "session.agree":
        return f"  {'arbiter':<16}agreement reached and signed"
    if kind == "session.close":
        return f"  {'session':<16}closed: {p.get('reason') or p.get('detail') or 'no deal'}"
    return None


def negotiate(session: Session, ceiling: Ceiling, out: Out, currency: str) -> None:
    """Bid at the buyer's fair edge, never above the ceiling, until the session ends."""
    seen: set[str] = set()

    def show() -> None:
        # Views carry a window of recent events, not always only new ones.
        for event in session.view.get("events") or []:
            if not isinstance(event, dict):
                continue
            mid = str(event.get("message_id") or json.dumps(event, sort_keys=True))
            if mid in seen:
                continue
            seen.add(mid)
            line = describe(event, currency)
            if line:
                out(line)

    show()
    for _ in range(MAX_MOVES):
        if session.finished:
            return
        if not session.your_turn:
            time.sleep(0.5)
            session.refresh()
            show()
            continue
        bid = bid_minor(session, ceiling)
        standing = session.merchant_offer_minor
        if standing is not None and standing <= bid:
            out(f"  {'you accept':<16}{money(standing, currency)}")
            session.accept()
        else:
            session.offer(bid, inclusions=session.table_inclusions or None)
        show()
    if not session.finished:
        session.reject("valyzen try: move limit reached")
        show()


def report(v: Verification, receipt: dict[str, Any], out: Out) -> None:
    labels = {
        "header": "header",
        "key": "key embedded",
        "signature": "signature",
        "signer": "signed by Valyzen",
        "mode": "mode",
        "terms": "terms match",
        "chain": "hash chain",
    }
    for check in v.checks:
        mark = "–" if check.skipped else ("✓" if check.ok else "✗")
        out(f"  {mark} {labels.get(check.id, check.id):<18}{check.detail}")
    out("")
    if v.ok:
        test = " TEST MODE: sandbox, not a real purchase." if v.signer.mode == "test" else ""
        out(f"Verified offline: signed by {v.signer.label} ({v.signer.mode} key {v.kid}).{test}")
    elif receipt.get("state") != "agreed":
        out("No agreement, so nothing was signed.")
    else:
        out("NOT verified. Do not act on these terms.")


def cmd_try(args: argparse.Namespace, out: Out) -> int:
    base = args.base_url or os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL
    key = args.key or os.environ.get(KEY_ENV)
    if not key:
        out(f"No API key. Get a free test key at {site_for(base)}/keys, then:")
        out(f"  export {KEY_ENV}=vz_pk_test_…")
        out("  valyzen try")
        return 2
    if "_live_" in key:
        out("valyzen try runs in the sandbox: use a test key (vz_pk_test_…), not a live one.")
        return 2
    client = GatewayClient(key, base_url=base)

    item = next((i for i in client.catalogue() if i.get("sku") == args.sku), None)
    if item is None:
        out(f"{args.sku} is not in this store's catalogue.")
        return 1
    currency = str(item.get("currency") or "USD")
    ceiling = Ceiling(args.ceiling if args.ceiling is not None else int(item["list_price_minor"]))
    out(
        f"{item.get('description') or args.sku}  ·  list {money(item.get('list_price_minor'), currency)}"
    )
    out(
        f"Your ceiling: {money(ceiling.amount_minor, currency)} "
        "(private: it stays in this process and is never sent)."
    )
    out("")

    session = client.open_session(
        args.sku, agent_id="valyzen-try", inclusions=list(item.get("inclusions") or [])
    )
    started = time.monotonic()
    negotiate(session, ceiling, out, currency)
    out("")

    receipt = session.receipt()
    terms = (receipt.get("signed_payload") or {}).get("final_terms") or {}
    price = (terms.get("price") or {}).get("amount_minor")
    if receipt.get("state") == "agreed":
        out(
            f"Agreed: {money(price, currency)} in {time.monotonic() - started:.1f}s · session {session.session_id}"
        )
    else:
        out(f"Session {session.session_id} ended: {receipt.get('state')}")
    v = verify_receipt(receipt, allow_test=True)  # the sandbox only signs test receipts
    report(v, receipt, out)

    if args.save:
        Path(args.save).write_text(json.dumps(receipt, indent=2) + "\n")
        out(f"Receipt saved to {args.save}  ·  valyzen verify {args.save}")
    if receipt.get("state") == "agreed":
        out("")
        out("Check it in your browser (the receipt stays in the link, never sent to a server):")
        out(verify_link(receipt, base_url=base))
    return 0 if v.ok or receipt.get("state") != "agreed" else 1


def cmd_verify(args: argparse.Namespace, out: Out) -> int:
    try:
        receipt = json.loads(Path(args.receipt).read_text())
        log = json.loads(Path(args.log).read_text()) if args.log else None
    except (OSError, ValueError) as exc:
        out(f"Cannot read input: {exc}")
        return 2
    if not isinstance(receipt, dict):
        out("A receipt is a JSON object.")
        return 2
    if log is not None and not isinstance(log, list):
        out("The log must be a JSON array of envelopes.")
        return 2
    out(f"Receipt for session {receipt.get('session_id')}: {receipt.get('state')}")
    v = verify_receipt(receipt, log=log, allow_test=args.allow_test)
    report(v, receipt, out)
    return 0 if v.ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="valyzen", description=__doc__.split("\n")[0])
    parser.add_argument("--version", action="version", version=f"valyzen {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    t = sub.add_parser("try", help="negotiate the Demo Monitor in the sandbox store")
    t.add_argument("--sku", default=DEMO_SKU, help=f"catalogue item (default {DEMO_SKU})")
    t.add_argument(
        "--ceiling", type=_price, help="your private limit, e.g. 420.00 (default: list price)"
    )
    t.add_argument("--key", help=f"API key (default ${KEY_ENV}; prefer the variable)")
    t.add_argument(
        "--base-url", help=f"gateway URL (default ${BASE_URL_ENV} or {DEFAULT_BASE_URL})"
    )
    t.add_argument("--save", metavar="FILE", help="also write the receipt JSON to FILE")

    v = sub.add_parser("verify", help="verify a receipt offline")
    v.add_argument("receipt", help="receipt JSON file")
    v.add_argument("--log", help="session log (JSON array of envelopes) to check the hash chain")
    v.add_argument(
        "--allow-test",
        action="store_true",
        help="accept test-mode (sandbox) receipts; they prove nothing to a third party",
    )
    return parser


def main(argv: Sequence[str] | None = None, out: Out = print) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "try":
            return cmd_try(args, out)
        return cmd_verify(args, out)
    except GatewayClientError as exc:
        hint = {
            401: "the key was refused: check it, or get a new one",
            429: "rate limited: wait a minute and try again",
        }.get(exc.status, "")
        out(
            f"Gateway error {exc.status} {exc.code}: {exc.message}" + (f" ({hint})" if hint else "")
        )
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
