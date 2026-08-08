"""User-initiated Nexus NXM downloads for non-Premium accounts."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from modlist_translate_tool.nexus.api_client import (
    NexusApiClient,
    NexusDownloadLink,
    NexusRateLimit,
)
from modlist_translate_tool.nexus.downloader import (
    DownloadRunResult,
    FileDownloader,
    download_archives_from_queue,
)
from modlist_translation_wizard.manifest import WizardManifestError
from modlist_translation_wizard.runtime import WizardPremiumPlanResult


class NxmLinkError(WizardManifestError):
    """Raised when a user-provided NXM link is invalid or unusable."""


@dataclass(frozen=True, slots=True)
class NxmDownloadAuthorization:
    game_domain: str
    mod_id: int
    file_id: int
    expires: int
    key: str = field(repr=False)

    def safe_payload(self) -> dict[str, Any]:
        return {
            "game_domain": self.game_domain,
            "translation_nexus_mod_id": self.mod_id,
            "translation_file_id": self.file_id,
            "expires_present": True,
            "key_present": True,
        }


@dataclass(frozen=True, slots=True)
class WizardNonPremiumDownloadResult:
    item_run: DownloadRunResult
    updated_queue_path: Path
    updated_queue_payload: dict[str, Any]
    result_path: Path
    result_payload: dict[str, Any]


def parse_nxm_download_link(
    value: str,
    *,
    now: int | None = None,
) -> NxmDownloadAuthorization:
    text = str(value or "").strip()
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise NxmLinkError("NXM link is malformed.") from exc
    if parts.scheme.casefold() != "nxm":
        raise NxmLinkError("A user-initiated nxm:// download link is required.")
    game_domain = parts.netloc.strip().casefold()
    path_parts = [part for part in parts.path.split("/") if part]
    if (
        not game_domain
        or len(path_parts) != 4
        or path_parts[0].casefold() != "mods"
        or path_parts[2].casefold() != "files"
    ):
        raise NxmLinkError("NXM link does not identify one Nexus mod file.")
    mod_id = _positive_int(path_parts[1])
    file_id = _positive_int(path_parts[3])
    if mod_id is None or file_id is None:
        raise NxmLinkError("NXM link contains an invalid mod or file id.")
    query = parse_qs(parts.query, keep_blank_values=True)
    key = _single_query_value(query, "key")
    expires_text = _single_query_value(query, "expires")
    expires = _positive_int(expires_text)
    if not key or expires is None:
        raise NxmLinkError("NXM link is missing its temporary key or expiry.")
    if any(character.isspace() or ord(character) < 32 for character in key):
        raise NxmLinkError("NXM link contains an invalid temporary key.")
    current_time = int(time.time()) if now is None else int(now)
    if expires <= current_time:
        raise NxmLinkError("NXM link has expired; request a new link from Nexus Mods.")
    return NxmDownloadAuthorization(
        game_domain=game_domain,
        mod_id=mod_id,
        file_id=file_id,
        expires=expires,
        key=key,
    )


def nexus_user_download_page_url(
    game_domain: str,
    mod_id: int,
    file_id: int,
) -> str:
    game = quote(str(game_domain).strip().casefold(), safe="")
    return (
        f"https://www.nexusmods.com/{game}/mods/{int(mod_id)}"
        f"?tab=files&file_id={int(file_id)}&nmm=1"
    )


def next_non_premium_download(
    queue_payload: dict[str, Any],
) -> dict[str, Any] | None:
    for item in queue_payload.get("items", []):
        if not isinstance(item, dict) or _queue_item_is_available(item):
            continue
        if str(item.get("status") or "").upper() == "FAILED":
            continue
        summary = _queue_item_download_summary(item)
        if summary is not None:
            return summary
    return None


def failed_non_premium_downloads(
    queue_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    failed: list[dict[str, Any]] = []
    for item in queue_payload.get("items", []):
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "").upper() != "FAILED":
            continue
        summary = _queue_item_download_summary(item)
        if summary is None:
            continue
        summary["last_error"] = str(item.get("last_error") or "")
        failed.append(summary)
    return failed


def unavailable_non_premium_downloads(
    queue_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return every required queue item that still needs user action."""
    unavailable: list[dict[str, Any]] = []
    for item in queue_payload.get("items", []):
        if not isinstance(item, dict) or _queue_item_is_available(item):
            continue
        summary = _queue_item_download_summary(item)
        if summary is None:
            continue
        summary["last_error"] = str(item.get("last_error") or "")
        unavailable.append(summary)
    return unavailable


def run_non_premium_nxm_download(
    *,
    plan: WizardPremiumPlanResult,
    api_key: str | None = None,
    nxm_url: str,
    queue_path: Path | str | None = None,
    overwrite: bool = False,
    file_downloader: FileDownloader | None = None,
    max_attempts: int = 3,
    client_factory: Callable[[str | None], Any] | None = None,
    now: int | None = None,
) -> WizardNonPremiumDownloadResult:
    normalized_api_key = str(api_key or "").strip()
    if not normalized_api_key:
        raise WizardManifestError("Nexus API key is required for non-Premium downloads.")
    authorization = parse_nxm_download_link(nxm_url, now=now)
    source_queue_path = Path(queue_path or plan.download_plan.queue_path)
    queue_payload = json.loads(source_queue_path.read_text(encoding="utf-8"))
    matched_item = _matching_queue_item(queue_payload, authorization)
    if matched_item is None:
        raise NxmLinkError(
            "NXM link does not match a required file in the active download queue."
        )

    output_root = plan.download_plan.queue_path.parent.parent / "non-premium-download-run"
    item_root = output_root / "items" / f"{authorization.mod_id}_{authorization.file_id}"
    item_root.mkdir(parents=True, exist_ok=True)
    item_queue_path = item_root / "request.queue.json"
    item_queue_payload = {
        "schema_version": queue_payload.get("schema_version", "download-queue.v1"),
        "items": [matched_item],
    }
    _write_json(item_queue_path, item_queue_payload)

    if client_factory is not None:
        nexus_client = client_factory(normalized_api_key)
    else:
        nexus_client = NexusApiClient(normalized_api_key)
    client = _TransientNxmDownloadClient(nexus_client, authorization)
    item_run = download_archives_from_queue(
        queue_path=item_queue_path,
        out_dir=item_root,
        client=client,
        overwrite=overwrite,
        file_downloader=file_downloader,
        max_attempts=max_attempts,
    )
    updated_item = item_run.updated_queue_payload["items"][0]
    merged_queue = _merge_queue_item(queue_payload, updated_item, authorization)
    updated_queue_path = output_root / "download_queue.updated.json"
    _write_json(updated_queue_path, merged_queue)

    run_summary = item_run.manifest_payload.get("summary", {})
    result_payload = {
        "schema_version": "wizard-non-premium-download-result.v1",
        "status": _result_status(updated_item),
        "authorization": authorization.safe_payload(),
        "source_page": nexus_user_download_page_url(
            authorization.game_domain,
            authorization.mod_id,
            authorization.file_id,
        ),
        "item_run_manifest": str(item_run.manifest_path),
        "updated_queue": str(updated_queue_path),
        "summary": run_summary,
        "secrets_persisted": False,
    }
    result_path = item_root / "wizard_non_premium_download_result.json"
    _write_json(result_path, result_payload)
    return WizardNonPremiumDownloadResult(
        item_run=item_run,
        updated_queue_path=updated_queue_path,
        updated_queue_payload=merged_queue,
        result_path=result_path,
        result_payload=result_payload,
    )


class _TransientNxmDownloadClient:
    def __init__(
        self,
        client: Any,
        authorization: NxmDownloadAuthorization,
    ) -> None:
        self._client = client
        self._authorization = authorization

    def get_mod_file(self, game_domain: str, mod_id: int, file_id: int):
        return self._client.get_mod_file(game_domain, mod_id, file_id)

    def get_download_links(
        self,
        game_domain: str,
        mod_id: int,
        file_id: int,
    ) -> tuple[list[NexusDownloadLink], NexusRateLimit]:
        authorization = self._authorization
        if (
            str(game_domain).casefold() != authorization.game_domain
            or int(mod_id) != authorization.mod_id
            or int(file_id) != authorization.file_id
        ):
            raise NxmLinkError("Transient NXM authorization was used for another file.")
        query = urlencode(
            {
                "key": authorization.key,
                "expires": str(authorization.expires),
            }
        )
        response = self._client.get_json(
            f"/games/{quote(authorization.game_domain, safe='')}"
            f"/mods/{authorization.mod_id}/files/{authorization.file_id}"
            f"/download_link.json?{query}"
        )
        return _download_links(response.payload), response.rate_limit

    def operation_lock(self, operation_name: str):
        return self._client.operation_lock(operation_name)

    def request_telemetry(self) -> dict[str, Any]:
        return self._client.request_telemetry()


def _download_links(payload: object) -> list[NexusDownloadLink]:
    if isinstance(payload, dict):
        raw_items = (
            payload.get("files")
            or payload.get("download_links")
            or payload.get("links")
            or []
        )
    else:
        raw_items = payload
    if not isinstance(raw_items, list):
        raise NxmLinkError("Nexus returned an unexpected download-link response.")
    result: list[NexusDownloadLink] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        uri = item.get("URI") or item.get("uri") or item.get("url")
        if not uri:
            continue
        result.append(
            NexusDownloadLink(
                name=_optional_text(item.get("name")),
                short_name=_optional_text(item.get("short_name") or item.get("shortName")),
                uri=str(uri),
            )
        )
    return result


def _matching_queue_item(
    queue_payload: dict[str, Any],
    authorization: NxmDownloadAuthorization,
) -> dict[str, Any] | None:
    for item in queue_payload.get("items", []):
        if not isinstance(item, dict):
            continue
        request = item.get("request") if isinstance(item.get("request"), dict) else {}
        if (
            str(request.get("game_domain") or "").casefold()
            == authorization.game_domain
            and _positive_int(request.get("translation_nexus_mod_id"))
            == authorization.mod_id
            and _positive_int(request.get("translation_file_id"))
            == authorization.file_id
        ):
            return json.loads(json.dumps(item))
    return None


def _merge_queue_item(
    queue_payload: dict[str, Any],
    updated_item: dict[str, Any],
    authorization: NxmDownloadAuthorization,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    replaced = False
    for item in queue_payload.get("items", []):
        if not isinstance(item, dict):
            continue
        request = item.get("request") if isinstance(item.get("request"), dict) else {}
        matches = (
            str(request.get("game_domain") or "").casefold()
            == authorization.game_domain
            and _positive_int(request.get("translation_nexus_mod_id"))
            == authorization.mod_id
            and _positive_int(request.get("translation_file_id"))
            == authorization.file_id
        )
        if matches:
            items.append(updated_item)
            replaced = True
        else:
            items.append(item)
    if not replaced:
        raise NxmLinkError("Required queue item disappeared during download.")
    return {
        **{key: value for key, value in queue_payload.items() if key not in {"items", "summary"}},
        "summary": _queue_summary(items),
        "items": items,
    }


def _queue_item_is_available(item: dict[str, Any]) -> bool:
    status = str(item.get("status") or "").upper()
    path_text = str(item.get("local_archive_path") or "").strip()
    return (
        status in {"READY", "DOWNLOADED", "SKIPPED_ALREADY_EXISTS"}
        and bool(path_text)
        and Path(path_text).is_file()
    )


def _queue_item_download_summary(item: dict[str, Any]) -> dict[str, Any] | None:
    request = item.get("request") if isinstance(item.get("request"), dict) else {}
    game_domain = str(request.get("game_domain") or "").strip().casefold()
    mod_id = _positive_int(request.get("translation_nexus_mod_id"))
    file_id = _positive_int(request.get("translation_file_id"))
    if not game_domain or mod_id is None or file_id is None:
        return None
    return {
        "game_domain": game_domain,
        "translation_nexus_mod_id": mod_id,
        "translation_file_id": file_id,
        "translation_name": str(request.get("translation_name") or ""),
        "translation_file_name": str(request.get("translation_file_name") or ""),
        "status": str(item.get("status") or "PLANNED"),
        "page_url": nexus_user_download_page_url(game_domain, mod_id, file_id),
    }


def _queue_summary(items: list[dict[str, Any]]) -> dict[str, int]:
    summary = {
        "item_count": len(items),
        "planned": 0,
        "skipped": 0,
        "ready": 0,
        "downloading": 0,
        "downloaded": 0,
        "failed": 0,
    }
    for item in items:
        status = str(item.get("status") or "").casefold()
        if status in summary:
            summary[status] += 1
    return summary


def _result_status(item: dict[str, Any]) -> str:
    if _queue_item_is_available(item):
        return "DOWNLOADED"
    if str(item.get("status") or "").upper() == "PLANNED":
        return "DEFERRED"
    return "FAILED"


def _single_query_value(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    if not values or len(values) != 1:
        return None
    value = str(values[0]).strip()
    return value or None


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
