import json
import subprocess
from types import SimpleNamespace
from pathlib import Path

from modlist_translation_wizard import conversion_worker


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
