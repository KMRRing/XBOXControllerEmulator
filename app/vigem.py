"""vigem — a virtual Xbox 360 controller through the ViGEmBus driver, in the standard library.

ViGEmBus (Virtual Gamepad Emulation Bus) is a kernel driver: once it is installed, a program can ask it to
"plug in" a wired Xbox 360 pad, and Windows and every game then see a real controller on XInput. Programs
normally reach it through Nefarius' ViGEmClient.dll. This file speaks the same protocol directly — device
I/O controls (IOCTLs, numbered requests a program sends to a driver) through `DeviceIoControl` — so there is
no DLL to match to the machine: it runs the same on an x64 Surface and an ARM64 one.

The protocol is ViGEmClient's (MIT, github.com/nefarius/ViGEmClient, include/ViGEm/km/BusShared.h):

    find the bus      SetupDi enumeration of GUID_DEVINTERFACE_BUSENUM_VIGEM, open it overlapped
    handshake         IOCTL_VIGEM_CHECK_VERSION with VIGEM_COMMON_VERSION
    plug in           IOCTL_VIGEM_PLUGIN_TARGET, serial numbers tried from 1 upwards until one is free,
                      then IOCTL_VIGEM_WAIT_DEVICE_READY (drivers before 1.17 reject it: that is success)
    send state        IOCTL_XUSB_SUBMIT_REPORT with an XUSB_REPORT
    unplug            IOCTL_VIGEM_UNPLUG_TARGET; closing the handle does the same, so a crash leaves no pad

Every request is overlapped and waited on with a timeout, so a driver that never answers cannot hang the
overlay's thread.
"""
from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes

# --------------------------------------------------------------------------------- the wire format
FILE_DEVICE_BUS_EXTENDER = 0x2A
METHOD_BUFFERED = 0
FILE_READ_DATA, FILE_WRITE_DATA = 1, 2
VIGEM_COMMON_VERSION = 0x0001
XBOX360_WIRED = 0
VENDOR, PRODUCT = 0x045E, 0x028E                     # Microsoft, Xbox 360 wired controller


def ctl_code(device: int, function: int, method: int, access: int) -> int:
    return (device << 16) | (access << 14) | (function << 2) | method


def _w(index: int) -> int:
    return ctl_code(FILE_DEVICE_BUS_EXTENDER, 0x801 + index, METHOD_BUFFERED, FILE_WRITE_DATA)


IOCTL_PLUGIN = _w(0x000)
IOCTL_UNPLUG = _w(0x001)
IOCTL_CHECK_VERSION = _w(0x002)
IOCTL_WAIT_READY = _w(0x003)
IOCTL_XUSB_SUBMIT = _w(0x201)


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8)]


BUS_GUID = GUID(0x96E42B22, 0xF5E9, 0x42F8, (ctypes.c_ubyte * 8)(0xB0, 0x43, 0xED, 0x0F, 0x93, 0x2F, 0x01, 0x4F))


class PluginTarget(ctypes.Structure):                  # VIGEM_PLUGIN_TARGET
    _fields_ = [("Size", ctypes.c_uint32), ("SerialNo", ctypes.c_uint32), ("TargetType", ctypes.c_int32),
                ("VendorId", ctypes.c_uint16), ("ProductId", ctypes.c_uint16)]


class SerialOnly(ctypes.Structure):                    # VIGEM_UNPLUG_TARGET, VIGEM_WAIT_DEVICE_READY, CHECK_VERSION
    _fields_ = [("Size", ctypes.c_uint32), ("Value", ctypes.c_uint32)]


class XusbReport(ctypes.Structure):                    # XUSB_REPORT
    _fields_ = [("wButtons", ctypes.c_uint16), ("bLeftTrigger", ctypes.c_uint8), ("bRightTrigger", ctypes.c_uint8),
                ("sThumbLX", ctypes.c_int16), ("sThumbLY", ctypes.c_int16),
                ("sThumbRX", ctypes.c_int16), ("sThumbRY", ctypes.c_int16)]


class XusbSubmit(ctypes.Structure):                    # XUSB_SUBMIT_REPORT
    _fields_ = [("Size", ctypes.c_uint32), ("SerialNo", ctypes.c_uint32), ("Report", XusbReport)]


class Overlapped(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", ctypes.c_uint32), ("OffsetHigh", ctypes.c_uint32), ("hEvent", ctypes.c_void_p)]


class SpDeviceInterfaceData(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("InterfaceClassGuid", GUID), ("Flags", ctypes.c_uint32),
                ("Reserved", ctypes.c_size_t)]


def pack_report(r: dict) -> XusbReport:
    return XusbReport(r["buttons"] & 0xFFFF, r["lt"] & 0xFF, r["rt"] & 0xFF, r["lx"], r["ly"], r["rx"], r["ry"])


# --------------------------------------------------------------------------------- Windows
class VigemError(RuntimeError):
    pass


NOT_INSTALLED = "ViGEmBus is not installed"
_api = None


def _bind():
    """The Win32 calls, with their types spelled out — without argtypes ctypes truncates 64-bit handles."""
    global _api
    if _api is not None:
        return _api
    if os.name != "nt":
        raise VigemError("Windows only")
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    sapi = ctypes.WinDLL("setupapi", use_last_error=True)
    H, B, D, P = wintypes.HANDLE, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p

    def bind(dll, name, res, *args):
        f = getattr(dll, name)
        f.restype, f.argtypes = res, list(args)
        return f

    _api = {
        "CreateFileW": bind(k32, "CreateFileW", H, wintypes.LPCWSTR, D, D, P, D, D, H),
        "CloseHandle": bind(k32, "CloseHandle", B, H),
        "CreateEventW": bind(k32, "CreateEventW", H, P, B, B, wintypes.LPCWSTR),
        "DeviceIoControl": bind(k32, "DeviceIoControl", B, H, D, P, D, P, D, ctypes.POINTER(D), ctypes.POINTER(Overlapped)),
        "GetOverlappedResult": bind(k32, "GetOverlappedResult", B, H, ctypes.POINTER(Overlapped), ctypes.POINTER(D), B),
        "WaitForSingleObject": bind(k32, "WaitForSingleObject", D, H, D),
        "CancelIoEx": bind(k32, "CancelIoEx", B, H, ctypes.POINTER(Overlapped)),
        "ResetEvent": bind(k32, "ResetEvent", B, H),
        "GetClassDevs": bind(sapi, "SetupDiGetClassDevsW", H, ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND, D),
        "EnumInterfaces": bind(sapi, "SetupDiEnumDeviceInterfaces", B, H, P, ctypes.POINTER(GUID), D,
                               ctypes.POINTER(SpDeviceInterfaceData)),
        "InterfaceDetail": bind(sapi, "SetupDiGetDeviceInterfaceDetailW", B, H, ctypes.POINTER(SpDeviceInterfaceData),
                                P, D, ctypes.POINTER(D), P),
        "DestroyList": bind(sapi, "SetupDiDestroyDeviceInfoList", B, H),
    }
    return _api


INVALID = ctypes.c_void_p(-1).value
ERROR_IO_PENDING, ERROR_INVALID_PARAMETER, WAIT_OBJECT_0 = 997, 87, 0


def bus_paths() -> list:
    """Every ViGEm bus the system knows (normally one), as device paths CreateFile can open."""
    a = _bind()
    info = a["GetClassDevs"](ctypes.byref(BUS_GUID), None, None, 0x02 | 0x10)   # DIGCF_PRESENT | DEVICEINTERFACE
    if not info or info == INVALID:
        return []
    paths = []
    try:
        index = 0
        while True:
            data = SpDeviceInterfaceData()
            data.cbSize = ctypes.sizeof(data)
            if not a["EnumInterfaces"](info, None, ctypes.byref(BUS_GUID), index, ctypes.byref(data)):
                break
            index += 1
            need = wintypes.DWORD(0)
            a["InterfaceDetail"](info, ctypes.byref(data), None, 0, ctypes.byref(need), None)
            if need.value < 6:
                continue
            buf = ctypes.create_string_buffer(need.value)
            # SP_DEVICE_INTERFACE_DETAIL_DATA_W.cbSize is the size of the fixed part: 8 on 64-bit, 6 on 32-bit.
            ctypes.c_uint32.from_buffer(buf).value = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            if a["InterfaceDetail"](info, ctypes.byref(data), buf, need, ctypes.byref(need), None):
                paths.append(ctypes.wstring_at(ctypes.addressof(buf) + 4))
    finally:
        a["DestroyList"](info)
    return paths


class Controller:
    """One virtual Xbox 360 pad. `connect()` plugs it in; `send(report)` sets its state; `close()` unplugs."""

    def __init__(self):
        self.handle = None
        self.serial = 0
        self.lock = threading.Lock()
        self.event = None

    # ---- one overlapped request, waited on with a timeout
    def _ioctl(self, code: int, struct, timeout_ms: int = 2000) -> tuple:
        a = _bind()
        ov = Overlapped()
        ov.hEvent = self.event
        a["ResetEvent"](self.event)
        got = wintypes.DWORD(0)
        ok = a["DeviceIoControl"](self.handle, code, ctypes.byref(struct), ctypes.sizeof(struct), None, 0,
                                  ctypes.byref(got), ctypes.byref(ov))
        if not ok:
            err = ctypes.get_last_error()
            if err != ERROR_IO_PENDING:
                return False, err
            if a["WaitForSingleObject"](self.event, timeout_ms) != WAIT_OBJECT_0:
                a["CancelIoEx"](self.handle, ctypes.byref(ov))
                a["GetOverlappedResult"](self.handle, ctypes.byref(ov), ctypes.byref(got), True)
                return False, "timeout"
        if a["GetOverlappedResult"](self.handle, ctypes.byref(ov), ctypes.byref(got), True):
            return True, 0
        return False, ctypes.get_last_error()

    def _open(self) -> None:
        a = _bind()
        paths = bus_paths()
        if not paths:
            raise VigemError(NOT_INSTALLED)
        last = "the bus refused the connection"
        for path in paths:
            h = a["CreateFileW"](path, 0xC0000000, 0x3, None, 3, 0x80 | 0x20000000 | 0x80000000 | 0x40000000, None)
            if not h or h == INVALID:                  # GENERIC_RW, share RW, OPEN_EXISTING, overlapped + no buffering
                last = f"could not open the bus (error {ctypes.get_last_error()})"
                continue
            self.handle = h
            ok, err = self._ioctl(IOCTL_CHECK_VERSION, SerialOnly(8, VIGEM_COMMON_VERSION))
            if ok:
                return
            last = f"driver version mismatch ({err})"
            a["CloseHandle"](h)
            self.handle = None
        raise VigemError(last)

    def connect(self) -> int:
        with self.lock:
            if self.serial:
                return self.serial
            a = _bind()
            self.event = a["CreateEventW"](None, True, False, None)
            try:
                self._open()
                for serial in range(1, 17):
                    plug = PluginTarget(ctypes.sizeof(PluginTarget), serial, XBOX360_WIRED, VENDOR, PRODUCT)
                    ok, _ = self._ioctl(IOCTL_PLUGIN, plug)
                    if not ok:
                        continue                        # that serial is taken: try the next
                    ready, err = self._ioctl(IOCTL_WAIT_READY, SerialOnly(8, serial), timeout_ms=5000)
                    if ready or err == ERROR_INVALID_PARAMETER:  # < 1.17 does not know the request: plugged is plugged
                        self.serial = serial
                        self.send({"buttons": 0, "lt": 0, "rt": 0, "lx": 0, "ly": 0, "rx": 0, "ry": 0}, locked=True)
                        return serial
                    self._ioctl(IOCTL_UNPLUG, SerialOnly(8, serial))
                    raise VigemError(f"the pad never became ready ({err})")
                raise VigemError("no free controller slot on the bus")
            except Exception:
                self._release()
                raise

    def send(self, report: dict, locked: bool = False) -> bool:
        if not locked:
            with self.lock:
                return self.send(report, locked=True)
        if not self.serial:
            return False
        ok, _ = self._ioctl(IOCTL_XUSB_SUBMIT, XusbSubmit(ctypes.sizeof(XusbSubmit), self.serial, pack_report(report)),
                            timeout_ms=500)
        return ok

    def _release(self) -> None:
        a = _bind()
        if self.handle:
            a["CloseHandle"](self.handle)
        if self.event:
            a["CloseHandle"](self.event)
        self.handle, self.event, self.serial = None, None, 0

    def close(self) -> None:
        with self.lock:
            if self.serial and self.handle:
                self._ioctl(IOCTL_UNPLUG, SerialOnly(8, self.serial))
            if self.handle or self.event:
                self._release()


def driver_status() -> dict:
    """Is the bus there — the cheap check, no pad is plugged in."""
    if os.name != "nt":
        return {"ok": False, "error": "Windows only"}
    try:
        found = bool(bus_paths())
    except Exception as e:                                  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    return {"ok": found, "error": "" if found else NOT_INSTALLED}
