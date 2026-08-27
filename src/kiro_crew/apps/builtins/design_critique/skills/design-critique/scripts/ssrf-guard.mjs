/**
 * ssrf-guard.mjs — address-validate EVERY browser request, not just the entry URL.
 *
 * The backend validates the URL/repo the operator types, but Chromium then follows
 * redirects and loads subresources on its own: a public page can 3xx to
 * http://10.0.0.1/ or embed a private-network subresource (169.254.169.254 cloud
 * metadata, a LAN box) and the browser would fetch it server-side. This installs a
 * request interceptor on a Playwright context/page that resolves each request's
 * host and aborts anything on an internal/private address. Loopback stays allowed:
 * the localhost preview and the repo's own loopback static server are the feature.
 *
 * Mirrors the backend `_url_target_allowed` rule (allow loopback + public; reject
 * private / link-local incl. 169.254.169.254 / reserved / multicast / unspecified).
 */
import dns from 'node:dns/promises'
import net from 'node:net'

function ipv4Disallowed(ip) {
  const p = ip.split('.').map(Number)
  if (p.length !== 4 || p.some(n => Number.isNaN(n) || n < 0 || n > 255)) return true
  const [a, b] = p
  if (a === 127) return false            // loopback — allowed (localhost preview)
  if (a === 10) return true              // private
  if (a === 172 && b >= 16 && b <= 31) return true   // private
  if (a === 192 && b === 168) return true            // private
  if (a === 169 && b === 254) return true            // link-local incl. metadata
  if (a === 100 && b >= 64 && b <= 127) return true  // CGNAT
  if (a === 0) return true               // unspecified / this-network
  if (a >= 224) return true              // multicast + reserved
  return false                           // public
}

function ipv6Disallowed(ip) {
  const s = ip.toLowerCase().replace(/^\[|\]$/g, '')
  if (s === '::1') return false          // loopback — allowed
  if (s === '::') return true            // unspecified
  if (/^fe[89ab]/.test(s)) return true   // link-local fe80::/10
  if (/^f[cd]/.test(s)) return true      // unique-local fc00::/7
  if (/^ff/.test(s)) return true         // multicast
  const dotted = s.match(/::ffff:(\d+\.\d+\.\d+\.\d+)$/)  // IPv4-mapped, dotted
  if (dotted) return ipv4Disallowed(dotted[1])
  const hex = s.match(/::ffff:([0-9a-f]{1,4}):([0-9a-f]{1,4})$/)  // IPv4-mapped, hex
  if (hex) {
    const hi = parseInt(hex[1], 16), lo = parseInt(hex[2], 16)
    return ipv4Disallowed([(hi >> 8) & 255, hi & 255, (lo >> 8) & 255, lo & 255].join('.'))
  }
  return false                           // global v6 — allowed
}

function ipDisallowed(ip) {
  const kind = net.isIP(ip)
  if (kind === 4) return ipv4Disallowed(ip)
  if (kind === 6) return ipv6Disallowed(ip)
  return true                            // not an IP literal — refuse
}

function isLoopbackIp(ip) {
  const kind = net.isIP(ip)
  if (kind === 4) return ip.split('.')[0] === '127'
  if (kind === 6) {
    const s = ip.toLowerCase().replace(/^\[|\]$/g, '')
    if (s === '::1') return true
    const m = s.match(/::ffff:(\d+\.\d+\.\d+\.\d+)$/)
    return !!m && m[1].split('.')[0] === '127'
  }
  return false
}

// Three-way verdict: 'public' | 'loopback' | 'internal'. Resolves ALL addresses;
// a name that resolves to any disallowed non-loopback address is 'internal', and
// a name mixing loopback with a public address is also 'internal' (rebinding-safe).
async function hostVerdict(hostname) {
  let addrs
  try {
    addrs = await dns.lookup(hostname, { all: true })
  } catch {
    return 'internal'
  }
  if (!addrs.length) return 'internal'
  let anyLoop = false
  let anyPublic = false
  for (const a of addrs) {
    if (isLoopbackIp(a.address)) { anyLoop = true; continue }
    if (ipDisallowed(a.address)) return 'internal'
    anyPublic = true
  }
  if (anyLoop) return anyPublic ? 'internal' : 'loopback'
  return 'public'
}

// `target` is a Playwright BrowserContext or Page (both expose `.route`).
// `allowOrigin` (e.g. "http://127.0.0.1:5173") is the ONLY origin for which a
// loopback request is permitted — the local build server, or a localhost-preview
// base — so a public page cannot reach some OTHER service on 127.0.0.1. Every
// request/redirect/subresource is re-resolved here; a public host is allowed,
// an internal one is aborted.
export async function installSsrfGuard(target, allowOrigin = null) {
  await target.route('**/*', async (route) => {
    try {
      const u = new URL(route.request().url())
      if (u.protocol !== 'http:' && u.protocol !== 'https:') return route.abort('blockedbyclient')
      const verdict = await hostVerdict(u.hostname)
      if (verdict === 'public') return route.continue()
      if (verdict === 'loopback') {
        return (allowOrigin && u.origin === allowOrigin) ? route.continue() : route.abort('blockedbyclient')
      }
      return route.abort('blockedbyclient')
    } catch {
      return route.abort('blockedbyclient')
    }
  })
}

export const _test = { ipDisallowed, isLoopbackIp, hostVerdict }
