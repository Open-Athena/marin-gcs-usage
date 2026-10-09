import { useSessionLogUntil } from './sessionLogBoot'

/** The notice's date: the last day logging runs (`until` is the exclusive end), in UTC like the switch. */
export const noticeDay = (until: number): string =>
  new Date(until - 1).toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' })

export const sessionLogNotice = (until: number): string =>
  `While enabled (through ${noticeDay(until)}), this site records your clicks and page views to help debug issues.`

/** The privacy line, shown only while the session log is on for this page (specs/session-log.md). */
export function SessionLogNote() {
  const until = useSessionLogUntil()
  return until === null ? null : <p className="session-log-note">{sessionLogNotice(until)}</p>
}
