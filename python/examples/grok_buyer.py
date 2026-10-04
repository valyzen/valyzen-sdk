#!/usr/bin/env python3
"""A Grok-driven buyer negotiating through the hosted gateway.

    export XAI_API_KEY=...            # or OPENAI_API_KEY + OPENAI_BASE_URL
    uv run python examples/grok_buyer.py --key vz_pk_test_... \
        --sku SKU-JACKET-001 --ceiling-minor 18000 --model grok-4

The model chooses moves through tool calls; the ceiling never leaves this
process (see valyzen.buyer). Any OpenAI-compatible endpoint works:
swap --model and set OPENAI_BASE_URL to point at another provider.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from valyzen import GatewayClient
from valyzen.buyer import Ceiling, OpenAICompatibleModel, run_buyer


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", default="https://api.valyzen.ai")
    ap.add_argument("--key", required=True, help="the merchant's publishable key")
    ap.add_argument("--sku", required=True)
    ap.add_argument("--ceiling-minor", type=int, required=True)
    ap.add_argument("--max-rounds", type=int, default=5)
    ap.add_argument("--model", default="grok-4")
    ap.add_argument("--agent-id", default="grok-buyer")
    ap.add_argument(
        "--llm-base-url",
        default=os.getenv("OPENAI_BASE_URL", "https://api.x.ai/v1"),
        help="OpenAI-compatible chat completions base URL",
    )
    args = ap.parse_args()

    api_key = os.getenv("XAI_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("set XAI_API_KEY (or OPENAI_API_KEY)", file=sys.stderr)
        return 2

    model = OpenAICompatibleModel(base_url=args.llm_base_url, api_key=api_key, model=args.model)
    client = GatewayClient(args.key, base_url=args.base_url)
    run = run_buyer(
        client,
        model,
        sku=args.sku,
        ceiling=Ceiling(args.ceiling_minor),
        agent_id=args.agent_id,
        max_rounds=args.max_rounds,
        log=lambda line: print(f"  {line}", file=sys.stderr),
    )
    print(
        json.dumps(
            {
                "session_id": run.session_id,
                "state": run.state,
                "moves": run.moves,
                "final_terms": (run.receipt or {}).get("final_terms"),
                "arbiter": (run.receipt or {}).get("arbiter"),
                "verification": run.verification,
            },
            indent=2,
        )
    )
    return 0 if run.state == "agreed" else 3


if __name__ == "__main__":
    raise SystemExit(main())
