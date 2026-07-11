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

_DEFAULT_RELEASE_ID = "lorerim"
_DEFAULT_MANIFEST_NAME = "manifest.json"
_EXTERNAL_RELEASE_ENV_VAR = "MTW_RELEASE_DIR"
_EXTERNAL_RELEASE_DIR_NAME = "release"
_DEFAULT_MANIFEST_PARTS = (
    "resources",
    "releases",
    _DEFAULT_RELEASE_ID,
    _DEFAULT_MANIFEST_NAME,
)


def load_default_bundled_manifest() -> dict[str, Any]:
    external_manifest = _external_default_manifest_path()
    if external_manifest is not None:
        return _load_manifest_file(external_manifest, source_label="external release")
    return _load_bundled_manifest_parts(_DEFAULT_MANIFEST_PARTS)


def copy_default_bundled_manifest(output_dir: Path | str) -> Path:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    manifest_name = _DEFAULT_MANIFEST_PARTS[-1]
    external_manifest = _external_default_manifest_path()
    if external_manifest is not None:
        manifest_path = output / manifest_name
        digest_path = output / f"{manifest_name}.sha256"
        manifest_path.write_bytes(external_manifest.read_bytes())
        digest_path.write_text(
            external_manifest.with_suffix(external_manifest.suffix + ".sha256").read_text(
                encoding="ascii"
            ),
            encoding="ascii",
        )
        return manifest_path
    root = files("modlist_translation_wizard")
    manifest = root.joinpath(*_DEFAULT_MANIFEST_PARTS)
    digest = root.joinpath(*_DEFAULT_MANIFEST_PARTS[:-1], manifest_name + ".sha256")
    manifest_path = output / manifest_name
    digest_path = output / f"{manifest_name}.sha256"
    manifest_path.write_bytes(manifest.read_bytes())
    digest_path.write_text(digest.read_text(encoding="ascii"), encoding="ascii")
    return manifest_path


def load_bundled_manifest(*, list_id: str, manifest_name: str) -> dict[str, Any]:
    safe_list_id = _safe_manifest_component(list_id, "list_id")
    safe_manifest_name = _safe_manifest_component(manifest_name, "manifest_name")
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


def external_release_dirs(list_id: str | None = None) -> tuple[Path, ...]:
    """Return release directories that may be changed without rebuilding the app."""

    safe_list_id = _safe_optional_manifest_component(list_id)
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

    candidates: list[Path] = []
    seen: set[str] = set()
    for root in roots:
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


def _external_default_manifest_path() -> Path | None:
    for release_dir in external_release_dirs(_DEFAULT_RELEASE_ID):
        manifest_path = release_dir / _DEFAULT_MANIFEST_NAME
        if manifest_path.is_file():
            return manifest_path
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
