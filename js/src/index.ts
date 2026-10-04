/**
 * Buyer-side client for the Valyzen Negotiation Gateway.
 *
 * Zero dependencies: `fetch` for transport, Web Crypto for offline receipt
 * verification. Works in Node 20+, Deno, Bun, and browsers. Nothing here holds
 * a buyer's private limit; keep that in your own code.
 *
 *   const client = new GatewayClient()          // VALYZEN_API_KEY, VALYZEN_BASE_URL in Node
 *   const session = await client.openSession('DEMO-MONITOR-27', { agentId: 'my-buyer' })
 *   await session.offer(41500, { inclusions: session.tableInclusions })
 *   const receipt = await session.receipt()
 *   const result = await verifyReceipt(receipt)  // offline; pinned Valyzen keys
 *   if (result.ok) actOn(result.signedTerms)
 */

export { ARBITER_KEYS, signerOf, type ArbiterKey, type Signer } from './keys.js'
export { encodeReceipt, siteFor, verifyLink } from './links.js'
export { canonicalize, commit, verifyReceipt, type Check, type VerifyResult, type VerifyOptions } from './receipt.js'
export const VERSION = '0.1.0'

const DEFAULT_BASE_URL = 'https://api.valyzen.ai'

function env(name: string): string | undefined {
  const proc = (globalThis as { process?: { env?: Record<string, string | undefined> } }).process
  return proc?.env?.[name]
}

export type Json = Record<string, unknown>

export interface Money {
  amount_minor: number
  currency: string
}

export interface Terms {
  price: Money
  inclusions?: { kind: string; value: string }[]
  expiry?: string
}

export interface SessionView extends Json {
  session_id: string
  mode: 'test' | 'live'
  sku: string
  state: 'open' | 'accepted' | 'negotiating' | 'corrective' | 'agreed' | 'closed'
  round: number
  max_rounds: number
  your_turn: boolean
  corrective: boolean
  offer_on_table: { proposer: 'buyer' | 'merchant'; terms: Terms } | null
  last_verdict: Json | null
  close: { reason: string; detail?: string | null } | null
  events: Json[]
  receipt: string | null
  session_token?: string
  duplicate?: boolean
}

import type { Receipt, Jwk } from './receipt.js'
export type { Receipt, Jwk } from './receipt.js'

export class GatewayClientError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
    /** The rest of the error body, e.g. `retry_after`. */
    public readonly details: Json = {},
  ) {
    super(`${status} ${code}: ${message}`)
    this.name = 'GatewayClientError'
  }
}

export interface ClientOptions {
  baseUrl?: string
  fetch?: typeof fetch
  retries?: number
  sleep?: (ms: number) => Promise<void>
}

export interface OpenSessionOptions {
  agentId: string
  maxRounds?: number
  currency?: string
  inclusions?: string[]
  utilities?: Record<string, string>
  commitment?: string
  description?: string
}

const RETRIABLE = new Set([500, 502, 503, 504])
const defaultSleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms))

export class GatewayClient {
  private readonly base: string
  private readonly fetchImpl: typeof fetch
  private readonly retries: number
  private readonly sleep: (ms: number) => Promise<void>

  private readonly key: string

  /** `key` and `options.baseUrl` default to VALYZEN_API_KEY / VALYZEN_BASE_URL where `process.env` exists. */
  constructor(key?: string, options: ClientOptions = {}) {
    const resolved = key ?? env('VALYZEN_API_KEY')
    if (!resolved) {
      throw new GatewayClientError(0, 'no_key', 'pass a key or set VALYZEN_API_KEY (get one at www.valyzen.ai/keys)')
    }
    const base = (options.baseUrl ?? env('VALYZEN_BASE_URL') ?? DEFAULT_BASE_URL).replace(/\/+$/, '')
    const host = new URL(base).hostname
    if (!base.startsWith('https://') && !['localhost', '127.0.0.1', '[::1]'].includes(host)) {
      // A key over plain HTTP is a key anyone on the path can read.
      throw new GatewayClientError(0, 'insecure_base_url', 'baseUrl must be https://')
    }
    this.key = resolved
    this.base = base
    this.fetchImpl = options.fetch ?? globalThis.fetch.bind(globalThis)
    this.retries = options.retries ?? 2
    this.sleep = options.sleep ?? defaultSleep
  }

  health(): Promise<Json> {
    return this.request('GET', '/healthz', { auth: null })
  }

  jwks(): Promise<{ keys: Jwk[] }> {
    return this.request('GET', '/.well-known/jwks.json', { auth: null }) as Promise<{ keys: Jwk[] }>
  }

  me(): Promise<Json> {
    return this.request('GET', '/v1/me')
  }

  async catalogue(): Promise<Json[]> {
    const out = await this.request('GET', '/v1/catalogue')
    return (out.items as Json[]) ?? []
  }

  async policies(): Promise<Json[]> {
    const out = await this.request('GET', '/v1/policies')
    return (out.policies as Json[]) ?? []
  }

  putPolicy(policy: Json & { sku: string }): Promise<Json> {
    return this.request('PUT', `/v1/policies/${encodeURIComponent(policy.sku)}`, { body: policy })
  }

  async sessions(limit = 20): Promise<Json[]> {
    const out = await this.request('GET', `/v1/sessions?limit=${limit}`)
    return (out.sessions as Json[]) ?? []
  }

  async openSession(sku: string, options: OpenSessionOptions): Promise<Session> {
    const declared: Json = { max_rounds: options.maxRounds ?? 5 }
    if (options.currency) declared.currency = options.currency
    if (options.inclusions) declared.inclusions = options.inclusions
    if (options.utilities) declared.utilities = options.utilities
    const buyer: Json = { agent_id: options.agentId, declared }
    if (options.commitment) buyer.commitment = options.commitment
    const body: Json = { sku, buyer }
    if (options.description) body.description = options.description
    const view = (await this.request('POST', '/v1/sessions', { body })) as SessionView
    const token = String(view.session_token)
    delete view.session_token
    return new Session(this, view.session_id, token, view)
  }

  async session(sessionId: string, token?: string): Promise<Session> {
    const auth = token ?? this.key
    const view = (await this.request('GET', `/v1/sessions/${sessionId}`, { auth })) as SessionView
    return new Session(this, sessionId, auth, view)
  }

  /** @internal */
  async request(
    method: string,
    path: string,
    options: { auth?: string | null; body?: unknown } = {},
  ): Promise<Json> {
    const headers: Record<string, string> = { accept: 'application/json' }
    const bearer = options.auth === undefined ? this.key : options.auth
    if (bearer) headers.authorization = `Bearer ${bearer}`
    if (options.body !== undefined) headers['content-type'] = 'application/json'
    let attempt = 0
    for (;;) {
      let response: Response
      const init: RequestInit = { method, headers }
      if (options.body !== undefined) init.body = JSON.stringify(options.body)
      try {
        response = await this.fetchImpl(this.base + path, init)
      } catch (error) {
        if (attempt >= this.retries) throw new GatewayClientError(0, 'transport', String(error))
        attempt += 1
        await this.sleep(250 * attempt)
        continue
      }
      if (RETRIABLE.has(response.status) && attempt < this.retries) {
        attempt += 1
        await this.sleep(250 * attempt)
        continue
      }
      return parse(response)
    }
  }
}

async function parse(response: Response): Promise<Json> {
  let body: unknown = null
  const text = await response.text()
  try {
    body = text ? JSON.parse(text) : null
  } catch {
    body = null
  }
  if (response.ok) {
    if (body === null || typeof body !== 'object' || Array.isArray(body)) {
      throw new GatewayClientError(response.status, 'bad_response', 'expected a JSON object')
    }
    return body as Json
  }
  const error = body && typeof body === 'object' ? (body as Json).error : undefined
  if (error && typeof error === 'object') {
    const { code, message, ...details } = error as Json
    throw new GatewayClientError(response.status, String(code ?? 'error'), String(message ?? response.statusText), details)
  }
  throw new GatewayClientError(response.status, 'http_error', text.slice(0, 200) || 'request failed')
}

/**
 * A buyer's handle on one session: moves, state, and the receipt.
 *
 * It remembers what the arbiter published along the way (the attested target
 * cost, the fair band, and the inclusion value W it scored per party's terms),
 * which is what {@link Session.fairEdgeMinor} is computed from.
 */
export class Session {
  private tTarget: number | null = null
  private band: string | null = null
  private readonly w = new Map<string, number>()
  private lastOffer: string | null = null

  constructor(
    private readonly client: GatewayClient,
    public readonly sessionId: string,
    public readonly token: string,
    public view: SessionView,
  ) {
    this.learn(view)
  }

  /** The inclusion kinds in the terms on the table. */
  get tableInclusions(): string[] {
    const items = this.view.offer_on_table?.terms.inclusions ?? []
    return items.map((i) => i.kind)
  }

  /**
   * The buyer's edge of the fair band, as a price for the terms on the table:
   * ceil(T_target / (1 + band)) + W. The cheapest price the arbiter still scores
   * as fair to the merchant. Public inputs only.
   */
  get fairEdgeMinor(): number | null {
    if (this.tTarget === null || this.band === null) return null
    const edge = ceilDiv(this.tTarget, this.band)
    const kinds = keyOf(this.tableInclusions)
    const w = this.w.get(`buyer|${kinds}`) ?? this.w.get(`merchant|${kinds}`) ?? 0
    return edge + w
  }

  private learn(view: Json): void {
    for (const key of ['log', 'events']) {
      const entries = view[key]
      if (!Array.isArray(entries)) continue
      for (const entry of entries) {
        try {
          this.learnOne(entry as Json)
        } catch {
          // A malformed event teaches nothing.
        }
      }
    }
  }

  private learnOne(entry: Json): void {
    const payload = (entry.payload ?? {}) as Json
    if (entry.type === 'session.accept') {
      const rules = payload.session_rules as Json
      const attested = payload.attested as Json
      const band = String(rules.fair_band)
      if (!/^\d+(\.\d+)?$/.test(band)) throw new Error('fair_band is not a decimal')
      this.band = band
      this.tTarget = Number((attested.t_target as Json).amount_minor)
    } else if (entry.type === 'offer.propose' || entry.type === 'offer.counter') {
      const terms = payload.terms as Json
      const items = (terms.inclusions as { kind: string }[] | undefined) ?? []
      this.lastOffer = `${String((entry.sender as Json).role)}|${keyOf(items.map((i) => i.kind))}`
    } else if (entry.type === 'arbitration.evaluate' && this.lastOffer !== null) {
      this.w.set(this.lastOffer, Number(payload.w_minor))
    }
  }

  get state(): string {
    return this.view.state
  }

  get finished(): boolean {
    return this.view.state === 'agreed' || this.view.state === 'closed'
  }

  get yourTurn(): boolean {
    return Boolean(this.view.your_turn)
  }

  get corrective(): boolean {
    return Boolean(this.view.corrective)
  }

  /** The merchant's standing price when the offer on the table is theirs. */
  get merchantOfferMinor(): number | null {
    const on = this.view.offer_on_table
    if (!on || on.proposer !== 'merchant') return null
    return on.terms.price.amount_minor
  }

  get suggestedPriceMinor(): number | null {
    return suggestedPrice(this.view)
  }

  offer(priceMinor: number, options: { inclusions?: string[]; justification?: string; messageId?: string } = {}) {
    const body: Json = { type: 'offer', price_minor: Math.trunc(priceMinor) }
    if (options.inclusions) body.inclusions = options.inclusions
    if (options.justification) body.justification = options.justification
    return this.send(body, options.messageId)
  }

  accept(options: { messageId?: string } = {}) {
    return this.send({ type: 'accept' }, options.messageId)
  }

  reject(reason?: string, options: { messageId?: string } = {}) {
    const body: Json = { type: 'reject' }
    if (reason) body.reason = reason
    return this.send(body, options.messageId)
  }

  reveal(privateSet: Json) {
    return this.send({ type: 'reveal', private: privateSet })
  }

  async refresh(): Promise<SessionView> {
    this.view = (await this.client.request('GET', `/v1/sessions/${this.sessionId}`, { auth: this.token })) as SessionView
    this.learn(this.view)
    return this.view
  }

  receipt(): Promise<Receipt> {
    return this.client.request('GET', `/v1/sessions/${this.sessionId}/receipt`, { auth: this.token }) as Promise<Receipt>
  }

  private async send(body: Json, messageId?: string): Promise<SessionView> {
    if (messageId) body.message_id = messageId
    this.view = (await this.client.request('POST', `/v1/sessions/${this.sessionId}/messages`, {
      auth: this.token,
      body,
    })) as SessionView
    this.learn(this.view)
    return this.view
  }
}

/** Exact ceil(t / (1 + band)) for a decimal-string band, in integers. */
function ceilDiv(t: number, band: string): number {
  const [whole = '0', frac = ''] = band.split('.')
  const scale = 10n ** BigInt(frac.length)
  const denom = scale + BigInt(whole) * scale + BigInt(frac || '0')
  const num = BigInt(t) * scale
  return Number((num + denom - 1n) / denom)
}

function keyOf(kinds: string[]): string {
  return [...new Set(kinds)].sort().join(',')
}

/** The arbiter's most recent suggested price in a view's events, if any. */
export function suggestedPrice(view: Json): number | null {
  const events = view.events
  if (!Array.isArray(events)) return null
  for (let i = events.length - 1; i >= 0; i--) {
    const event = events[i]
    if (!event || typeof event !== 'object') continue
    const e = event as Json
    if (e.type !== 'arbitration.reject' && e.type !== 'arbitration.evaluate') continue
    const payload = (e.payload ?? {}) as Json
    const suggestion = payload.suggested_adjustment as Json | undefined
    if (suggestion && typeof suggestion.amount_minor === 'number') return suggestion.amount_minor
  }
  return null
}

