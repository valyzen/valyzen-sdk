/**
 * Offline receipt verification. Never contacts the gateway.
 *
 * `artifact` is a detached Ed25519 JWS (RFC 7797, b64 false) over the RFC 8785
 * canonical bytes of `signed_payload`; `jwks` carries the key by kid. Checks:
 * header · key · signature · signer (a pinned Valyzen key) · terms · chain
 * (with the session log). Act on `ok` and `signedTerms`, never on `status`
 * alone: a forger's own key produces a valid signature too.
 */
import { ARBITER_KEYS, signerOf, type ArbiterKey, type Signer } from './keys.js'

type Json = Record<string, unknown>

export interface Jwk {
  kty: string
  crv?: string
  x?: string
  kid?: string
  alg?: string
  use?: string
}

export interface Terms {
  price: { amount_minor: number; currency: string }
  inclusions?: { kind: string; value: string }[]
  expiry?: string
}

export interface Receipt extends Json {
  session_id: string
  state: string
  final_terms?: Terms | null
  arbiter?: { agent_id: string; relationship: string }
  chain?: { head: string; length: number; retention_days?: number }
  artifact?: { protected: string; signature: string } | null
  signed_payload?: { final_terms: Terms; session: Json } | null
  jwks?: { keys: Jwk[] }
  mode?: string
}

export interface Check {
  id: 'header' | 'key' | 'signature' | 'signer' | 'terms' | 'chain'
  ok: boolean
  detail: string
  skipped?: boolean
}

export interface VerifyResult {
  /** True only for a pinned Valyzen signature over terms that match (and, given a log, a chain that holds). */
  ok: boolean
  /** The signature alone: `valid` says nothing about who signed. */
  status: 'valid' | 'invalid' | 'unverifiable' | 'unsigned'
  kid: string | null
  signer: Signer
  /** The terms the arbiter signed: the only terms to act on. */
  signedTerms: Terms | null
  checks: Check[]
}

export interface VerifyOptions {
  /** The session log (envelopes) to check the hash chain. */
  log?: Json[]
  /** Keys to trust instead of Valyzen's pinned arbiter keys. */
  trustedKeys?: ReadonlyMap<string, ArbiterKey>
  subtle?: SubtleCrypto
}

const isRecord = (v: unknown): v is Json => typeof v === 'object' && v !== null && !Array.isArray(v)

/** RFC 8785 canonical JSON for the value space JSON.stringify covers. */
export function canonicalize(value: unknown): string {
  if (value === null || typeof value !== 'object') return JSON.stringify(value) ?? 'null'
  if (Array.isArray(value)) return `[${value.map((v) => canonicalize(v)).join(',')}]`
  const obj = value as Json
  const keys = Object.keys(obj)
    .filter((k) => obj[k] !== undefined)
    .sort()
  return `{${keys.map((k) => `${JSON.stringify(k)}:${canonicalize(obj[k])}`).join(',')}}`
}

function fromBase64Url(s: string): Uint8Array<ArrayBuffer> {
  if (!/^[A-Za-z0-9_-]*$/.test(s)) throw new Error('not base64url')
  const b64 = s.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (s.length % 4)) % 4)
  const bin = atob(b64)
  const out = new Uint8Array(new ArrayBuffer(bin.length))
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out
}

function toHex(bytes: ArrayBuffer): string {
  return Array.from(new Uint8Array(bytes), (b) => b.toString(16).padStart(2, '0')).join('')
}

/** `sha256:<hex>` over the canonical bytes: the form the chain uses. */
export async function commit(value: unknown, subtle: SubtleCrypto = globalThis.crypto.subtle): Promise<string> {
  return `sha256:${toHex(await subtle.digest('SHA-256', new TextEncoder().encode(canonicalize(value))))}`
}

function decodeHeader(protectedB64u: string): Json | null {
  try {
    const parsed = JSON.parse(new TextDecoder().decode(fromBase64Url(protectedB64u)))
    return isRecord(parsed) ? parsed : null
  } catch {
    return null
  }
}

export async function verifyReceipt(receipt: Receipt, options: VerifyOptions = {}): Promise<VerifyResult> {
  const subtle = options.subtle ?? globalThis.crypto?.subtle
  const checks: Check[] = []
  let status: VerifyResult['status'] = 'unsigned'
  let kid: string | null = null
  let sigOk = false
  let signer: Signer = { kind: 'unknown' }
  let signedTerms: Terms | null = null
  const artifact = receipt.artifact
  const payload = receipt.signed_payload

  if (!subtle) {
    checks.push({ id: 'signature', ok: false, detail: 'No Web Crypto in this runtime; nothing can be checked.' })
    return { ok: false, status: 'unverifiable', kid, signer, signedTerms, checks }
  }

  if (!isRecord(artifact) || !isRecord(payload)) {
    const agreed = receipt.state === 'agreed'
    const why = agreed
      ? 'the receipt says agreed but carries no signed artifact'
      : 'no agreement was signed; the session closed without one'
    checks.push({ id: 'header', ok: false, skipped: !agreed, detail: why })
    checks.push({ id: 'signature', ok: false, skipped: !agreed, detail: 'nothing to verify' })
    if (agreed) status = 'unverifiable'
  } else {
    const header = typeof artifact.protected === 'string' ? decodeHeader(artifact.protected) : null
    kid = header && typeof header.kid === 'string' ? header.kid : null
    const headerOk =
      header !== null &&
      header.alg === 'EdDSA' &&
      header.b64 === false &&
      Array.isArray(header.crit) &&
      header.crit.length === 1 &&
      header.crit[0] === 'b64' &&
      kid !== null
    checks.push({
      id: 'header',
      ok: headerOk,
      detail: headerOk ? `EdDSA, detached payload, key ${kid}.` : 'The protected header is not an EdDSA detached JWS with a kid.',
    })
    if (!headerOk) {
      status = 'unverifiable'
      checks.push({ id: 'signature', ok: false, detail: 'Cannot verify without a well-formed header.' })
    } else {
      const embedded = (Array.isArray(receipt.jwks?.keys) ? receipt.jwks.keys : []).filter((k) => isRecord(k) && k.kid === kid)
      const jwk = embedded.length === 1 ? embedded[0] : undefined
      const keyOk = Boolean(jwk && jwk.kty === 'OKP' && jwk.crv === 'Ed25519' && typeof jwk.x === 'string')
      checks.push({
        id: 'key',
        ok: keyOk,
        detail: keyOk ? `Ed25519 key ${kid} is embedded in the receipt.` : `No single Ed25519 key with kid ${kid} in the receipt's JWKS.`,
      })
      if (!keyOk || !jwk?.x) {
        status = 'unverifiable'
        checks.push({ id: 'signature', ok: false, detail: 'Cannot verify without the key.' })
      } else {
        try {
          const key = await subtle.importKey('raw', fromBase64Url(jwk.x), { name: 'Ed25519' }, false, ['verify'])
          const input = new TextEncoder().encode(`${artifact.protected}.${canonicalize(payload)}`)
          sigOk = await subtle.verify({ name: 'Ed25519' }, key, fromBase64Url(String(artifact.signature)), input)
        } catch {
          sigOk = false
        }
        status = sigOk ? 'valid' : 'invalid'
        checks.push({
          id: 'signature',
          ok: sigOk,
          detail: sigOk
            ? 'The signature verifies over the canonical signed payload.'
            : 'The signature does not verify: the signed payload, the signature, or the key is not what was signed.',
        })
        signer = signerOf(kid, receipt.jwks, options.trustedKeys ?? ARBITER_KEYS)
        checks.push({
          id: 'signer',
          ok: signer.kind === 'arbiter',
          detail:
            signer.kind === 'arbiter'
              ? `Signed by ${signer.label} (${signer.mode} key ${kid}).`
              : `Key ${kid} is not a Valyzen arbiter key: the signature proves only that whoever made this receipt signed it.`,
        })
      }
    }

    const session = isRecord(payload.session) ? payload.session : {}
    signedTerms = isRecord(payload.final_terms) ? (payload.final_terms as unknown as Terms) : null
    const termsOk = canonicalize(receipt.final_terms ?? null) === canonicalize(payload.final_terms ?? null)
    const idOk = session.session_id === receipt.session_id
    checks.push({
      id: 'terms',
      ok: termsOk && idOk,
      detail:
        termsOk && idOk
          ? 'final_terms and session_id match what was signed.'
          : !termsOk
            ? "The receipt's final_terms differ from the signed terms."
            : 'The signed session id differs from the receipt.',
    })
  }

  if (!options.log) {
    const chain = receipt.chain
    checks.push({
      id: 'chain',
      ok: false,
      skipped: true,
      detail: chain
        ? `Pass the session log to verify the chain; the receipt names head ${chain.head.slice(0, 23)}… over ${chain.length} envelopes.`
        : 'Pass the session log to verify the chain.',
    })
  } else {
    checks.push({ id: 'chain', ...(await verifyChain(options.log, receipt, subtle)) })
  }

  const ok = checks.every((c) => c.skipped || c.ok) && sigOk && signer.kind === 'arbiter'
  return { ok, status, kid, signer, signedTerms, checks }
}

async function verifyChain(log: Json[], r: Receipt, subtle: SubtleCrypto): Promise<{ ok: boolean; detail: string }> {
  if (!Array.isArray(log) || log.length === 0 || !log.every(isRecord)) {
    return { ok: false, detail: 'The log must be a non-empty array of envelopes.' }
  }
  for (let i = 1; i < log.length; i++) {
    if (log[i]!.prev_hash !== (await commit(log[i - 1], subtle))) {
      return { ok: false, detail: `Envelope ${i} (${String(log[i]!.type)}) does not link to the one before it.` }
    }
  }
  const head = await commit(log[log.length - 1], subtle)
  if (r.chain && r.chain.head !== head) {
    return { ok: false, detail: 'Every link holds, but the last envelope is not the head the receipt names.' }
  }
  if (r.chain && r.chain.length !== log.length) {
    return { ok: false, detail: `The receipt names ${r.chain.length} envelopes; the log holds ${log.length}.` }
  }
  const signedHead = r.signed_payload?.session?.chain_head
  if (typeof signedHead === 'string' && log.length >= 2 && signedHead !== (await commit(log[log.length - 2], subtle))) {
    return { ok: false, detail: 'The signed chain head is not the hash of the envelope before session.agree.' }
  }
  return { ok: true, detail: `${log.length} envelopes, each linked to its predecessor; the last is the head the receipt names.` }
}
