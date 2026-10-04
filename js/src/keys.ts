/**
 * Valyzen's arbiter signing keys, pinned by kid AND public key.
 *
 * A receipt carries the key that signed it, and anyone can embed a key under any
 * kid, including ours. A receipt is a Valyzen receipt only when the embedded key
 * for its kid is byte-for-byte one of these. Source: each gateway's
 * /.well-known/jwks.json; the same list is pinned on www.valyzen.ai/verify.
 */
export interface ArbiterKey {
  readonly x: string
  readonly label: string
  readonly mode: 'live' | 'test'
}

export const ARBITER_KEYS: ReadonlyMap<string, ArbiterKey> = new Map<string, ArbiterKey>([
  ['ed25519-28860223a6941457', { x: 'wd270_PgIwLChz_kk9_wHgG26cb70_muCjnbN8WrkeE', label: 'the Valyzen arbiter', mode: 'live' }],
  ['ed25519-0190292b036517a6', { x: '4xj3LRerKGkc3sRyYORRE3mJRiyNIEePDEp25XMfkjE', label: 'the Valyzen arbiter', mode: 'test' }],
  ['ed25519-2f3286feb69fb953', { x: 'Er_WT_nxhGzA8212gQp8dCBwvQRC4TmT1s8n185Znyg', label: 'the Valyzen development arbiter', mode: 'test' }],
])

export type Signer = { kind: 'arbiter'; label: string; mode: 'live' | 'test' } | { kind: 'unknown' }

type JwkLike = { kid?: unknown; kty?: unknown; crv?: unknown; x?: unknown }

export function signerOf(
  kid: string | null,
  jwks: { keys?: unknown } | null | undefined,
  trusted: ReadonlyMap<string, ArbiterKey> = ARBITER_KEYS,
): Signer {
  if (typeof kid !== 'string') return { kind: 'unknown' }
  const keys = Array.isArray(jwks?.keys) ? (jwks.keys as JwkLike[]) : []
  const embedded = keys.filter((k) => k && typeof k === 'object' && k.kid === kid)
  const pinned = trusted.get(kid) // a Map: no prototype keys
  const only = embedded.length === 1 ? embedded[0] : undefined
  if (pinned && only && only.kty === 'OKP' && only.crv === 'Ed25519' && only.x === pinned.x) {
    return { kind: 'arbiter', label: pinned.label, mode: pinned.mode }
  }
  return { kind: 'unknown' }
}
