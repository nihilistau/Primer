/* KairosNote — "Kairos asked: <text>" while Primer carries out a command from Kairos. The
 * Desktop renders it above every window (a lens opens its own window on top of the Reader), as a
 * small toast centred near the top, clear of the dock. The live region is always there, so a
 * screen reader hears each new note; only its text changes. The Desktop clears `note` after
 * NOTE_MS (ui/src/remote.js); the inner span fades out in its last second.
 * note: {text, key} (remote.js kairosNote) or null. */
export default function KairosNote({ note }) {
  return (
    <div className="kairos-live" role="status" aria-live="polite">
      {note && note.text ? <span key={note.key} className="kairos-note">Kairos asked: {note.text}</span> : null}
    </div>
  )
}
