import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { GatewayClient, GatewayClientError, suggestedPrice, verifyLink, type Receipt } from '../src/index'

const receipt = JSON.parse(readFileSync(new URL('../../fixtures/dev-receipt.json', import.meta.url), 'utf8')) as Receipt
const KEY = 'vz_pk_test_unit_secretvalue'

/** A tiny in-memory gateway: enough routes to exercise the client. */
function fakeGateway() {
  const calls: { method: string; url: string; auth: string | undefined; body?: Record<string, unknown> }[] = []
  let state = 'negotiating'
  const accept = {
    type: 'session.accept',
    sender: { role: 'arbiter' },
    payload: { session_rules: { fair_band: '0.05' }, attested: { t_target: { amount_minor: 41900, currency: 'USD' } } },
  }
  const view = (extra: Record<string, unknown> = {}) => ({
    session_id: 's1',
    mode: 'test',
    sku: 'DEMO-MONITOR-27',
    state,
    round: 1,
    max_rounds: 5,
    your_turn: state === 'negotiating',
    corrective: false,
    offer_on_table: {
      proposer: 'merchant',
      terms: { price: { amount_minor: 41900, currency: 'USD' }, inclusions: [{ kind: 'warranty', value: 'included' }] },
    },
    last_verdict: { verdict: 'fair' },
    close: null,
    events: [
      accept,
      { type: 'offer.propose', sender: { role: 'merchant' }, payload: { terms: { inclusions: [{ kind: 'warranty' }] } } },
      { type: 'arbitration.evaluate', sender: { role: 'arbiter' }, payload: { w_minor: 0, suggested_adjustment: { amount_minor: 41900 } } },
    ],
    receipt: null,
    ...extra,
  })
  const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status })
  const fetchImpl: typeof fetch = async (input, init) => {
    const url = String(input)
    const auth = (init?.headers as Record<string, string> | undefined)?.authorization
    const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : undefined
    calls.push({ method: init?.method ?? 'GET', url, auth, ...(body ? { body } : {}) })
    if (url.endsWith('/healthz')) return json(200, { ok: true })
    if (!auth) return json(401, { error: { code: 'unauthenticated', message: 'send a key' } })
    if (url.endsWith('/v1/catalogue')) return json(200, { items: [{ sku: 'DEMO-MONITOR-27' }] })
    if (url.endsWith('/v1/sessions') && init?.method === 'POST') return json(201, { ...view(), session_token: 'vz_st_test_s1_tok' })
    if (url.endsWith('/v1/sessions/s1/messages')) {
      if (body?.type === 'accept') state = 'agreed'
      return json(200, view())
    }
    if (url.endsWith('/v1/sessions/s1')) return json(200, view())
    if (url.endsWith('/v1/sessions/s1/receipt')) return json(200, receipt)
    return json(404, { error: { code: 'session_not_found', message: 'no' } })
  }
  return { fetchImpl, calls }
}

describe('GatewayClient', () => {
  it('opens a session, learns the fair edge, moves with the session token, fetches the receipt', async () => {
    const { fetchImpl, calls } = fakeGateway()
    const client = new GatewayClient(KEY, { baseUrl: 'http://localhost:8000/', fetch: fetchImpl })
    expect((await client.health()).ok).toBe(true)
    const session = await client.openSession('DEMO-MONITOR-27', { agentId: 'b' })
    expect(session.token).toBe('vz_st_test_s1_tok')
    expect(session.view.session_token).toBeUndefined()
    expect(session.merchantOfferMinor).toBe(41900)
    expect(session.tableInclusions).toEqual(['warranty'])
    expect(session.fairEdgeMinor).toBe(39905) // ceil(41900 / 1.05) + W 0, same as the Python SDK
    await session.offer(41500, { inclusions: session.tableInclusions, messageId: 'm1' })
    await session.accept()
    expect(session.finished).toBe(true)
    expect((await session.receipt()).session_id).toBe(receipt.session_id)
    const moves = calls.filter((c) => c.url.endsWith('/messages'))
    expect(moves[0]?.auth).toBe('Bearer vz_st_test_s1_tok')
    expect(moves[0]?.body).toEqual({ type: 'offer', price_minor: 41500, inclusions: ['warranty'], message_id: 'm1' })
    expect(calls[0]?.auth).toBeUndefined() // health is public
    expect(calls[1]?.auth).toBe(`Bearer ${KEY}`)
  })

  it('retries server errors, then raises refusals with their code and details', async () => {
    let n = 0
    const fetchImpl: typeof fetch = async () => {
      n += 1
      if (n < 3) return new Response('', { status: 503 })
      return new Response(JSON.stringify({ error: { code: 'rate_limited', message: 'slow', retry_after: 30 } }), { status: 429 })
    }
    const client = new GatewayClient(KEY, { baseUrl: 'https://api-dev.valyzen.ai', fetch: fetchImpl, sleep: async () => {} })
    const err = await client.catalogue().catch((e: unknown) => e)
    expect(err).toBeInstanceOf(GatewayClientError)
    expect((err as GatewayClientError).code).toBe('rate_limited')
    expect((err as GatewayClientError).details).toEqual({ retry_after: 30 })
    expect(n).toBe(3)
  })

  it('reads the key from VALYZEN_API_KEY and refuses to run without one', () => {
    const saved = process.env.VALYZEN_API_KEY
    try {
      delete process.env.VALYZEN_API_KEY
      expect(() => new GatewayClient()).toThrow(/VALYZEN_API_KEY/)
      process.env.VALYZEN_API_KEY = KEY
      expect(() => new GatewayClient()).not.toThrow()
    } finally {
      if (saved === undefined) delete process.env.VALYZEN_API_KEY
      else process.env.VALYZEN_API_KEY = saved
    }
  })

  it('refuses plain http except to localhost', () => {
    expect(() => new GatewayClient(KEY, { baseUrl: 'http://api.valyzen.ai' })).toThrow(/https/)
    expect(() => new GatewayClient(KEY, { baseUrl: 'http://127.0.0.1:8000' })).not.toThrow()
  })

  it('suggestedPrice reads the freshest arbiter suggestion', () => {
    expect(suggestedPrice({ events: [{ type: 'arbitration.evaluate', payload: { suggested_adjustment: { amount_minor: 5 } } }] })).toBe(5)
    expect(suggestedPrice({ events: 'nope' })).toBeNull()
  })
})

describe('verifyLink', () => {
  it('round-trips through raw DEFLATE and points at the matching site', async () => {
    const link = await verifyLink(receipt, 'https://api-dev.valyzen.ai')
    expect(link.startsWith('https://dev.valyzen.ai/verify#r=v1.')).toBe(true)
    const value = link.split('#r=v1.')[1]!
    expect(value).toMatch(/^[A-Za-z0-9_-]+$/)
    const b64 = value.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (value.length % 4)) % 4)
    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))
    const out = new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream('deflate-raw')))
    expect(JSON.parse(await out.text())).toEqual(receipt)
  })
})
