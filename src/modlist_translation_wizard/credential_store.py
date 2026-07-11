"""Credential storage boundary for end-user wizard secrets."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass
from typing import Protocol


class CredentialStoreError(RuntimeError):
    """Raised when credential persistence fails."""


class CredentialStore(Protocol):
    def read_secret(self, target: str) -> str | None:
        """Return a secret for the target, or None if it is not stored."""

    def write_secret(self, target: str, secret: str) -> None:
        """Persist a secret for the target."""

    def delete_secret(self, target: str) -> None:
        """Delete any secret for the target."""


@dataclass(slots=True)
class MemoryCredentialStore:
    """Test and development-only credential store."""

    _secrets: dict[str, str]

    def __init__(self) -> None:
        self._secrets = {}

    def read_secret(self, target: str) -> str | None:
        return self._secrets.get(str(target))

    def write_secret(self, target: str, secret: str) -> None:
        self._secrets[str(target)] = str(secret)

    def delete_secret(self, target: str) -> None:
        self._secrets.pop(str(target), None)


class WindowsCredentialStore:
    """Windows Credential Manager backed store for Nexus API keys.

    The secret value is stored as a generic credential. Public reports should only
    expose the target name, never the credential blob.
    """

    _CRED_TYPE_GENERIC = 1
    _CRED_PERSIST_LOCAL_MACHINE = 2
    _ERROR_NOT_FOUND = 1168
    _MAX_BLOB_BYTES = 5120

    def __init__(self) -> None:
        if os.name != "nt":
            raise CredentialStoreError("Windows Credential Manager is only available on Windows.")
        self._advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self._advapi32.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)),
        ]
        self._advapi32.CredReadW.restype = wintypes.BOOL
        self._advapi32.CredWriteW.argtypes = [
            ctypes.POINTER(_CREDENTIALW),
            wintypes.DWORD,
        ]
        self._advapi32.CredWriteW.restype = wintypes.BOOL
        self._advapi32.CredDeleteW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self._advapi32.CredDeleteW.restype = wintypes.BOOL
        self._advapi32.CredFree.argtypes = [ctypes.c_void_p]
        self._advapi32.CredFree.restype = None

    def read_secret(self, target: str) -> str | None:
        credential = ctypes.POINTER(_CREDENTIALW)()
        ok = self._advapi32.CredReadW(
            wintypes.LPCWSTR(str(target)),
            wintypes.DWORD(self._CRED_TYPE_GENERIC),
            wintypes.DWORD(0),
            ctypes.byref(credential),
        )
        if not ok:
            error = ctypes.get_last_error()
            if error == self._ERROR_NOT_FOUND:
                return None
            raise CredentialStoreError(f"CredReadW failed with Windows error {error}.")
        try:
            raw = ctypes.string_at(
                credential.contents.CredentialBlob,
                credential.contents.CredentialBlobSize,
            )
            return raw.decode("utf-16-le")
        finally:
            self._advapi32.CredFree(credential)

    def write_secret(self, target: str, secret: str) -> None:
        text = str(secret)
        if not text.strip():
            raise CredentialStoreError("Refusing to store an empty secret.")
        blob = text.encode("utf-16-le")
        if len(blob) > self._MAX_BLOB_BYTES:
            raise CredentialStoreError("Secret is too large for Windows Credential Manager.")
        blob_buffer = ctypes.create_string_buffer(blob)
        credential = _CREDENTIALW()
        credential.Type = self._CRED_TYPE_GENERIC
        credential.TargetName = str(target)
        credential.CredentialBlobSize = len(blob)
        credential.CredentialBlob = ctypes.cast(blob_buffer, ctypes.POINTER(wintypes.BYTE))
        credential.Persist = self._CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = "NexusMods API key"
        ok = self._advapi32.CredWriteW(ctypes.byref(credential), wintypes.DWORD(0))
        if not ok:
            error = ctypes.get_last_error()
            raise CredentialStoreError(f"CredWriteW failed with Windows error {error}.")

    def delete_secret(self, target: str) -> None:
        ok = self._advapi32.CredDeleteW(
            wintypes.LPCWSTR(str(target)),
            wintypes.DWORD(self._CRED_TYPE_GENERIC),
            wintypes.DWORD(0),
        )
        if not ok:
            error = ctypes.get_last_error()
            if error == self._ERROR_NOT_FOUND:
                return
            raise CredentialStoreError(f"CredDeleteW failed with Windows error {error}.")


class _FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]


class _CREDENTIAL_ATTRIBUTEW(ctypes.Structure):
    _fields_ = [
        ("Keyword", wintypes.LPWSTR),
        ("Flags", wintypes.DWORD),
        ("ValueSize", wintypes.DWORD),
        ("Value", ctypes.POINTER(wintypes.BYTE)),
    ]


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(wintypes.BYTE)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.POINTER(_CREDENTIAL_ATTRIBUTEW)),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]
