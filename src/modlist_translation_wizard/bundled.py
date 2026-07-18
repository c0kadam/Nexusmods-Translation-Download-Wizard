"""Access to curated manifests bundled with the installed wizard package."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any

from modlist_translation_wizard.manifest import (
    WizardManifestError,
    normalize_wizard_manifest_payload,
    validate_wizard_manifest,
)
from modlist_translation_wizard.remote_manifest import (
    REMOTE_MANIFEST_CONFIG_NAME,
    RemoteManifestConfig,
    RemoteManifestResolution,
    clear_remote_manifest_cache,
    resolve_remote_manifest,
)

_FALLBACK_RELEASE_ID = "lorerim"
_DEFAULT_MANIFEST_NAME = "manifest.json"
_RELEASE_CONFIG_NAME = "release_config.json"
_DEFAULT_RELEASE_ENV_VAR = "MTW_DEFAULT_RELEASE_ID"
_EXTERNAL_RELEASE_ENV_VAR = "MTW_RELEASE_DIR"
_EXTERNAL_RELEASE_DIR_NAME = "release"
_REMOTE_DISABLE_ENV_VAR = "MTW_DISABLE_REMOTE_MANIFEST"
_REMOTE_CONFIG_ENV_VAR = "MTW_REMOTE_MANIFEST_CONFIG"
_REMOTE_INDEX_URL_ENV_VAR = "MTW_REMOTE_MANIFEST_INDEX_URL"
_LAST_DEFAULT_MANIFEST_SOURCE: dict[str, str | None] = {"source": "unknown"}

MANIFEST_MODE_OTA = "OTA"
MANIFEST_MODE_LOCAL = "LOCAL"
DEFAULT_MANIFEST_MODE = MANIFEST_MODE_OTA


def load_default_bundled_manifest(
    *,
    manifest_mode: str = DEFAULT_MANIFEST_MODE,
) -> dict[str, Any]:
    mode = normalize_manifest_mode(manifest_mode)
    release_id = _default_release_id()
    manifest_name = _default_manifest_name()
    remote_warning: str | None = None
    if mode == MANIFEST_MODE_OTA:
        remote = _resolve_remote_manifest_for(
            list_id=release_id,
            manifest_name=manifest_name,
            required=True,
        )
        if remote is not None:
            info = _remote_source_info(remote)
            info["requested_mode"] = mode
            _set_default_manifest_source(info)
            return remote.payload
        remote_warning = str(default_manifest_source_info().get("warning") or "").strip()
        raise _ota_manifest_unavailable_error(remote_warning)
    clear_default_remote_manifest_cache()
    external_manifest = _external_default_manifest_path(release_id, manifest_name)
    if external_manifest is not None:
        _set_local_source(
            source="external",
            path=str(external_manifest),
            requested_mode=mode,
            warning=remote_warning,
        )
        return _load_manifest_file(external_manifest, source_label="external release")
    parts = _default_manifest_parts(release_id, manifest_name)
    _set_local_source(
        source="bundled",
        path="/".join(parts),
        requested_mode=mode,
        warning=remote_warning,
    )
    try:
        return _load_bundled_manifest_parts(parts)
    except FileNotFoundError as exc:
        raise _manifest_unavailable_error(
            requested_mode=mode,
            expected_path="/".join(parts),
            remote_warning=remote_warning,
        ) from exc


def copy_default_bundled_manifest(
    output_dir: Path | str,
    *,
    manifest_mode: str = DEFAULT_MANIFEST_MODE,
) -> Path:
    mode = normalize_manifest_mode(manifest_mode)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    release_id = _default_release_id()
    manifest_name = _default_manifest_name()
    remote_warning: str | None = None
    if mode == MANIFEST_MODE_OTA:
        remote = _resolve_remote_manifest_for(
            list_id=release_id,
            manifest_name=manifest_name,
            required=True,
        )
        if remote is not None:
            info = _remote_source_info(remote)
            info["requested_mode"] = mode
            _set_default_manifest_source(info)
            return _copy_manifest_pair(
                manifest_path=remote.manifest_path,
                digest_path=remote.digest_path,
                output_dir=output,
                manifest_name=manifest_name,
            )
        remote_warning = str(default_manifest_source_info().get("warning") or "").strip()
        raise _ota_manifest_unavailable_error(remote_warning)
    clear_default_remote_manifest_cache()
    external_manifest = _external_default_manifest_path(release_id, manifest_name)
    if external_manifest is not None:
        _set_local_source(
            source="external",
            path=str(external_manifest),
            requested_mode=mode,
            warning=remote_warning,
        )
        return _copy_manifest_pair(
            manifest_path=external_manifest,
            digest_path=external_manifest.with_suffix(external_manifest.suffix + ".sha256"),
            output_dir=output,
            manifest_name=manifest_name,
        )
    root = files("modlist_translation_wizard")
    parts = _default_manifest_parts(release_id, manifest_name)
    manifest = root.joinpath(*parts)
    digest = root.joinpath(*parts[:-1], manifest_name + ".sha256")
    manifest_path = output / manifest_name
    digest_path = output / f"{manifest_name}.sha256"
    try:
        manifest_path.write_bytes(manifest.read_bytes())
        digest_path.write_text(digest.read_text(encoding="ascii"), encoding="ascii")
    except FileNotFoundError as exc:
        raise _manifest_unavailable_error(
            requested_mode=mode,
            expected_path="/".join(parts),
            remote_warning=remote_warning,
        ) from exc
    _set_local_source(
        source="bundled",
        path="/".join(parts),
        requested_mode=mode,
        warning=remote_warning,
    )
    return manifest_path


def load_bundled_manifest(*, list_id: str, manifest_name: str) -> dict[str, Any]:
    safe_list_id = _safe_manifest_component(list_id, "list_id")
    safe_manifest_name = _safe_manifest_component(manifest_name, "manifest_name")
    remote = _resolve_remote_manifest_for(
        list_id=safe_list_id,
        manifest_name=safe_manifest_name,
    )
    if remote is not None:
        return remote.payload
    for release_dir in external_release_dirs(safe_list_id):
        manifest_path = release_dir / safe_manifest_name
        if manifest_path.is_file():
            return _load_manifest_file(manifest_path, source_label="external release")
    try:
        return _load_bundled_manifest_parts(
            ("resources", "releases", safe_list_id, safe_manifest_name)
        )
    except FileNotFoundError:
        return _load_bundled_manifest_parts(
            ("resources", "manifests", safe_list_id, safe_manifest_name)
        )


def default_manifest_source_info() -> dict[str, str | None]:
    return dict(_LAST_DEFAULT_MANIFEST_SOURCE)


def default_release_info() -> dict[str, str]:
    return {
        "release_id": _default_release_id(),
        "manifest_name": _default_manifest_name(),
    }


def clear_default_remote_manifest_cache() -> None:
    """Remove the current release's session-only OTA manifest cache."""

    try:
        release_id = _default_release_id()
        payload = _remote_manifest_config_payload(release_id)
        if payload is None:
            return
        config = RemoteManifestConfig.from_payload(
            payload,
            default_list_id=release_id,
            default_manifest_name=_default_manifest_name(),
        )
    except (OSError, ValueError, WizardManifestError):
        return
    if config is not None:
        clear_remote_manifest_cache(config)


def normalize_manifest_mode(value: str | None) -> str:
    mode = str(value or "").strip().upper()
    if mode in {MANIFEST_MODE_OTA, "REMOTE"}:
        return MANIFEST_MODE_OTA
    if mode in {MANIFEST_MODE_LOCAL, "YEREL"}:
        return MANIFEST_MODE_LOCAL
    raise WizardManifestError(f"unsupported manifest mode: {value}")


def external_release_dirs(list_id: str | None = None) -> tuple[Path, ...]:
    """Return release directories that may be changed without rebuilding the app."""

    safe_list_id = _safe_optional_manifest_component(list_id)
    candidates: list[Path] = []
    seen: set[str] = set()
    for root in _external_release_roots():
        for candidate in _release_dir_variants(root, safe_list_id):
            key = str(candidate.resolve() if candidate.exists() else candidate).casefold()
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)
    return tuple(candidates)


def _load_bundled_manifest_parts(parts: tuple[str, ...]) -> dict[str, Any]:
    root = files("modlist_translation_wizard")
    manifest = root.joinpath(*parts)
    digest = root.joinpath(*parts[:-1], parts[-1] + ".sha256")
    manifest_bytes = manifest.read_bytes()
    digest_parts = digest.read_text(encoding="ascii").split()
    if not digest_parts:
        raise WizardManifestError("bundled manifest digest is empty")
    expected = digest_parts[0].casefold()
    actual = hashlib.sha256(manifest_bytes).hexdigest()
    if expected != actual:
        raise WizardManifestError("bundled manifest SHA-256 mismatch")
    payload = normalize_wizard_manifest_payload(json.loads(manifest_bytes.decode("utf-8")))
    validate_wizard_manifest(payload)
    return payload


def _external_release_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    env_root = os.environ.get(_EXTERNAL_RELEASE_ENV_VAR)
    if env_root:
        roots.append(Path(env_root))
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent / _EXTERNAL_RELEASE_DIR_NAME)
    argv0 = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
    if argv0 is not None:
        roots.append(argv0.parent / _EXTERNAL_RELEASE_DIR_NAME)
    roots.append(Path.cwd() / _EXTERNAL_RELEASE_DIR_NAME)

    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root.resolve() if root.exists() else root).casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(root)
    return tuple(unique)


def _external_default_manifest_path(release_id: str, manifest_name: str) -> Path | None:
    for release_dir in external_release_dirs(release_id):
        manifest_path = release_dir / manifest_name
        if manifest_path.is_file():
            return manifest_path
    return None


def _default_release_id() -> str:
    env_release_id = os.environ.get(_DEFAULT_RELEASE_ENV_VAR)
    if env_release_id:
        return _safe_manifest_component(env_release_id, "release_id")
    config = _release_config_payload()
    if config is not None:
        configured = config.get("release_id") or config.get("list_id")
        if configured:
            return _safe_manifest_component(str(configured), "release_id")
    return _FALLBACK_RELEASE_ID


def _default_manifest_name() -> str:
    config = _release_config_payload()
    if config is not None:
        configured = config.get("manifest_name")
        if configured:
            return _safe_manifest_component(str(configured), "manifest_name")
    return _DEFAULT_MANIFEST_NAME


def _default_manifest_parts(release_id: str, manifest_name: str) -> tuple[str, ...]:
    return ("resources", "releases", release_id, manifest_name)


def _release_config_payload() -> dict[str, Any] | None:
    for root in _external_release_roots():
        candidate = root / _RELEASE_CONFIG_NAME
        if candidate.is_file():
            return _read_json_path(candidate)
    package_root = files("modlist_translation_wizard")
    candidate = package_root.joinpath("resources", _RELEASE_CONFIG_NAME)
    if candidate.is_file():
        return json.loads(candidate.read_text(encoding="utf-8"))
    return None


def _load_manifest_file(path: Path, *, source_label: str) -> dict[str, Any]:
    digest_path = path.with_suffix(path.suffix + ".sha256")
    if not digest_path.exists():
        raise WizardManifestError(f"{source_label} manifest digest is missing: {digest_path}")
    manifest_bytes = path.read_bytes()
    digest_parts = digest_path.read_text(encoding="ascii").split()
    if not digest_parts:
        raise WizardManifestError(f"{source_label} manifest digest is empty")
    expected = digest_parts[0].casefold()
    actual = hashlib.sha256(manifest_bytes).hexdigest()
    if expected != actual:
        raise WizardManifestError(f"{source_label} manifest SHA-256 mismatch")
    payload = normalize_wizard_manifest_payload(json.loads(manifest_bytes.decode("utf-8")))
    validate_wizard_manifest(payload)
    return payload


def _resolve_remote_manifest_for(
    *,
    list_id: str,
    manifest_name: str,
    required: bool = False,
) -> RemoteManifestResolution | None:
    payload = _remote_manifest_config_payload(list_id)
    if payload is None:
        if required:
            _set_default_manifest_source(
                {
                    "source": "remote_failed",
                    "requested_mode": MANIFEST_MODE_OTA,
                    "warning": "OTA manifest yapılandırması bulunamadı veya devre dışı.",
                }
            )
        return None
    try:
        config = RemoteManifestConfig.from_payload(
            payload,
            default_list_id=list_id,
            default_manifest_name=manifest_name,
        )
        if config is None:
            if required:
                _set_default_manifest_source(
                    {
                        "source": "remote_failed",
                        "requested_mode": MANIFEST_MODE_OTA,
                        "warning": "OTA manifest kanalı etkin değil.",
                    }
                )
            return None
        remote = resolve_remote_manifest(
            config,
            force_refresh=True,
            raise_on_failure=required,
            allow_stale_cache=False,
        )
        if remote is None and required:
            _set_default_manifest_source(
                {
                    "source": "remote_failed",
                    "requested_mode": MANIFEST_MODE_OTA,
                    "warning": "OTA manifest alınamadı ve kullanılabilir önbellek bulunamadı.",
                }
            )
        return remote
    except WizardManifestError as exc:
        _set_default_manifest_source(
            {
                "source": "remote_failed",
                "warning": str(exc),
            }
        )
        return None


def _set_local_source(
    *,
    source: str,
    path: str,
    requested_mode: str,
    warning: str | None,
) -> None:
    fallback = requested_mode == MANIFEST_MODE_OTA
    _set_default_manifest_source(
        {
            "source": source,
            "requested_mode": requested_mode,
            "active_mode": MANIFEST_MODE_LOCAL,
            "fallback_from": MANIFEST_MODE_OTA if fallback else None,
            "path": path,
            "warning": warning or None,
        }
    )


def _remote_manifest_config_payload(list_id: str) -> dict[str, Any] | None:
    if _env_flag(_REMOTE_DISABLE_ENV_VAR):
        return None
    config_path = os.environ.get(_REMOTE_CONFIG_ENV_VAR)
    if config_path:
        return _read_json_path(Path(config_path))
    index_url = os.environ.get(_REMOTE_INDEX_URL_ENV_VAR)
    if index_url:
        return {
            "enabled": True,
            "list_id": list_id,
            "index_url": index_url,
        }
    for release_dir in external_release_dirs(list_id):
        candidate = release_dir / REMOTE_MANIFEST_CONFIG_NAME
        if candidate.is_file():
            return _read_json_path(candidate)
    package_root = files("modlist_translation_wizard")
    bootstrap_candidate = package_root.joinpath(
        "resources",
        REMOTE_MANIFEST_CONFIG_NAME,
    )
    if bootstrap_candidate.is_file():
        return json.loads(bootstrap_candidate.read_text(encoding="utf-8"))
    candidate = package_root.joinpath(
        "resources",
        "releases",
        list_id,
        REMOTE_MANIFEST_CONFIG_NAME,
    )
    if candidate.is_file():
        return json.loads(candidate.read_text(encoding="utf-8"))
    return None


def _manifest_unavailable_error(
    *,
    requested_mode: str,
    expected_path: str,
    remote_warning: str | None,
) -> WizardManifestError:
    _set_default_manifest_source(
        {
            "source": "unavailable",
            "requested_mode": requested_mode,
            "active_mode": None,
            "path": expected_path,
            "warning": remote_warning or None,
        }
    )
    if requested_mode == MANIFEST_MODE_OTA:
        ota_detail = remote_warning or "OTA manifest alınamadı."
        return WizardManifestError(
            f"Çeviri listesi yüklenemedi. OTA: {ota_detail} "
            f"Yerel manifest de bulunamadı: {expected_path}"
        )
    return WizardManifestError(f"Yerel manifest bulunamadı: {expected_path}")


def _ota_manifest_unavailable_error(remote_warning: str | None) -> WizardManifestError:
    detail = remote_warning or "OTA manifest alınamadı."
    _set_default_manifest_source(
        {
            "source": "unavailable",
            "requested_mode": MANIFEST_MODE_OTA,
            "active_mode": None,
            "warning": detail,
        }
    )
    return WizardManifestError(
        f"Güncel OTA çeviri listesi alınamadı: {detail} "
        "Önbellek kullanılmadı. Yerel paketi kullanmak için 'Yerel' kaynağını seçin."
    )


def _copy_manifest_pair(
    *,
    manifest_path: Path,
    digest_path: Path,
    output_dir: Path,
    manifest_name: str,
) -> Path:
    copied_manifest = output_dir / manifest_name
    copied_digest = output_dir / f"{manifest_name}.sha256"
    copied_manifest.write_bytes(manifest_path.read_bytes())
    copied_digest.write_text(digest_path.read_text(encoding="ascii"), encoding="ascii")
    return copied_manifest


def _remote_source_info(remote: RemoteManifestResolution) -> dict[str, str | None]:
    return {
        "source": remote.source,
        "active_mode": MANIFEST_MODE_OTA,
        "list_id": remote.list_id,
        "channel": remote.channel,
        "version": remote.version,
        "manifest_url": remote.manifest_url,
        "index_url": remote.index_url,
        "path": str(remote.manifest_path),
        "warning": remote.warning,
    }


def _set_default_manifest_source(info: dict[str, str | None]) -> None:
    _LAST_DEFAULT_MANIFEST_SOURCE.clear()
    _LAST_DEFAULT_MANIFEST_SOURCE.update(info)


def _read_json_path(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise WizardManifestError(f"remote manifest config must be a JSON object: {path}")
    return payload


def _env_flag(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().casefold() in {"1", "true", "yes", "on"}


def _release_dir_variants(root: Path, safe_list_id: str | None) -> tuple[Path, ...]:
    if safe_list_id:
        return (
            root,
            root / "resources" / "releases" / safe_list_id,
            root / "resources" / "branding" / safe_list_id,
        )
    return (root,)


def _safe_optional_manifest_component(value: str | None) -> str | None:
    if value is None:
        return None
    return _safe_manifest_component(value, "list_id")


def _safe_manifest_component(value: str, label: str) -> str:
    text = str(value or "").strip()
    if not text or text in {".", ".."}:
        raise WizardManifestError(f"{label} is required")
    if any(separator in text for separator in ("/", "\\")):
        raise WizardManifestError(f"{label} must be a single path component")
    return text
