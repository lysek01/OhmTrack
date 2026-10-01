#!/usr/bin/env python3
"""
Native Windows HID access via ctypes — zero third-party dependencies and no
vendor DLLs. Uses only the OS libraries (setupapi.dll, hid.dll, kernel32.dll)
that ship with Windows, so this app can be distributed standalone.

Public API:
    dev = WinHID(0x1A86, 0xE429)
    dev.open()            -> True/False
    dev.write(b"...")     -> bytes written (report-ID byte first)
    dev.read(timeout_ms)  -> bytes (report-ID byte stripped, like hidapi)
    dev.close()

Only depends on the Python standard library. Windows only. On Linux/macOS the
driver falls back to the pip `hidapi` package (see ut620_driver.py).
"""

import ctypes as C
from ctypes import wintypes

setupapi = C.WinDLL("setupapi", use_last_error=True)
hid = C.WinDLL("hid", use_last_error=True)
kernel32 = C.WinDLL("kernel32", use_last_error=True)

DIGCF_PRESENT = 0x02
DIGCF_DEVICEINTERFACE = 0x10
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x01
FILE_SHARE_WRITE = 0x02
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
ERROR_IO_PENDING = 997
WAIT_TIMEOUT = 0x102
INVALID_HANDLE_VALUE = C.c_void_p(-1).value


class GUID(C.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", wintypes.BYTE * 8)]


class SP_DEVICE_INTERFACE_DATA(C.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("InterfaceClassGuid", GUID),
                ("Flags", wintypes.DWORD), ("Reserved", C.POINTER(wintypes.ULONG))]


class HIDD_ATTRIBUTES(C.Structure):
    _fields_ = [("Size", wintypes.ULONG), ("VendorID", wintypes.USHORT),
                ("ProductID", wintypes.USHORT), ("VersionNumber", wintypes.USHORT)]


class HIDP_CAPS(C.Structure):
    _fields_ = [("Usage", wintypes.USHORT), ("UsagePage", wintypes.USHORT),
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
                ("NumberFeatureDataIndices", wintypes.USHORT)]


class OVERLAPPED(C.Structure):
    _fields_ = [("Internal", C.POINTER(wintypes.ULONG)),
                ("InternalHigh", C.POINTER(wintypes.ULONG)),
                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE)]


setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
setupapi.SetupDiGetClassDevsW.argtypes = [C.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [wintypes.HANDLE, C.c_void_p, C.POINTER(GUID),
                                                 wintypes.DWORD, C.POINTER(SP_DEVICE_INTERFACE_DATA)]
setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [wintypes.HANDLE, C.POINTER(SP_DEVICE_INTERFACE_DATA),
                                                      C.c_void_p, wintypes.DWORD, C.POINTER(wintypes.DWORD), C.c_void_p]
setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]

hid.HidD_GetHidGuid.argtypes = [C.POINTER(GUID)]
hid.HidD_GetAttributes.argtypes = [wintypes.HANDLE, C.POINTER(HIDD_ATTRIBUTES)]
hid.HidD_GetAttributes.restype = wintypes.BOOLEAN
hid.HidD_GetPreparsedData.argtypes = [wintypes.HANDLE, C.POINTER(C.c_void_p)]
hid.HidD_GetPreparsedData.restype = wintypes.BOOLEAN
hid.HidD_FreePreparsedData.argtypes = [C.c_void_p]
hid.HidP_GetCaps.argtypes = [C.c_void_p, C.POINTER(HIDP_CAPS)]

kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, C.c_void_p,
                                 wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
kernel32.ReadFile.argtypes = [wintypes.HANDLE, C.c_void_p, wintypes.DWORD,
                              C.POINTER(wintypes.DWORD), C.POINTER(OVERLAPPED)]
kernel32.WriteFile.argtypes = [wintypes.HANDLE, C.c_void_p, wintypes.DWORD,
                               C.POINTER(wintypes.DWORD), C.POINTER(OVERLAPPED)]
kernel32.GetOverlappedResult.argtypes = [wintypes.HANDLE, C.POINTER(OVERLAPPED),
                                         C.POINTER(wintypes.DWORD), wintypes.BOOL]
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.CancelIo.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.CreateEventW.argtypes = [C.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]


def _iter_hid_paths():
    guid = GUID()
    hid.HidD_GetHidGuid(C.byref(guid))
    hdev = setupapi.SetupDiGetClassDevs = setupapi.SetupDiGetClassDevsW(
        C.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if hdev == INVALID_HANDLE_VALUE or not hdev:
        return
    try:
        idx = 0
        while True:
            did = SP_DEVICE_INTERFACE_DATA()
            did.cbSize = C.sizeof(SP_DEVICE_INTERFACE_DATA)
            if not setupapi.SetupDiEnumDeviceInterfaces(hdev, None, C.byref(guid), idx, C.byref(did)):
                break
            idx += 1
            req = wintypes.DWORD(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, C.byref(did), None, 0, C.byref(req), None)
            if req.value == 0:
                continue
            buf = C.create_string_buffer(req.value)
            # SP_DEVICE_INTERFACE_DETAIL_DATA_W.cbSize = 8 on 64-bit, 6 on 32-bit
            C.cast(buf, C.POINTER(wintypes.DWORD))[0] = 8 if C.sizeof(C.c_void_p) == 8 else 6
            if not setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, C.byref(did), buf, req.value, None, None):
                continue
            path = C.wstring_at(C.addressof(buf) + C.sizeof(wintypes.DWORD))
            yield path
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(hdev)


class WinHID:
    def __init__(self, vid, pid):
        self.vid, self.pid = vid, pid
        self.handle = None
        self.in_len = 65
        self.out_len = 8
        self._event = None

    def open(self) -> bool:
        for path in _iter_hid_paths():
            h = kernel32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE,
                                     FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                                     OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None)
            if not h or h == INVALID_HANDLE_VALUE:
                continue
            attrs = HIDD_ATTRIBUTES()
            attrs.Size = C.sizeof(HIDD_ATTRIBUTES)
            if hid.HidD_GetAttributes(h, C.byref(attrs)) and \
               attrs.VendorID == self.vid and attrs.ProductID == self.pid:
                pp = C.c_void_p()
                if hid.HidD_GetPreparsedData(h, C.byref(pp)):
                    caps = HIDP_CAPS()
                    if hid.HidP_GetCaps(pp, C.byref(caps)) == 0x00110000:  # HIDP_STATUS_SUCCESS
                        if caps.InputReportByteLength:
                            self.in_len = caps.InputReportByteLength
                        if caps.OutputReportByteLength:
                            self.out_len = caps.OutputReportByteLength
                    hid.HidD_FreePreparsedData(pp)
                self.handle = h
                self._event = kernel32.CreateEventW(None, True, False, None)
                return True
            kernel32.CloseHandle(h)
        return False

    def write(self, data: bytes) -> int:
        if not self.handle:
            return -1
        out = bytearray(self.out_len)
        n = min(len(data), self.out_len)
        out[:n] = data[:n]
        written = wintypes.DWORD(0)
        ov = OVERLAPPED(); ov.hEvent = self._event
        kernel32.ResetEvent(self._event)
        ok = kernel32.WriteFile(self.handle, bytes(out), self.out_len, C.byref(written), C.byref(ov))
        if not ok and C.get_last_error() == ERROR_IO_PENDING:
            kernel32.WaitForSingleObject(self._event, 1000)
            kernel32.GetOverlappedResult(self.handle, C.byref(ov), C.byref(written), False)
        return written.value

    def read(self, timeout_ms: int = 300) -> bytes:
        if not self.handle:
            return b""
        buf = C.create_string_buffer(self.in_len)
        read = wintypes.DWORD(0)
        ov = OVERLAPPED(); ov.hEvent = self._event
        kernel32.ResetEvent(self._event)
        ok = kernel32.ReadFile(self.handle, buf, self.in_len, C.byref(read), C.byref(ov))
        if not ok:
            if C.get_last_error() != ERROR_IO_PENDING:
                return b""
            if kernel32.WaitForSingleObject(self._event, timeout_ms) == WAIT_TIMEOUT:
                kernel32.CancelIo(self.handle)
                return b""
            if not kernel32.GetOverlappedResult(self.handle, C.byref(ov), C.byref(read), False):
                return b""
        n = read.value
        if n <= 0:
            return b""
        # strip the leading report-ID byte (report 0), matching hidapi behaviour
        return bytes(buf.raw[1:n])

    def close(self):
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = None
        if self._event:
            kernel32.CloseHandle(self._event)
            self._event = None


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    d = WinHID(0x1A86, 0xE429)
    if not d.open():
        print("device not found (on? any other program using it closed?)")
    else:
        print(f"opened; in_len={d.in_len} out_len={d.out_len}")
        d.write(bytes([0x00, 0x05, 0xAB, 0xCD, 0x05, 0x00, 0x05]))
        r = d.read(400)
        print("reply:", " ".join(f"{b:02x}" for b in r[:12]))
        d.close()
