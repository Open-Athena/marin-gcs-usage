// Root shell, with unfurl meta keyed on the hostname: cw-s3.oa.dev's `/` IS
// the CoreWeave view, and crawlers never run the React router, so the CW
// title/description/og:image have to be rewritten at the edge (this logic
// lived at functions/cw.ts when the CW view was a path on gcs.oa.dev).
interface Env {
  ASSETS: { fetch: (req: Request) => Promise<Response> }
}

const CW = {
  title: 'Marin CoreWeave usage',
  desc: 'Storage usage across the marin-us-east-02a CoreWeave S3 bucket.',
  image: 'https://cw-s3.oa.dev/og-cw.jpg',
  page: 'https://cw-s3.oa.dev/',
}

class MetaRewriter {
  element(el: { getAttribute: (n: string) => string | null; setAttribute: (n: string, v: string) => void }) {
    const key = el.getAttribute('property') ?? el.getAttribute('name')
    if (!key) return
    const value =
      key === 'og:title' || key === 'twitter:title' ? CW.title
      : key === 'og:description' || key === 'twitter:description' || key === 'description' ? CW.desc
      : key === 'og:image' || key === 'twitter:image' ? CW.image
      : key === 'og:url' ? CW.page
      : null
    if (value) el.setAttribute('content', value)
  }
}

class TitleRewriter {
  element(el: { setInnerContent: (v: string) => void }) {
    el.setInnerContent(CW.title)
  }
}

export const onRequest = async (ctx: { request: Request; env: Env }): Promise<Response> => {
  const shell = await ctx.env.ASSETS.fetch(
    // `/`, not `/index.html`: the asset server canonicalises the latter with a
    // 308 whose empty body would sail through the rewriter untouched.
    new Request(new URL('/', ctx.request.url).toString(), ctx.request),
  )
  const host = new URL(ctx.request.url).hostname
  if (!shell.ok || !/^cw[-.]/.test(host)) return shell
  return new HTMLRewriter()
    .on('meta', new MetaRewriter())
    .on('title', new TitleRewriter())
    .transform(shell)
}
