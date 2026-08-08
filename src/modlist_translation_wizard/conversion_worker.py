"""Hidden conversion worker used by the GUI build.

The archive conversion path can exercise native libraries and thousands of
external 7-Zip process calls. Running it in a child process keeps the installer
GUI alive even if the compiled worker process terminates unexpectedly.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from modlist_translate_tool.dsd.dynamic_string_converter import (
    PLUGIN_CONVERSION_WORKER_COMMAND_ENV,
)
from modlist_translation_wizard.archive_tools import (
    activate_archive_tool,
    write_archive_tool_diagnostic,
)
from modlist_translation_wizard.runtime import (
    WizardConversionResult,
    convert_downloaded_translations_from_manifest,
    load_wizard_conversion_result,
)

_MAX_WORKER_ATTEMPTS = 2
_WORKER_RETRY_DELAY_SECONDS = 0.75
_DEDICATED_WORKER_EXE_NAME = "CeviriWorker.exe"
_WINDOWS_NATIVE_FAILURES = {
    0xC0000005: "STATUS_ACCESS_VIOLATION",
    0xC0000017: "STATUS_NO_MEMORY",
    0xC00000FD: "STATUS_STACK_OVERFLOW",
    0xC0000374: "STATUS_HEAP_CORRUPTION",
    0xC0000409: "STATUS_STACK_BUFFER_OVERRUN",
}


def run_conversion_in_worker(
    *,
    manifest_path: Path | str,
    profile_scan_path: Path | str,
    decisions_path: Path | str,
    download_queue_path: Path | str,
    out_dir: Path | str,
    staging_root: Path | str,
    output_mod_name_override: str,
    allow_profile_drift: bool,
) -> WizardConversionResult:
    output_dir = Path(out_dir)
    worker_dir = output_dir / "conversion-worker"
    worker_dir.mkdir(parents=True, exist_ok=True)
    config_path = worker_dir / "request.json"
    status_path = worker_dir / "status.json"
    progress_path = worker_dir / "progress.json"
    result_path = output_dir / "wizard_conversion_result.json"
    payload = {
        "schema_version": "mtw-conversion-worker-request.v1",
        "manifest_path": str(manifest_path),
        "profile_scan_path": str(profile_scan_path),
        "decisions_path": str(decisions_path),
        "download_queue_path": str(download_queue_path),
        "out_dir": str(output_dir),
        "staging_root": str(staging_root),
        "output_mod_name_override": str(output_mod_name_override),
        "allow_profile_drift": bool(allow_profile_drift),
        "status_path": str(status_path),
        "progress_status_path": str(progress_path),
    }

    for attempt in range(1, _MAX_WORKER_ATTEMPTS + 1):
        for path in (status_path, progress_path, result_path):
            path.unlink(missing_ok=True)
        payload["attempt"] = attempt
        config_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        completed = _run_hidden_worker(_worker_command(config_path))
        status = _read_status(status_path)
        if status.get("ok") is True:
            result_text = str(status.get("result_path") or result_path)
            return load_wizard_conversion_result(result_text)

        recovered = _load_completed_result_if_available(result_path)
        if recovered is not None:
            return recovered

        diagnostics_path = _preserve_worker_failure(
            worker_dir=worker_dir,
            status_path=status_path,
            progress_path=progress_path,
            completed=completed,
            attempt=attempt,
        )
        if attempt < _MAX_WORKER_ATTEMPTS and _is_retryable_worker_failure(
            completed=completed,
            status=status,
        ):
            time.sleep(_WORKER_RETRY_DELAY_SECONDS)
            continue

        raise RuntimeError(
            _worker_failure_message(
                completed=completed,
                status=status,
                status_path=status_path,
                progress_path=progress_path,
                diagnostics_path=diagnostics_path,
                attempt=attempt,
            )
        )

    raise AssertionError("conversion worker attempt loop ended unexpectedly")


def run_conversion_worker(config_path: Path | str) -> int:
    config = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
    status_path = Path(str(config["status_path"]))
    progress_status_path = Path(str(config.get("progress_status_path") or status_path))
    status_path.parent.mkdir(parents=True, exist_ok=True)
    _write_status(
        status_path,
        {
            "schema_version": "mtw-conversion-worker-status.v1",
            "ok": False,
            "created_at": _now(),
            "stage": "started",
        },
    )
    previous_plugin_worker_command = os.environ.get(PLUGIN_CONVERSION_WORKER_COMMAND_ENV)
    archive_backend_status: dict[str, Any] = {}
    try:
        os.environ[PLUGIN_CONVERSION_WORKER_COMMAND_ENV] = json.dumps(_plugin_worker_command())
        archive_tool = activate_archive_tool()
        archive_tool_diagnostic_path = write_archive_tool_diagnostic(
            status_path.parent / "archive_backend.json",
            archive_tool,
        )
        archive_backend_status = {
            "archive_backend_status": archive_tool.status,
            "archive_backend_source": archive_tool.source,
            "archive_backend_version": archive_tool.version,
            "archive_backend_diagnostic": str(archive_tool_diagnostic_path),
        }
        if (
            not archive_tool.available
            and _queue_requires_external_archive_tool(
                Path(str(config["download_queue_path"]))
            )
        ):
            raise RuntimeError(
                "7-Zip arsiv motoru calistirilamadi. Paket icindeki arac guvenlik "
                "yazilimi tarafindan engellenmis olabilir ve kullanilabilir bir sistem "
                f"7-Zip kurulumu bulunamadi. Tani: {archive_tool_diagnostic_path}"
            )
        _write_status(
            status_path,
            {
                "schema_version": "mtw-conversion-worker-status.v1",
                "ok": False,
                "created_at": _now(),
                "stage": "running_conversion",
                **archive_backend_status,
            },
        )
        result = convert_downloaded_translations_from_manifest(
            manifest_path=Path(str(config["manifest_path"])),
            profile_scan_path=Path(str(config["profile_scan_path"])),
            decisions_path=Path(str(config["decisions_path"])),
            download_queue_path=Path(str(config["download_queue_path"])),
            out_dir=Path(str(config["out_dir"])),
            staging_root=Path(str(config["staging_root"])),
            output_mod_name_override=str(config["output_mod_name_override"]),
            allow_profile_drift=bool(config["allow_profile_drift"]),
            progress_status_path=progress_status_path,
            seven_zip_path=Path(archive_tool.path) if archive_tool.path else None,
        )
    except BaseException as exc:  # noqa: BLE001 - child process boundary must report SystemExit too.
        _write_status(
            status_path,
            {
                "schema_version": "mtw-conversion-worker-status.v1",
                "ok": False,
                "created_at": _now(),
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(),
                **archive_backend_status,
            },
        )
        return 1
    finally:
        if previous_plugin_worker_command is None:
            os.environ.pop(PLUGIN_CONVERSION_WORKER_COMMAND_ENV, None)
        else:
            os.environ[PLUGIN_CONVERSION_WORKER_COMMAND_ENV] = previous_plugin_worker_command

    _write_status(
        status_path,
        {
            "schema_version": "mtw-conversion-worker-status.v1",
            "ok": True,
            "created_at": _now(),
            "result_path": str(result.result_path),
            "output_mod_path": str(result.conversion.output_mod_path),
            **archive_backend_status,
        },
    )
    return 0


def _worker_command(config_path: Path) -> list[str]:
    launcher = _standalone_worker_launcher()
    if launcher is not None:
        return [str(launcher), "--convert-worker", str(config_path)]
    return [
        str(Path(sys.executable)),
        "-B",
        "-m",
        "modlist_translation_wizard",
        "--convert-worker",
        str(config_path),
    ]


def _plugin_worker_command() -> list[str]:
    launcher = _standalone_worker_launcher()
    if launcher is not None:
        return [str(launcher), "--plugin-convert-worker"]
    return [
        str(Path(sys.executable)),
        "-B",
        "-m",
        "modlist_translation_wizard",
        "--plugin-convert-worker",
    ]


def _standalone_worker_launcher() -> Path | None:
    launcher = _standalone_launcher()
    if launcher is None:
        return None

    dedicated_worker = launcher.with_name(_DEDICATED_WORKER_EXE_NAME)
    if dedicated_worker.is_file():
        return dedicated_worker
    return launcher


def _standalone_launcher() -> Path | None:
    compiled = _is_compiled_runtime()
    argv0 = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if argv0 is not None:
        try:
            resolved = argv0.resolve()
        except OSError:
            resolved = None
        if (
            resolved is not None
            and resolved.is_file()
            and resolved.suffix.casefold() == ".exe"
            and (compiled or resolved.name.casefold() not in {"python.exe", "pythonw.exe"})
        ):
            return resolved
    if not compiled:
        return None

    candidates = []
    if sys.argv and sys.argv[0]:
        candidates.append(Path(sys.argv[0]))
    candidates.append(Path(sys.executable))
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_file() and resolved.suffix.casefold() == ".exe":
            return resolved
    return None


def _is_compiled_runtime() -> bool:
    return bool(getattr(sys, "frozen", False) or globals().get("__compiled__") is not None)


def _run_hidden_worker(args: list[str]) -> subprocess.CompletedProcess[bytes]:
    kwargs: dict[str, Any] = {
        "check": False,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = startupinfo
    return subprocess.run(args, **kwargs)


def _read_status(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _is_retryable_worker_failure(
    *,
    completed: subprocess.CompletedProcess[bytes],
    status: dict[str, Any],
) -> bool:
    if status.get("ok") is True:
        return False
    if str(status.get("error_type") or "").strip():
        return False
    if str(status.get("error") or "").strip():
        return False
    return True


def _preserve_worker_failure(
    *,
    worker_dir: Path,
    status_path: Path,
    progress_path: Path,
    completed: subprocess.CompletedProcess[bytes],
    attempt: int,
) -> Path:
    snapshots = (
        (status_path, worker_dir / "last_failed_status.json"),
        (progress_path, worker_dir / "last_failed_progress.json"),
    )
    for source, destination in snapshots:
        if source.is_file():
            shutil.copy2(source, destination)
        else:
            destination.unlink(missing_ok=True)

    stderr_path = worker_dir / "last_failed_stderr.log"
    stderr = _decode_output(completed.stderr).strip()
    if stderr:
        stderr_path.write_text(stderr[-8000:] + "\n", encoding="utf-8")
    else:
        stderr_path.unlink(missing_ok=True)

    diagnostics_path = worker_dir / "last_failed_worker.json"
    progress = _read_status(progress_path)
    native_failure = _windows_native_failure(completed.returncode)
    _write_json_atomic(
        diagnostics_path,
        {
            "schema_version": "mtw-conversion-worker-failure.v1",
            "created_at": _now(),
            "attempt": attempt,
            "max_attempts": _MAX_WORKER_ATTEMPTS,
            "returncode": completed.returncode,
            "native_failure": native_failure,
            "retryable": _is_retryable_worker_failure(
                completed=completed,
                status=_read_status(status_path),
            ),
            "status_snapshot": str(snapshots[0][1]) if snapshots[0][1].is_file() else None,
            "progress_snapshot": str(snapshots[1][1]) if snapshots[1][1].is_file() else None,
            "stderr_snapshot": str(stderr_path) if stderr_path.is_file() else None,
            "last_progress": progress or None,
        },
    )
    return diagnostics_path


def _worker_failure_message(
    *,
    completed: subprocess.CompletedProcess[bytes],
    status: dict[str, Any],
    status_path: Path,
    progress_path: Path,
    diagnostics_path: Path,
    attempt: int,
) -> str:
    error = str(status.get("error") or "").strip()
    error_type = str(status.get("error_type") or "WorkerFailed")
    details = f"{error_type}: {error}" if error else error_type
    stderr = _decode_output(completed.stderr).strip()
    if stderr:
        details = f"{details}\n\nWorker stderr:\n{stderr[-2000:]}"
    archive_backend_diagnostic = str(
        status.get("archive_backend_diagnostic") or ""
    ).strip()
    archive_backend_line = (
        f"\nArsiv backend tanisi: {archive_backend_diagnostic}"
        if archive_backend_diagnostic
        else ""
    )
    progress = _read_status(progress_path)
    progress_lines = _worker_progress_lines(progress)
    native_failure = _windows_native_failure(completed.returncode)
    native_failure_line = (
        f"\nYerel Windows hatasi: {native_failure['hex_code']} "
        f"{native_failure['name']}"
        if native_failure is not None
        else ""
    )
    return (
        "Ceviri hazirlama islemi ayri worker process icinde basarisiz oldu.\n"
        f"Cikis kodu: {completed.returncode}\n"
        f"Deneme: {attempt}/{_MAX_WORKER_ATTEMPTS}\n"
        f"Detay: {details}\n"
        f"Worker durum dosyasi: {status_path}\n"
        f"Ilerleme dosyasi: {progress_path}\n"
        f"Korunan hata tanisi: {diagnostics_path}"
        f"{archive_backend_line}"
        f"{native_failure_line}"
        f"{progress_lines}"
    )


def _windows_native_failure(returncode: int) -> dict[str, str] | None:
    if os.name != "nt" or returncode == 0:
        return None
    unsigned_code = int(returncode) & 0xFFFFFFFF
    name = _WINDOWS_NATIVE_FAILURES.get(unsigned_code)
    if name is None:
        return None
    return {
        "hex_code": f"0x{unsigned_code:08X}",
        "name": name,
    }


def _worker_progress_lines(progress: dict[str, Any]) -> str:
    if not progress:
        return ""
    stage = str(progress.get("converter_stage") or progress.get("stage") or "").strip()
    lines = [f"Son asama: {stage}"] if stage else []
    processed = progress.get("processed_archives")
    total = progress.get("total_archives")
    if processed is not None or total is not None:
        lines.append(f"Arsiv ilerlemesi: {processed or 0}/{total or '?'}")
    mod_id = progress.get("translation_nexus_mod_id")
    file_id = progress.get("translation_file_id")
    if mod_id is not None or file_id is not None:
        lines.append(f"Nexus hedefi: {mod_id or '?'}/{file_id or '?'}")
    archive_path = str(progress.get("archive_path") or "").strip()
    if archive_path:
        lines.append(f"Son arsiv: {archive_path}")
    member_path = str(progress.get("member_path") or "").strip()
    if member_path:
        lines.append(f"Son dosya: {member_path}")
    return "\n" + "\n".join(lines) if lines else ""


def _write_status(path: Path, payload: dict[str, Any]) -> None:
    _write_json_atomic(path, payload)


def _load_completed_result_if_available(path: Path) -> WizardConversionResult | None:
    if not path.is_file() or path.stat().st_size <= 0:
        return None
    try:
        result = load_wizard_conversion_result(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    status = str(result.result_payload.get("status") or "").upper()
    if status in {"COMPLETED", "COMPLETED_WITH_FAILURES"}:
        return result
    return None


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _decode_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return value.decode("utf-8", errors="replace")


def _queue_requires_external_archive_tool(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, dict):
            continue
        archive_path = str(item.get("local_archive_path") or "").strip()
        if Path(archive_path).suffix.casefold() in {".7z", ".rar"}:
            return True
    return False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
