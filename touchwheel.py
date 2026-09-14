"""Touch-to-wheel shim for Windows Terminal.

Windows Terminal never forwards touch input to the hosted application. In
``ControlInteractivity::TouchMoved`` a touch pan calls ``UpdateScrollbar`` ->
``_core->UserScrollViewport`` unconditionally, without consulting
``_canSendVTMouseInput`` or ``_shouldSendAlternateScroll`` the way
``ControlInteractivity::MouseWheel`` does. An application owning the alternate
screen buffer therefore has no scrollback for the local scroll to move, and the
gesture is a no-op.

Synthetic wheel input does pass that gate. This shim listens to the touch
digitizer directly through Raw Input with ``RIDEV_INPUTSINK`` -- which still
delivers HID reports while Windows Terminal owns the touch -- and converts a
pan into wheel events aimed at the terminal window.

Delivery has two modes. By default the cursor is parked over the terminal and
``SendInput`` synthesises a real wheel event; with ``--no-park`` the wheel is
posted straight to the terminal's XAML input site and the cursor is never
touched. Either way a tap emits nothing, so tapping other windows to change
focus keeps working.

Usage:
    uv run --no-project python touchwheel.py --no-park
    uv run --no-project python touchwheel.py --debug
"""

from __future__ import annotations

import argparse
import ctypes
import json
import pathlib
import sys
import time
from ctypes import wintypes

SETTINGS_PATH = pathlib.Path(__file__).resolve().with_name("touchwheel.json")

user32 = ctypes.WinDLL("user32", use_last_error=True)
hid = ctypes.WinDLL("hid", use_last_error=True)

# --- Win32 constants -------------------------------------------------------

RIDEV_INPUTSINK = 0x00000100
RID_INPUT = 0x10000003
RIDI_PREPARSEDDATA = 0x20000005
RIDI_DEVICENAME = 0x20000007

RIM_TYPEMOUSE = 0
RIM_TYPEHID = 2
WM_INPUT = 0x00FF
WM_QUIT = 0x0012
WM_MOUSEWHEEL = 0x020A
PM_REMOVE = 0x0001
QS_ALLINPUT = 0x04FF
INFINITE = 0xFFFFFFFF

HID_USAGE_PAGE_GENERIC = 0x01
HID_USAGE_PAGE_DIGITIZER = 0x0D
HID_USAGE_GENERIC_MOUSE = 0x02
HID_USAGE_GENERIC_X = 0x30
HID_USAGE_GENERIC_Y = 0x31
HID_USAGE_DIGITIZER_TOUCH_SCREEN = 0x04
HID_USAGE_DIGITIZER_TIP_SWITCH = 0x42
HID_USAGE_DIGITIZER_CONTACT_COUNT = 0x54

HIDP_STATUS_SUCCESS = 0x00110000

INPUT_MOUSE = 0
MOUSEEVENTF_WHEEL = 0x0800
WHEEL_DELTA = 120

HWND_MESSAGE = wintypes.HWND(-3)

# Window classes acted on by default: terminals and other classic Win32 hosts
# that ignore touch entirely, so nothing else is already scrolling them.
DEFAULT_TARGET_CLASSES = (
    "CASCADIA_HOSTING_WINDOW_CLASS",  # Windows Terminal
    "ConsoleWindowClass",  # conhost, cmd, PowerShell
    "VirtualConsoleClass",  # ConEmu
    "mintty",  # Git Bash
    "PuTTY",
    "org.wezfurlong.wezterm",
    "Alacritty",
)

# Consulted only in all-windows mode. These handle touch themselves, so adding
# synthetic wheel on top scrolls them twice.
DEFAULT_EXCLUDE_CLASSES = (
    "Chrome_WidgetWin_1",  # Chrome, Edge, Electron
    "Chrome_WidgetWin_0",
    "MozillaWindowClass",
    "ApplicationFrameWindow",  # UWP hosts
    "Windows.UI.Core.CoreWindow",
    "CabinetWClass",  # Explorer
    "ExploreWClass",
    "Progman",  # desktop
    "WorkerW",
    "Shell_TrayWnd",  # taskbar
    "XLMAIN",  # Excel
    "OpusApp",  # Word
)


# --- Structures ------------------------------------------------------------

ULONG_PTR = (
    ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong
)


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", wintypes.USHORT),
        ("usUsage", wintypes.USHORT),
        ("dwFlags", wintypes.DWORD),
        ("hwndTarget", wintypes.HWND),
    ]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType", wintypes.DWORD),
        ("dwSize", wintypes.DWORD),
        ("hDevice", wintypes.HANDLE),
        ("wParam", wintypes.WPARAM),
    ]


class RAWHID(ctypes.Structure):
    _fields_ = [
        ("dwSizeHid", wintypes.DWORD),
        ("dwCount", wintypes.DWORD),
        ("bRawData", ctypes.c_ubyte * 1),
    ]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [
        ("Usage", wintypes.USHORT),
        ("UsagePage", wintypes.USHORT),
        ("InputReportByteLength", wintypes.USHORT),
        ("OutputReportByteLength", wintypes.USHORT),
        ("FeatureReportByteLength", wintypes.USHORT),
        ("Reserved", wintypes.USHORT * 17),
        ("NumberLinkCollectionNodes", wintypes.USHORT),
        ("NumberInputButtonCaps", wintypes.USHORT),
        ("NumberInputValueCaps", wintypes.USHORT),
        ("NumberInputDataIndices", wintypes.USHORT),
        ("NumberOutputButtonCaps", wintypes.USHORT),
        ("NumberOutputValueCaps", wintypes.USHORT),
        ("NumberOutputDataIndices", wintypes.USHORT),
        ("NumberFeatureButtonCaps", wintypes.USHORT),
        ("NumberFeatureValueCaps", wintypes.USHORT),
        ("NumberFeatureDataIndices", wintypes.USHORT),
    ]


class _VALUE_RANGE(ctypes.Structure):
    _fields_ = [
        ("UsageMin", wintypes.USHORT),
        ("UsageMax", wintypes.USHORT),
        ("StringMin", wintypes.USHORT),
        ("StringMax", wintypes.USHORT),
        ("DesignatorMin", wintypes.USHORT),
        ("DesignatorMax", wintypes.USHORT),
        ("DataIndexMin", wintypes.USHORT),
        ("DataIndexMax", wintypes.USHORT),
    ]


class _VALUE_NOTRANGE(ctypes.Structure):
    _fields_ = [
        ("Usage", wintypes.USHORT),
        ("Reserved1", wintypes.USHORT),
        ("StringIndex", wintypes.USHORT),
        ("Reserved2", wintypes.USHORT),
        ("DesignatorIndex", wintypes.USHORT),
        ("Reserved3", wintypes.USHORT),
        ("DataIndex", wintypes.USHORT),
        ("Reserved4", wintypes.USHORT),
    ]


class _VALUE_UNION(ctypes.Union):
    _fields_ = [("Range", _VALUE_RANGE), ("NotRange", _VALUE_NOTRANGE)]


class HIDP_VALUE_CAPS(ctypes.Structure):
    _fields_ = [
        ("UsagePage", wintypes.USHORT),
        ("ReportID", ctypes.c_ubyte),
        ("IsAlias", ctypes.c_ubyte),
        ("BitField", wintypes.USHORT),
        ("LinkCollection", wintypes.USHORT),
        ("LinkUsage", wintypes.USHORT),
        ("LinkUsagePage", wintypes.USHORT),
        ("IsRange", ctypes.c_ubyte),
        ("IsStringRange", ctypes.c_ubyte),
        ("IsDesignatorRange", ctypes.c_ubyte),
        ("IsAbsolute", ctypes.c_ubyte),
        ("HasNull", ctypes.c_ubyte),
        ("Reserved", ctypes.c_ubyte),
        ("BitSize", wintypes.USHORT),
        ("ReportCount", wintypes.USHORT),
        ("Reserved2", wintypes.USHORT * 5),
        ("UnitsExp", wintypes.ULONG),
        ("Units", wintypes.ULONG),
        ("LogicalMin", wintypes.LONG),
        ("LogicalMax", wintypes.LONG),
        ("PhysicalMin", wintypes.LONG),
        ("PhysicalMax", wintypes.LONG),
        ("u", _VALUE_UNION),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]


WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_longlong, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


# --- Prototypes ------------------------------------------------------------

user32.GetRawInputData.argtypes = [
    wintypes.HANDLE,
    wintypes.UINT,
    wintypes.LPVOID,
    ctypes.POINTER(wintypes.UINT),
    wintypes.UINT,
]
user32.GetRawInputData.restype = wintypes.UINT

user32.GetRawInputDeviceInfoW.argtypes = [
    wintypes.HANDLE,
    wintypes.UINT,
    wintypes.LPVOID,
    ctypes.POINTER(wintypes.UINT),
]
user32.GetRawInputDeviceInfoW.restype = wintypes.UINT

user32.RegisterRawInputDevices.argtypes = [
    ctypes.POINTER(RAWINPUTDEVICE),
    wintypes.UINT,
    wintypes.UINT,
]
user32.RegisterRawInputDevices.restype = wintypes.BOOL

user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT

user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.DefWindowProcW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.DefWindowProcW.restype = ctypes.c_longlong

user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM

user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND

user32.GetMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
]
user32.GetMessageW.restype = ctypes.c_int

ENUMCHILDPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.EnumChildWindows.argtypes = [wintypes.HWND, ENUMCHILDPROC, wintypes.LPARAM]
user32.EnumChildWindows.restype = wintypes.BOOL

user32.PostMessageW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.PostMessageW.restype = wintypes.BOOL

user32.PeekMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
    wintypes.UINT,
]
user32.PeekMessageW.restype = wintypes.BOOL

user32.MsgWaitForMultipleObjectsEx.argtypes = [
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
]
user32.MsgWaitForMultipleObjectsEx.restype = wintypes.DWORD

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE

POINTER_DEVICE_PRODUCT_STRING_MAX = 520


class POINTER_DEVICE_INFO(ctypes.Structure):
    _fields_ = [
        ("displayOrientation", wintypes.DWORD),
        ("device", wintypes.HANDLE),
        ("pointerDeviceType", wintypes.DWORD),
        ("monitor", wintypes.HANDLE),
        ("startingCursorId", wintypes.ULONG),
        ("maxActiveContacts", wintypes.USHORT),
        ("productString", wintypes.WCHAR * POINTER_DEVICE_PRODUCT_STRING_MAX),
    ]


user32.GetPointerDevices.argtypes = [
    ctypes.POINTER(wintypes.UINT),
    ctypes.POINTER(POINTER_DEVICE_INFO),
]
user32.GetPointerDevices.restype = wintypes.BOOL

user32.GetPointerDeviceRects.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(wintypes.RECT),
    ctypes.POINTER(wintypes.RECT),
]
user32.GetPointerDeviceRects.restype = wintypes.BOOL

user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND

user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND

user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL

GA_ROOT = 2

HANDLER_ROUTINE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
kernel32.SetConsoleCtrlHandler.argtypes = [HANDLER_ROUTINE, wintypes.BOOL]
kernel32.SetConsoleCtrlHandler.restype = wintypes.BOOL

kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE

ERROR_ALREADY_EXISTS = 183
MUTEX_NAME = "Local\\touchwheel-single-instance"

hid.HidP_GetCaps.argtypes = [wintypes.LPVOID, ctypes.POINTER(HIDP_CAPS)]
hid.HidP_GetCaps.restype = wintypes.LONG

hid.HidP_GetValueCaps.argtypes = [
    ctypes.c_int,
    ctypes.POINTER(HIDP_VALUE_CAPS),
    ctypes.POINTER(wintypes.USHORT),
    wintypes.LPVOID,
]
hid.HidP_GetValueCaps.restype = wintypes.LONG

hid.HidP_GetUsageValue.argtypes = [
    ctypes.c_int,
    wintypes.USHORT,
    wintypes.USHORT,
    wintypes.USHORT,
    ctypes.POINTER(wintypes.ULONG),
    wintypes.LPVOID,
    ctypes.c_char_p,
    wintypes.ULONG,
]
hid.HidP_GetUsageValue.restype = wintypes.LONG

HIDP_INPUT = 0


# --- Device cache ----------------------------------------------------------


class DeviceInfo:
    """Preparsed data, logical axis ranges, and screen mapping for a digitizer."""

    __slots__ = ("preparsed", "x_min", "x_max", "y_min", "y_max", "name", "display")

    def __init__(self, preparsed, name: str) -> None:
        self.preparsed = preparsed
        self.name = name
        self.x_min, self.x_max = 0, 4096
        self.y_min, self.y_max = 0, 4096
        # Screen rectangle this digitizer maps onto, or None if unavailable.
        self.display: wintypes.RECT | None = None

    @property
    def y_span(self) -> int:
        span = self.y_max - self.y_min
        return span if span > 0 else 4096

    @property
    def x_span(self) -> int:
        span = self.x_max - self.x_min
        return span if span > 0 else 4096

    def to_screen(self, x: int, y: int) -> tuple[int, int] | None:
        """Map digitizer logical coordinates to a screen point."""
        if self.display is None:
            return None
        rect = self.display
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width <= 0 or height <= 0:
            return None
        sx = rect.left + (x - self.x_min) / self.x_span * width
        sy = rect.top + (y - self.y_min) / self.y_span * height
        return int(sx), int(sy)


def _display_rect_for(handle) -> wintypes.RECT | None:
    """Find the screen rectangle the digitizer behind `handle` maps onto.

    GetPointerDevices reports the same device handles Raw Input uses, and
    GetPointerDeviceRects gives that device's logical extent alongside the
    display rectangle it is bound to.
    """
    target = ctypes.cast(handle, ctypes.c_void_p).value
    count = wintypes.UINT(0)
    if not user32.GetPointerDevices(ctypes.byref(count), None) or count.value == 0:
        return None
    devices = (POINTER_DEVICE_INFO * count.value)()
    if not user32.GetPointerDevices(ctypes.byref(count), devices):
        return None

    for dev in devices[: count.value]:
        if ctypes.cast(dev.device, ctypes.c_void_p).value != target:
            continue
        device_rect = wintypes.RECT()
        display_rect = wintypes.RECT()
        if user32.GetPointerDeviceRects(
            dev.device, ctypes.byref(device_rect), ctypes.byref(display_rect)
        ):
            return display_rect
        return None
    return None


_devices: dict[int, DeviceInfo | None] = {}


def _device_name(handle) -> str:
    size = wintypes.UINT(0)
    user32.GetRawInputDeviceInfoW(handle, RIDI_DEVICENAME, None, ctypes.byref(size))
    if size.value == 0:
        return "<unknown>"
    buf = ctypes.create_unicode_buffer(size.value)
    user32.GetRawInputDeviceInfoW(handle, RIDI_DEVICENAME, buf, ctypes.byref(size))
    return buf.value


def get_device(handle) -> DeviceInfo | None:
    """Fetch and cache preparsed data plus the Y logical range for a device."""
    key = ctypes.cast(handle, ctypes.c_void_p).value or 0
    if key in _devices:
        return _devices[key]

    size = wintypes.UINT(0)
    user32.GetRawInputDeviceInfoW(handle, RIDI_PREPARSEDDATA, None, ctypes.byref(size))
    if size.value == 0:
        _devices[key] = None
        return None

    buf = ctypes.create_string_buffer(size.value)
    got = user32.GetRawInputDeviceInfoW(
        handle, RIDI_PREPARSEDDATA, buf, ctypes.byref(size)
    )
    if got == 0xFFFFFFFF:
        _devices[key] = None
        return None

    caps = HIDP_CAPS()
    if hid.HidP_GetCaps(buf, ctypes.byref(caps)) != HIDP_STATUS_SUCCESS:
        _devices[key] = None
        return None

    info = DeviceInfo(buf, _device_name(handle))

    count = wintypes.USHORT(caps.NumberInputValueCaps)
    if count.value:
        arr = (HIDP_VALUE_CAPS * count.value)()
        status = hid.HidP_GetValueCaps(HIDP_INPUT, arr, ctypes.byref(count), buf)
        if status == HIDP_STATUS_SUCCESS:
            for cap in arr[: count.value]:
                if cap.UsagePage != HID_USAGE_PAGE_GENERIC:
                    continue
                usage = cap.u.Range.UsageMin if cap.IsRange else cap.u.NotRange.Usage
                if usage == HID_USAGE_GENERIC_X:
                    info.x_min, info.x_max = cap.LogicalMin, cap.LogicalMax
                elif usage == HID_USAGE_GENERIC_Y:
                    info.y_min, info.y_max = cap.LogicalMin, cap.LogicalMax

    info.display = _display_rect_for(handle)
    _devices[key] = info
    return info


def read_usage(info: DeviceInfo, page: int, usage: int, report: bytes) -> int | None:
    value = wintypes.ULONG(0)
    status = hid.HidP_GetUsageValue(
        HIDP_INPUT,
        page,
        0,
        usage,
        ctypes.byref(value),
        info.preparsed,
        report,
        len(report),
    )
    if status != HIDP_STATUS_SUCCESS:
        return None
    return value.value


# --- Window targeting ------------------------------------------------------


class Targets:
    """Which windows a pan should be converted into wheel input for.

    There is no reliable way to ask Windows whether a given window handles
    touch: an app that consumes WM_POINTER looks the same from outside as one
    that ignores it. Injecting wheel into an app that already scrolls itself
    makes it scroll twice, so the decision has to be a list rather than a
    probe. In list mode only the named classes are acted on; in all-windows
    mode everything is, except the named exclusions.
    """

    def __init__(
        self,
        classes: str | None = None,
        exclude: str | None = None,
        all_windows: bool = False,
    ) -> None:
        self.all_windows = all_windows
        self.classes = self._split(classes, DEFAULT_TARGET_CLASSES)
        self.exclude = self._split(exclude, DEFAULT_EXCLUDE_CLASSES)

    @staticmethod
    def _split(value: str | None, fallback) -> set[str]:
        if not value:
            return {c.casefold() for c in fallback}
        return {part.strip().casefold() for part in value.split(",") if part.strip()}

    def matches(self, hwnd) -> bool:
        if not hwnd:
            return False
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        name = buf.value.casefold()
        if self.all_windows:
            return name not in self.exclude
        return name in self.classes


def foreground_target(targets: Targets) -> wintypes.HWND | None:
    hwnd = user32.GetForegroundWindow()
    return hwnd if targets.matches(hwnd) else None


def window_at(point: tuple[int, int] | None) -> wintypes.HWND | None:
    """Top-level window under a screen point, whatever it is."""
    if point is None:
        return None
    pt = wintypes.POINT(point[0], point[1])
    child = user32.WindowFromPoint(pt)
    if not child:
        return None
    return user32.GetAncestor(child, GA_ROOT)


def target_at(point: tuple[int, int] | None, targets: Targets) -> wintypes.HWND | None:
    """The targeted window under a screen point, focused or not.

    A real wheel scrolls whatever the pointer hovers over, with no focus
    change. Targeting by position rather than focus reproduces that, and
    sidesteps the fact that Windows Terminal takes focus on its own schedule.
    """
    root = window_at(point)
    return root if targets.matches(root) else None


def window_center(hwnd) -> tuple[int, int]:
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2


class MouseWatcher:
    """Tracks where the physical mouse last was.

    Raw Input is what separates a real mouse from the mouse move Windows
    synthesises out of touch: injected input arrives with a null device handle,
    a physical device carries its own. A low-level mouse hook cannot make that
    distinction here -- measured on this hardware, touch-promoted moves carry
    neither LLMHF_INJECTED nor the MI_WP_SIGNATURE extra info, so every event
    looks physical.
    """

    def __init__(self) -> None:
        self.x = 0
        self.y = 0
        self.seen = False
        self.ignore_until = 0.0

    def ignore(self, seconds: float = 0.05) -> None:
        """Discount moves this program is about to make itself."""
        self.ignore_until = time.monotonic() + seconds

    def note_real_move(self) -> None:
        if time.monotonic() < self.ignore_until:
            return
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        self.x, self.y = pt.x, pt.y
        self.seen = True

    def last_real_position(self) -> wintypes.POINT | None:
        if not self.seen:
            # No real mouse movement observed yet, so there is nothing to put
            # back.
            return None
        return wintypes.POINT(self.x, self.y)


mouse_watcher = MouseWatcher()


def send_wheel(notches: int) -> None:
    """Synthesise a wheel event at the current cursor position."""
    evt = INPUT(type=INPUT_MOUSE)
    evt.u.mi = MOUSEINPUT(
        dx=0,
        dy=0,
        mouseData=ctypes.c_uint32(notches * WHEEL_DELTA).value,
        dwFlags=MOUSEEVENTF_WHEEL,
        time=0,
        dwExtraInfo=0,
    )
    user32.SendInput(1, ctypes.byref(evt), ctypes.sizeof(INPUT))


def input_site(hwnd):
    """The XAML island child that actually consumes pointer messages.

    A Windows Terminal window has both `Windows.UI.Input.InputSite.WindowClass`
    and `Windows.UI.Composition.DesktopWindowContentBridge` beneath it. The
    input site is the one that owns pointer input, so prefer it explicitly
    rather than taking whichever the enumeration happens to reach first.
    """
    sites: list[int] = []
    bridges: list[int] = []

    def cb(child, _lparam):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(child, buf, 256)
        if "InputSite" in buf.value:
            sites.append(child)
        elif "ContentBridge" in buf.value:
            bridges.append(child)
        return True

    user32.EnumChildWindows(hwnd, ENUMCHILDPROC(cb), 0)
    if sites:
        return sites[0]
    if bridges:
        return bridges[0]
    return hwnd


def post_wheel(hwnd, notches: int) -> None:
    """Deliver a wheel message straight to the terminal, cursor untouched."""
    cx, cy = window_center(hwnd)
    target = input_site(hwnd)
    wparam = (ctypes.c_uint32(notches * WHEEL_DELTA).value & 0xFFFF) << 16
    lparam = (cy << 16) | (cx & 0xFFFF)
    user32.PostMessageW(target, WM_MOUSEWHEEL, wparam, lparam)


# --- Gesture state ---------------------------------------------------------


class Gesture:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.targets = Targets(
            classes=getattr(args, "target_classes", None),
            exclude=getattr(args, "exclude_classes", None),
            all_windows=getattr(args, "all_windows", False),
        )
        self.anchor_y: int | None = None
        self.residual = 0.0
        self.saved_cursor: wintypes.POINT | None = None
        self.parked = False
        self.last_hwnd = None
        self.hwnd_since = 0.0
        # Window captured for the current gesture, held until the finger lifts.
        self.capture_hwnd = None
        self.capture_by_position = False
        # Set when a gesture began over a window that is not a terminal. It
        # stays set until the finger lifts, so dragging across a terminal
        # mid-gesture cannot steal the pan.
        self.rejected = False
        # Tap-vs-pan discrimination: nothing is emitted until the contact has
        # travelled past the slop radius.
        self.start_y: int | None = None
        self.panning = False
        # Not every digitizer emits a contact-count-zero report on lift, so the
        # end of a gesture is also inferred from reports simply stopping.
        self.last_report = 0.0
        # Windows promotes touch to a mouse move, so a tap drags the pointer to
        # the finger and leaves it there. Remember where it was and put it back.
        self.cursor_before: wintypes.POINT | None = None
        self.last_point: tuple[int, int] | None = None
        self.restore_until = 0.0
        # Fling state: velocity in notches/sec, carried after the finger lifts.
        self.velocity = 0.0
        self.last_emit = 0.0
        self.fling_hwnd = None
        self.fling_point: tuple[int, int] | None = None
        self.fling_v = 0.0
        self.fling_residual = 0.0
        self.fling_last = 0.0

    @property
    def active(self) -> bool:
        return self.anchor_y is not None or self.capture_hwnd is not None

    @property
    def flinging(self) -> bool:
        return self.fling_v != 0.0

    def park_cursor(self, hwnd, point: tuple[int, int] | None = None) -> None:
        if self.parked or self.args.no_park:
            return
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        self.saved_cursor = pt
        # Park under the finger when the digitizer's screen mapping is known,
        # otherwise fall back to the middle of the target window.
        cx, cy = point if point is not None else window_center(hwnd)
        mouse_watcher.ignore()
        user32.SetCursorPos(cx, cy)
        self.parked = True

    def restore_cursor(self) -> None:
        if self.parked and self.saved_cursor is not None:
            mouse_watcher.ignore()
            user32.SetCursorPos(self.saved_cursor.x, self.saved_cursor.y)
        self.parked = False
        self.saved_cursor = None

    def stop_fling(self) -> None:
        self.fling_v = 0.0
        self.fling_residual = 0.0
        self.fling_hwnd = None
        self.fling_point = None

    def end(self) -> None:
        """Finger lifted: hand any remaining speed to the fling, then reset."""
        if (
            not self.args.no_fling
            and self.panning
            and self.fling_hwnd is not None
            and abs(self.velocity) >= self.args.fling_min
        ):
            self.fling_v = self.velocity
            self.fling_residual = 0.0
            self.fling_last = time.monotonic()
            if self.args.debug:
                print(f"  fling start v={self.fling_v:+.1f} notches/s", flush=True)
        else:
            self.stop_fling()
        self.arm_cursor_restore()
        self.anchor_y = None
        self.residual = 0.0
        self.velocity = 0.0
        self.capture_hwnd = None
        self.rejected = False
        self.start_y = None
        self.panning = False
        self.restore_cursor()

    def arm_cursor_restore(self) -> None:
        """Put the pointer back where it was before the finger touched down.

        Only when the pointer is sitting near the last contact, which is the
        signature of Windows having dragged it there. If it is anywhere else
        the user moved the mouse during the gesture, and their position wins.
        Re-asserted for a short window because the promotion can land after the
        final HID report.
        """
        if self.args.no_keep_cursor or self.cursor_before is None:
            self.restore_until = 0.0
            self.cursor_before = None
            return

        now = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(now))
        moved = (now.x, now.y) != (self.cursor_before.x, self.cursor_before.y)
        near_touch = self.last_point is not None and (
            abs(now.x - self.last_point[0]) <= self.args.cursor_snap_px
            and abs(now.y - self.last_point[1]) <= self.args.cursor_snap_px
        )
        if moved and near_touch:
            self.restore_until = time.monotonic() + self.args.cursor_restore_ms / 1000.0
            if self.args.debug:
                print(
                    f"  cursor back to ({self.cursor_before.x}, "
                    f"{self.cursor_before.y})",
                    flush=True,
                )
        else:
            if self.args.debug:
                where = f"({now.x}, {now.y})"
                why = "pointer never moved" if not moved else "pointer far from touch"
                print(f"  cursor left at {where}: {why}", flush=True)
            self.restore_until = 0.0
            self.cursor_before = None

    def tick(self) -> None:
        """Advance a fling and time out abandoned gestures.

        Called from the message loop between HID reports.
        """
        if self.restore_until:
            if time.monotonic() < self.restore_until and self.cursor_before is not None:
                mouse_watcher.ignore()
                user32.SetCursorPos(self.cursor_before.x, self.cursor_before.y)
            else:
                self.restore_until = 0.0
                self.cursor_before = None

        if self.active and self.last_report:
            idle_ms = (time.monotonic() - self.last_report) * 1000.0
            if idle_ms > self.args.lift_timeout_ms:
                if self.args.debug:
                    print(f"  lift inferred after {idle_ms:.0f}ms idle", flush=True)
                self.end()

        if not self.flinging:
            return
        now = time.monotonic()
        dt = now - self.fling_last
        self.fling_last = now
        if dt <= 0:
            return

        hwnd = self.fling_hwnd
        if hwnd is None or not user32.IsWindow(hwnd):
            self.stop_fling()
            return

        total = self.fling_v * dt + self.fling_residual
        whole = int(total)
        self.fling_residual = total - whole
        if whole:
            if self.args.no_park:
                post_wheel(hwnd, whole)
            else:
                send_wheel(whole)

        # Exponential decay expressed per second, so the curve does not depend
        # on how often the loop happens to wake up.
        self.fling_v *= self.args.fling_friction**dt
        if abs(self.fling_v) < self.args.fling_min:
            if self.args.debug:
                print("  fling end", flush=True)
            self.stop_fling()

    def _target(self, info: DeviceInfo, point: tuple[int, int] | None):
        """Pick the terminal to scroll: under the finger first, focus as fallback.

        The window is captured for the whole gesture. Resolving per report lets
        an imprecise mapping, or a finger drifting over a window edge, hand
        successive notches to different windows -- which reads as every window
        under the touch scrolling at once.
        """
        if self.capture_hwnd is not None:
            if user32.IsWindow(self.capture_hwnd):
                return self.capture_hwnd, self.capture_by_position
            self.capture_hwnd = None

        # A gesture that began somewhere else belongs to that window for its
        # whole life. Without this, dragging out of a browser and across a
        # terminal starts scrolling the terminal too, while the browser keeps
        # scrolling from the touch input Windows delivered to it directly.
        if self.rejected:
            return None, False

        if point is not None:
            root = window_at(point)
            if self.targets.matches(root):
                self.capture_hwnd = root
                self.capture_by_position = True
                return root, True
            # Under a real window that is not targeted: not ours. Under nothing
            # at all, stay undecided and let the next report try.
            if root:
                self.rejected = True
                if self.args.debug:
                    print("  gesture ignored: window not targeted", flush=True)
            return None, False

        # No screen mapping for this digitizer, so fall back to the focused
        # window. That path needs the settle delay, because Windows Terminal
        # takes focus on its own schedule and the opening reports of a pan
        # aimed elsewhere still resolve to the previous window.
        hwnd = foreground_target(self.targets)
        if hwnd is None:
            return None, False
        key = ctypes.cast(hwnd, ctypes.c_void_p).value
        now = time.monotonic()
        if key != self.last_hwnd:
            self.last_hwnd = key
            self.hwnd_since = now
            return None, False
        if (now - self.hwnd_since) * 1000.0 < self.args.settle_ms:
            return None, False
        self.capture_hwnd = hwnd
        self.capture_by_position = False
        return hwnd, False

    def update(
        self, info: DeviceInfo, contacts: int, x: int | None, y: int | None
    ) -> None:
        if contacts <= 0 or y is None:
            if self.anchor_y is not None or self.capture_hwnd is not None:
                self.end()
            return

        if self.cursor_before is None and not self.args.no_keep_cursor:
            # Where the real mouse last was. Not GetCursorPos: by the time the
            # first HID report arrives Windows has usually already promoted the
            # touch into a mouse move, so the pointer is at the finger and
            # snapshotting it here restores nothing.
            self.cursor_before = mouse_watcher.last_real_position()

        self.last_report = time.monotonic()

        # A new contact always kills an in-flight fling, the way it does on a
        # phone: touching the screen stops the scroll.
        if self.flinging and self.anchor_y is None:
            self.stop_fling()

        point = info.to_screen(x, y) if x is not None else None
        if point is not None:
            self.last_point = point
        hwnd, by_position = self._target(info, point)
        if hwnd is None:
            # Re-anchor while no target is resolved, so travel spent waiting
            # never banks up and jumps into whatever resolves next.
            self.anchor_y = y
            self.residual = 0.0
            return

        if self.anchor_y is None:
            self.anchor_y = y
            self.start_y = y
            return

        dy_logical = y - self.anchor_y
        if dy_logical == 0:
            return

        # Logical units -> screen pixels across the digitizer's own extent.
        if info.display is not None:
            panel_height = max(info.display.bottom - info.display.top, 1)
        else:
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            panel_height = max(rect.bottom - rect.top, 1)
        dy_px = dy_logical / info.y_span * panel_height

        # A tap is not a one-pixel pan: fingers roll on contact and on lift.
        # Emit nothing until the contact has moved past the slop radius, then
        # re-anchor so the slop itself is not converted into a jump.
        if not self.panning:
            if self.start_y is None:
                self.start_y = y
            travel_px = abs(y - self.start_y) / info.y_span * panel_height
            if travel_px < self.args.slop_px:
                return
            self.panning = True
            self.anchor_y = y
            self.residual = 0.0
            if self.args.debug:
                print(f"  pan starts after {travel_px:.0f}px", flush=True)
            return

        notches_f = dy_px / self.args.pixels_per_notch
        if self.args.natural:
            notches_f = -notches_f

        total = notches_f + self.residual
        whole = int(total)
        self.residual = total - whole
        self.anchor_y = y

        # Track speed continuously, not just on emitted notches, so a slow pan
        # that emits rarely still reports an honest velocity for the fling.
        now = time.monotonic()
        if self.last_emit:
            dt = now - self.last_emit
            if dt > 0:
                inst = notches_f / dt
                alpha = self.args.velocity_smoothing
                self.velocity = alpha * inst + (1.0 - alpha) * self.velocity
        self.last_emit = now
        self.fling_hwnd = hwnd
        self.fling_point = point

        if whole:
            # A tap never reaches this point, so the cursor is only ever moved
            # by a gesture that is genuinely a pan. Taps on other windows keep
            # working normally.
            if self.args.no_park:
                post_wheel(hwnd, whole)
            else:
                self.park_cursor(hwnd, point)
                send_wheel(whole)
            if self.args.debug:
                mode = "post" if self.args.no_park else "send"
                how = "point" if by_position else "focus"
                print(
                    f"  wheel {whole:+d} ({mode}/{how}, dy={dy_logical:+d})", flush=True
                )


# --- Raw input plumbing ----------------------------------------------------


def handle_input(lparam, gesture: Gesture, args: argparse.Namespace) -> None:
    size = wintypes.UINT(0)
    user32.GetRawInputData(
        wintypes.HANDLE(lparam),
        RID_INPUT,
        None,
        ctypes.byref(size),
        ctypes.sizeof(RAWINPUTHEADER),
    )
    if size.value == 0:
        return

    buf = ctypes.create_string_buffer(size.value)
    got = user32.GetRawInputData(
        wintypes.HANDLE(lparam),
        RID_INPUT,
        buf,
        ctypes.byref(size),
        ctypes.sizeof(RAWINPUTHEADER),
    )
    if got != size.value:
        return

    header = ctypes.cast(buf, ctypes.POINTER(RAWINPUTHEADER)).contents
    if header.dwType == RIM_TYPEMOUSE:
        # A null device handle means the input was injected -- by this program,
        # or by the touch-to-mouse promotion. Only a real device counts.
        if header.hDevice:
            mouse_watcher.note_real_move()
        return
    if header.dwType != RIM_TYPEHID:
        return

    hid_offset = ctypes.sizeof(RAWINPUTHEADER)
    rawhid = ctypes.cast(ctypes.byref(buf, hid_offset), ctypes.POINTER(RAWHID)).contents
    data_offset = hid_offset + RAWHID.bRawData.offset

    info = get_device(header.hDevice)
    if info is None:
        return

    size_hid = rawhid.dwSizeHid
    for i in range(rawhid.dwCount):
        start = data_offset + i * size_hid
        report = buf.raw[start : start + size_hid]
        if not report:
            continue

        contacts = read_usage(
            info, HID_USAGE_PAGE_DIGITIZER, HID_USAGE_DIGITIZER_CONTACT_COUNT, report
        )
        if contacts is None:
            tip = read_usage(
                info, HID_USAGE_PAGE_DIGITIZER, HID_USAGE_DIGITIZER_TIP_SWITCH, report
            )
            contacts = 1 if tip else 0

        x = read_usage(info, HID_USAGE_PAGE_GENERIC, HID_USAGE_GENERIC_X, report)
        y = read_usage(info, HID_USAGE_PAGE_GENERIC, HID_USAGE_GENERIC_Y, report)

        if args.debug:
            point = info.to_screen(x, y) if x is not None and y is not None else None
            print(
                f"contacts={contacts} x={x} y={y} screen={point} dev={info.name[-40:]}",
                flush=True,
            )

        gesture.update(info, contacts, x, y)


def claim_single_instance() -> bool:
    """Take the process-wide lock. False if another copy already holds it.

    Two copies both convert the same pan, so every notch is delivered twice and
    the scroll reads as doubled. With an installed task and a manual launch
    both possible, that is easy to do by accident.
    """
    kernel32.CreateMutexW(None, True, MUTEX_NAME)
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def run(args: argparse.Namespace) -> int:
    if not args.allow_multiple and not claim_single_instance():
        print(
            "touchwheel is already running. Stop it first, or pass "
            "--allow-multiple if you really want a second copy.",
            file=sys.stderr,
        )
        return 1

    gesture = Gesture(args)

    def wndproc(hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            try:
                handle_input(lparam, gesture, args)
            except Exception as exc:  # keep the pump alive
                print(f"error: {exc}", file=sys.stderr, flush=True)
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    proc = WNDPROC(wndproc)
    cls = WNDCLASSW()
    cls.lpfnWndProc = proc
    cls.lpszClassName = "TouchWheelSink"
    cls.hInstance = kernel32.GetModuleHandleW(None)

    atom = user32.RegisterClassW(ctypes.byref(cls))
    if not atom:
        print("RegisterClassW failed", file=sys.stderr)
        return 1

    hwnd = user32.CreateWindowExW(
        0,
        cls.lpszClassName,
        "touchwheel",
        0,
        0,
        0,
        0,
        0,
        HWND_MESSAGE,
        None,
        cls.hInstance,
        None,
    )
    if not hwnd:
        print("CreateWindowExW failed", file=sys.stderr)
        return 1

    # The mouse is registered too, purely to learn where the physical pointer
    # is: injected input arrives with a null device handle, so real movement
    # can be told apart from the move Windows makes out of a touch.
    devices = (RAWINPUTDEVICE * 2)(
        RAWINPUTDEVICE(
            usUsagePage=HID_USAGE_PAGE_DIGITIZER,
            usUsage=HID_USAGE_DIGITIZER_TOUCH_SCREEN,
            dwFlags=RIDEV_INPUTSINK,
            hwndTarget=hwnd,
        ),
        RAWINPUTDEVICE(
            usUsagePage=HID_USAGE_PAGE_GENERIC,
            usUsage=HID_USAGE_GENERIC_MOUSE,
            dwFlags=RIDEV_INPUTSINK,
            hwndTarget=hwnd,
        ),
    )
    if not user32.RegisterRawInputDevices(devices, 2, ctypes.sizeof(RAWINPUTDEVICE)):
        err = ctypes.get_last_error()
        print(f"RegisterRawInputDevices failed (error {err})", file=sys.stderr)
        return 1

    # Belt and braces for Ctrl+C: the console handler runs on its own thread and
    # does not depend on the interpreter being between bytecodes.
    def ctrl_handler(event):
        if event in (0, 1, 2, 5, 6):  # C, BREAK, CLOSE, LOGOFF, SHUTDOWN
            user32.PostMessageW(hwnd, WM_QUIT, 0, 0)
            return True
        return False

    handler = HANDLER_ROUTINE(ctrl_handler)
    kernel32.SetConsoleCtrlHandler(handler, True)

    print("touchwheel running. Pan on the touchscreen with Windows Terminal focused.")
    print("Ctrl+C to stop.")

    # GetMessageW blocks inside a C call, so Python's SIGINT handler never runs
    # and Ctrl+C is ignored. Wait with a timeout instead and drain the queue,
    # which leaves gaps for the interpreter to raise KeyboardInterrupt.
    msg = wintypes.MSG()
    while True:
        # Wake often enough to animate a fling smoothly and to notice a lift
        # promptly. With no gesture in progress there is nothing to time, so
        # block until input arrives rather than polling: a timeout here costs
        # real idle CPU for no benefit, and WM_INPUT wakes the wait anyway.
        if gesture.flinging or gesture.restore_until:
            timeout = 8
        elif gesture.active:
            timeout = 20
        else:
            timeout = INFINITE
        user32.MsgWaitForMultipleObjectsEx(0, None, timeout, QS_ALLINPUT, 0)
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            if msg.message == WM_QUIT:
                return 0
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        gesture.tick()


def load_settings(path: pathlib.Path = SETTINGS_PATH) -> dict:
    """Saved options, keyed by argparse destination name.

    The background copy is launched with no arguments so that editing settings
    takes effect on the next start without rewriting the autostart entry.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(values: dict, path: pathlib.Path = SETTINGS_PATH) -> None:
    path.write_text(json.dumps(values, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pixels-per-notch",
        type=float,
        default=40.0,
        help="screen pixels of finger travel per wheel notch (default: 40)",
    )
    parser.add_argument(
        "--natural",
        action="store_true",
        help="invert direction (drag up scrolls content up)",
    )
    parser.add_argument(
        "--no-park",
        action="store_true",
        help="do not move the cursor over the terminal during a gesture",
    )
    parser.add_argument(
        "--settle-ms",
        type=float,
        default=150.0,
        help="ignore pan travel for this long after the foreground window "
        "changes, so a pan started on another window does not move the old "
        "one first (default: 150)",
    )
    parser.add_argument(
        "--slop-px",
        type=float,
        default=12.0,
        help="finger travel, in screen pixels, before a contact counts as a "
        "pan rather than a tap (default: 12)",
    )
    parser.add_argument(
        "--lift-timeout-ms",
        type=float,
        default=80.0,
        help="treat a gesture as ended when no HID report arrives for this "
        "long, for digitizers that send no contact-count-zero report on lift "
        "(default: 80)",
    )
    parser.add_argument(
        "--no-fling",
        action="store_true",
        help="stop scrolling the instant the finger lifts (no inertia)",
    )
    parser.add_argument(
        "--fling-friction",
        type=float,
        default=0.06,
        help="fraction of fling speed surviving each second; lower stops "
        "sooner (default: 0.06)",
    )
    parser.add_argument(
        "--fling-min",
        type=float,
        default=4.0,
        help="notches/sec below which a fling will not start, and at which a "
        "running fling stops (default: 4)",
    )
    parser.add_argument(
        "--velocity-smoothing",
        type=float,
        default=0.3,
        help="EMA weight for the newest speed sample, 0-1 (default: 0.3)",
    )
    parser.add_argument(
        "--no-keep-cursor",
        action="store_true",
        help="let touch drag the mouse pointer, as Windows does by default",
    )
    parser.add_argument(
        "--cursor-restore-ms",
        type=float,
        default=200.0,
        help="how long to hold the pointer in place after a lift, since the "
        "touch-to-mouse promotion can land after the last HID report "
        "(default: 200)",
    )
    parser.add_argument(
        "--cursor-snap-px",
        type=float,
        default=60.0,
        help="only restore when the pointer ended this close to the contact, "
        "so a deliberate mouse move during a gesture is not undone "
        "(default: 60)",
    )
    parser.add_argument(
        "--all-windows",
        action="store_true",
        help="act on every window except the excluded classes, instead of "
        "only the targeted ones",
    )
    parser.add_argument(
        "--target-classes",
        default="",
        help="comma-separated window classes to act on (default: terminals)",
    )
    parser.add_argument(
        "--exclude-classes",
        default="",
        help="comma-separated window classes to skip in all-windows mode; "
        "these already handle touch themselves and would scroll twice",
    )
    parser.add_argument(
        "--allow-multiple",
        action="store_true",
        help="skip the single-instance lock (two copies double every notch)",
    )
    parser.add_argument("--debug", action="store_true", help="print every HID report")

    # Saved settings replace the built-in defaults; an explicit flag still wins.
    known = {action.dest for action in parser._actions}
    parser.set_defaults(**{k: v for k, v in load_settings().items() if k in known})

    args = parser.parse_args()

    try:
        return run(args)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
