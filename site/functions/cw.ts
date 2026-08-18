// `/cw` serves the same SPA shell as `/`, with CoreWeave-specific unfurl meta.
//
// The shell can't just be a second static `index.html`: the built one carries
// hashed asset filenames that only the bundler knows. So fetch the real shell
// and rewrite its <title>/og:/twitter: tags at the edge, which is also what
// makes the card correct for crawlers — they never run the React router, so
// client-side `document.title` alone would leave every store sharing `/`'s card.
//
// Only `/cw` itself is handled; `/cw/og` (the screenshot route) falls through to
// the SPA fallback, as does every other client route.
interface Env {
  ASSETS: { fetch: (req: Request) => Promise<Response> }
}

const TITLE = 'Marin CoreWeave usage'
const DESC = 'Storage usage across the marin-us-east-02a CoreWeave S3 bucket.'
const IMAGE = 'https://gcs.oa.dev/og-cw.jpg'
const PAGE = 'https://gcs.oa.dev/cw'

class MetaRewriter {
  element(el: { getAttribute: (n: string) => string | null; setAttribute: (n: string, v: string) => void }) {
    const key = el.getAttribute('property') ?? el.getAttribute('name')
    if (!key) return
    const value =
      key === 'og:title' || key === 'twitter:title' ? TITLE
      : key === 'og:description' || key === 'twitter:description' || key === 'description' ? DESC
      : key === 'og:image' || key === 'twitter:image' ? IMAGE
      : key === 'og:url' ? PAGE
      : null
    if (value) el.setAttribute('content', value)
  }
}

class TitleRewriter {
  element(el: { setInnerContent: (v: string) => void }) {
    el.setInnerContent(TITLE)
  }
}

export const onRequest = async (ctx: { request: Request; env: Env }): Promise<Response> => {
  // Ask for `/`, not `/index.html`: the asset server canonicalises the latter
  // with a 308 whose empty body would sail through the rewriter untouched.
  const shell = await ctx.env.ASSETS.fetch(
    new Request(new URL('/', ctx.request.url).toString(), ctx.request),
  )
  if (!shell.ok) return shell
  return new HTMLRewriter()
    .on('meta', new MetaRewriter())
    .on('title', new TitleRewriter())
    .transform(shell)
}
