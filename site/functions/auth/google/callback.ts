/**
 * OIDC callback: verify state + nonce, trade the code for an id_token, verify
 * it against Google's JWKS, then `gate.signIn(email)` → app session cookie →
 * 302 back to `?next`. Authenticated-but-not-on-the-allowlist 302s to
 * `/?denied=<email>`. This is the whole ZT-free identity path; everything
 * downstream (gate, D1, sessions, share links) is unchanged. See
 * specs/oidc-cutover.md.
 */
import { oidcCallback } from '@open-athena/auth/oidc'
import { type Ctx } from '../../_lib/auth.js'
import { markDevSession } from '../../_lib/devsession.js'
import { oidcConfig } from '../../_lib/oidc.js'

export const onRequest = async (ctx: Ctx): Promise<Response> => {
  const cfg = oidcConfig(ctx.env, ctx.request)
  if (!cfg) return new Response('OIDC not configured\n', { status: 503 })
  return markDevSession(await oidcCallback(cfg)({ request: ctx.request }), ctx.request)
}
