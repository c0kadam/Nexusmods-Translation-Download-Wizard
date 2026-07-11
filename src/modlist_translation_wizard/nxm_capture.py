"""Temporary Windows NXM protocol capture for the running wizard."""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

try:
    import winreg
except ImportError:  # pragma: no cover - Windows-only production feature.
    winreg = None  # type: ignore[assignment]

_MAX_MESSAGE_BYTES = 16 * 1024
_REGISTRY_PROTOCOL_PATH = r"Software\Classes\nxm"
_REGISTRY_COMMAND_PATH = rf"{_REGISTRY_PROTOCOL_PATH}\shell\open\command"


class NxmCaptureError(RuntimeError):
    """Raised when the temporary NXM capture cannot be established."""


@dataclass(frozen=True, slots=True)
class NxmBindingStatus:
    active: bool
    installed_command: str
    previous_command: str | None


class NxmCaptureServer:
    def __init__(
        self,
        callback: Callable[[str], None],
        *,
        state_path: Path | str | None = None,
    ) -> None:
        self._callback = callback
        self.state_path = Path(state_path) if state_path else nxm_capture_state_path()
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._token: str | None = None

    @property
    def active(self) -> bool:
        return self._socket is not None and self._thread is not None

    def start(self) -> None:
        if self.active:
            return
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        listener.settimeout(0.25)
        self._socket = listener
        self._token = secrets.token_urlsafe(32)
        state = {
            "schema_version": "mtw-nxm-capture-session.v1",
            "host": "127.0.0.1",
            "port": int(listener.getsockname()[1]),
            "token": self._token,
            "pid": os.getpid(),
        }
        _write_private_json(self.state_path, state)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._listen,
            name="mtw-nxm-capture",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        listener = self._socket
        self._socket = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        self._remove_owned_state()
        self._token = None

    def _listen(self) -> None:
        while not self._stop_event.is_set():
            listener = self._socket
            if listener is None:
                return
            try:
                connection, _address = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(2.0)
                try:
                    payload = _receive_message(connection)
                    if payload.get("token") != self._token:
                        raise NxmCaptureError("invalid capture session")
                    nxm_url = _validated_nxm_url(payload.get("nxm_url"))
                    self._callback(nxm_url)
                    _send_reply(connection, "ok")
                except Exception:
                    _send_reply(connection, "rejected")

    def _remove_owned_state(self) -> None:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return
        if payload.get("token") == self._token:
            self.state_path.unlink(missing_ok=True)


class WindowsNxmProtocolBinding:
    def __init__(
        self,
        *,
        command: str | None = None,
        backup_path: Path | str | None = None,
    ) -> None:
        self.command = command or nxm_bridge_command()
        self.backup_path = (
            Path(backup_path) if backup_path else nxm_handler_backup_path()
        )

    def bind(self) -> NxmBindingStatus:
        _require_windows_registry()
        self.recover_stale_binding()
        previous_exists, previous_command = _read_registry_command()
        backup = {
            "schema_version": "mtw-nxm-handler-backup.v1",
            "previous_command_existed": previous_exists,
            "previous_command": previous_command,
            "installed_command": self.command,
        }
        _write_private_json(self.backup_path, backup)
        _write_registry_command(self.command)
        return NxmBindingStatus(
            active=True,
            installed_command=self.command,
            previous_command=previous_command,
        )

    def restore(self) -> bool:
        _require_windows_registry()
        backup = self._read_backup()
        if backup is None:
            return False
        current_exists, current_command = _read_registry_command()
        installed_command = str(backup.get("installed_command") or "")
        if not current_exists or current_command != installed_command:
            self.backup_path.unlink(missing_ok=True)
            return False
        if backup.get("previous_command_existed"):
            _write_registry_command(str(backup.get("previous_command") or ""))
        else:
            _delete_registry_command()
        self.backup_path.unlink(missing_ok=True)
        return True

    def recover_stale_binding(self) -> bool:
        backup = self._read_backup()
        if backup is None:
            return False
        current_exists, current_command = _read_registry_command()
        installed_command = str(backup.get("installed_command") or "")
        if current_exists and current_command == installed_command:
            if backup.get("previous_command_existed"):
                _write_registry_command(str(backup.get("previous_command") or ""))
            else:
                _delete_registry_command()
            self.backup_path.unlink(missing_ok=True)
            return True
        self.backup_path.unlink(missing_ok=True)
        return False

    def _read_backup(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.backup_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None


def forward_nxm_to_running_wizard(
    nxm_url: str,
    *,
    state_path: Path | str | None = None,
    timeout_seconds: float = 2.0,
) -> bool:
    safe_url = _validated_nxm_url(nxm_url)
    session_path = Path(state_path) if state_path else nxm_capture_state_path()
    try:
        state = json.loads(session_path.read_text(encoding="utf-8"))
        host = str(state["host"])
        port = int(state["port"])
        token = str(state["token"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
    if host != "127.0.0.1" or not (1 <= port <= 65535) or not token:
        return False
    message = json.dumps(
        {"token": token, "nxm_url": safe_url},
        ensure_ascii=True,
    ).encode("utf-8") + b"\n"
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds) as connection:
            connection.settimeout(timeout_seconds)
            connection.sendall(message)
            reply = _receive_message(connection)
    except OSError:
        return False
    return reply.get("status") == "ok"


def launch_previous_nxm_handler(
    nxm_url: str,
    *,
    backup_path: Path | str | None = None,
) -> bool:
    safe_url = _validated_nxm_url(nxm_url)
    path = Path(backup_path) if backup_path else nxm_handler_backup_path()
    try:
        backup = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if not backup.get("previous_command_existed"):
        return False
    command = str(backup.get("previous_command") or "").strip()
    if not command:
        return False
    quoted_url = f'"{safe_url}"'
    rendered = command.replace('"%1"', quoted_url).replace("%1", quoted_url)
    if rendered == command:
        rendered = f"{command} {quoted_url}"
    try:
        subprocess.Popen(rendered, close_fds=True)
    except OSError:
        return False
    return True


def nxm_bridge_command() -> str:
    if _running_as_standalone_executable():
        executable = _standalone_launcher_executable()
        return f'"{executable}" "--nxm-bridge" "%1"'
    executable = Path(sys.executable)
    pythonw = executable.with_name("pythonw.exe")
    if pythonw.is_file():
        executable = pythonw
    bridge_script = Path(__file__).with_name("nxm_bridge.py").resolve()
    return f'"{executable}" "{bridge_script}" "%1"'


def _running_as_standalone_executable() -> bool:
    executable_name = Path(sys.executable).name.casefold()
    return bool(
        getattr(sys, "frozen", False)
        or globals().get("__compiled__") is not None
        or executable_name not in {"python.exe", "pythonw.exe"}
    )


def _standalone_launcher_executable() -> Path:
    argv_executable = Path(sys.argv[0])
    if (
        argv_executable.suffix.casefold() == ".exe"
        and argv_executable.name.casefold() not in {"python.exe", "pythonw.exe"}
    ):
        return argv_executable.resolve()
    return Path(sys.executable)


def nxm_capture_state_path() -> Path:
    return _application_data_dir() / "nxm-capture-session.json"


def nxm_handler_backup_path() -> Path:
    return _application_data_dir() / "nxm-handler-backup.json"


def _application_data_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    root = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return root / "Modlist Translation Wizard"


def _validated_nxm_url(value: object) -> str:
    text = str(value or "").strip()
    if not text.casefold().startswith("nxm://"):
        raise NxmCaptureError("invalid NXM protocol URL")
    if any(character in text for character in {'"', "\r", "\n", "\x00"}):
        raise NxmCaptureError("unsafe NXM protocol URL")
    if len(text.encode("utf-8")) > _MAX_MESSAGE_BYTES // 2:
        raise NxmCaptureError("NXM protocol URL is too long")
    return text


def _receive_message(connection: socket.socket) -> dict[str, Any]:
    chunks = bytearray()
    while len(chunks) <= _MAX_MESSAGE_BYTES:
        part = connection.recv(min(4096, _MAX_MESSAGE_BYTES - len(chunks) + 1))
        if not part:
            break
        chunks.extend(part)
        if b"\n" in part:
            break
    if len(chunks) > _MAX_MESSAGE_BYTES:
        raise NxmCaptureError("capture message too large")
    line = bytes(chunks).split(b"\n", 1)[0]
    payload = json.loads(line.decode("utf-8"))
    if not isinstance(payload, dict):
        raise NxmCaptureError("capture message must be an object")
    return payload


def _send_reply(connection: socket.socket, status: str) -> None:
    try:
        connection.sendall(
            json.dumps({"status": status}, ensure_ascii=True).encode("ascii") + b"\n"
        )
    except OSError:
        pass


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    temporary.replace(path)


def _require_windows_registry() -> None:
    if winreg is None:
        raise NxmCaptureError("NXM protocol capture is available only on Windows.")


def _read_registry_command() -> tuple[bool, str | None]:
    _require_windows_registry()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REGISTRY_COMMAND_PATH) as key:
            value, _value_type = winreg.QueryValueEx(key, "")
    except FileNotFoundError:
        return False, None
    return True, str(value)


def _write_registry_command(command: str) -> None:
    _require_windows_registry()
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _REGISTRY_PROTOCOL_PATH) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:nxm")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _REGISTRY_COMMAND_PATH) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)


def _delete_registry_command() -> None:
    _require_windows_registry()
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, _REGISTRY_COMMAND_PATH)
    except FileNotFoundError:
        pass
