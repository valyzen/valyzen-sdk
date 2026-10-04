/**
 * Prefilled verify links: `<site>/verify#r=v1.<base64url(raw DEFLATE(JSON))>`.
 * The receipt rides in the URL fragment, which browsers never send to a server.
 */
export const LINK_VERSION = 'v1.'

const SITES: Record<string, string> = {
  'api.valyzen.ai': 'https://www.valyzen.ai',
  'api-dev.valyzen.ai': 'https://dev.valyzen.ai',
}

function toBase64Url(bytes: Uint8Array): string {
  let s = ''
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]!)
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

export async function encodeReceipt(receipt: unknown): Promise<string> {
  const json = new TextEncoder().encode(JSON.stringify(receipt))
  const stream = new Blob([json]).stream().pipeThrough(new CompressionStream('deflate-raw'))
  const packed = new Uint8Array(await new Response(stream).arrayBuffer())
  return LINK_VERSION + toBase64Url(packed)
}

/** The website that verifies receipts from this gateway. */
export function siteFor(baseUrl: string): string {
  try {
    return SITES[new URL(baseUrl).hostname] ?? 'https://www.valyzen.ai'
  } catch {
    return 'https://www.valyzen.ai'
  }
}

export async function verifyLink(receipt: unknown, baseUrl = 'https://api.valyzen.ai'): Promise<string> {
  return `${siteFor(baseUrl)}/verify#r=${await encodeReceipt(receipt)}`
}
