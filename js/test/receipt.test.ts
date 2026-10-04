import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { ARBITER_KEYS, canonicalize, commit, verifyReceipt, type Receipt } from '../src/index'

const load = (name: string) => JSON.parse(readFileSync(new URL(`../../fixtures/${name}`, import.meta.url), 'utf8')) as Receipt
const check = (r: Awaited<ReturnType<typeof verifyReceipt>>, id: string) => r.checks.find((c) => c.id === id)!

async function chained(n: number) {
  const log: Record<string, unknown>[] = []
  for (let i = 0; i < n; i++) {
    const e: Record<string, unknown> = { type: `step.${i}`, message_id: String(i), payload: { i } }
    if (log.length) e.prev_hash = await commit(log[log.length - 1])
    log.push(e)
  }
  log[n - 1]!.type = 'session.agree'
  return log
}

async function withChain(r: Receipt, log: Record<string, unknown>[]): Promise<Receipt> {
  const out = structuredClone(r)
  out.chain = { head: await commit(log[log.length - 1]), length: log.length, retention_days: 90 }
  out.signed_payload!.session.chain_head = await commit(log[log.length - 2])
  return out
}

type Log = Record<string, unknown>[]

describe('verifyReceipt', () => {
  it('verifies a real dev receipt and names the dev arbiter', async () => {
    const r = await verifyReceipt(load('dev-receipt.json'), { allowTest: true })
    expect(r.ok).toBe(true)
    expect(r.status).toBe('valid')
    expect(r.signer).toEqual({ kind: 'arbiter', label: 'the Valyzen development arbiter', mode: 'test' })
    expect(r.signedTerms?.price.amount_minor).toBe(41900)
    expect(check(r, 'chain').skipped).toBe(true)
  })

  it('does not accept a test-mode receipt as proof unless asked to', async () => {
    const r = await verifyReceipt(load('dev-receipt.json'))
    expect(check(r, 'signature').ok).toBe(true)
    expect(check(r, 'mode').ok).toBe(false)
    expect(r.ok).toBe(false)
  })

  it('fails the chain when the receipt carries no signed chain head', async () => {
    const log = await chained(5)
    const receipt = await withChain(load('dev-receipt.json'), log)
    delete receipt.signed_payload!.session.chain_head
    const r = await verifyReceipt(receipt, { log, allowTest: true })
    expect(check(r, 'chain').ok).toBe(false)
    expect(check(r, 'chain').detail).toContain('no signed chain head')
  })

  it('verifies a session log that links end to end', async () => {
    const log = await chained(5)
    const r = await verifyReceipt(await withChain(load('dev-receipt.json'), log), { log })
    expect(check(r, 'chain').ok).toBe(true)
  })

  it('treats a closed session as unsigned, not forged', async () => {
    const receipt = { ...load('dev-receipt.json'), state: 'closed', artifact: null, signed_payload: null, final_terms: null }
    const r = await verifyReceipt(receipt)
    expect(r.status).toBe('unsigned')
    expect(r.ok).toBe(false)
  })

  // The 0.0 gateway SDK reported valid here: the signed payload is intact, the receipt's terms are not.
  it('fails tampered final_terms even though the signature is valid', async () => {
    const receipt = load('dev-receipt.json')
    receipt.final_terms!.price.amount_minor = 100
    const r = await verifyReceipt(receipt)
    expect(check(r, 'signature').ok).toBe(true)
    expect(check(r, 'terms').ok).toBe(false)
    expect(r.ok).toBe(false)
    expect(r.signedTerms?.price.amount_minor).toBe(41900)
  })

  it('breaks the signature when the signed payload is edited', async () => {
    const receipt = load('dev-receipt.json')
    receipt.signed_payload!.final_terms.price.amount_minor = 100
    receipt.final_terms!.price.amount_minor = 100
    expect((await verifyReceipt(receipt)).status).toBe('invalid')
  })

  it("refuses a receipt signed with someone else's key", async () => {
    const r = await verifyReceipt(load('forged-receipt.json'))
    expect(check(r, 'signature').ok).toBe(true)
    expect(r.signer.kind).toBe('unknown')
    expect(r.ok).toBe(false)
  })

  it('refuses our kid carrying a different key', async () => {
    const r = await verifyReceipt(load('forged-kid-receipt.json'))
    expect(ARBITER_KEYS.has(r.kid!)).toBe(true)
    expect(r.ok).toBe(false)
  })

  it('refuses two keys under one kid', async () => {
    const receipt = load('dev-receipt.json')
    receipt.jwks!.keys.push({ ...receipt.jwks!.keys[0]! })
    expect((await verifyReceipt(receipt)).ok).toBe(false)
  })

  it('rejects non-base64url signatures instead of decoding leniently', async () => {
    const receipt = load('dev-receipt.json')
    receipt.artifact!.signature = receipt.artifact!.signature.replace(/-/g, '+').replace(/_/g, '/') + '=='
    expect((await verifyReceipt(receipt)).status).toBe('invalid')
  })

  it.each<[string, (log: Log) => unknown, string]>([
    ['an edited envelope', (log) => (log[2] = { ...log[2], payload: { i: 99 } }), 'does not link'],
    ['a truncated log', (log) => log.pop(), 'head the receipt names'],
    ['a reordered log', (log) => log.reverse(), 'does not link'],
  ])('fails the chain for %s', async (_name, mutate, expected) => {
    const log = await chained(5)
    const receipt = await withChain(load('dev-receipt.json'), log)
    mutate(log)
    const r = await verifyReceipt(receipt, { log })
    expect(check(r, 'chain').ok).toBe(false)
    expect(check(r, 'chain').detail).toContain(expected)
    expect(r.ok).toBe(false)
  })

  it('canonicalises with sorted keys and no whitespace', () => {
    expect(canonicalize({ b: 1, a: [true, null, 'x'] })).toBe('{"a":[true,null,"x"],"b":1}')
  })

  it('agrees with the Python SDK on the shared fixtures', async () => {
    const results = await Promise.all(
      ['dev', 'forged', 'forged-kid'].map((f) => verifyReceipt(load(`${f}-receipt.json`), { allowTest: true })),
    )
    expect(results.map((r) => [r.ok, r.signer.kind])).toEqual([
      [true, 'arbiter'],
      [false, 'unknown'],
      [false, 'unknown'],
    ])
  })
})
