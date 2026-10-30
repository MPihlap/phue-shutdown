"""Keep the "Studio sound system" plug in step with the USB soundcard's power.

The monitors must be off before the soundcard loses power, or they bang. So the plug
is turned off at shutdown, sign-out, sleep and hibernate, and when the soundcard is
unplugged. It is turned back on at logon, after resume and when the soundcard comes
back, each time a few seconds after the soundcard is up.

Run with pythonw.exe (no console) at logon. It runs in the background and listens
for Windows' session, power and device messages.

Manual use: python studio_plug.py on|off
"""
import ctypes
import logging
import sys
import threading
from ctypes import wintypes
from pathlib import Path

from phue import Bridge

from find_ip import find_ip

BRIDGE_HOSTNAME = "001788aedf4d"
FALLBACK_IP = "192.168.1.159"
LIGHT_NAME = "Studio sound system"
SOUNDCARD_ID = r"USB\VID_31E9&PID_0005\S12-030066"  # SSL 12, from Device Manager > Details > Device instance path
SETTLE_DELAY = 3  # seconds for the soundcard to power up before the monitors come on

logging.basicConfig(
    filename=Path(__file__).with_name("studio_plug.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)
logging.getLogger("phue").setLevel(logging.WARNING)  # phue logs the bridge username at INFO

# The bridge is kept between calls so the sleep path skips the IP lookup;
# Windows only gives about two seconds to handle a suspend.
_bridge = None
_light_id = None


def set_plug(on):
    global _bridge, _light_id
    try:
        if _bridge is None:
            bridge = Bridge(find_ip(BRIDGE_HOSTNAME) or FALLBACK_IP)
            light_id = bridge.get_light_id_by_name(LIGHT_NAME)
            if not light_id:
                raise LookupError(f"No light named {LIGHT_NAME!r}")
            _light_id = int(light_id)  # phue treats a string ID as a light name
            _bridge = bridge
        # phue only logs bridge errors, so check the response ourselves
        result = _bridge.set_light(_light_id, "on", on)
        errors = [item["error"]["description"] for item in result[0] if "error" in item]
        if errors:
            raise RuntimeError("; ".join(errors))
    except Exception as e:
        _bridge = None  # look the bridge up again next time, in case its IP changed
        log.warning("Turning %s failed: %r", "on" if on else "off", e)
        return False
    log.info("Turned %s %s", LIGHT_NAME, "on" if on else "off")
    return True


class PlugController:
    """Applies the wanted plug state on a worker thread, retrying until the network is up.

    Turning off before power loss is done synchronously instead, from the window
    procedure, because Windows proceeds with the sleep or shutdown once it returns.
    """

    def __init__(self):
        self.cond = threading.Condition()
        self.target = None  # wanted state; None until the first request
        self.is_on = None  # last state the bridge confirmed; None if unknown
        self.generation = 0  # bumped on every new target, so stale retries stop
        self.delay = 0
        # One bridge request at a time, so a turn-on in flight can't land after a turn-off
        self.io_lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    def set_target(self, on, delay=0):
        with self.cond:
            if self.target == on:
                return
            self.target = on
            self.generation += 1
            self.delay = delay
            self.cond.notify_all()

    def turn_off_now(self, attempts):
        self.set_target(False)
        with self.io_lock:
            for _ in range(attempts):
                if set_plug(False):
                    self._confirm(False)
                    return
            self._confirm(None)

    def _confirm(self, state):
        with self.cond:
            self.is_on = state
            self.cond.notify_all()

    def _superseded(self, generation, timeout):
        with self.cond:
            return self.cond.wait_for(lambda: self.generation != generation, timeout)

    def _run(self):
        while True:
            with self.cond:
                self.cond.wait_for(lambda: self.target is not None and self.target != self.is_on)
                on, generation, delay = self.target, self.generation, self.delay
            if self._superseded(generation, delay):
                continue
            attempt = 0
            while True:
                attempt += 1
                with self.io_lock:
                    # Superseded, or turn_off_now already did it while we waited for the lock
                    if self.generation != generation or self.is_on == on:
                        break
                    ok = set_plug(on)
                    self._confirm(on if ok else None)
                    if ok:
                        break
                # Every 5 s for the first two minutes (network coming up), then every minute
                if self._superseded(generation, 5 if attempt < 24 else 60):
                    break


# --- Win32 plumbing for receiving session, power and device messages ---

WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
WM_POWERBROADCAST = 0x0218
WM_DEVICECHANGE = 0x0219
PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
DBT_DEVNODES_CHANGED = 0x0007
ERROR_ALREADY_EXISTS = 183
CR_SUCCESS = 0
CM_LOCATE_DEVNODE_NORMAL = 0  # only finds devices that are currently present

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
cfgmgr32 = ctypes.WinDLL("cfgmgr32")
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
cfgmgr32.CM_Locate_DevNodeW.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.LPCWSTR, wintypes.ULONG]


def soundcard_present():
    devinst = wintypes.DWORD()
    return cfgmgr32.CM_Locate_DevNodeW(ctypes.byref(devinst), SOUNDCARD_ID, CM_LOCATE_DEVNODE_NORMAL) == CR_SUCCESS


plug = None  # PlugController, created in main
soundcard_was_present = False


@WNDPROC
def wnd_proc(hwnd, msg, wparam, lparam):
    global soundcard_was_present
    if msg == WM_QUERYENDSESSION:
        return 1  # allow the shutdown
    if msg == WM_ENDSESSION:
        log.info("WM_ENDSESSION ending=%s flags=0x%08x", bool(wparam), lparam & 0xFFFFFFFF)
        # wparam is FALSE if the shutdown was cancelled. Sign-outs are not skipped:
        # with Fast Startup, shutdown arrives as a logoff (ENDSESSION_LOGOFF set).
        if wparam:
            user32.ShutdownBlockReasonCreate(hwnd, "Turning off studio sound system")
            plug.turn_off_now(attempts=2)
            user32.ShutdownBlockReasonDestroy(hwnd)
        return 0
    if msg == WM_POWERBROADCAST:
        if wparam == PBT_APMSUSPEND:  # sleep or hibernate; USB power goes once we return
            log.info("Suspending")
            plug.turn_off_now(attempts=2)
        elif wparam in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
            log.info("Resumed (0x%x)", wparam)
            if soundcard_present():
                plug.set_target(True, delay=SETTLE_DELAY)
        return 1
    if msg == WM_DEVICECHANGE and wparam == DBT_DEVNODES_CHANGED:
        present = soundcard_present()
        if present != soundcard_was_present:
            soundcard_was_present = present
            log.info("Soundcard %s", "connected" if present else "disconnected")
            if present:
                plug.set_target(True, delay=SETTLE_DELAY)
            else:
                plug.set_target(False)  # too late to prevent a bang, but don't leave the monitors on
        return 1
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def run_message_loop():
    hinstance = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSW(lpfnWndProc=wnd_proc, hInstance=hinstance, lpszClassName="StudioPlugShutdownWatcher")
    if not user32.RegisterClassW(ctypes.byref(wc)):
        raise ctypes.WinError(ctypes.get_last_error())
    # A hidden top-level window (not message-only) so it receives the session, power and device broadcasts
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

    plug = PlugController()
    soundcard_was_present = soundcard_present()
    if soundcard_was_present:
        log.info("Started; turning plug on")
        plug.set_target(True)  # retried until the network is up after logon
    else:
        log.info("Started; waiting for soundcard")
    run_message_loop()
