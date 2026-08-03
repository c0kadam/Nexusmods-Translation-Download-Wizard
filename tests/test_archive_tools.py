import hashlib
import json
import subprocess
from pathlib import Path

from modlist_translation_wizard import archive_tools


def test_bundled_archive_tool_is_preferred_and_activated(
    tmp_path: Path,
    monkeypatch,
) -> None:
    seven_zip = _bundled_tool(tmp_path, monkeypatch)
    environment: dict[str, str] = {}

    resolution = archive_tools.activate_archive_tool(
        environ=environment,
        runtime_roots=[tmp_path],
        which=lambda _command: None,
        runner=_successful_runner,
    )

    assert resolution.available is True
    assert resolution.path == str(seven_zip)
    assert resolution.source == "bundled"
    assert resolution.version == "26.02"
    assert environment[archive_tools.SEVEN_ZIP_ENV_VAR] == str(seven_zip)


def test_blocked_bundled_tool_falls_back_to_configured_tool(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bundled = _bundled_tool(tmp_path, monkeypatch)
    configured = tmp_path / "configured" / "7z.exe"
    configured.parent.mkdir()
    configured.write_bytes(b"configured")

    def runner(args, **_kwargs):
        if Path(args[0]) == bundled:
            raise PermissionError("blocked by policy")
        return subprocess.CompletedProcess(
            args,
            0,
            b"7-Zip 25.01 (x64)\n",
            b"",
        )

    resolution = archive_tools.resolve_archive_tool(
        environ={archive_tools.SEVEN_ZIP_ENV_VAR: str(configured)},
        runtime_roots=[tmp_path],
        which=lambda _command: None,
        runner=runner,
    )

    assert resolution.available is True
    assert resolution.path == str(configured)
    assert resolution.source == "environment"
    assert resolution.attempts[0].reason.startswith("launch_failed:PermissionError")


def test_corrupt_bundled_tool_is_rejected_before_launch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    seven_zip = _bundled_tool(tmp_path, monkeypatch)
    seven_zip.write_bytes(b"changed")
    runner_calls = 0

    def runner(args, **_kwargs):
        nonlocal runner_calls
        runner_calls += 1
        return _successful_runner(args)

    resolution = archive_tools.resolve_archive_tool(
        environ={
            "ProgramFiles": str(tmp_path / "missing-program-files"),
            "ProgramFiles(x86)": str(tmp_path / "missing-program-files-x86"),
        },
        runtime_roots=[tmp_path],
        which=lambda _command: None,
        runner=runner,
    )

    assert resolution.available is False
    assert resolution.attempts[0].reason == "bundled_7z_exe_sha256_mismatch"
    assert runner_calls == 0


def test_archive_tool_diagnostic_contains_backend_attempts(tmp_path: Path) -> None:
    resolution = archive_tools.ArchiveToolResolution(
        status="UNAVAILABLE",
        path=None,
        source=None,
        version=None,
        attempts=(
            archive_tools.ArchiveToolAttempt(
                source="bundled",
                path=r"C:\Example\tools\7zip\7z.exe",
                status="REJECTED",
                reason="launch_failed:PermissionError",
            ),
        ),
    )

    path = archive_tools.write_archive_tool_diagnostic(
        tmp_path / "archive_backend.json",
        resolution,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["status"] == "UNAVAILABLE"
    assert payload["attempts"][0]["source"] == "bundled"
    assert payload["attempts"][0]["reason"] == "launch_failed:PermissionError"


def _bundled_tool(tmp_path: Path, monkeypatch) -> Path:
    tool_dir = tmp_path / "tools" / "7zip"
    tool_dir.mkdir(parents=True)
    seven_zip = tool_dir / "7z.exe"
    seven_zip_dll = tool_dir / "7z.dll"
    seven_zip.write_bytes(b"official-exe")
    seven_zip_dll.write_bytes(b"official-dll")
    monkeypatch.setattr(
        archive_tools,
        "_BUNDLED_SHA256",
        {
            "7z.exe": hashlib.sha256(b"official-exe").hexdigest(),
            "7z.dll": hashlib.sha256(b"official-dll").hexdigest(),
        },
    )
    return seven_zip


def _successful_runner(args, **_kwargs):
    return subprocess.CompletedProcess(
        args,
        0,
        b"7-Zip 26.02 (x64)\n",
        b"",
    )
