from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from modlist_translation_wizard.nxm_bridge import main as nxm_bridge_main
from modlist_translation_wizard.nxm_capture import (
    WindowsNxmProtocolBinding,
    _read_registry_command,
)


def main() -> int:
    arguments = sys.argv[1:]
    if arguments and arguments[0] == "--nxm-bridge":
        return nxm_bridge_main(arguments[1:])
    if arguments and arguments[0] == "--nxm-bind-smoke":
        return _nxm_bind_smoke(arguments[1:])
    if arguments and arguments[0] == "--convert-worker":
        from modlist_translation_wizard.conversion_worker import run_conversion_worker

        if len(arguments) != 2:
            return 2
        os._exit(run_conversion_worker(Path(arguments[1])))
    if arguments and arguments[0] == "--plugin-convert-worker":
        from modlist_translate_tool.dsd.dynamic_string_converter import (
            run_plugin_pair_conversion_worker,
        )

        if len(arguments) != 2:
            return 2
        os._exit(run_plugin_pair_conversion_worker(Path(arguments[1])))
    if arguments and str(arguments[0]).casefold().startswith("nxm://"):
        return nxm_bridge_main(arguments[:1])
    if len(arguments) >= 2 and Path(arguments[0]).name.casefold() == "nxm_bridge.py":
        return nxm_bridge_main(arguments[1:])
    from modlist_translation_wizard.installer_gui import main as gui_main

    gui_main()
    return 0


def _nxm_bind_smoke(arguments: list[str]) -> int:
    if len(arguments) != 1:
        return 2
    output_path = Path(arguments[0])
    binding = WindowsNxmProtocolBinding()
    payload: dict[str, object] = {
        "schema_version": "mtw-nxm-bind-smoke.v1",
        "installed_command": binding.command,
        "ok": False,
    }
    exit_code = 1
    try:
        status = binding.bind()
        exists, current_command = _read_registry_command()
        payload.update(
            {
                "previous_command": status.previous_command,
                "registry_exists_during_bind": exists,
                "registry_command_during_bind": current_command,
                "ok": exists and current_command == status.installed_command,
            }
        )
        exit_code = 0 if payload["ok"] else 1
    except Exception as exc:  # noqa: BLE001 - hidden packaging diagnostic.
        payload.update({"error_type": type(exc).__name__, "error": str(exc)})
        exit_code = 2
    finally:
        try:
            payload["restored"] = binding.restore()
            exists, current_command = _read_registry_command()
            payload["registry_exists_after_restore"] = exists
            payload["registry_command_after_restore"] = current_command
        except Exception as exc:  # noqa: BLE001 - hidden packaging diagnostic.
            payload["restore_error_type"] = type(exc).__name__
            payload["restore_error"] = str(exc)
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass
    return exit_code


def _write_crash_log(exc: BaseException) -> None:
    local_app_data = os.environ.get("LOCALAPPDATA")
    root = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    path = root / "Modlist Translation Wizard" / "last_crash.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(
                [
                    "Modlist Translation Wizard startup crash",
                    f"created_at={datetime.now(timezone.utc).isoformat()}",
                    f"exception_type={type(exc).__name__}",
                    "",
                    "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
                ]
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - GUI build has no console for startup errors.
        _write_crash_log(exc)
        raise SystemExit(1) from exc
