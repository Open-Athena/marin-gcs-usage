/**
 * Local-dev sign-in marker. The app session cookie (`oa_auth`) is HttpOnly, so
 * the SPA's localhost identity stub (`DEV_IDENTITY` in src/auth.ts) can't tell
 * a real sign-in happened and would keep stubbing — `?wall` would show the
 * wall again right after a successful Google / email-code sign-in. On
 * localhost only, mirror a session-minting response with a JS-visible
 * `oa_dev_session=1` cookie; the stub yields to the real whoami probe when it
 * sees it. Cloudflare routes by the real hostname, so this never fires in prod.
 */
export const DEV_SESSION_COOKIE = 'oa_dev_session'

const isLocalHost = (url: string): boolean => {
  const h = new URL(url).hostname
  return h === 'localhost' || h === '127.0.0.1'
}

/** If `res` sets the app session cookie and the request is to localhost, add
 *  the marker cookie alongside it. Otherwise `res` is returned untouched. */
export const markDevSession = (res: Response, request: Request): Response => {
  if (!isLocalHost(request.url)) return res
  const setsSession = res.headers.getSetCookie().some(c => c.startsWith('oa_auth='))
  if (!setsSession) return res
  const out = new Response(res.body, res)
  out.headers.append('set-cookie', `${DEV_SESSION_COOKIE}=1; Path=/; SameSite=Lax`)
  return out
}
