import json
import threading

from modlist_translation_wizard import __main__ as wizard_main
from modlist_translation_wizard import nxm_bridge
from modlist_translation_wizard import nxm_capture
from modlist_translation_wizard.nxm_capture import (
    NxmCaptureServer,
    WindowsNxmProtocolBinding,
    forward_nxm_to_running_wizard,
)


def test_capture_server_forwards_nxm_url_without_persisting_it(tmp_path) -> None:
    captured: list[str] = []
    received = threading.Event()
    state_path = tmp_path / "capture.json"

    def callback(value: str) -> None:
        captured.append(value)
        received.set()

    server = NxmCaptureServer(callback, state_path=state_path)
    server.start()
    nxm_url = (
        "nxm://skyrimspecialedition/mods/333/files/444"
        "?key=temporary-secret&expires=2000000000"
    )
    try:
        assert forward_nxm_to_running_wizard(
            nxm_url,
            state_path=state_path,
        )
        assert received.wait(2)
        assert captured == [nxm_url]
        assert "temporary-secret" not in state_path.read_text(encoding="utf-8")
    finally:
        server.stop()

    assert not state_path.exists()


def test_windows_binding_restores_previous_handler(tmp_path, monkeypatch) -> None:
    registry = {"exists": True, "command": '"Vortex.exe" "-d" "%1"'}

    monkeypatch.setattr(nxm_capture, "_require_windows_registry", lambda: None)
    monkeypatch.setattr(
        nxm_capture,
        "_read_registry_command",
        lambda: (registry["exists"], registry["command"]),
    )

    def write(command: str) -> None:
        registry["exists"] = True
        registry["command"] = command

    def delete() -> None:
        registry["exists"] = False
        registry["command"] = None

    monkeypatch.setattr(nxm_capture, "_write_registry_command", write)
    monkeypatch.setattr(nxm_capture, "_delete_registry_command", delete)
    binding = WindowsNxmProtocolBinding(
        command='"pythonw.exe" "nxm_bridge.py" "%1"',
        backup_path=tmp_path / "backup.json",
    )

    status = binding.bind()

    assert status.previous_command == '"Vortex.exe" "-d" "%1"'
    assert registry["command"] == binding.command
    backup = json.loads(binding.backup_path.read_text(encoding="utf-8"))
    assert backup["previous_command"] == status.previous_command

    assert binding.restore() is True
    assert registry["command"] == status.previous_command
    assert not binding.backup_path.exists()


def test_nxm_bridge_command_uses_main_executable_when_frozen(monkeypatch) -> None:
    monkeypatch.setattr(nxm_capture.sys, "frozen", True, raising=False)
    monkeypatch.setattr(nxm_capture.sys, "executable", r"FixtureApp\CeviriAraci.exe")

    command = nxm_capture.nxm_bridge_command()

    assert command == r'"FixtureApp\CeviriAraci.exe" "--nxm-bridge" "%1"'


def test_nxm_bridge_command_uses_main_executable_when_nuitka_compiled(monkeypatch) -> None:
    monkeypatch.setattr(nxm_capture, "__compiled__", object(), raising=False)
    monkeypatch.setattr(nxm_capture.sys, "executable", r"FixtureApp\CeviriAraci.exe")

    command = nxm_capture.nxm_bridge_command()

    assert command == r'"FixtureApp\CeviriAraci.exe" "--nxm-bridge" "%1"'


def test_nxm_bridge_command_uses_non_python_executable_as_standalone(monkeypatch) -> None:
    monkeypatch.setattr(nxm_capture.sys, "executable", r"FixtureApp\CeviriAraci.exe")

    command = nxm_capture.nxm_bridge_command()

    assert command == r'"FixtureApp\CeviriAraci.exe" "--nxm-bridge" "%1"'


def test_nxm_bridge_command_prefers_launcher_exe_when_nuitka_reports_python(
    tmp_path,
    monkeypatch,
) -> None:
    launcher = tmp_path / "CeviriAraci.exe"
    monkeypatch.setattr(nxm_capture, "__compiled__", object(), raising=False)
    monkeypatch.setattr(nxm_capture.sys, "executable", r"FixtureApp\python.exe")
    monkeypatch.setattr(nxm_capture.sys, "argv", [str(launcher)])

    command = nxm_capture.nxm_bridge_command()

    assert command == f'"{launcher.resolve()}" "--nxm-bridge" "%1"'


def test_nxm_bridge_diagnostic_never_persists_temporary_key(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(nxm_bridge, "_application_data_dir", lambda: tmp_path)

    nxm_bridge._write_bridge_diagnostic(
        "forwarded_to_running_wizard",
        (
            "nxm://skyrimspecialedition/mods/333/files/444"
            "?key=temporary-secret&expires=2000000000"
        ),
    )

    payload = json.loads((tmp_path / "nxm-bridge-last.json").read_text(encoding="utf-8"))
    serialized = json.dumps(payload)
    assert "temporary-secret" not in serialized
    assert payload["status"] == "forwarded_to_running_wizard"
    assert payload["nxm"]["mod_id"] == "333"
    assert payload["nxm"]["file_id"] == "444"
    assert payload["nxm"]["has_key"] is True


def test_main_routes_legacy_frozen_bridge_arguments(monkeypatch) -> None:
    captured: list[list[str]] = []
    monkeypatch.setattr(
        wizard_main,
        "nxm_bridge_main",
        lambda args: captured.append(list(args)) or 0,
    )
    monkeypatch.setattr(
        wizard_main.sys,
        "argv",
        [
            r"FixtureApp\CeviriAraci.exe",
            r"FixtureApp\modlist_translation_wizard\nxm_bridge.py",
            "nxm://skyrimspecialedition/mods/1/files/2?key=k&expires=2000000000",
        ],
    )

    assert wizard_main.main() == 0
    assert captured == [
        ["nxm://skyrimspecialedition/mods/1/files/2?key=k&expires=2000000000"]
    ]


def test_stale_binding_is_recovered_after_unclean_shutdown(
    tmp_path,
    monkeypatch,
) -> None:
    installed = '"pythonw.exe" "nxm_bridge.py" "%1"'
    previous = '"Vortex.exe" "-d" "%1"'
    registry = {"exists": True, "command": installed}
    backup_path = tmp_path / "backup.json"
    backup_path.write_text(
        json.dumps(
            {
                "previous_command_existed": True,
                "previous_command": previous,
                "installed_command": installed,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(nxm_capture, "_require_windows_registry", lambda: None)
    monkeypatch.setattr(
        nxm_capture,
        "_read_registry_command",
        lambda: (registry["exists"], registry["command"]),
    )
    monkeypatch.setattr(
        nxm_capture,
        "_write_registry_command",
        lambda command: registry.update(exists=True, command=command),
    )
    monkeypatch.setattr(
        nxm_capture,
        "_delete_registry_command",
        lambda: registry.update(exists=False, command=None),
    )
    binding = WindowsNxmProtocolBinding(
        command=installed,
        backup_path=backup_path,
    )

    assert binding.recover_stale_binding() is True
    assert registry["command"] == previous
    assert not backup_path.exists()
