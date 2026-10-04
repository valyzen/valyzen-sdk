# valyzen

Python client and CLI for the [Valyzen Negotiation Gateway](https://www.valyzen.ai):
negotiate under a neutral arbiter, then verify the signed receipt offline.

```sh
pip install valyzen
export VALYZEN_API_KEY=vz_pk_test_…    # free test key: https://www.valyzen.ai/keys
valyzen try                            # a sample buyer negotiates the Demo Monitor
valyzen verify receipt.json            # verify any receipt offline
```

```python
from valyzen import GatewayClient, verify_receipt

client = GatewayClient()                      # VALYZEN_API_KEY, VALYZEN_BASE_URL
session = client.open_session("DEMO-MONITOR-27", agent_id="my-buyer",
                              inclusions=["warranty", "cable"])
while not session.finished:
    edge = session.fair_edge_minor            # cheapest price the arbiter scores fair
    standing = session.merchant_offer_minor
    if standing is not None and standing <= edge:
        session.accept()
    else:
        session.offer(edge, inclusions=session.table_inclusions)

result = verify_receipt(session.receipt())    # offline, pinned Valyzen keys
if result.ok:
    print(result.signed_terms)                # act on the signed terms only
```

`valyzen.buyer` adds a guard that keeps a private ceiling in your process
(`Ceiling`) and an LLM buyer over any OpenAI-compatible chat API (`run_buyer`,
`OpenAICompatibleModel`); `FairEdgeModel` is the same buyer without a model.

Source, docs and the TypeScript SDK: https://github.com/valyzen/valyzen-sdk · Apache-2.0
