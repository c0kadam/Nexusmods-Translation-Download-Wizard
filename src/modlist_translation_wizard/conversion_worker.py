"""Hidden conversion worker used by the GUI build.

The archive conversion path can exercise native libraries and thousands of
external 7-Zip process calls. Running it in a child process keeps the installer
GUI alive even if the compiled worker process terminates unexpectedly.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from modlist_translate_tool.dsd.dynamic_string_converter import (
    PLUGIN_CONVERSION_WORKER_COMMAND_ENV,
)
from modlist_translation_wizard.runtime import (
    WizardConversionResult,
    convert_downloaded_translations_from_manifest,
    load_wizard_conversion_result,
)


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
    result_path = output_dir / "wizard_conversion_result.json"
    for path in (status_path,):
        path.unlink(missing_ok=True)
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
    }
    config_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    completed = _run_hidden_worker(_worker_command(config_path))
    status = _read_status(status_path)
    if status.get("ok") is not True:
        error = str(status.get("error") or "").strip()
        error_type = str(status.get("error_type") or "WorkerFailed")
        details = f"{error_type}: {error}" if error else error_type
        stderr = _decode_output(completed.stderr).strip()
        if stderr:
            details = f"{details}\n\nWorker stderr:\n{stderr[-2000:]}"
        raise RuntimeError(
            "Ceviri hazirlama islemi ayri worker process icinde basarisiz oldu.\n"
            f"Cikis kodu: {completed.returncode}\n"
            f"Detay: {details}\n"
            f"Worker durum dosyasi: {status_path}"
        )
    result_text = str(status.get("result_path") or result_path)
    return load_wizard_conversion_result(result_text)


def run_conversion_worker(config_path: Path | str) -> int:
    config = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
    status_path = Path(str(config["status_path"]))
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
    try:
        os.environ[PLUGIN_CONVERSION_WORKER_COMMAND_ENV] = json.dumps(_plugin_worker_command())
        _write_status(
            status_path,
            {
                "schema_version": "mtw-conversion-worker-status.v1",
                "ok": False,
                "created_at": _now(),
                "stage": "running_conversion",
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
            progress_status_path=status_path,
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
        },
    )
    return 0


def _worker_command(config_path: Path) -> list[str]:
    launcher = _standalone_launcher()
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
    launcher = _standalone_launcher()
    if launcher is not None:
        return [str(launcher), "--plugin-convert-worker"]
    return [
        str(Path(sys.executable)),
        "-B",
        "-m",
        "modlist_translation_wizard",
        "--plugin-convert-worker",
    ]


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


def _write_status(path: Path, payload: dict[str, Any]) -> None:
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
