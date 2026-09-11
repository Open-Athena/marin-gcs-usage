import { useEffect } from 'react'
import { useLocation } from 'react-router-dom'

// The last hash the scroll-spy itself wrote: the deep-link effect must ignore
// it, or a router-driven location change would re-scroll to wherever the
// reader already is.
let spyHash = ''
// True while a `#hash` deep link is still scrolling into place; the
// scroll-spy holds off until then (at scrollY 0 it would clear the hash
// before the section exists).
let deepLinkPending = false
// Reader-initiated scrolling (not the programmatic kind) — ends a deep link's pursuit.
const USER_SCROLL_EVENTS = ['wheel', 'touchmove', 'keydown'] as const
/** The sticky bar's current height (px) — where anchored sections park. */
const topbarH = (): number =>
  parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--topbar-h')) || 48

/** A page's section `#hash`, both ways. A deep link (`…#runs`, or any element
 * id) scrolls its target under the sticky bar and keeps nudging for up to
 * ~20 s while the page's sections land (`deps` re-arm it as data arrives; a
 * reader's own scroll ends the pursuit). A scroll-spy writes the section in
 * view (`ids`, top to bottom) back to the hash with replaceState — no
 * history entries, no jumps — so a reload or a copied URL reopens where the
 * reader was. `legacy` maps retired anchor ids onto current ones. */
export function useSectionHash(ids: readonly string[], deps: readonly unknown[], legacy: Record<string, string> = {}): void {
  const { hash } = useLocation()
  useEffect(() => {
    if (!hash || hash === spyHash) return
    const raw = hash.slice(1)
    const id = legacy[raw] ?? raw
    deepLinkPending = true
    let last = NaN
    let tries = 0
    const stop = () => {
      clearInterval(iv)
      deepLinkPending = false
      for (const ev of USER_SCROLL_EVENTS) window.removeEventListener(ev, stop)
    }
    const iv = setInterval(() => {
      if (++tries > 40) { stop(); return }
      const el = document.getElementById(id)
      if (!el) return
      const top = el.getBoundingClientRect().top
      if (Math.abs(top - topbarH()) < 4 && top === last) { stop(); return } // parked
      last = top
      // Instant, not smooth: this is page-load positioning, not a navigation
      // the reader watches — and a smooth animation restarted every nudge
      // (or paused in a background tab) never gets there.
      el.scrollIntoView({ behavior: 'instant', block: 'start' })
    }, 500)
    for (const ev of USER_SCROLL_EVENTS) window.addEventListener(ev, stop, { passive: true })
    return stop
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hash, ...deps])
  useEffect(() => {
    let raf = 0
    const onScroll = () => {
      if (raf || deepLinkPending) return
      raf = requestAnimationFrame(() => {
        raf = 0
        // Reference line near the top (not ⅓ viewport): a short section
        // scrolled to the top should own the hash, not its taller successor.
        const yRef = Math.min(window.innerHeight / 3, 150)
        let cur = ''
        for (const id of ids) {
          const el = document.getElementById(id)
          if (el && el.getBoundingClientRect().top <= yRef) cur = `#${id}`
        }
        if (window.scrollY < 40) cur = '' // parked at the top — no anchor
        if (cur === window.location.hash) return
        spyHash = cur
        history.replaceState(history.state, '', window.location.pathname + window.location.search + cur)
      })
    }
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => { window.removeEventListener('scroll', onScroll); if (raf) cancelAnimationFrame(raf) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ids.join(',')])
}
