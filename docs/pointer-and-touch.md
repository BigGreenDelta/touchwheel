# How Windows moves the pointer during a touch

Measured on this machine, 2026-09-15, against a SiS digitizer (`VID_0457&PID_0819`) and
a Logi Bolt receiver (`VID_046D&PID_C548`). Everything here was observed, not inferred
from documentation. Several of these findings contradict what the code's own comments
assumed at the time, so they are worth keeping.

## The pointer teleport

When a finger lands, Windows may promote the touch into a mouse move and drag the
pointer to the contact. Whether it does depends entirely on the window underneath:

| Window | Consumes touch? | Pointer teleports? |
| --- | --- | --- |
| Windows Terminal (`CASCADIA_HOSTING_WINDOW_CLASS`) | yes | **no** |
| Chrome (`Chrome_WidgetWin_1`) | yes, its own way | **yes** |

This asymmetry is the single most confusing thing about the behaviour. Panning over the
terminal leaves the pointer alone with no help from us at all, so the terminal path can
look "fixed" while an identical code path misbehaves everywhere else. Any test of
pointer handling that only pans over a terminal proves nothing.

## The promotion is invisible to Raw Input

**There is no mouse packet to intercept.** Instrumenting `handle_input` to count every
`RIM_TYPEMOUSE` packet arriving during a Chrome pan gives:

```
promoted=0 held=0 skipped_inactive=0
```

Zero. Not a null-handle packet, not a packet flagged `MOUSE_MOVE_ABSOLUTE`, nothing. The
pointer simply is somewhere else the next time anyone asks. So the teleport cannot be
cancelled by reacting to input; it can only be noticed by polling `GetCursorPos`, which
is what `Gesture.hold_cursor` does after every HID report and on every tick.

A separate probe *did* see one null-handle `RIM_TYPEMOUSE` packet in an earlier session,
so null-handle mouse input does exist on this machine. It is just not how the touch
promotion arrives.

## How often the pointer has to be put back is app-dependent

| App | Corrections per pan |
| --- | --- |
| Chrome | 1 |
| Telegram | one on nearly every HID report, ~100 per pan |

Chrome teleports the pointer once, at touch-down, and leaves it there. Telegram re-drags
it continuously, so the correction has to be re-applied for the whole gesture. Do not
assume one is enough; it was true of the first app measured and not of the second.

This is why `hold_cursor` is written to be idempotent and cheap: it compares against the
target first and returns early when the pointer is already there. A one-shot design
would have worked on Chrome and silently failed on Telegram.

With a hand on the mouse mid-pan, the target follows the hand rather than staying fixed,
which is visible in the log as the held coordinates drifting between reports:

```
  cursor held at (2894, 1138)
  cursor held at (2954, 1137)
  cursor held at (2966, 1136)
```

That is the intended precedence: the finger never gets the pointer, the hand always does.

## Telling a hand on the mouse from a desk that shakes

Raw Input reports the physical mouse honestly, and its device handle is non-null, so
`note_real_move` counts it. Two populations, same single pan:

| | packets | total travel |
| --- | --- | --- |
| Hand deliberately moving the mouse | 210 | 842 counts |
| Hand nowhere near the mouse, finger pressing the screen | 38 | ~22 counts, nearly all +-1 |

Pressing and dragging on a touch panel shakes the desk, and a high-DPI optical sensor
reports that as a trickle of tiny packets. It is real device movement, not noise on the
wire, so it cannot be filtered by device identity.

This *looks* like it should break the "was the mouse moved by hand" guard, which counts
packets rather than distance. It does not, in practice: the jitter population only
appears when the mouse is on the same surface being tapped, and the guard erring toward
"leave the pointer alone" is the safe direction. Changing it to a distance threshold was
tried and reverted as an unnecessary complication. If it ever does misfire, the numbers
above are the place to start.

## Reading `--debug` output

Each gesture ends with one verdict line:

| Line | Meaning |
| --- | --- |
| `cursor held at (x, y)` | The teleport was caught and undone mid-gesture. |
| `cursor back to (x, y)` | Post-lift repair fired; the pointer had moved and was near the contact. |
| `cursor left at (x, y): pointer never moved` | Nothing to repair. Normal after a `cursor held` line. |
| `cursor left at (x, y): pointer far from touch` | The pointer is not where Windows would have put it, so it was left alone. |
| `cursor left alone: mouse was moved by hand` | Raw Input saw the physical mouse move during the gesture. The hand wins. |

And one line that invalidates the whole run if you did not expect it:

```
  gesture ignored: window not targeted
```

The pan was over a window that is not in `DEFAULT_TARGET_CLASSES`, so nothing was
scrolled. **Check for this line first.** A run full of `contacts=` lines and no `wheel`
lines is a run that measured nothing. Chrome is in `DEFAULT_EXCLUDE_CLASSES` by design,
because it handles touch itself and synthetic wheel on top would scroll it twice.

To find out what is actually under a touch point:

```powershell
uv run --no-project python -c @'
import ctypes
from ctypes import wintypes
u = ctypes.WinDLL("user32", use_last_error=True)
pt = wintypes.POINT(3028, 2321)
root = u.GetAncestor(u.WindowFromPoint(pt), 2)
buf = ctypes.create_unicode_buffer(256)
u.GetClassNameW(root, buf, 256)
print(buf.value)
'@
```

## Things that look like bugs and are not

- **`cursor left alone` on every pan.** Correct if a hand was on the mouse. Verify by
  keeping both hands off it before suspecting the code.
- **The pointer not returning over Chrome while the mouse is being moved.** The hand owns
  the pointer; that is the intended precedence.
- **A stale copy still running.** `touchwheel` holds a single-instance mutex, and a
  backgrounded debug run keeps holding it. "touchwheel is already running" usually means
  a previous test never exited, not that autostart is up.
