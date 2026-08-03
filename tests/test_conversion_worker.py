import json
import subprocess
from types import SimpleNamespace
from pathlib import Path

import pytest

from modlist_translation_wizard import conversion_worker
from modlist_translation_wizard.archive_tools import (
    ArchiveToolAttempt,
    ArchiveToolResolution,
)


def test_worker_command_uses_python_module_in_source_runtime(tmp_path: Path, monkeypatch) -> None:
    python = tmp_path / "python.exe"
    python.write_bytes(b"")
    monkeypatch.setattr(conversion_worker.sys, "executable", str(python))
    monkeypatch.setattr(conversion_worker.sys, "argv", ["-m"])

    command = conversion_worker._worker_command(tmp_path / "request.json")

    assert command[:4] == [
        str(python),
        "-B",
        "-m",
        "modlist_translation_wizard",
    ]
    assert command[4:] == ["--convert-worker", str(tmp_path / "request.json")]


def test_worker_command_uses_launcher_exe_in_compiled_runtime(tmp_path: Path, monkeypatch) -> None:
    launcher = tmp_path / "CeviriAraci.exe"
    launcher.write_bytes(b"")
    monkeypatch.setattr(conversion_worker.sys, "argv", [str(launcher)])
    monkeypatch.setattr(conversion_worker.sys, "executable", str(tmp_path / "python.exe"))
    monkeypatch.setattr(conversion_worker, "_is_compiled_runtime", lambda: True)

    command = conversion_worker._worker_command(tmp_path / "request.json")

    assert command == [str(launcher.resolve()), "--convert-worker", str(tmp_path / "request.json")]


def test_plugin_worker_command_uses_launcher_exe_in_compiled_runtime(tmp_path: Path, monkeypatch) -> None:
    launcher = tmp_path / "CeviriAraci.exe"
    launcher.write_bytes(b"")
    monkeypatch.setattr(conversion_worker.sys, "argv", [str(launcher)])
    monkeypatch.setattr(conversion_worker.sys, "executable", str(tmp_path / "python.exe"))
    monkeypatch.setattr(conversion_worker, "_is_compiled_runtime", lambda: True)

    command = conversion_worker._plugin_worker_command()

    assert command == [str(launcher.resolve()), "--plugin-convert-worker"]


def test_conversion_worker_records_and_passes_archive_backend(
    tmp_path: Path,
    monkeypatch,
) -> None:
    seven_zip = tmp_path / "tools" / "7zip" / "7z.exe"
    seven_zip.parent.mkdir(parents=True)
    seven_zip.write_bytes(b"tool")
    resolution = ArchiveToolResolution(
        status="AVAILABLE",
        path=str(seven_zip),
        source="bundled",
        version="26.02",
        attempts=(
            ArchiveToolAttempt(
                source="bundled",
                path=str(seven_zip),
                status="AVAILABLE",
            ),
        ),
    )
    captured = {}
    result_path = tmp_path / "runtime" / "wizard_conversion_result.json"
    output_mod_path = tmp_path / "mods" / "Example - Turkce Ceviri"

    def fake_convert(**kwargs):
        captured["seven_zip_path"] = kwargs["seven_zip_path"]
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text('{"status":"COMPLETED"}\n', encoding="utf-8")
        return SimpleNamespace(
            result_path=result_path,
            conversion=SimpleNamespace(output_mod_path=output_mod_path),
        )

    monkeypatch.setattr(conversion_worker, "activate_archive_tool", lambda: resolution)
    monkeypatch.setattr(
        conversion_worker,
        "convert_downloaded_translations_from_manifest",
        fake_convert,
    )

    worker_dir = tmp_path / "runtime" / "conversion-worker"
    request_path = worker_dir / "request.json"
    request_path.parent.mkdir(parents=True)
    status_path = worker_dir / "status.json"
    request_path.write_text(
        json.dumps(
            {
                "manifest_path": str(tmp_path / "manifest.json"),
                "profile_scan_path": str(tmp_path / "profile.json"),
                "decisions_path": str(tmp_path / "decisions.json"),
                "download_queue_path": str(tmp_path / "queue.json"),
                "out_dir": str(tmp_path / "runtime"),
                "staging_root": str(tmp_path / "mods"),
                "output_mod_name_override": "Example - Turkce Ceviri",
                "allow_profile_drift": True,
                "status_path": str(status_path),
                "progress_status_path": str(worker_dir / "progress.json"),
            }
        ),
        encoding="utf-8",
    )

    exit_code = conversion_worker.run_conversion_worker(request_path)
    status = json.loads(status_path.read_text(encoding="utf-8"))
    diagnostic = json.loads(
        (worker_dir / "archive_backend.json").read_text(encoding="utf-8")
    )

    assert exit_code == 0
    assert captured["seven_zip_path"] == seven_zip
    assert status["ok"] is True
    assert diagnostic["status"] == "AVAILABLE"
    assert diagnostic["source"] == "bundled"


def test_conversion_worker_stops_early_when_external_archive_backend_is_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    resolution = ArchiveToolResolution(
        status="UNAVAILABLE",
        path=None,
        source=None,
        version=None,
        attempts=(
            ArchiveToolAttempt(
                source="bundled",
                path=str(tmp_path / "tools" / "7zip" / "7z.exe"),
                status="REJECTED",
                reason="launch_failed:PermissionError",
            ),
        ),
    )
    monkeypatch.setattr(conversion_worker, "activate_archive_tool", lambda: resolution)

    def fail_if_called(**_kwargs):
        raise AssertionError("conversion should not start without an archive backend")

    monkeypatch.setattr(
        conversion_worker,
        "convert_downloaded_translations_from_manifest",
        fail_if_called,
    )

    worker_dir = tmp_path / "runtime" / "conversion-worker"
    worker_dir.mkdir(parents=True)
    queue_path = tmp_path / "queue.json"
    queue_path.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "status": "READY",
                        "local_archive_path": str(tmp_path / "translation.7z"),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    status_path = worker_dir / "status.json"
    request_path = worker_dir / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "manifest_path": str(tmp_path / "manifest.json"),
                "profile_scan_path": str(tmp_path / "profile.json"),
                "decisions_path": str(tmp_path / "decisions.json"),
                "download_queue_path": str(queue_path),
                "out_dir": str(tmp_path / "runtime"),
                "staging_root": str(tmp_path / "mods"),
                "output_mod_name_override": "Example - Turkce Ceviri",
                "allow_profile_drift": True,
                "status_path": str(status_path),
                "progress_status_path": str(worker_dir / "progress.json"),
            }
        ),
        encoding="utf-8",
    )

    exit_code = conversion_worker.run_conversion_worker(request_path)
    status = json.loads(status_path.read_text(encoding="utf-8"))

    assert exit_code == 1
    assert status["error_type"] == "RuntimeError"
    assert "7-Zip arsiv motoru calistirilamadi" in status["error"]
    assert status["archive_backend_status"] == "UNAVAILABLE"
    assert Path(status["archive_backend_diagnostic"]).is_file()


def test_zip_only_queue_does_not_require_external_archive_tool(tmp_path: Path) -> None:
    queue_path = tmp_path / "queue.json"
    queue_path.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "status": "READY",
                        "local_archive_path": str(tmp_path / "translation.zip"),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    assert conversion_worker._queue_requires_external_archive_tool(queue_path) is False


def test_worker_accepts_success_status_after_abnormal_process_exit(tmp_path: Path, monkeypatch) -> None:
    result = object()

    def fake_run_hidden_worker(args: list[str]) -> subprocess.CompletedProcess[bytes]:
        config_path = Path(args[-1])
        config = json.loads(config_path.read_text(encoding="utf-8"))
        status_path = Path(config["status_path"])
        progress_path = Path(config["progress_status_path"])
        assert progress_path != status_path
        progress_path.write_text(
            json.dumps(
                {
                    "schema_version": "mtw-conversion-worker-status.v1",
                    "ok": False,
                    "stage": "running_archive_conversion",
                    "processed_archives": 1,
                    "total_archives": 2,
                }
            ),
            encoding="utf-8",
        )
        status_path.write_text(
            json.dumps(
                {
                    "schema_version": "mtw-conversion-worker-status.v1",
                    "ok": True,
                    "result_path": str(tmp_path / "result.json"),
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args, -1073740940, b"", b"")

    monkeypatch.setattr(conversion_worker, "_run_hidden_worker", fake_run_hidden_worker)
    monkeypatch.setattr(conversion_worker, "_worker_command", lambda config_path: ["worker.exe", str(config_path)])
    monkeypatch.setattr(conversion_worker, "load_wizard_conversion_result", lambda path: result)

    loaded = conversion_worker.run_conversion_in_worker(
        manifest_path=tmp_path / "manifest.json",
        profile_scan_path=tmp_path / "profile.json",
        decisions_path=tmp_path / "decisions.json",
        download_queue_path=tmp_path / "queue.json",
        out_dir=tmp_path / "runtime",
        staging_root=tmp_path / "mods",
        output_mod_name_override="LoreRim - Turkce Ceviri",
        allow_profile_drift=True,
    )

    assert loaded is result


def test_worker_recovers_completed_result_when_status_file_is_empty(
    tmp_path: Path,
    monkeypatch,
) -> None:
    result_path = tmp_path / "runtime" / "wizard_conversion_result.json"
    result = SimpleNamespace(result_payload={"status": "COMPLETED"})

    def fake_run_hidden_worker(args: list[str]) -> subprocess.CompletedProcess[bytes]:
        config_path = Path(args[-1])
        config = json.loads(config_path.read_text(encoding="utf-8"))
        status_path = Path(config["status_path"])
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text('{"status": "COMPLETED"}\n', encoding="utf-8")
        status_path.write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(args, 3, b"", b"")

    monkeypatch.setattr(conversion_worker, "_run_hidden_worker", fake_run_hidden_worker)
    monkeypatch.setattr(
        conversion_worker,
        "_worker_command",
        lambda config_path: ["worker.exe", str(config_path)],
    )
    monkeypatch.setattr(conversion_worker, "load_wizard_conversion_result", lambda path: result)

    loaded = conversion_worker.run_conversion_in_worker(
        manifest_path=tmp_path / "manifest.json",
        profile_scan_path=tmp_path / "profile.json",
        decisions_path=tmp_path / "decisions.json",
        download_queue_path=tmp_path / "queue.json",
        out_dir=tmp_path / "runtime",
        staging_root=tmp_path / "mods",
        output_mod_name_override="LoreRim - Turkce Ceviri",
        allow_profile_drift=True,
    )

    assert loaded is result


def test_worker_does_not_recover_stale_result_after_failed_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stale_result = tmp_path / "runtime" / "wizard_conversion_result.json"
    stale_result.parent.mkdir(parents=True, exist_ok=True)
    stale_result.write_text('{"status": "COMPLETED"}\n', encoding="utf-8")

    def fake_run_hidden_worker(args: list[str]) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args, 3, b"", b"")

    monkeypatch.setattr(conversion_worker, "_run_hidden_worker", fake_run_hidden_worker)
    monkeypatch.setattr(
        conversion_worker,
        "_worker_command",
        lambda config_path: ["worker.exe", str(config_path)],
    )
    monkeypatch.setattr(conversion_worker.time, "sleep", lambda _seconds: None)

    try:
        conversion_worker.run_conversion_in_worker(
            manifest_path=tmp_path / "manifest.json",
            profile_scan_path=tmp_path / "profile.json",
            decisions_path=tmp_path / "decisions.json",
            download_queue_path=tmp_path / "queue.json",
            out_dir=tmp_path / "runtime",
            staging_root=tmp_path / "mods",
            output_mod_name_override="LoreRim - Turkce Ceviri",
            allow_profile_drift=True,
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("stale result should not be accepted")


def test_worker_retries_once_after_unreported_process_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    result = object()
    calls = 0

    def fake_run_hidden_worker(args: list[str]) -> subprocess.CompletedProcess[bytes]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(args, 3, b"", b"")
        config = json.loads(Path(args[-1]).read_text(encoding="utf-8"))
        Path(config["status_path"]).write_text(
            json.dumps(
                {
                    "schema_version": "mtw-conversion-worker-status.v1",
                    "ok": True,
                    "result_path": str(tmp_path / "result.json"),
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(conversion_worker, "_run_hidden_worker", fake_run_hidden_worker)
    monkeypatch.setattr(
        conversion_worker,
        "_worker_command",
        lambda config_path: ["worker.exe", str(config_path)],
    )
    monkeypatch.setattr(conversion_worker, "load_wizard_conversion_result", lambda _path: result)
    monkeypatch.setattr(conversion_worker.time, "sleep", lambda _seconds: None)

    loaded = conversion_worker.run_conversion_in_worker(
        manifest_path=tmp_path / "manifest.json",
        profile_scan_path=tmp_path / "profile.json",
        decisions_path=tmp_path / "decisions.json",
        download_queue_path=tmp_path / "queue.json",
        out_dir=tmp_path / "runtime",
        staging_root=tmp_path / "mods",
        output_mod_name_override="LoreRim - Turkce Ceviri",
        allow_profile_drift=True,
    )

    diagnostics = json.loads(
        (tmp_path / "runtime" / "conversion-worker" / "last_failed_worker.json").read_text(
            encoding="utf-8"
        )
    )
    assert loaded is result
    assert calls == 2
    assert diagnostics["attempt"] == 1
    assert diagnostics["returncode"] == 3
    assert diagnostics["retryable"] is True


def test_worker_does_not_retry_reported_conversion_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls = 0

    def fake_run_hidden_worker(args: list[str]) -> subprocess.CompletedProcess[bytes]:
        nonlocal calls
        calls += 1
        config = json.loads(Path(args[-1]).read_text(encoding="utf-8"))
        Path(config["status_path"]).write_text(
            json.dumps(
                {
                    "schema_version": "mtw-conversion-worker-status.v1",
                    "ok": False,
                    "error_type": "PermissionError",
                    "error": "output is locked",
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args, 1, b"", b"")

    monkeypatch.setattr(conversion_worker, "_run_hidden_worker", fake_run_hidden_worker)
    monkeypatch.setattr(
        conversion_worker,
        "_worker_command",
        lambda config_path: ["worker.exe", str(config_path)],
    )

    with pytest.raises(RuntimeError, match="Deneme: 1/2"):
        conversion_worker.run_conversion_in_worker(
            manifest_path=tmp_path / "manifest.json",
            profile_scan_path=tmp_path / "profile.json",
            decisions_path=tmp_path / "decisions.json",
            download_queue_path=tmp_path / "queue.json",
            out_dir=tmp_path / "runtime",
            staging_root=tmp_path / "mods",
            output_mod_name_override="LoreRim - Turkce Ceviri",
            allow_profile_drift=True,
        )

    diagnostics = json.loads(
        (tmp_path / "runtime" / "conversion-worker" / "last_failed_worker.json").read_text(
            encoding="utf-8"
        )
    )
    assert calls == 1
    assert diagnostics["retryable"] is False
