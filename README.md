# touchwheel

Scroll full-screen terminal applications with a touchscreen in Windows Terminal.

Windows Terminal cannot do this on its own, and the limitation is structural rather than a
setting you have missed. `touchwheel` reads the touch digitizer directly and turns a pan
into wheel input aimed at the terminal under your finger.

Built for reading Claude Code transcripts on a secondary touch panel, but nothing in it is
specific to Claude Code - it works for any application that owns the alternate screen
buffer, `vim` and `htop` included.

## Why it is needed

Windows Terminal splits pointer input by device type. In `TermControl.cpp`, `Mouse` and
`Pen` go through `_TrySendMouseEvent` and are forwarded to the hosted application as VT
mouse sequences. `Touch` takes a different path entirely:

```cpp
void ControlInteractivity::TouchMoved(const Core::Point newTouchPoint)
{
    if (_focused && _touchAnchor) {
        ...
        UpdateScrollbar(newValue);   // -> _core->UserScrollViewport(viewTop)
        _touchAnchor = newTouchPoint;
    }
}
```

Compare `ControlInteractivity::MouseWheel` in the same file:

```cpp
if (!_core->IsInReadOnlyMode() &&
    (_canSendVTMouseInput(modifiers) || _shouldSendAlternateScroll(modifiers, delta)))
{
    return _sendMouseEventHelper(terminalPosition, WM_MOUSEWHEEL, ...);
}
```

Wheel input consults `_canSendVTMouseInput` and `_shouldSendAlternateScroll` and forwards
to the application. **Touch consults neither.** It scrolls Windows Terminal's own viewport
and stops there.

That is invisible for a normal shell prompt, which has thousands of scrollback lines to
move. An application owning the alternate screen buffer has none - the viewport *is* the
buffer - so the gesture fires and does nothing at all.

Synthetic wheel input does pass the gate. That is the whole trick.

## How it works

- A message-only window registers for Raw Input on HID usage page `0x0D` usage `0x04`
  (touch screen) with `RIDEV_INPUTSINK`, which keeps delivering reports even though
  Windows Terminal owns the touch.
- Each report yields contact count (`0x0D`/`0x54`, falling back to tip switch `0x42`) and
  X/Y (`0x01`/`0x30`, `0x31`), read through `HidP_GetUsageValue` against the device's
  preparsed data. Logical axis ranges come from `HidP_GetValueCaps`, so scaling is
  per-device rather than hardcoded.
- `GetPointerDevices` plus `GetPointerDeviceRects` give the screen rectangle the digitizer
  is bound to, so finger coordinates map to a screen point.
- The target is the terminal under that point via `WindowFromPoint` ->
  `GetAncestor(GA_ROOT)` -> window class check. Focus is never consulted and never
  changed, which matches how a real wheel scrolls whatever it hovers over.
- **A gesture belongs to the window it started on.** Ownership is decided once, at the first
  report with a resolved screen point: capture the window if it is a terminal, otherwise
  ignore the gesture until the finger lifts. Dragging out of a browser and across a terminal
  therefore does nothing to the terminal, and a captured terminal keeps receiving the pan
  after the finger leaves it.
- Travel is converted to wheel notches linearly, with the fractional remainder carried
  between reports so slow pans do not stall.

## Requirements

- Windows 10/11 with a touchscreen
- Windows Terminal
- `uv` (no third-party Python packages; everything is `ctypes` against Win32)

## Usage

```powershell
uv run --no-project python touchwheel.py --no-park
```

Leave it running in its own window. Pan on the touchscreen over any Windows Terminal
window.

Only one copy may run: a second would convert the same pan and deliver every notch twice.
The process takes a named mutex and a second launch exits immediately (`--allow-multiple`
overrides, if you have a reason).

## Running it in the background

Run `touchwheel-service.cmd` with no arguments, or double-click it, for a menu:

```
  Autostart : NOT installed
  Process   : not running

  [1]  Install autostart  (and start now)

  [2]  Start
  [3]  Stop
  [4]  Status

  [5]  Remove autostart   (and stop)
  [Q]  Quit
```

The same actions work as arguments, for scripting:

```
touchwheel-service.cmd install     start at every logon, and start now
touchwheel-service.cmd uninstall   stop it and remove autostart
touchwheel-service.cmd start       start it now
touchwheel-service.cmd stop        stop it now
touchwheel-service.cmd status      show autostart state and running processes
```

Launches `pythonw.exe` (resolved through `uv`), so there is no console window and no `uv`
wrapper process left in the tree.

**This is deliberately not a Windows service.** Services run in session 0, which cannot see
the desktop's windows and cannot inject input into them - `SendInput`, `PostMessage` and the
Raw Input sink all have to live in the interactive session. Autostart therefore goes through
the per-user `Run` key rather than a scheduled task, because `schtasks /SC ONLOGON` requires
elevation and this needs none.

### Delivery modes

| Mode | Behaviour |
| --- | --- |
| `--no-park` | Posts `WM_MOUSEWHEEL` straight to the terminal's XAML input site. The mouse cursor is never moved. Recommended. |
| default | Parks the cursor under your finger and synthesises a real wheel event with `SendInput`, restoring the cursor on lift. Use if posting turns out not to reach your terminal. |

### Options

| Flag | Default | Purpose |
| --- | --- | --- |
| `--pixels-per-notch` | 40 | Finger travel per wheel notch. Lower is faster. |
| `--slop-px` | 12 | Travel before a contact counts as a pan rather than a tap. |
| `--lift-timeout-ms` | 80 | Infer a lift when reports stop, for digitizers that send no contact-count-zero report. |
| `--natural` | off | Invert direction. |
| `--no-fling` | off | Stop dead on lift instead of coasting. |
| `--fling-friction` | 0.06 | Fraction of fling speed surviving each second. Lower stops sooner. |
| `--fling-min` | 4 | Notches/sec below which a fling will not start, and at which one stops. |
| `--velocity-smoothing` | 0.3 | EMA weight for the newest speed sample. |
| `--settle-ms` | 150 | Only used on the focus fallback path, when no screen mapping is available. |
| `--debug` | off | Print every HID report, the mapped screen point, and each wheel emission. |

## Tuning for Claude Code

Claude Code applies its own acceleration to wheel input, and on Windows it is on by
default:

```js
useDecayCurve: !o && (p || d === "win32" || s)   // d is the literal "win32"
...
let Re = Math.pow(0.5, se / uit);
w.mult = Math.min(Me, 1 + (w.mult - 1) * Re + u7t * Re);
```

`Re` approaches 1 as the gap between wheel events shrinks, so the multiplier compounds
during fast scrolling. Because `touchwheel` emits a dense stream of events, this is very
noticeable - but it affects a physical mouse identically.

Turn it off in `~/.claude/settings.json` and restart Claude Code:

```json
{
  "wheelScrollAccelerationEnabled": false
}
```

There is no `/config` entry for it; the settings file is the only way in.
`CLAUDE_CODE_SCROLL_SPEED` separately sets the base scroll rate (quantised to 0.25 below
1, capped at 10).

## Troubleshooting

Run with `--debug` and read the first lines.

| Symptom | Cause | Fix |
| --- | --- | --- |
| No report lines at all | Digitizer does not enumerate as usage `0x0D`/`0x04` | Widen the Raw Input registration |
| `y=None` | Y sits in a link collection rather than collection 0 | Needs per-contact parsing |
| `screen=None` | `GetPointerDevices` handle match failed | Falls back to focus targeting; `--settle-ms` applies |
| Wheel lines appear, nothing scrolls | Wrong delivery target | Drop `--no-park` to use the cursor-park path |
| Taps scroll | Slop too small, or lift not detected | Raise `--slop-px`; check for `lift inferred` lines |
| `lift inferred` fires mid-pan | Report rate slower than the timeout | Raise `--lift-timeout-ms` |

## The proper fix

This shim exists to work around a gap in Windows Terminal. The real fix is upstream and
small: route touch pan deltas through the same alternate-scroll translation that wheel
input already uses, so `TouchMoved` consults `_canSendVTMouseInput` and
`_shouldSendAlternateScroll` the way `MouseWheel` does. That would give touch scrolling in
full-screen applications with no external process at all.
