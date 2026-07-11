"""Curated manifest export for list-specific end-user wizards."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from modlist_translation_wizard.version import TOOL_NAME, __version__

WIZARD_MANIFEST_SCHEMA_VERSION = "mtt-wizard-manifest.v2"
_SUPPORTED_CHANNELS = {"stable", "extended"}
_SECRET_QUERY_KEYS = {
    "api_key",
    "apikey",
    "expires",
    "key",
    "session",
    "signature",
    "token",
}


class WizardManifestError(ValueError):
    """Raised when a wizard manifest violates the distribution contract."""


@dataclass(frozen=True, slots=True)
class WizardManifestBuildResult:
    manifest_path: Path
    digest_path: Path
    payload: dict[str, Any]
    sha256: str


def export_wizard_manifest(
    *,
    profile_scan_path: Path | str,
    decisions_path: Path | str,
    output_path: Path | str,
    list_id: str,
    list_name: str,
    list_version: str,
    output_mod_name: str,
    channel: str = "stable",
    release_state: str = "DRAFT",
    registered_app_slug: str | None = None,
    created_at: str | None = None,
) -> WizardManifestBuildResult:
    profile = json.loads(Path(profile_scan_path).read_text(encoding="utf-8"))
    decisions = json.loads(Path(decisions_path).read_text(encoding="utf-8"))
    payload = build_wizard_manifest(
        profile=profile,
        decisions=decisions,
        list_id=list_id,
        list_name=list_name,
        list_version=list_version,
        output_mod_name=output_mod_name,
        channel=channel,
        release_state=release_state,
        registered_app_slug=registered_app_slug,
        created_at=created_at,
        source_metadata={
            "profile_scan_sha256": _file_sha256(Path(profile_scan_path)),
            "translation_decisions_sha256": _file_sha256(Path(decisions_path)),
        },
    )
    return write_wizard_manifest(payload, output_path)


def build_wizard_manifest(
    *,
    profile: dict[str, Any],
    decisions: dict[str, Any],
    list_id: str,
    list_name: str,
    list_version: str,
    output_mod_name: str,
    channel: str = "stable",
    release_state: str = "DRAFT",
    registered_app_slug: str | None = None,
    created_at: str | None = None,
    source_metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    normalized_channel = str(channel or "stable").strip().casefold()
    if normalized_channel not in _SUPPORTED_CHANNELS:
        raise WizardManifestError(f"unsupported wizard channel: {channel!r}")
    allowed_statuses = {"APPROVED"}
    if normalized_channel == "extended":
        allowed_statuses.add("NEEDS_REVIEW")

    entries: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}
    for decision in decisions.get("decisions", []):
        if not isinstance(decision, dict):
            _increment(skipped, "invalid_decision")
            continue
        status = str(decision.get("status") or "")
        if status not in allowed_statuses:
            _increment(skipped, f"status_{status.casefold() or 'unknown'}")
            continue
        base = decision.get("base") if isinstance(decision.get("base"), dict) else {}
        candidate = (
            decision.get("selected_candidate")
            if isinstance(decision.get("selected_candidate"), dict)
            else None
        )
        if candidate is None:
            _increment(skipped, "selected_candidate_missing")
            continue
        base_nexus = base.get("nexus") if isinstance(base.get("nexus"), dict) else {}
        base_mod_id = _positive_int(
            base_nexus.get("mod_id") or candidate.get("base_nexus_mod_id")
        )
        if base_mod_id is None:
            _increment(skipped, "base_nexus_mod_id_missing")
            continue
        artifacts = _candidate_artifacts(candidate)
        if not artifacts:
            _increment(skipped, "download_identity_incomplete")
            continue
        base_name = _clean_text(base.get("name")) or f"Nexus mod {base_mod_id}"
        entry_seed = {
            "base_nexus_mod_id": base_mod_id,
            "base_name": base_name,
            "artifacts": [
                (item["translation_nexus_mod_id"], item["translation_file_id"])
                for item in artifacts
            ],
        }
        entry_id = "entry-" + hashlib.sha256(_canonical_bytes(entry_seed)).hexdigest()[:16]
        entries.append(
            {
                "entry_id": entry_id,
                "base": {
                    "name": base_name,
                    "version": _clean_text(base.get("version")),
                    "nexus_mod_id": base_mod_id,
                    "nexus_file_id": _positive_int(
                        base_nexus.get("file_id") or candidate.get("base_nexus_file_id")
                    ),
                    "plugins": _clean_string_list(base.get("plugins")),
                },
                "selection": {
                    "status": status,
                    "score": int(decision.get("score") or 0),
                    "source": _clean_text(candidate.get("source")) or "NexusMods",
                    "translation_name": _clean_text(
                        candidate.get("translation_name") or candidate.get("display_name")
                    ),
                    "reasons": _clean_string_list(decision.get("reasons")),
                    "warnings": _clean_string_list(
                        [
                            *_as_list(decision.get("warnings")),
                            *_as_list(candidate.get("warnings")),
                        ]
                    ),
                },
                "artifacts": artifacts,
            }
        )

    entries.sort(
        key=lambda item: (
            int(item["base"]["nexus_mod_id"]),
            str(item["base"]["name"]).casefold(),
            str(item["entry_id"]),
        )
    )
    profile_fingerprint = wizard_profile_fingerprint(profile)
    profile_name = _clean_text((profile.get("mo2") or {}).get("profile")) or "Unknown"
    artifact_count = sum(len(item["artifacts"]) for item in entries)
    unique_artifacts = {
        (
            artifact["game_domain"],
            artifact["translation_nexus_mod_id"],
            artifact["translation_file_id"],
        )
        for entry in entries
        for artifact in entry["artifacts"]
    }
    payload = {
        "schema_version": WIZARD_MANIFEST_SCHEMA_VERSION,
        "manifest_id": _required_identifier(list_id, "list_id")
        + f"-{_required_identifier(str(decisions.get('language') or 'tr'), 'language')}"
        + f"-{normalized_channel}",
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "release_state": str(release_state or "DRAFT").strip().upper(),
        "tool": {"name": TOOL_NAME, "version": __version__},
        "modlist": {
            "id": _required_identifier(list_id, "list_id"),
            "name": _required_text(list_name, "list_name"),
            "version": _required_text(list_version, "list_version"),
            "supported_profiles": [profile_name],
            "profile_fingerprint_sha256": profile_fingerprint,
        },
        "language": _clean_text(decisions.get("language")) or "tr",
        "channel": normalized_channel,
        "output": {
            "mod_name": _required_text(output_mod_name, "output_mod_name"),
            "install_mode": "STAGED_MO2_MOD",
            "profile_activation_requires_confirmation": True,
        },
        "nexus": {
            "discovery_enabled": False,
            "request_scope": "KNOWN_MOD_AND_FILE_IDS_ONLY",
            "authentication": {
                "primary": "REGISTERED_APPLICATION_SSO",
                "registered_app_slug": _clean_text(registered_app_slug),
                "manual_api_key": "TESTING_ONLY",
                "secret_storage": "OS_CREDENTIAL_STORE",
            },
            "delivery": {
                "premium_api": "SUPPORTED",
                "non_premium_nxm": "SUPPORTED",
                "non_premium_contract": ["nxm_key", "expires", "user_initiated"],
            },
        },
        "source": {
            "profile_scan_schema": profile.get("schema_version"),
            "translation_decisions_schema": decisions.get("schema_version"),
            "input_sha256": dict(source_metadata or {}),
        },
        "summary": {
            "entry_count": len(entries),
            "artifact_reference_count": artifact_count,
            "unique_download_count": len(unique_artifacts),
            "skipped": dict(sorted(skipped.items())),
        },
        "entries": entries,
    }
    validate_wizard_manifest(payload)
    return payload


def write_wizard_manifest(
    payload: dict[str, Any], output_path: Path | str
) -> WizardManifestBuildResult:
    validate_wizard_manifest(payload)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    path.write_text(rendered, encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    digest_path = path.with_suffix(path.suffix + ".sha256")
    digest_path.write_text(f"{digest}  {path.name}\n", encoding="ascii")
    return WizardManifestBuildResult(path, digest_path, payload, digest)


def load_wizard_manifest(
    path: Path | str,
    *,
    verify_digest: bool = True,
) -> dict[str, Any]:
    manifest_path = Path(path)
    if verify_digest:
        digest_path = manifest_path.with_suffix(manifest_path.suffix + ".sha256")
        if not digest_path.exists():
            raise WizardManifestError(f"missing wizard manifest digest: {digest_path}")
        expected = digest_path.read_text(encoding="ascii").split()[0].casefold()
        actual = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        if expected != actual:
            raise WizardManifestError("wizard manifest SHA-256 mismatch")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    validate_wizard_manifest(payload)
    return payload


def validate_wizard_manifest(payload: dict[str, Any]) -> None:
    if payload.get("schema_version") != WIZARD_MANIFEST_SCHEMA_VERSION:
        raise WizardManifestError("unsupported wizard manifest schema")
    nexus = payload.get("nexus") if isinstance(payload.get("nexus"), dict) else {}
    if nexus.get("discovery_enabled") is not False:
        raise WizardManifestError("distributed wizard manifests must disable discovery")
    if nexus.get("request_scope") != "KNOWN_MOD_AND_FILE_IDS_ONLY":
        raise WizardManifestError("wizard request scope must be known Nexus IDs only")
    if payload.get("channel") not in _SUPPORTED_CHANNELS:
        raise WizardManifestError("invalid wizard release channel")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise WizardManifestError("wizard manifest entries must be a list")
    entry_ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise WizardManifestError("invalid wizard manifest entry")
        entry_id = _required_text(entry.get("entry_id"), "entry_id")
        if entry_id in entry_ids:
            raise WizardManifestError(f"duplicate wizard entry id: {entry_id}")
        entry_ids.add(entry_id)
        selection = entry.get("selection") if isinstance(entry.get("selection"), dict) else {}
        if selection.get("status") not in {"APPROVED", "NEEDS_REVIEW"}:
            raise WizardManifestError(f"invalid selection status in {entry_id}")
        artifacts = entry.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            raise WizardManifestError(f"wizard entry has no artifacts: {entry_id}")
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise WizardManifestError(f"invalid artifact in {entry_id}")
            if _positive_int(artifact.get("translation_nexus_mod_id")) is None:
                raise WizardManifestError(f"artifact has no Nexus mod id in {entry_id}")
            if _positive_int(artifact.get("translation_file_id")) is None:
                raise WizardManifestError(f"artifact has no Nexus file id in {entry_id}")
            source_url = str(artifact.get("source_url") or "")
            if any(f"{key}=" in source_url.casefold() for key in _SECRET_QUERY_KEYS):
                raise WizardManifestError(f"secret-like query parameter in {entry_id}")


def wizard_profile_fingerprint(profile: dict[str, Any]) -> str:
    mods: list[dict[str, Any]] = []
    for item in profile.get("mods", []):
        if not isinstance(item, dict) or not item.get("enabled"):
            continue
        nexus = item.get("nexus") if isinstance(item.get("nexus"), dict) else {}
        mods.append(
            {
                "name": _clean_text(item.get("name")),
                "version": _clean_text(item.get("version")),
                "nexus_mod_id": _positive_int(nexus.get("mod_id")),
                "nexus_file_id": _positive_int(nexus.get("file_id")),
                "plugins": sorted(_clean_string_list(item.get("plugins")), key=str.casefold),
            }
        )
    snapshot = {
        "profile": _clean_text((profile.get("mo2") or {}).get("profile")),
        "mods": mods,
        "active_plugins": sorted(
            _clean_string_list(profile.get("active_plugins")), key=str.casefold
        ),
    }
    return hashlib.sha256(_canonical_bytes(snapshot)).hexdigest()


def _candidate_artifacts(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    nexus = candidate.get("nexus") if isinstance(candidate.get("nexus"), dict) else {}
    game_domain = _clean_text(nexus.get("game_domain")) or "skyrimspecialedition"
    translation_mod_id = _positive_int(
        candidate.get("translation_nexus_mod_id") or nexus.get("mod_id")
    )
    primary_file_id = _positive_int(candidate.get("translation_file_id") or nexus.get("file_id"))
    if translation_mod_id is None or primary_file_id is None:
        return []
    variants = [
        {
            "translation_file_id": primary_file_id,
            "translation_file_name": candidate.get("translation_file_name"),
            "translation_version": candidate.get("translation_version") or candidate.get("version"),
            "expected_size": candidate.get("expected_size"),
            "reason": "primary_translation_file",
        }
    ]
    for item in _as_list(candidate.get("additional_translation_files")):
        if isinstance(item, dict):
            variants.append(item)
    artifacts: list[dict[str, Any]] = []
    seen: set[int] = set()
    for variant in variants:
        file_id = _positive_int(
            variant.get("translation_file_id")
            or variant.get("file_id")
            or variant.get("id")
        )
        if file_id is None or file_id in seen:
            continue
        seen.add(file_id)
        artifacts.append(
            {
                "game_domain": game_domain,
                "translation_nexus_mod_id": translation_mod_id,
                "translation_file_id": file_id,
                "translation_file_name": _clean_text(variant.get("translation_file_name")),
                "translation_version": _clean_text(variant.get("translation_version")),
                "expected_size": _positive_int(variant.get("expected_size")),
                "expected_sha256": _clean_text(variant.get("expected_sha256")),
                "required": True,
                "reason": _clean_text(variant.get("reason")) or "required_translation_file",
                "source_url": (
                    f"https://www.nexusmods.com/{game_domain}/mods/{translation_mod_id}"
                    f"?tab=files&file_id={file_id}"
                ),
            }
        )
    return artifacts


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _increment(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


def _required_identifier(value: object, label: str) -> str:
    text = _required_text(value, label).casefold().replace("_", "-").replace(" ", "-")
    normalized = "".join(character for character in text if character.isalnum() or character == "-")
    normalized = "-".join(part for part in normalized.split("-") if part)
    if not normalized:
        raise WizardManifestError(f"{label} has no usable identifier characters")
    return normalized


def _required_text(value: object, label: str) -> str:
    text = _clean_text(value)
    if not text:
        raise WizardManifestError(f"{label} is required")
    return text


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _clean_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _as_list(value: object) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _clean_string_list(value: object) -> list[str]:
    return list(dict.fromkeys(text for item in _as_list(value) if (text := _clean_text(item))))


# The v2 contract is kept in a separate module so existing installations importing
# this public module continue to resolve the same API names during the schema migration.
from modlist_translation_wizard.manifest_v2 import (  # noqa: E402,F401
    WIZARD_MANIFEST_SCHEMA_VERSION,
    WizardManifestBuildResult,
    WizardManifestError,
    build_wizard_manifest,
    export_wizard_manifest,
    load_wizard_manifest,
    normalize_wizard_manifest_payload,
    validate_wizard_manifest,
    wizard_profile_fingerprint,
    write_wizard_manifest,
)
