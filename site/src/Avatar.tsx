import { useState } from 'react'

// User avatars. GitHub is a free public photo URL (github.com/<handle>.png), and
// our canonical user ids are GitHub-username-shaped, so most resolve; a 404 (or a
// W&B-only id) falls back to a colored initial. Google (CF Access doesn't expose
// the pfp) and Discord (needs the archive→id map) would need explicit handles in
// identities.yaml — see the avatar-sources spec.

export const avatarHue = (s: string): number => {
  let h = 0
  for (const c of s) h = (h * 31 + c.codePointAt(0)!) % 360
  return h
}

/**
 * Email or display string → GitHub-username guess: the local part, sanitized
 * like rigging's `sanitize_username` (lowercase; runs of non-`[a-z0-9_-]` → '-').
 * So `ryan.williams@openathena.ai` → `ryan-williams` = the canonical id / GH handle.
 */
export const whoToHandle = (who: string): string =>
  who.split('@')[0].toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '')

export function Avatar({ handle, label, size = 20 }: { handle: string; label?: string; size?: number }) {
  const [failed, setFailed] = useState(false)
  const name = (label ?? handle).trim()
  const initial = name ? name[0].toUpperCase() : '?'
  if (failed || !handle) {
    return (
      <span
        className="user-avatar fallback"
        style={{ width: size, height: size, fontSize: Math.round(size * 0.48), background: `hsl(${avatarHue(name || handle)} 55% 42%)` }}
        aria-hidden
      >
        {initial}
      </span>
    )
  }
  return (
    <img
      className="user-avatar"
      src={`https://github.com/${handle}.png?size=${size * 2}`}
      width={size}
      height={size}
      alt={name}
      loading="lazy"
      onError={() => setFailed(true)}
    />
  )
}
