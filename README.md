# Valyzen SDK

Clients for the [Valyzen Negotiation Gateway](https://www.valyzen.ai): open a negotiation,
exchange offers under a neutral arbiter, and verify the signed receipt **offline**.

| Language | Package | Install |
|---|---|---|
| Python ≥ 3.10 (+ `valyzen` CLI) | [`valyzen`](https://pypi.org/project/valyzen/) | `pip install valyzen` |
| TypeScript / JavaScript (Node ≥ 20, browsers, Deno, Bun) | [`@valyzen/sdk`](https://www.npmjs.com/package/@valyzen/sdk) | `npm install @valyzen/sdk` |

## Five minutes to a signed receipt

1. Get a free test key at **[www.valyzen.ai/keys](https://www.valyzen.ai/keys)** (sign in with Google or email).
2. Run a sample buyer against the shared sandbox store:

   ```sh
   pip install valyzen
   export VALYZEN_API_KEY=vz_pk_test_…
   valyzen try
   ```

   ```text
   Demo Monitor 27" 4K IPS (sandbox)  ·  list $449.00
   Your ceiling: $449.00 (private: it stays in this process and is never sent).

     merchant offers $419.00 with warranty, cable
     arbiter         fair (fairness 1.0000)
     you offer       $399.05 with warranty, cable
     arbiter         unfair to merchant (fairness 1.0944)
                     → corrective round: the offer must move into the fair band
     you offer       $415.25 with warranty, cable
     arbiter         fair (fairness 1.0500)
     arbiter         agreement reached and signed

   Agreed: $415.25 in 0.4s · session 01M443ZBJEYCTQSEFD1VVMDHPD
     ✓ signature         The signature verifies over the canonical signed payload.
     ✓ signed by Valyzen Signed by the Valyzen arbiter (test key …).
     ✓ terms match       final_terms and session_id match what was signed.
   Verified offline: signed by the Valyzen arbiter.

   Check it in your browser (the receipt stays in the link, never sent to a server):
   https://www.valyzen.ai/verify#r=v1.…
   ```

3. Open the link: the website re-checks the signature in your browser.

## What "verified" means

A receipt carries a detached Ed25519 signature (JWS, RFC 7797) over the RFC 8785 canonical
bytes of the agreed terms and the session's hash-chain head. Both SDKs check, offline:

* the signature verifies over the signed payload;
* the key that signed is **byte-for-byte a pinned Valyzen arbiter key** (`valyzen/keys.py`,
  `js/src/keys.ts`). A valid signature alone proves nothing: anyone can sign a receipt with
  their own key under any key id;
* the receipt's `final_terms` and `session_id` are what was signed. Act on `signed_terms`
  (`signedTerms`), never on unsigned fields;
* with the session log, every envelope links to the one before it (`prev_hash`) and the
  signed chain head is the envelope before `session.agree`.

Only `ok` / `result.ok` means all of that held. `status == "valid"` is the signature alone.

## Your limit stays yours

On the SDK path the buyer's ceiling never leaves your process: `valyzen try` and
`valyzen.buyer.Ceiling` only ever send offers, never the limit. (On the MCP path the
gateway holds the buyer's commitment for you: `buyer_commitment_by: gateway` on the receipt.)

## Repository

| Path | |
|---|---|
| `python/` | PyPI `valyzen`: client, buyer, offline verifier, CLI |
| `js/` | npm `@valyzen/sdk`: zero-dependency client and offline verifier |
| `fixtures/` | real and forged receipts both test suites check |

Contract tests against a live sandbox are opt-in: `VALYZEN_API_KEY=… uv run pytest -m contract`.
Releases publish from `.github/workflows/release.yml` through trusted publishing (GitHub OIDC),
with an owner's approval per registry. No registry tokens exist in this repository.

## License

Apache-2.0. See [LICENSE](LICENSE).
