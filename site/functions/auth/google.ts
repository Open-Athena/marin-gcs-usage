/**
 * OIDC sign-in start: 302 to Google's consent screen (our own OAuth client).
 * The callback (`google/callback.ts`) mints the app session, so the gate, D1
 * allowlist, scopes, and share links all apply as for any other sign-in.
 * Returns to `?next` afterwards. Dormant until the Google client secrets are
 * set — see specs/done/oidc-cutover.md.
 */
import { oidcStart } from '@open-athena/auth/oidc'
import { type Ctx } from '../_lib/auth.js'
import { oidcConfig } from '../_lib/oidc.js'

export const onRequest = async (ctx: Ctx): Promise<Response> => {
  const cfg = oidcConfig(ctx.env, ctx.request)
  if (!cfg) {
    return new Response('OIDC not configured (DB / SESSION_SECRET / GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET)\n', { status: 503 })
  }
  return oidcStart(cfg)({ request: ctx.request })
}
