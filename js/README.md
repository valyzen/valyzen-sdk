# @valyzen/sdk

Zero-dependency TypeScript/JavaScript client for the
[Valyzen Negotiation Gateway](https://www.valyzen.ai): negotiate under a neutral arbiter,
then verify the signed receipt offline with Web Crypto. Node ≥ 20, browsers, Deno, Bun.

```sh
npm install @valyzen/sdk
export VALYZEN_API_KEY=vz_pk_test_…    # free test key: https://www.valyzen.ai/keys
```

```ts
import { GatewayClient, verifyReceipt, verifyLink } from '@valyzen/sdk'

const client = new GatewayClient()            // VALYZEN_API_KEY, VALYZEN_BASE_URL in Node
const session = await client.openSession('DEMO-MONITOR-27', { agentId: 'my-buyer', inclusions: ['warranty', 'cable'] })
while (!session.finished) {
  const edge = session.fairEdgeMinor!         // cheapest price the arbiter scores fair
  const standing = session.merchantOfferMinor
  if (standing !== null && standing <= edge) await session.accept()
  else await session.offer(edge, { inclusions: session.tableInclusions })
}

const receipt = await session.receipt()
const result = await verifyReceipt(receipt)   // offline, pinned Valyzen keys
if (result.ok) console.log(result.signedTerms, await verifyLink(receipt))
```

`result.ok` is true only when the signature verifies, the signing key is a pinned Valyzen
arbiter key, and the receipt's terms are the signed ones. `result.status === 'valid'` is
the signature alone: anyone can sign a receipt with their own key.

In browsers, pass the key from your server, never ship a secret key (`vz_sk_…`) to a page.

Source, docs and the Python SDK: https://github.com/valyzen/valyzen-sdk · Apache-2.0
