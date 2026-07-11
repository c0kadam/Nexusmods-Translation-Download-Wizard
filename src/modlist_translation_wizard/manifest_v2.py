"""Target-centric manifest contract for the end-user wizard."""

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
_TARGET_TYPES = {"PLUGIN", "INTERFACE", "BESTIARY", "NATIVE"}
_INSTALL_MODES = {"DSD_CONVERT", "NATIVE_INSTALL", "BUNDLE_DSD"}
_ADD_ON_INSTALL_MODES = {"OUTPUT_MOD_OVERLAY"}
_CONFIDENCE_LEVELS = {
    "VERIFIED_CURATED",
    "VERIFIED_CONVERTED",
    "VERIFIED_ARCHIVE",
    "VERIFIED_METADATA",
    "VERIFIED_BUNDLE",
}
_LEGACY_MANIFEST_KEY_ALIASES = {
    "ss" + "eat_download_list": "curated_download_list",
    "ss" + "eat_download_list.json": "curated_download_list.json",
    "ss" + "eat_download_list.merged.json": "curated_download_list.merged.json",
    "ss" + "eat_dsd_conversion_manifest.json": "mtw_dsd_conversion_manifest.json",
    "ss" + "eat_dsd_conversion_report.md": "mtw_dsd_conversion_report.md",
}
_LEGACY_MANIFEST_VALUE_ALIASES = {
    "VERIFIED_" + "SSE" + "_AT": "VERIFIED_CURATED",
    "SSE" + "_AT_DOWNLOAD_LIST": "CURATED_DOWNLOAD_LIST",
    "ss" + "eat_selected_download": "curated_selected_download",
    "ss" + "eat_download_list.json": "curated_download_list.json",
    "ss" + "eat_download_list.merged.json": "curated_download_list.merged.json",
    "ss" + "eat_dsd_conversion_manifest.json": "mtw_dsd_conversion_manifest.json",
    "ss" + "eat_dsd_conversion_report.md": "mtw_dsd_conversion_report.md",
}
_PLUGIN_EXTENSIONS = {".esp", ".esm", ".esl"}
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
    """Export a simple v2 manifest from decisions.

    Public release manifests should normally be produced by MTT's merged builder,
    which also consumes curated download and archive/conversion evidence.
    """

    profile_path = Path(profile_scan_path)
    decisions_path = Path(decisions_path)
    profile = json.loads(profile_path.read_text(encoding="utf-8-sig"))
    decisions = json.loads(decisions_path.read_text(encoding="utf-8-sig"))
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
            "profile_scan_sha256": _file_sha256(profile_path),
            "translation_decisions_sha256": _file_sha256(decisions_path),
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
    """Build a target-centric v2 manifest from already-curated decisions."""

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
        targets = _clean_string_list(
            [*_as_list(base.get("plugins")), *_as_list(base.get("translation_targets"))]
        )
        if not targets:
            _increment(skipped, "target_missing")
            continue
        for target_path in targets:
            artifacts = _candidate_artifacts(candidate, target_path)
            if not artifacts:
                _increment(skipped, "download_identity_incomplete")
                continue
            target_id = _target_id(target_path)
            target_type = _target_type(target_path)
            confidence = _candidate_confidence(candidate)
            entries.append(
                {
                    "target_id": target_id,
                    "target": {
                        "path": target_path,
                        "normalized_path": _normalize_target(target_path),
                        "type": target_type,
                    },
                    "base": _base_payload(base, candidate),
                    "selection": {
                        "status": status,
                        "confidence": confidence,
                        "translation_name": _clean_text(
                            candidate.get("translation_name")
                            or candidate.get("display_name")
                        ),
                        "score": int(decision.get("score") or 0),
                        "provenance": ["MTT_DECISIONS"],
                        "reasons": _clean_string_list(decision.get("reasons")),
                        "warnings": _clean_string_list(
                            [
                                *_as_list(decision.get("warnings")),
                                *_as_list(candidate.get("warnings")),
                            ]
                        ),
                    },
                    "install": {"mode": _install_mode(target_type)},
                    "artifacts": artifacts,
                    "alternatives": [],
                }
            )

    entries = _dedupe_entries(entries)
    entries.sort(key=lambda item: item["target"]["normalized_path"])
    unique_artifacts = {
        (
            artifact["game_domain"],
            artifact["translation_nexus_mod_id"],
            artifact["translation_file_id"],
        )
        for entry in entries
        for artifact in entry["artifacts"]
    }
    profile_name = _clean_text((profile.get("mo2") or {}).get("profile")) or "Unknown"
    payload = {
        "schema_version": WIZARD_MANIFEST_SCHEMA_VERSION,
        "manifest_id": (
            f"{_required_identifier(list_id, 'list_id')}"
            f"-{_required_identifier(decisions.get('language') or 'tr', 'language')}"
            f"-{normalized_channel}"
        ),
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "release_state": str(release_state or "DRAFT").strip().upper(),
        "tool": {"name": TOOL_NAME, "version": __version__},
        "modlist": {
            "id": _required_identifier(list_id, "list_id"),
            "name": _required_text(list_name, "list_name"),
            "version": _required_text(list_version, "list_version"),
            "supported_profiles": [profile_name],
            "profile_fingerprint_sha256": wizard_profile_fingerprint(profile),
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
            "target_count": len(entries),
            "entry_count": len(entries),
            "artifact_reference_count": sum(
                len(item["artifacts"]) for item in entries
            ),
            "unique_download_count": len(unique_artifacts),
            "conflict_count": 0,
            "skipped": dict(sorted(skipped.items())),
        },
        "entries": entries,
    }
    validate_wizard_manifest(payload)
    return payload


def write_wizard_manifest(
    payload: dict[str, Any], output_path: Path | str
) -> WizardManifestBuildResult:
    payload = normalize_wizard_manifest_payload(payload)
    validate_wizard_manifest(payload)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
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
    payload = normalize_wizard_manifest_payload(
        json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    )
    validate_wizard_manifest(payload)
    return payload


def normalize_wizard_manifest_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy using MTW-native source and confidence vocabulary."""

    normalized = _normalize_manifest_terms(payload)
    if not isinstance(normalized, dict):
        raise WizardManifestError("wizard manifest payload must be an object")
    return normalized


def validate_wizard_manifest(payload: dict[str, Any]) -> None:
    payload = normalize_wizard_manifest_payload(payload)
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
    add_on_packages = payload.get("add_on_packages", [])
    if add_on_packages is not None and not isinstance(add_on_packages, list):
        raise WizardManifestError("wizard manifest add_on_packages must be a list")

    target_ids: set[str] = set()
    target_paths: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise WizardManifestError("invalid wizard manifest entry")
        target_id = _required_text(entry.get("target_id"), "target_id")
        if target_id in target_ids:
            raise WizardManifestError(f"duplicate wizard target id: {target_id}")
        target_ids.add(target_id)
        target = entry.get("target") if isinstance(entry.get("target"), dict) else {}
        target_path = _required_text(target.get("path"), f"target path in {target_id}")
        normalized = _normalize_target(target.get("normalized_path") or target_path)
        if normalized != _normalize_target(target_path):
            raise WizardManifestError(f"target normalized path mismatch in {target_id}")
        if normalized in target_paths:
            raise WizardManifestError(f"duplicate wizard target path: {target_path}")
        target_paths.add(normalized)
        if target.get("type") not in _TARGET_TYPES:
            raise WizardManifestError(f"invalid target type in {target_id}")

        selection = entry.get("selection") if isinstance(entry.get("selection"), dict) else {}
        if selection.get("status") not in {"APPROVED", "NEEDS_REVIEW"}:
            raise WizardManifestError(f"invalid selection status in {target_id}")
        if selection.get("confidence") not in _CONFIDENCE_LEVELS:
            raise WizardManifestError(f"invalid selection confidence in {target_id}")
        install = entry.get("install") if isinstance(entry.get("install"), dict) else {}
        if install.get("mode") not in _INSTALL_MODES:
            raise WizardManifestError(f"invalid install mode in {target_id}")

        artifacts = entry.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            raise WizardManifestError(f"wizard target has no artifacts: {target_id}")
        artifact_ids: set[str] = set()
        for artifact in artifacts:
            _validate_artifact(artifact, target_id, normalized, artifact_ids)
        alternatives = entry.get("alternatives", [])
        if not isinstance(alternatives, list):
            raise WizardManifestError(f"invalid alternatives in {target_id}")
        for artifact in alternatives:
            _validate_artifact(
                artifact,
                target_id,
                normalized,
                artifact_ids,
                require_target=False,
            )

    add_on_ids: set[str] = set()
    for package in add_on_packages or []:
        _validate_add_on_package(package, add_on_ids)


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
                "plugins": sorted(
                    _clean_string_list(item.get("plugins")),
                    key=str.casefold,
                ),
            }
        )
    snapshot = {
        "profile": _clean_text((profile.get("mo2") or {}).get("profile")),
        "mods": mods,
        "active_plugins": sorted(
            _clean_string_list(profile.get("active_plugins")),
            key=str.casefold,
        ),
    }
    return hashlib.sha256(_canonical_bytes(snapshot)).hexdigest()


def _candidate_artifacts(
    candidate: dict[str, Any], target_path: str
) -> list[dict[str, Any]]:
    nexus = candidate.get("nexus") if isinstance(candidate.get("nexus"), dict) else {}
    game_domain = _clean_text(nexus.get("game_domain")) or "skyrimspecialedition"
    primary_mod_id = _positive_int(
        candidate.get("translation_nexus_mod_id") or nexus.get("mod_id")
    )
    primary_file_id = _positive_int(
        candidate.get("translation_file_id") or nexus.get("file_id")
    )
    if primary_mod_id is None or primary_file_id is None:
        return []
    variants: list[dict[str, Any]] = [
        {
            **candidate,
            "translation_nexus_mod_id": primary_mod_id,
            "translation_file_id": primary_file_id,
        }
    ]
    variants.extend(
        item
        for item in _as_list(candidate.get("additional_translation_files"))
        if isinstance(item, dict)
    )
    artifacts: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for variant in variants:
        variant_nexus = (
            variant.get("nexus") if isinstance(variant.get("nexus"), dict) else {}
        )
        mod_id = _positive_int(
            variant.get("translation_nexus_mod_id")
            or variant_nexus.get("mod_id")
            or primary_mod_id
        )
        file_id = _positive_int(
            variant.get("translation_file_id")
            or variant.get("file_id")
            or variant.get("id")
            or variant_nexus.get("file_id")
        )
        if mod_id is None or file_id is None or (mod_id, file_id) in seen:
            continue
        seen.add((mod_id, file_id))
        artifact_game = (
            _clean_text(variant_nexus.get("game_domain")) or game_domain
        )
        artifacts.append(
            {
                "artifact_id": f"nexusmods:{artifact_game}:{mod_id}:{file_id}",
                "source": "MTT_DECISIONS",
                "game_domain": artifact_game,
                "translation_nexus_mod_id": mod_id,
                "translation_file_id": file_id,
                "translation_file_name": _clean_text(
                    variant.get("translation_file_name")
                    or candidate.get("translation_file_name")
                ),
                "translation_version": _clean_text(
                    variant.get("translation_version")
                    or candidate.get("translation_version")
                    or candidate.get("version")
                ),
                "expected_size": _positive_int(
                    variant.get("expected_size") or candidate.get("expected_size")
                ),
                "expected_sha256": _clean_text(
                    variant.get("expected_sha256") or candidate.get("expected_sha256")
                ),
                "required": True,
                "provides": [target_path],
                "evidence": ["curated_decision_target"],
                "install_mode": _install_mode(_target_type(target_path)),
                "source_url": (
                    f"https://www.nexusmods.com/{artifact_game}/mods/{mod_id}"
                    f"?tab=files&file_id={file_id}"
                ),
            }
        )
    return artifacts


def _validate_artifact(
    artifact: object,
    target_id: str,
    normalized_target: str,
    artifact_ids: set[str],
    *,
    require_target: bool = True,
) -> None:
    if not isinstance(artifact, dict):
        raise WizardManifestError(f"invalid artifact in {target_id}")
    artifact_id = _required_text(artifact.get("artifact_id"), "artifact_id")
    if artifact_id in artifact_ids:
        raise WizardManifestError(f"duplicate artifact in {target_id}: {artifact_id}")
    artifact_ids.add(artifact_id)
    if artifact.get("install_mode") not in _INSTALL_MODES:
        raise WizardManifestError(f"artifact has invalid install mode in {target_id}")
    if artifact.get("install_mode") != "BUNDLE_DSD":
        if _positive_int(artifact.get("translation_nexus_mod_id")) is None:
            raise WizardManifestError(f"artifact has no Nexus mod id in {target_id}")
        if _positive_int(artifact.get("translation_file_id")) is None:
            raise WizardManifestError(f"artifact has no Nexus file id in {target_id}")
    provides = {
        _normalize_target(item) for item in _as_list(artifact.get("provides"))
    }
    if require_target and normalized_target not in provides:
        raise WizardManifestError(f"artifact does not provide target in {target_id}")
    source_url = str(artifact.get("source_url") or "")
    if any(f"{key}=" in source_url.casefold() for key in _SECRET_QUERY_KEYS):
        raise WizardManifestError(f"secret-like query parameter in {target_id}")


def _validate_add_on_package(package: object, add_on_ids: set[str]) -> None:
    if not isinstance(package, dict):
        raise WizardManifestError("invalid add-on package")
    package_id = _required_text(package.get("id"), "add-on package id")
    if package_id in add_on_ids:
        raise WizardManifestError(f"duplicate add-on package id: {package_id}")
    add_on_ids.add(package_id)
    if package.get("enabled") is False:
        return
    if package.get("install_mode") not in _ADD_ON_INSTALL_MODES:
        raise WizardManifestError(f"add-on package has invalid install mode: {package_id}")
    if _positive_int(package.get("translation_nexus_mod_id")) is None:
        raise WizardManifestError(f"add-on package has no Nexus mod id: {package_id}")
    if _positive_int(package.get("translation_file_id")) is None:
        raise WizardManifestError(f"add-on package has no Nexus file id: {package_id}")
    game_domain = _clean_text(package.get("game_domain"))
    if not game_domain:
        raise WizardManifestError(f"add-on package has no game domain: {package_id}")
    source_url = str(package.get("source_url") or "")
    if any(f"{key}=" in source_url.casefold() for key in _SECRET_QUERY_KEYS):
        raise WizardManifestError(f"secret-like query parameter in add-on package: {package_id}")


def _base_payload(
    base: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    nexus = base.get("nexus") if isinstance(base.get("nexus"), dict) else {}
    return {
        "name": _clean_text(base.get("name"))
        or f"Nexus mod {candidate.get('base_nexus_mod_id')}",
        "version": _clean_text(base.get("version")),
        "nexus_mod_id": _positive_int(
            nexus.get("mod_id") or candidate.get("base_nexus_mod_id")
        ),
        "nexus_file_id": _positive_int(
            nexus.get("file_id") or candidate.get("base_nexus_file_id")
        ),
    }


def _dedupe_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for entry in entries:
        normalized = entry["target"]["normalized_path"]
        current = result.get(normalized)
        if current is None:
            result[normalized] = entry
            continue
        current_artifacts = {
            item["artifact_id"]: item for item in current["artifacts"]
        }
        for artifact in entry["artifacts"]:
            current_artifacts.setdefault(artifact["artifact_id"], artifact)
        current["artifacts"] = list(current_artifacts.values())
    return list(result.values())


def _candidate_confidence(candidate: dict[str, Any]) -> str:
    if candidate.get("dsd_validation") or candidate.get("conversion_validated"):
        return "VERIFIED_CONVERTED"
    if _as_list(candidate.get("archive_contains_plugins")):
        return "VERIFIED_ARCHIVE"
    return "VERIFIED_METADATA"


def _target_id(path: str) -> str:
    digest = hashlib.sha256(_normalize_target(path).encode("utf-8")).hexdigest()
    return "target-" + digest[:16]


def _target_type(path: str) -> str:
    normalized = str(path).replace("\\", "/")
    suffix = Path(normalized).suffix.casefold()
    if suffix in _PLUGIN_EXTENSIONS:
        return "PLUGIN"
    if normalized.casefold().startswith("interface/creatures/") and suffix == ".json":
        return "BESTIARY"
    if normalized.casefold().startswith("interface/"):
        return "INTERFACE"
    return "NATIVE"


def _install_mode(target_type: str) -> str:
    return "DSD_CONVERT" if target_type == "PLUGIN" else "NATIVE_INSTALL"


def _normalize_target(value: object) -> str:
    return "/".join(
        part
        for part in str(value or "").replace("\\", "/").strip("/").casefold().split("/")
        if part
    )


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
    normalized = "".join(
        character for character in text if character.isalnum() or character == "-"
    )
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
    result: list[str] = []
    seen: set[str] = set()
    for item in _as_list(value):
        text = _clean_text(item)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _normalize_manifest_terms(value: object) -> object:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = _LEGACY_MANIFEST_KEY_ALIASES.get(str(key), key)
            result[normalized_key] = _normalize_manifest_terms(item)
        return result
    if isinstance(value, list):
        return [_normalize_manifest_terms(item) for item in value]
    if isinstance(value, str):
        return _LEGACY_MANIFEST_VALUE_ALIASES.get(value, value)
    return value
