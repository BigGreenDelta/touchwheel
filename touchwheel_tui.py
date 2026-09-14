"""Textual front end for touchwheel: settings, autostart, and process control.

Settings are written to `touchwheel.json` beside the script. The background
copy is launched with no arguments and reads that file at startup, so editing a
value here changes what the next start uses without touching the autostart
entry.

Run it with:
    uv run --with textual python touchwheel_tui.py
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
from ctypes import wintypes

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Footer, Header, Input, Label, Static

import touchwheel

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "touchwheel"
SCRIPT = touchwheel.SETTINGS_PATH.with_name("touchwheel.py")

# dest name, label, kind, help
FIELDS: list[tuple[str, str, str, str]] = [
    (
        "no_park",
        "Post wheel directly (no cursor park)",
        "bool",
        "Post WM_MOUSEWHEEL to the terminal instead of moving the cursor.",
    ),
    (
        "pixels_per_notch",
        "Pixels per wheel notch",
        "float",
        "Finger travel per notch. Lower scrolls faster.",
    ),
    (
        "slop_px",
        "Tap slop (px)",
        "float",
        "Travel before a contact counts as a pan rather than a tap.",
    ),
    ("natural", "Invert direction", "bool", "Drag up scrolls content up."),
    ("no_fling", "Disable inertia", "bool", "Stop dead on lift instead of coasting."),
    (
        "fling_friction",
        "Fling friction",
        "float",
        "Fraction of fling speed surviving each second. Lower stops sooner.",
    ),
    (
        "fling_min",
        "Fling minimum (notches/sec)",
        "float",
        "Below this a fling will not start, and a running one stops.",
    ),
    (
        "velocity_smoothing",
        "Velocity smoothing",
        "float",
        "EMA weight for the newest speed sample, 0-1.",
    ),
    (
        "lift_timeout_ms",
        "Lift timeout (ms)",
        "float",
        "Treat the gesture as ended when reports stop for this long.",
    ),
    (
        "settle_ms",
        "Focus settle (ms)",
        "float",
        "Only used when the digitizer has no screen mapping.",
    ),
    (
        "debug",
        "Debug logging",
        "bool",
        "Print every HID report. Noisy; pointless for a windowless copy.",
    ),
]

DEFAULTS: dict[str, object] = {
    "no_park": True,
    "pixels_per_notch": 40.0,
    "slop_px": 12.0,
    "natural": False,
    "no_fling": False,
    "fling_friction": 0.06,
    "fling_min": 4.0,
    "velocity_smoothing": 0.3,
    "lift_timeout_ms": 80.0,
    "settle_ms": 150.0,
    "debug": False,
}


# --- Win32 / process helpers ----------------------------------------------

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.OpenMutexW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

SYNCHRONIZE = 0x00100000


def is_running() -> bool:
    """True when a copy holds the single-instance mutex."""
    handle = kernel32.OpenMutexW(SYNCHRONIZE, False, touchwheel.MUTEX_NAME)
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return False


_PYTHONW: str | None = None


def pythonw() -> str:
    """Path to a durable windowless interpreter.

    Not `sys.executable`: this TUI is normally launched through
    `uv run --with textual`, which builds a throwaway environment under the uv
    cache. Baking that path into autostart would point at a directory that
    disappears. Ask uv for the interpreter it would use with no extras instead,
    which is the same one the background copy runs under.
    """
    global _PYTHONW
    if _PYTHONW is not None:
        return _PYTHONW

    import pathlib

    candidate = pathlib.Path(getattr(sys, "_base_executable", sys.executable))
    import os

    # Drop the inherited virtualenv, or uv reuses the very throwaway
    # environment we are trying to resolve our way out of.
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("VIRTUAL_ENV", "PYTHONHOME", "UV_PROJECT_ENV")
    }
    try:
        result = subprocess.run(
            [
                "uv",
                "run",
                "--no-project",
                "python",
                "-c",
                "import sys; print(sys.executable)",
            ],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        resolved = pathlib.Path(result.stdout.strip())
        if resolved.exists():
            candidate = resolved
    except (OSError, subprocess.CalledProcessError):
        pass

    windowless = candidate.with_name("pythonw.exe")
    _PYTHONW = str(windowless if windowless.exists() else candidate)
    return _PYTHONW


def autostart_installed() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_NAME)
        return True
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    import winreg

    access = winreg.KEY_SET_VALUE
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, access) as k:
        if enabled:
            command = f'"{pythonw()}" "{SCRIPT}"'
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except FileNotFoundError:
                pass


def start_process() -> None:
    # DETACHED_PROCESS so the copy outlives this TUI.
    subprocess.Popen(
        [pythonw(), str(SCRIPT)],
        creationflags=0x00000008 | 0x00000200,
        close_fds=True,
    )


def stop_process() -> None:
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' OR "
            "Name='python.exe'\" | Where-Object { $_.CommandLine -like "
            "'*touchwheel.py*' } | ForEach-Object { Stop-Process -Id "
            "$_.ProcessId -Force }",
        ],
        capture_output=True,
        check=False,
    )


# --- App -------------------------------------------------------------------


class TouchwheelApp(App):
    CSS = """
    Screen { layout: vertical; }
    #state { padding: 1 2; background: $boost; }
    #actions { height: auto; padding: 0 2 1 2; }
    #actions Button { margin-right: 1; }
    .row { height: auto; padding: 0 2; }
    .row Label { width: 42; padding-top: 1; }
    .row Input { width: 18; }
    .hint { color: $text-muted; padding: 0 2 1 44; }
    #saved { padding: 0 2; color: $success; }
    """

    BINDINGS = [
        ("s", "save", "Save"),
        ("r", "restart", "Restart"),
        ("q", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="state")
        with Horizontal(id="actions"):
            yield Button("Start", id="start", variant="success")
            yield Button("Stop", id="stop", variant="error")
            yield Button("Install autostart", id="install")
            yield Button("Remove autostart", id="remove")
        with VerticalScroll():
            values = {**DEFAULTS, **touchwheel.load_settings()}
            for dest, label, kind, hint in FIELDS:
                with Vertical(classes="row"):
                    with Horizontal():
                        yield Label(label)
                        current = values.get(dest, DEFAULTS[dest])
                        if kind == "bool":
                            text = "yes" if current else "no"
                        else:
                            text = str(current)
                        yield Input(value=text, id=f"f_{dest}")
                    yield Static(hint, classes="hint")
        yield Static("", id="saved")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "touchwheel"
        self.refresh_state()
        self.set_interval(2.0, self.refresh_state)

    def refresh_state(self) -> None:
        running = "RUNNING" if is_running() else "not running"
        auto = "installed" if autostart_installed() else "not installed"
        self.query_one("#state", Static).update(
            f"Process: {running}        Autostart: {auto}\n"
            f"Settings: {touchwheel.SETTINGS_PATH}"
        )

    def collect(self) -> dict | None:
        values: dict[str, object] = {}
        for dest, label, kind, _hint in FIELDS:
            raw = self.query_one(f"#f_{dest}", Input).value.strip()
            if kind == "bool":
                if raw.lower() in ("yes", "true", "1", "on"):
                    values[dest] = True
                elif raw.lower() in ("no", "false", "0", "off"):
                    values[dest] = False
                else:
                    self.notify(f"{label}: expected yes or no", severity="error")
                    return None
            else:
                try:
                    values[dest] = float(raw)
                except ValueError:
                    self.notify(f"{label}: expected a number", severity="error")
                    return None
        return values

    def action_save(self) -> None:
        values = self.collect()
        if values is None:
            return
        touchwheel.save_settings(values)
        self.query_one("#saved", Static).update(
            "Saved. A running copy keeps its old settings until restarted."
        )

    def action_restart(self) -> None:
        values = self.collect()
        if values is not None:
            touchwheel.save_settings(values)
        stop_process()
        start_process()
        self.query_one("#saved", Static).update("Restarted with the current settings.")
        self.refresh_state()

    @on(Button.Pressed, "#start")
    def do_start(self) -> None:
        start_process()
        self.refresh_state()

    @on(Button.Pressed, "#stop")
    def do_stop(self) -> None:
        stop_process()
        self.refresh_state()

    @on(Button.Pressed, "#install")
    def do_install(self) -> None:
        set_autostart(True)
        if not is_running():
            start_process()
        self.refresh_state()

    @on(Button.Pressed, "#remove")
    def do_remove(self) -> None:
        set_autostart(False)
        stop_process()
        self.refresh_state()


def main() -> int:
    TouchwheelApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
