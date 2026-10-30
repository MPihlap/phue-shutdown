"""Turn the "Studio sound system" plug on at Windows logon and off at shutdown.

Run with pythonw.exe (no console) from the Startup folder. It turns the plug on,
then waits in the background for Windows' end-session message and turns it off.

Manual use: python studio_plug.py on|off
"""
import ctypes
import logging
import sys
import time
from ctypes import wintypes
from pathlib import Path

from phue import Bridge

from find_ip import find_ip

BRIDGE_HOSTNAME = "001788aedf4d"
FALLBACK_IP = "192.168.1.159"
LIGHT_NAME = "Studio sound system"

logging.basicConfig(
    filename=Path(__file__).with_name("studio_plug.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)
logging.getLogger("phue").setLevel(logging.WARNING)  # phue logs the bridge username at INFO


def set_plug(on, attempts=1, delay=5):
    for attempt in range(1, attempts + 1):
        try:
            bridge = Bridge(find_ip(BRIDGE_HOSTNAME) or FALLBACK_IP)
            bridge.set_light(LIGHT_NAME, "on", on)
            log.info("Turned %s %s", LIGHT_NAME, "on" if on else "off")
            return True
        except Exception:
            log.exception("Attempt %d to turn %s failed", attempt, "on" if on else "off")
            if attempt < attempts:
                time.sleep(delay)
    return False


# --- Win32 plumbing for receiving the shutdown message ---

WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
ERROR_ALREADY_EXISTS = 183

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


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


user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.ShutdownBlockReasonCreate.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
user32.ShutdownBlockReasonDestroy.argtypes = [wintypes.HWND]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE


@WNDPROC
def wnd_proc(hwnd, msg, wparam, lparam):
    if msg == WM_QUERYENDSESSION:
        return 1  # allow the shutdown
    if msg == WM_ENDSESSION:
        log.info("WM_ENDSESSION ending=%s flags=0x%08x", bool(wparam), lparam & 0xFFFFFFFF)
        # wparam is FALSE if the shutdown was cancelled. Sign-outs are not skipped:
        # with Fast Startup, shutdown arrives as a logoff (ENDSESSION_LOGOFF set).
        if wparam:
            user32.ShutdownBlockReasonCreate(hwnd, "Turning off studio sound system")
            set_plug(False, attempts=2, delay=1)
            user32.ShutdownBlockReasonDestroy(hwnd)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def wait_for_shutdown():
    hinstance = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSW(lpfnWndProc=wnd_proc, hInstance=hinstance, lpszClassName="StudioPlugShutdownWatcher")
    if not user32.RegisterClassW(ctypes.byref(wc)):
        raise ctypes.WinError(ctypes.get_last_error())
    # A hidden top-level window (not message-only) so it receives the shutdown broadcast
    hwnd = user32.CreateWindowExW(0, wc.lpszClassName, "Studio plug", 0, 0, 0, 0, 0, None, None, hinstance, None)
    if not hwnd:
        raise ctypes.WinError(ctypes.get_last_error())

    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("on", "off"):
        sys.exit(0 if set_plug(sys.argv[1] == "on") else 1)

    # Only one watcher at a time; the handle stays open for the life of the process
    instance_mutex = kernel32.CreateMutexW(None, False, "StudioPlugShutdownWatcher")
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        log.info("Another instance is already running; exiting")
        sys.exit(0)

    log.info("Started; waiting for network to turn plug on")
    # The network may not be ready right after logon, so retry for ~2 minutes
    set_plug(True, attempts=24, delay=5)
    wait_for_shutdown()
