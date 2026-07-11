"""Short-lived protocol helper launched by Windows for an nxm:// URL."""

from __future__ import annotations

import sys
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from nxm_capture import (  # type: ignore[import-not-found]
        NxmCaptureError,
        forward_nxm_to_running_wizard,
        launch_previous_nxm_handler,
    )
else:
    from modlist_translation_wizard.nxm_capture import (
        NxmCaptureError,
        forward_nxm_to_running_wizard,
        launch_previous_nxm_handler,
    )


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        _write_bridge_diagnostic("invalid_arguments", None, argv_count=len(arguments))
        return 2
    nxm_url = arguments[0]
    try:
        if forward_nxm_to_running_wizard(nxm_url):
            _write_bridge_diagnostic("forwarded_to_running_wizard", nxm_url)
            return 0
        if launch_previous_nxm_handler(nxm_url):
            _write_bridge_diagnostic("forwarded_to_previous_handler", nxm_url)
            return 0
        _write_bridge_diagnostic("no_running_wizard_or_fallback", nxm_url)
        return 1
    except NxmCaptureError as exc:
        _write_bridge_diagnostic(
            "rejected",
            nxm_url,
            error_type=type(exc).__name__,
        )
        return 2


def _write_bridge_diagnostic(
    status: str,
    nxm_url: str | None,
    **extra: object,
) -> None:
    payload = {
        "schema_version": "mtw-nxm-bridge-last.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "status": status,
        "nxm": _safe_nxm_metadata(nxm_url),
        **extra,
    }
    path = _application_data_dir() / "nxm-bridge-last.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def _safe_nxm_metadata(nxm_url: str | None) -> dict[str, object]:
    if not nxm_url:
        return {"present": False}
    try:
        parts = urlsplit(str(nxm_url))
    except ValueError:
        return {"present": True, "parseable": False}
    path_parts = [part for part in parts.path.split("/") if part]
    metadata: dict[str, object] = {
        "present": True,
        "parseable": True,
        "scheme": parts.scheme,
        "game_domain": parts.netloc,
        "has_query": bool(parts.query),
    }
    if len(path_parts) >= 4:
        metadata["mod_id"] = path_parts[1]
        metadata["file_id"] = path_parts[3]
    query = parse_qs(parts.query, keep_blank_values=True)
    metadata["has_key"] = bool(query.get("key"))
    metadata["has_expires"] = bool(query.get("expires"))
    return metadata


def _application_data_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    root = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return root / "Modlist Translation Wizard"


if __name__ == "__main__":
    raise SystemExit(main())
