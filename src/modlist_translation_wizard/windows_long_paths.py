"""User-controlled Windows long-path registry support."""

from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    import winreg
except ImportError:  # pragma: no cover - exercised on non-Windows hosts.
    winreg = None  # type: ignore[assignment]


REGISTRY_PATH = r"SYSTEM\CurrentControlSet\Control\FileSystem"
REGISTRY_VALUE = "LongPathsEnabled"
_ERROR_CANCELLED = 1223
_SEE_MASK_NOCLOSEPROCESS = 0x00000040
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_ELEVATED_PROCESS_TIMEOUT_MS = 300_000


@dataclass(frozen=True, slots=True)
class WindowsLongPathStatus:
    available: bool
    enabled: bool
    raw_value: int | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class WindowsLongPathEnableResult:
    status: WindowsLongPathStatus
    changed: bool
    cancelled: bool = False


class WindowsLongPathError(RuntimeError):
    """Raised when the explicit long-path enable request cannot complete."""


def windows_long_path_status() -> WindowsLongPathStatus:
    if sys.platform != "win32" or winreg is None:
        return WindowsLongPathStatus(available=False, enabled=False)

    access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, REGISTRY_PATH, 0, access) as key:
            value, value_type = winreg.QueryValueEx(key, REGISTRY_VALUE)
    except FileNotFoundError:
        return WindowsLongPathStatus(available=True, enabled=False, raw_value=None)
    except OSError as exc:
        return WindowsLongPathStatus(
            available=True,
            enabled=False,
            raw_value=None,
            error=str(exc),
        )

    parsed = int(value) if value_type == winreg.REG_DWORD else None
    return WindowsLongPathStatus(
        available=True,
        enabled=parsed == 1,
        raw_value=parsed,
    )


def enable_windows_long_paths() -> WindowsLongPathEnableResult:
    current = windows_long_path_status()
    if not current.available:
        raise WindowsLongPathError("Bu ayar yalnızca Windows üzerinde kullanılabilir.")
    if current.enabled:
        return WindowsLongPathEnableResult(status=current, changed=False)

    exit_code = _run_elevated_registry_update()
    if exit_code is None:
        return WindowsLongPathEnableResult(
            status=windows_long_path_status(),
            changed=False,
            cancelled=True,
        )
    if exit_code != 0:
        raise WindowsLongPathError(
            f"Windows uzun yol ayarı değiştirilemedi. reg.exe çıkış kodu: {exit_code}"
        )

    updated = windows_long_path_status()
    if not updated.enabled:
        detail = f" ({updated.error})" if updated.error else ""
        raise WindowsLongPathError(
            "Windows uzun yol ayarı yazıldı ancak doğrulanamadı" + detail
        )
    return WindowsLongPathEnableResult(status=updated, changed=True)


def _run_elevated_registry_update() -> int | None:
    if sys.platform != "win32":
        raise WindowsLongPathError("Yönetici işlemi yalnızca Windows üzerinde çalıştırılabilir.")

    from ctypes import wintypes

    class ShellExecuteInfoW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    windows_root = Path(os.environ.get("SystemRoot") or r"C:\Windows")
    reg_executable = windows_root / "System32" / "reg.exe"
    if not reg_executable.is_file():
        raise WindowsLongPathError(f"Windows kayıt aracı bulunamadı: {reg_executable}")

    parameters = (
        r'ADD "HKLM\SYSTEM\CurrentControlSet\Control\FileSystem" '
        r'/v LongPathsEnabled /t REG_DWORD /d 1 /f'
    )
    info = ShellExecuteInfoW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = _SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = str(reg_executable)
    info.lpParameters = parameters
    info.nShow = 0

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(ShellExecuteInfoW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        error = ctypes.get_last_error()
        if error == _ERROR_CANCELLED:
            return None
        raise WindowsLongPathError(
            f"Yönetici işlemi başlatılamadı. Windows hata kodu: {error}"
        )
    if not info.hProcess:
        raise WindowsLongPathError("Yönetici işlemi için süreç tanıtıcısı alınamadı.")

    try:
        wait_result = kernel32.WaitForSingleObject(
            info.hProcess,
            _ELEVATED_PROCESS_TIMEOUT_MS,
        )
        if wait_result == _WAIT_TIMEOUT:
            raise WindowsLongPathError("Yönetici onayı zaman aşımına uğradı.")
        if wait_result != _WAIT_OBJECT_0:
            raise WindowsLongPathError(
                f"Yönetici işlemi beklenemedi. Windows sonucu: {wait_result}"
            )
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(exit_code)):
            error = ctypes.get_last_error()
            raise WindowsLongPathError(
                f"Yönetici işleminin sonucu okunamadı. Windows hata kodu: {error}"
            )
        return int(exit_code.value)
    finally:
        kernel32.CloseHandle(info.hProcess)
