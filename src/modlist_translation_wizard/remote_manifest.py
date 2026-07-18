"""Remote manifest channel support.

The remote channel is deliberately data-only: it downloads JSON manifests,
validates their digest and schema, then caches the accepted file locally.
No code or scripts are ever loaded from the remote location.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from modlist_translation_wizard.manifest import (
    WizardManifestError,
    normalize_wizard_manifest_payload,
    validate_wizard_manifest,
)
from modlist_translation_wizard.version import TOOL_NAME, __version__

REMOTE_INDEX_SCHEMA_VERSION = "mtw-remote-manifest-index.v1"
REMOTE_CONFIG_SCHEMA_VERSION = "mtw-remote-manifest-config.v1"
REMOTE_MANIFEST_CONFIG_NAME = "remote_manifest.json"

_DEFAULT_CHANNEL = "stable"
_DEFAULT_TIMEOUT_SECONDS = 8.0
_DEFAULT_CACHE_TTL_SECONDS = 60 * 60
_DEFAULT_MAX_INDEX_BYTES = 256 * 1024
_DEFAULT_MAX_MANIFEST_BYTES = 32 * 1024 * 1024
_CACHE_DIR_ENV_VAR = "MTW_REMOTE_MANIFEST_CACHE_DIR"

UrlFetcher = Callable[[str, float, int, tuple[str, ...]], bytes]


class RemoteManifestError(WizardManifestError):
    """Raised when a remote manifest cannot be trusted or loaded."""


@dataclass(frozen=True, slots=True)
class RemoteManifestConfig:
    enabled: bool
    list_id: str
    remote_list_id: str
    manifest_name: str
    channel: str
    index_url: str
    allow_hosts: tuple[str, ...]
    cache_root: Path
    cache_ttl_seconds: int
    timeout_seconds: float
    max_index_bytes: int
    max_manifest_bytes: int
    allow_stale_cache: bool

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        default_list_id: str,
        default_manifest_name: str,
    ) -> "RemoteManifestConfig | None":
        if not isinstance(payload, dict) or payload.get("enabled") is False:
            return None
        list_id = _safe_component(payload.get("list_id") or default_list_id, "list_id")
        remote_list_id = _safe_remote_path_component(
            payload.get("remote_list_id")
            or payload.get("remote_path_id")
            or payload.get("modlist_path")
            or list_id,
            "remote_list_id",
        )
        manifest_name = _safe_component(
            payload.get("manifest_name") or default_manifest_name,
            "manifest_name",
        )
        channel = _safe_component(payload.get("channel") or _DEFAULT_CHANNEL, "channel")
        index_url = _configured_index_url(
            payload,
            list_id=list_id,
            remote_list_id=remote_list_id,
            channel=channel,
            manifest_name=manifest_name,
        )
        if not index_url:
            return None
        allow_hosts = _normalize_hosts(payload.get("allow_hosts"), index_url)
        cache_root = _cache_root_from_payload(payload.get("cache_root"))
        return cls(
            enabled=True,
            list_id=list_id,
            remote_list_id=remote_list_id,
            manifest_name=manifest_name,
            channel=channel,
            index_url=index_url,
            allow_hosts=allow_hosts,
            cache_root=cache_root,
            cache_ttl_seconds=_non_negative_int(
                payload.get("cache_ttl_seconds"),
                _DEFAULT_CACHE_TTL_SECONDS,
            ),
            timeout_seconds=_positive_float(
                payload.get("timeout_seconds"),
                _DEFAULT_TIMEOUT_SECONDS,
            ),
            max_index_bytes=_positive_int(
                payload.get("max_index_bytes"),
                _DEFAULT_MAX_INDEX_BYTES,
            ),
            max_manifest_bytes=_positive_int(
                payload.get("max_manifest_bytes"),
                _DEFAULT_MAX_MANIFEST_BYTES,
            ),
            allow_stale_cache=payload.get("allow_stale_cache") is not False,
        )


@dataclass(frozen=True, slots=True)
class RemoteManifestResolution:
    manifest_path: Path
    digest_path: Path
    payload: dict[str, Any]
    source: str
    list_id: str
    channel: str
    version: str
    manifest_url: str
    index_url: str
    warning: str | None = None


def resolve_remote_manifest(
    config: RemoteManifestConfig,
    *,
    force_refresh: bool = False,
    raise_on_failure: bool = False,
    allow_stale_cache: bool | None = None,
    fetcher: UrlFetcher | None = None,
) -> RemoteManifestResolution | None:
    if not config.enabled:
        return None
    if not force_refresh:
        cached = _load_cached_manifest(config, require_fresh=True)
        if cached is not None:
            return cached

    try:
        return _download_remote_manifest(config, fetcher=fetcher or _fetch_url_bytes)
    except RemoteManifestError as exc:
        stale_cache_allowed = (
            config.allow_stale_cache
            if allow_stale_cache is None
            else bool(allow_stale_cache)
        )
        if stale_cache_allowed:
            cached = _load_cached_manifest(config, require_fresh=False, warning=str(exc))
            if cached is not None:
                return cached
        if raise_on_failure:
            raise
        return None


def clear_remote_manifest_cache(config: RemoteManifestConfig) -> None:
    """Delete the verified OTA cache for one modlist and channel."""

    cache_dir = _cache_dir(config)
    manifest_path = cache_dir / config.manifest_name
    digest_path = manifest_path.with_suffix(manifest_path.suffix + ".sha256")
    metadata_path = cache_dir / "metadata.json"
    cached_files = (
        manifest_path,
        digest_path,
        metadata_path,
        manifest_path.with_name(f".{manifest_path.name}.tmp"),
        digest_path.with_name(f".{digest_path.name}.tmp"),
        metadata_path.with_name(f".{metadata_path.name}.tmp"),
    )
    for path in cached_files:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue

    for directory in (
        cache_dir,
        cache_dir.parent,
        cache_dir.parent.parent,
        config.cache_root,
    ):
        try:
            directory.rmdir()
        except OSError:
            break


def _download_remote_manifest(
    config: RemoteManifestConfig,
    *,
    fetcher: UrlFetcher,
) -> RemoteManifestResolution:
    index_bytes = fetcher(
        config.index_url,
        config.timeout_seconds,
        config.max_index_bytes,
        config.allow_hosts,
    )
    index_payload = _decode_json(index_bytes, "remote manifest index")
    entry = _select_index_entry(index_payload, config)
    manifest_url = _required_url(entry.get("url") or entry.get("manifest_url"), config)
    expected_sha256 = _required_sha256(entry.get("sha256") or entry.get("manifest_sha256"))
    min_version = _clean_text(entry.get("min_app_version"))
    if min_version and not _version_at_least(__version__, min_version):
        raise RemoteManifestError(
            f"remote manifest requires {TOOL_NAME} {min_version} or newer"
        )

    manifest_bytes = fetcher(
        manifest_url,
        config.timeout_seconds,
        config.max_manifest_bytes,
        config.allow_hosts,
    )
    actual_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if actual_sha256.casefold() != expected_sha256.casefold():
        raise RemoteManifestError("remote manifest SHA-256 mismatch")
    payload = _validate_manifest_payload(manifest_bytes, config)

    version = _clean_text(entry.get("version") or payload.get("manifest_id")) or ""
    cache_dir = _cache_dir(config)
    cache_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = cache_dir / config.manifest_name
    digest_path = manifest_path.with_suffix(manifest_path.suffix + ".sha256")
    metadata_path = cache_dir / "metadata.json"
    _atomic_write_bytes(manifest_path, manifest_bytes)
    _atomic_write_text(digest_path, f"{actual_sha256}  {config.manifest_name}\n")
    _atomic_write_text(
        metadata_path,
        json.dumps(
            {
                "schema_version": "mtw-remote-manifest-cache.v1",
                "downloaded_at": datetime.now(timezone.utc).isoformat(),
                "index_url": config.index_url,
                "manifest_url": manifest_url,
                "list_id": config.list_id,
                "channel": config.channel,
                "version": version,
                "sha256": actual_sha256,
            },
            indent=2,
            sort_keys=True,
        ),
    )
    return RemoteManifestResolution(
        manifest_path=manifest_path,
        digest_path=digest_path,
        payload=payload,
        source="remote_download",
        list_id=config.list_id,
        channel=config.channel,
        version=version,
        manifest_url=manifest_url,
        index_url=config.index_url,
    )


def _load_cached_manifest(
    config: RemoteManifestConfig,
    *,
    require_fresh: bool,
    warning: str | None = None,
) -> RemoteManifestResolution | None:
    manifest_path = _cache_dir(config) / config.manifest_name
    digest_path = manifest_path.with_suffix(manifest_path.suffix + ".sha256")
    metadata_path = manifest_path.parent / "metadata.json"
    if not manifest_path.is_file() or not digest_path.is_file():
        return None
    metadata = _read_json_file(metadata_path)
    if require_fresh and not _cache_is_fresh(metadata, config.cache_ttl_seconds):
        return None
    try:
        manifest_bytes = manifest_path.read_bytes()
        digest_parts = digest_path.read_text(encoding="ascii").split()
        if not digest_parts:
            return None
        actual_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        if actual_sha256.casefold() != digest_parts[0].casefold():
            return None
        payload = _validate_manifest_payload(manifest_bytes, config)
    except (OSError, UnicodeError, json.JSONDecodeError, WizardManifestError):
        return None
    return RemoteManifestResolution(
        manifest_path=manifest_path,
        digest_path=digest_path,
        payload=payload,
        source="remote_cache",
        list_id=config.list_id,
        channel=config.channel,
        version=str(metadata.get("version") or payload.get("manifest_id") or ""),
        manifest_url=str(metadata.get("manifest_url") or ""),
        index_url=str(metadata.get("index_url") or config.index_url),
        warning=warning,
    )


def _validate_manifest_payload(
    manifest_bytes: bytes,
    config: RemoteManifestConfig,
) -> dict[str, Any]:
    payload = normalize_wizard_manifest_payload(
        json.loads(manifest_bytes.decode("utf-8"))
    )
    validate_wizard_manifest(payload)
    modlist = payload.get("modlist") if isinstance(payload.get("modlist"), dict) else {}
    manifest_list_id = _safe_component(modlist.get("id"), "manifest modlist id")
    manifest_channel = _safe_component(payload.get("channel"), "manifest channel")
    if manifest_list_id != config.list_id:
        raise RemoteManifestError("remote manifest list id does not match release")
    if manifest_channel != config.channel:
        raise RemoteManifestError("remote manifest channel does not match release")
    return payload


def _select_index_entry(
    index_payload: dict[str, Any],
    config: RemoteManifestConfig,
) -> dict[str, Any]:
    if not isinstance(index_payload, dict):
        raise RemoteManifestError("remote manifest index must be a JSON object")
    schema = _clean_text(index_payload.get("schema_version"))
    if schema and schema != REMOTE_INDEX_SCHEMA_VERSION:
        raise RemoteManifestError("unsupported remote manifest index schema")
    entries = index_payload.get("manifests")
    if not isinstance(entries, list):
        entries = index_payload.get("releases")
    if not isinstance(entries, list) and isinstance(index_payload.get("manifest"), dict):
        entries = [index_payload["manifest"]]
    if not isinstance(entries, list):
        raise RemoteManifestError("remote manifest index has no manifests list")
    default_list_id = index_payload.get("list_id") or config.list_id
    default_channel = index_payload.get("channel") or config.channel
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            entry_list_id = _safe_component(
                entry.get("list_id") or default_list_id,
                "index list_id",
            )
            entry_channel = _safe_component(
                entry.get("channel") or default_channel,
                "index channel",
            )
        except RemoteManifestError:
            continue
        if entry_list_id == config.list_id and entry_channel == config.channel:
            return entry
    raise RemoteManifestError("remote manifest index has no matching release")


def _fetch_url_bytes(
    url: str,
    timeout_seconds: float,
    max_bytes: int,
    allow_hosts: tuple[str, ...],
) -> bytes:
    _validate_remote_url(url, allow_hosts)
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": f"{TOOL_NAME}/{__version__}",
        },
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            final_url = response.geturl()
            _validate_remote_url(final_url, allow_hosts)
            data = response.read(max_bytes + 1)
    except (OSError, URLError) as exc:
        raise RemoteManifestError(f"remote manifest download failed: {exc}") from exc
    if len(data) > max_bytes:
        raise RemoteManifestError("remote manifest response exceeds size limit")
    return data


def _required_url(value: object, config: RemoteManifestConfig) -> str:
    url = _clean_text(value)
    if not url:
        raise RemoteManifestError("remote manifest URL is required")
    _validate_remote_url(url, config.allow_hosts)
    return url


def _validate_remote_url(url: str, allow_hosts: tuple[str, ...]) -> None:
    parts = urlsplit(url)
    if parts.scheme.casefold() != "https":
        raise RemoteManifestError("remote manifest URL must use HTTPS")
    host = (parts.hostname or "").casefold()
    if not host:
        raise RemoteManifestError("remote manifest URL host is required")
    if host not in allow_hosts:
        raise RemoteManifestError("remote manifest URL host is not allowlisted")


def _decode_json(data: bytes, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RemoteManifestError(f"{label} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise RemoteManifestError(f"{label} must be a JSON object")
    return payload


def _required_sha256(value: object) -> str:
    text = _clean_text(value)
    if not text or len(text) != 64:
        raise RemoteManifestError("remote manifest SHA-256 is required")
    try:
        int(text, 16)
    except ValueError as exc:
        raise RemoteManifestError("remote manifest SHA-256 is invalid") from exc
    return text.casefold()


def _cache_is_fresh(metadata: dict[str, Any], ttl_seconds: int) -> bool:
    if ttl_seconds <= 0:
        return False
    raw = _clean_text(metadata.get("downloaded_at"))
    if not raw:
        return False
    try:
        downloaded_at = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if downloaded_at.tzinfo is None:
        downloaded_at = downloaded_at.replace(tzinfo=timezone.utc)
    age = time.time() - downloaded_at.timestamp()
    return age <= ttl_seconds


def _cache_dir(config: RemoteManifestConfig) -> Path:
    return (
        config.cache_root
        / "manifests"
        / _safe_component(config.list_id, "list_id")
        / _safe_component(config.channel, "channel")
    )


def _cache_root_from_payload(value: object) -> Path:
    env_root = os.environ.get(_CACHE_DIR_ENV_VAR)
    if env_root:
        return Path(env_root)
    text = _clean_text(value)
    if text:
        return Path(text)
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "Modlist Translation Wizard" / "remote"
    return Path.home() / "AppData" / "Local" / "Modlist Translation Wizard" / "remote"


def _normalize_hosts(value: object, index_url: str) -> tuple[str, ...]:
    hosts = []
    if isinstance(value, list):
        hosts.extend(str(item).strip().casefold() for item in value if str(item).strip())
    index_host = (urlsplit(index_url).hostname or "").casefold()
    if index_host:
        hosts.append(index_host)
    deduped: list[str] = []
    for host in hosts:
        if host and host not in deduped:
            deduped.append(host)
    return tuple(deduped)


def _configured_index_url(
    payload: dict[str, Any],
    *,
    list_id: str,
    remote_list_id: str,
    channel: str,
    manifest_name: str,
) -> str:
    template = _clean_text(payload.get("index_url_template"))
    if template:
        return _format_url_template(
            template,
            list_id=list_id,
            remote_list_id=remote_list_id,
            channel=channel,
            manifest_name=manifest_name,
        )
    index_url = _clean_text(payload.get("index_url"))
    if index_url:
        return index_url
    repository = _clean_text(
        payload.get("github_repository")
        or payload.get("repository")
        or payload.get("repo")
    )
    if not repository:
        return ""
    owner, repo = _github_repository_parts(repository)
    ref = _safe_remote_path_component(
        payload.get("github_ref") or payload.get("ref") or payload.get("branch") or "main",
        "github_ref",
    )
    path_template = _clean_text(payload.get("index_path") or "{remote_list_id}/index.json")
    index_path = _format_url_template(
        path_template,
        list_id=list_id,
        remote_list_id=remote_list_id,
        channel=channel,
        manifest_name=manifest_name,
    )
    index_path = _safe_relative_url_path(index_path, "index_path")
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{index_path}"


def _format_url_template(
    template: str,
    *,
    list_id: str,
    remote_list_id: str,
    channel: str,
    manifest_name: str,
) -> str:
    return template.format(
        list_id=list_id,
        remote_list_id=remote_list_id,
        channel=channel,
        manifest_name=manifest_name,
    )


def _github_repository_parts(value: str) -> tuple[str, str]:
    text = value.strip().removeprefix("https://github.com/").strip("/")
    parts = [part for part in text.split("/") if part]
    if len(parts) != 2:
        raise RemoteManifestError("GitHub repository must be owner/name")
    owner = _safe_remote_path_component(parts[0], "github owner")
    repo = _safe_remote_path_component(parts[1], "github repository")
    return owner, repo


def _safe_remote_path_component(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not text or text in {".", ".."}:
        raise RemoteManifestError(f"{label} is required")
    if any(separator in text for separator in ("/", "\\")):
        raise RemoteManifestError(f"{label} must be a single path component")
    if any(ord(char) < 32 for char in text):
        raise RemoteManifestError(f"{label} contains control characters")
    return text


def _safe_relative_url_path(value: object, label: str) -> str:
    text = str(value or "").replace("\\", "/").strip("/")
    if not text:
        raise RemoteManifestError(f"{label} is required")
    parts = [part for part in text.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise RemoteManifestError(f"{label} must be a safe relative path")
    if any(any(ord(char) < 32 for char in part) for part in parts):
        raise RemoteManifestError(f"{label} contains control characters")
    return "/".join(parts)


def _safe_component(value: object, label: str) -> str:
    text = str(value or "").strip().casefold()
    if not text or text in {".", ".."}:
        raise RemoteManifestError(f"{label} is required")
    if any(separator in text for separator in ("/", "\\")):
        raise RemoteManifestError(f"{label} must be a single path component")
    return text


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _positive_int(value: object, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _non_negative_int(value: object, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default


def _positive_float(value: object, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _version_at_least(current: str, required: str) -> bool:
    return _version_parts(current) >= _version_parts(required)


def _version_parts(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in str(value or "").replace("-", ".").split("."):
        if not chunk.isdigit():
            break
        parts.append(int(chunk))
    return tuple(parts or [0])


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def _atomic_write_text(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
