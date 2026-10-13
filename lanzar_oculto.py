"""Corre watchdog.py en un escritorio de Windows oculto: el Chrome (no headless, para pasar Cloudflare)
se abre ahí y nunca roba el foco de lo que estés usando (juegos, etc.).

Uso (lo corre la tarea programada):  pythonw lanzar_oculto.py
"""
import ctypes
import pathlib
import sys
from ctypes import wintypes

DESKTOP = "WatchdogHV"
HERE = pathlib.Path(__file__).resolve().parent
k32, u32 = ctypes.WinDLL("kernel32", use_last_error=True), ctypes.WinDLL("user32", use_last_error=True)


class STARTUPINFO(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


u32.CreateDesktopW.restype = wintypes.HANDLE
k32.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL,
                               wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFO),
                               ctypes.POINTER(PROCESS_INFORMATION)]


def main():
    GENERIC_ALL = 0x10000000
    hdesk = u32.CreateDesktopW(DESKTOP, None, None, 0, GENERIC_ALL, None)
    if not hdesk:
        raise ctypes.WinError(ctypes.get_last_error())
    si = STARTUPINFO(cb=ctypes.sizeof(STARTUPINFO), lpDesktop=f"WinSta0\\{DESKTOP}")
    pi = PROCESS_INFORMATION()
    cmd = f'"{sys.executable}" "{HERE / "watchdog.py"}"'
    if not k32.CreateProcessW(None, ctypes.create_unicode_buffer(cmd), None, None, False,
                              0x08000000, None, str(HERE), ctypes.byref(si), ctypes.byref(pi)):  # CREATE_NO_WINDOW
        raise ctypes.WinError(ctypes.get_last_error())
    k32.WaitForSingleObject(pi.hProcess, 10 * 60 * 1000)
    for h in (pi.hProcess, pi.hThread):
        k32.CloseHandle(h)
    u32.CloseDesktop(hdesk)


if __name__ == "__main__":
    main()
