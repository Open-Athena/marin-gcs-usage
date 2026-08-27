// `/user/:id` unfurl: the user's display name in the title (name + handle is
// the public ceiling — never sizes or $), sharing the /users og image.
import { IDENTITIES } from '../../src/identities.gen.js'
import { unfurlShell } from '../_lib/unfurl.js'

interface Env {
  ASSETS: { fetch: (req: Request) => Promise<Response> }
}

export const onRequest = (ctx: { request: Request; env: Env }): Promise<Response> => {
  const url = new URL(ctx.request.url)
  const id = decodeURIComponent(url.pathname.split('/')[2] ?? '')
  const name = IDENTITIES[id]?.name ?? (id ? id.split('-')[0].replace(/^./, c => c.toUpperCase()) : 'User')
  return unfurlShell(ctx, {
    title: `${name} — Marin GCS usage`,
    desc: 'Per-user storage breakdown: what’s keep-marked, sweep-marked, and still undecided.',
    image: `${url.origin}/og-users.jpg`,
    page: `${url.origin}${url.pathname}`,
  })
}
