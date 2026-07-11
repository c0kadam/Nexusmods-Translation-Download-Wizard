"""Runtime planning for a curated wizard manifest without Nexus discovery."""

from __future__ import annotations

import json
import hashlib
import shutil
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from modlist_translate_tool.archives.safe_extractor import (
    ExtractionLimits,
    extract_zip_item,
)
from modlist_translate_tool.downloads.local_store import planned_archive_path
from modlist_translate_tool.downloads.models import DownloadQueueItem, DownloadRequest
from modlist_translate_tool.downloads.planner import (
    DownloadPlanRunResult,
    plan_downloads_from_decisions,
)
from modlist_translate_tool.models.extraction import ExtractionPlanItem
from modlist_translate_tool.nexus.api_client import NexusApiClient
from modlist_translate_tool.nexus.downloader import (
    DownloadRunResult,
    FileDownloader,
    download_archives_from_queue,
)
from modlist_translate_tool.reports.download_report_writer import render_download_plan_report
from modlist_translation_wizard.archive_conversion import (
    WizardArchiveConversionRunResult,
    convert_downloaded_archives_to_mtw_dsd,
)
from modlist_translation_wizard.manifest import (
    WizardManifestError,
    load_wizard_manifest,
    wizard_profile_fingerprint,
)
from modlist_translation_wizard.nexus_auth import temporary_nexus_api_key_env


@dataclass(frozen=True, slots=True)
class WizardPremiumPlanResult:
    preflight_path: Path
    decisions_path: Path
    download_plan: DownloadPlanRunResult
    preflight_payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class WizardPremiumDownloadResult:
    plan: WizardPremiumPlanResult
    download_run: DownloadRunResult


@dataclass(frozen=True, slots=True)
class WizardConversionResult:
    result_path: Path
    staging_root: Path
    conversion: WizardArchiveConversionRunResult
    result_payload: dict[str, Any]


MTW_CONVERSION_MANIFEST_NAME = "mtw_dsd_conversion_manifest.json"
MTW_CONVERSION_REPORT_NAME = "mtw_dsd_conversion_report.md"
_ADD_ON_ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar"}
_ADD_ON_EXTRACTION_LIMITS = ExtractionLimits(
    max_files=20_000,
    max_total_uncompressed_bytes=4 * 1024 * 1024 * 1024,
    max_single_file_bytes=1024 * 1024 * 1024,
)
_LEGACY_RUNTIME_KEY_ALIASES = {
    "s" + "seat_db_export_enabled": "translation_memory_export_enabled",
    "s" + "seat_db_output": "translation_memory_output",
    "s" + "seat_db_canonical_name_count": "translation_memory_canonical_name_count",
    "s" + "seat_db_export_plugins": "translation_memory_export_plugins",
    "s" + "seat_db_export_entries": "translation_memory_export_entries",
    "s" + "seat_db_export": "translation_memory_export",
}
_LEGACY_RUNTIME_VALUE_ALIASES = {
    "s" + "seat-dsd-conversion.v1": "mtw-dsd-conversion.v1",
}


def non_premium_capability() -> dict[str, Any]:
    return {
        "status": "SUPPORTED",
        "transport": "NEXUS_NXM_KEY_EXPIRES",
        "requires_user_initiated_nxm_link": True,
        "accepted_fields": ["game_domain", "mod_id", "file_id", "key", "expires"],
        "secrets_must_not_be_persisted": ["key", "expires", "download_url"],
    }


def build_wizard_preflight(
    manifest: dict[str, Any],
    profile: dict[str, Any],
    *,
    delivery_mode: str = "PREMIUM_API",
) -> dict[str, Any]:
    mode = str(delivery_mode or "PREMIUM_API").strip().upper()
    if mode not in {"PREMIUM_API", "NON_PREMIUM_NXM"}:
        raise WizardManifestError(f"unsupported wizard delivery mode: {delivery_mode!r}")
    expected_fingerprint = str(
        (manifest.get("modlist") or {}).get("profile_fingerprint_sha256") or ""
    )
    actual_fingerprint = wizard_profile_fingerprint(profile)
    exact_profile = expected_fingerprint == actual_fingerprint
    installed_targets = _profile_targets(profile)
    installed_mods_by_id = _profile_mods_by_nexus_id(profile)
    matched: list[str] = []
    missing: list[dict[str, Any]] = []
    compatibility_warnings: list[str] = []
    if expected_fingerprint and not exact_profile:
        compatibility_warnings.append(
            "Profil parmak izi paketle birebir eşleşmiyor; hedefler mümkün olduğunda "
            "kurulu Nexus mod bilgileriyle doğrulandı."
        )
    for entry in manifest.get("entries", []):
        target = entry.get("target") if isinstance(entry.get("target"), dict) else {}
        base = entry.get("base") if isinstance(entry.get("base"), dict) else {}
        target_id = str(entry.get("target_id") or "")
        target_path = str(target.get("path") or "")
        expected_mod_id = _positive_int(base.get("nexus_mod_id"))
        owners = installed_targets.get(_normalize_target(target_path), [])
        if not owners and expected_mod_id is not None:
            owner = installed_mods_by_id.get(expected_mod_id)
            if owner is not None:
                owners = [owner]
                if not exact_profile:
                    compatibility_warnings.append(
                        f"{target_path}: hedef dosya güncel profil taramasında görünmedi; "
                        f"kurulu Nexus mod id {expected_mod_id} ile eşleştirildi."
                    )
        if not owners:
            missing.append(
                {
                    "target_id": target_id,
                    "target_path": target_path,
                    "target_type": target.get("type"),
                    "base_name": base.get("name"),
                    "base_nexus_mod_id": base.get("nexus_mod_id"),
                }
            )
            continue
        matched.append(target_id)
        installed = next(
            (item for item in owners if _base_mod_id(item) == expected_mod_id),
            owners[0],
        )
        expected_file_id = _positive_int(base.get("nexus_file_id"))
        installed_file_id = _base_file_id(installed)
        if expected_file_id and installed_file_id and expected_file_id != installed_file_id:
            compatibility_warnings.append(
                f"{target_path}: installed Nexus file id {installed_file_id} "
                f"does not match manifest file id {expected_file_id}"
            )

    if not missing:
        status = "READY"
    else:
        status = "REVIEW_REQUIRED"
    return {
        "schema_version": "wizard-preflight.v1",
        "manifest_id": manifest.get("manifest_id"),
        "delivery_mode": mode,
        "status": status,
        "discovery_performed": False,
        "profile": {
            "name": (profile.get("mo2") or {}).get("profile"),
            "expected_fingerprint_sha256": expected_fingerprint,
            "actual_fingerprint_sha256": actual_fingerprint,
            "exact_match": exact_profile,
        },
        "summary": {
            "manifest_entries": len(manifest.get("entries", [])),
            "matched_entries": len(matched),
            "missing_entries": len(missing),
            "compatibility_warning_count": len(compatibility_warnings),
        },
        "matched_entry_ids": matched,
        "matched_target_ids": matched,
        "missing_entries": missing,
        "compatibility_warnings": compatibility_warnings,
        "non_premium": non_premium_capability(),
    }


def plan_premium_downloads_from_manifest(
    *,
    manifest_path: Path | str,
    profile_scan_path: Path | str,
    download_dir: Path | str,
    out_dir: Path | str,
    auth_env_var: str = "NEXUS_API_KEY",
    api_key: str | None = None,
    allow_profile_drift: bool = False,
) -> WizardPremiumPlanResult:
    return plan_downloads_from_manifest(
        manifest_path=manifest_path,
        profile_scan_path=profile_scan_path,
        download_dir=download_dir,
        out_dir=out_dir,
        delivery_mode="PREMIUM_API",
        auth_env_var=auth_env_var,
        api_key=api_key,
        allow_profile_drift=allow_profile_drift,
    )


def plan_downloads_from_manifest(
    *,
    manifest_path: Path | str,
    profile_scan_path: Path | str,
    download_dir: Path | str,
    out_dir: Path | str,
    delivery_mode: str,
    auth_env_var: str = "NEXUS_API_KEY",
    api_key: str | None = None,
    allow_profile_drift: bool = False,
) -> WizardPremiumPlanResult:
    manifest_file = Path(manifest_path)
    manifest = load_wizard_manifest(manifest_file)
    profile = json.loads(Path(profile_scan_path).read_text(encoding="utf-8"))
    preflight = build_wizard_preflight(
        manifest,
        profile,
        delivery_mode=delivery_mode,
    )
    if preflight["status"] != "READY" and not allow_profile_drift:
        raise WizardManifestError(
            "wizard profile preflight is not READY; refusing download planning"
        )
    matched_ids = set(preflight["matched_target_ids"])
    locally_satisfied_sources = _manifest_locally_satisfied_artifact_sources(manifest)
    decisions_payload = _manifest_decisions(
        manifest,
        matched_ids,
        profile=profile,
        locally_satisfied_artifact_sources=locally_satisfied_sources,
    )
    download_cache_roots = _manifest_download_cache_roots(manifest, manifest_file)
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    preflight_path = output_dir / "wizard_preflight.json"
    decisions_path = output_dir / "wizard_translation_decisions.json"
    preflight_path.write_text(
        json.dumps(preflight, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    decisions_path.write_text(
        json.dumps(decisions_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    with temporary_nexus_api_key_env(api_key, env_var_name=auth_env_var):
        plan = plan_downloads_from_decisions(
            decisions_path=decisions_path,
            language=str(manifest.get("language") or "tr"),
            download_dir=download_dir,
            out_dir=output_dir / "premium-download-plan",
            include_needs_review=manifest.get("channel") == "extended",
            auth_env_var=auth_env_var,
            additional_download_dirs=download_cache_roots,
        )
    plan = _append_add_on_packages_to_download_plan(
        plan,
        manifest=manifest,
        download_dir=Path(download_dir),
        download_cache_roots=download_cache_roots,
    )
    return WizardPremiumPlanResult(preflight_path, decisions_path, plan, preflight)


def run_premium_downloads_from_manifest(
    *,
    manifest_path: Path | str,
    profile_scan_path: Path | str,
    download_dir: Path | str,
    out_dir: Path | str,
    api_key: str,
    auth_env_var: str = "NEXUS_API_KEY",
    allow_profile_drift: bool = False,
    overwrite: bool = False,
    file_downloader: FileDownloader | None = None,
    max_items: int | None = None,
    max_attempts: int = 3,
    client_factory: Callable[[str], NexusApiClient] | None = None,
) -> WizardPremiumDownloadResult:
    plan = plan_premium_downloads_from_manifest(
        manifest_path=manifest_path,
        profile_scan_path=profile_scan_path,
        download_dir=download_dir,
        out_dir=out_dir,
        auth_env_var=auth_env_var,
        api_key=api_key,
        allow_profile_drift=allow_profile_drift,
    )
    return run_premium_downloads_from_plan(
        plan=plan,
        api_key=api_key,
        auth_env_var=auth_env_var,
        overwrite=overwrite,
        file_downloader=file_downloader,
        max_items=max_items,
        max_attempts=max_attempts,
        client_factory=client_factory,
    )


def run_premium_downloads_from_plan(
    *,
    plan: WizardPremiumPlanResult,
    api_key: str,
    auth_env_var: str = "NEXUS_API_KEY",
    overwrite: bool = False,
    file_downloader: FileDownloader | None = None,
    max_items: int | None = None,
    max_attempts: int = 3,
    client_factory: Callable[[str], NexusApiClient] | None = None,
    queue_path: Path | str | None = None,
) -> WizardPremiumDownloadResult:
    if not str(api_key or "").strip():
        raise WizardManifestError("Nexus API key is required for Premium downloads")
    factory = client_factory or (lambda key: NexusApiClient(key))
    client = factory(api_key)
    output_dir = plan.download_plan.queue_path.parent.parent
    download_run = download_archives_from_queue(
        queue_path=queue_path or plan.download_plan.queue_path,
        out_dir=output_dir / "premium-download-run",
        client=client,
        overwrite=overwrite,
        file_downloader=file_downloader,
        max_items=max_items,
        max_attempts=max_attempts,
    )
    return WizardPremiumDownloadResult(plan=plan, download_run=download_run)


def convert_downloaded_translations_from_manifest(
    *,
    manifest_path: Path | str,
    profile_scan_path: Path | str,
    decisions_path: Path | str,
    download_queue_path: Path | str,
    out_dir: Path | str,
    staging_root: Path | str | None = None,
    seven_zip_path: Path | str | None = None,
    overwrite: bool = True,
    allow_profile_drift: bool = False,
    output_mod_name_override: str | None = None,
    progress_status_path: Path | str | None = None,
) -> WizardConversionResult:
    _write_conversion_progress(progress_status_path, "loading_manifest")
    manifest_file = Path(manifest_path)
    manifest = load_wizard_manifest(manifest_path)
    _write_conversion_progress(progress_status_path, "loading_profile")
    profile = json.loads(Path(profile_scan_path).read_text(encoding="utf-8"))
    _write_conversion_progress(progress_status_path, "building_preflight")
    preflight = build_wizard_preflight(manifest, profile, delivery_mode="PREMIUM_API")
    if preflight["status"] != "READY" and not allow_profile_drift:
        raise WizardManifestError(
            "wizard profile preflight is not READY; refusing conversion"
        )

    _write_conversion_progress(progress_status_path, "reading_download_queue")
    queue_payload = json.loads(Path(download_queue_path).read_text(encoding="utf-8"))
    _write_conversion_progress(progress_status_path, "checking_download_readiness")
    readiness = _download_readiness(manifest, queue_payload)
    if not readiness["complete"]:
        raise WizardManifestError(
            "download queue is incomplete; "
            f"{readiness['missing_count']} required archive(s) are unavailable"
        )

    _write_conversion_progress(progress_status_path, "preparing_output")
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stage_root = Path(staging_root) if staging_root is not None else output_dir / "staging" / "mods"
    stage_root.mkdir(parents=True, exist_ok=True)
    modlist_name = str((manifest.get("modlist") or {}).get("name") or "Modlist")
    language = str(manifest.get("language") or "tr")
    expected_output_name = str((manifest.get("output") or {}).get("mod_name") or "").strip()
    output_name_override = output_mod_name_override or expected_output_name or None
    local_dsd_sources = _manifest_local_dsd_sources(manifest, manifest_file)
    converter_kwargs = {
        "profile_scan_path": profile_scan_path,
        "decisions_path": decisions_path,
        "download_queue_path": download_queue_path,
        "modlist_name": modlist_name,
        "language": language,
        "output_root": stage_root,
        "out_dir": output_dir / "conversion",
        "seven_zip_path": seven_zip_path,
        "overwrite": overwrite,
        "local_dsd_sources": local_dsd_sources,
        "export_" + "ss" + "eat_db": False,
        "mark_review_outputs": manifest.get("channel") == "extended",
        "output_mod_name_override": output_name_override,
        "progress_status_path": progress_status_path,
    }
    _write_conversion_progress(progress_status_path, "running_archive_conversion")
    conversion = _publish_conversion_outputs_for_mtw(
        convert_downloaded_archives_to_mtw_dsd(**converter_kwargs)
    )

    conversion_summary = conversion.manifest_payload.get("summary", {})
    _write_conversion_progress(progress_status_path, "applying_add_on_packages")
    add_on_payload = _apply_add_on_packages(
        manifest=manifest,
        queue_payload=queue_payload,
        output_mod_path=Path(conversion.output_mod_path),
        staging_root=stage_root,
        out_dir=output_dir,
    )
    add_on_summary = add_on_payload.get("summary", {})
    conversion_failed = int(conversion_summary.get("failed_items") or 0) > 0
    add_on_failed = int(add_on_summary.get("failed") or 0) > 0
    result_payload = {
        "schema_version": "wizard-conversion-result.v1",
        "manifest_id": manifest.get("manifest_id"),
        "status": (
            "COMPLETED_WITH_FAILURES"
            if conversion_failed or add_on_failed
            else "COMPLETED"
        ),
        "install_state": "STAGED_NOT_INSTALLED",
        "staging_root": str(stage_root),
        "output_mod_path": str(conversion.output_mod_path),
        "conversion_manifest": str(conversion.manifest_path),
        "conversion_report": str(conversion.report_path),
        "summary": conversion_summary,
        "downloads": readiness,
        "add_on_packages": add_on_payload,
        "local_dsd_sources": [str(path) for path in local_dsd_sources],
    }
    _write_conversion_progress(progress_status_path, "writing_result")
    result_path = output_dir / "wizard_conversion_result.json"
    result_path.write_text(
        json.dumps(result_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return WizardConversionResult(
        result_path=result_path,
        staging_root=stage_root,
        conversion=conversion,
        result_payload=result_payload,
    )


def _write_conversion_progress(path: Path | str | None, stage: str) -> None:
    if path is None:
        return
    status_path = Path(path)
    try:
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(
            json.dumps(
                {
                    "schema_version": "mtw-conversion-worker-status.v1",
                    "ok": False,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "stage": stage,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def load_wizard_conversion_result(result_path: Path | str) -> WizardConversionResult:
    path = Path(result_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    conversion_manifest_path = Path(str(payload.get("conversion_manifest") or ""))
    conversion_report_path = Path(str(payload.get("conversion_report") or ""))
    output_mod_path = Path(str(payload.get("output_mod_path") or ""))
    staging_root = Path(str(payload.get("staging_root") or output_mod_path.parent))
    try:
        conversion_manifest_payload = json.loads(
            conversion_manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError, json.JSONDecodeError):
        conversion_manifest_payload = payload.get("summary") or {}
    conversion = WizardArchiveConversionRunResult(
        manifest_path=conversion_manifest_path,
        report_path=conversion_report_path,
        output_mod_path=output_mod_path,
        manifest_payload=conversion_manifest_payload,
    )
    return WizardConversionResult(
        result_path=path,
        staging_root=staging_root,
        conversion=conversion,
        result_payload=payload,
    )


def _publish_conversion_outputs_for_mtw(
    conversion: WizardArchiveConversionRunResult,
) -> WizardArchiveConversionRunResult:
    manifest_path = _move_runtime_artifact(
        Path(conversion.manifest_path),
        MTW_CONVERSION_MANIFEST_NAME,
    )
    report_path = _move_runtime_artifact(
        Path(conversion.report_path),
        MTW_CONVERSION_REPORT_NAME,
    )
    manifest_payload = _sanitize_runtime_payload_for_mtw(conversion.manifest_payload)
    if manifest_path.exists():
        file_payload = _sanitize_json_artifact_for_mtw(manifest_path)
        if isinstance(file_payload, dict):
            manifest_payload = file_payload
    if report_path.exists():
        _sanitize_report_artifact_for_mtw(report_path)
    return replace(
        conversion,
        manifest_path=manifest_path,
        report_path=report_path,
        manifest_payload=manifest_payload,
    )


def _move_runtime_artifact(path: Path, target_name: str) -> Path:
    target = path.with_name(target_name)
    if path == target:
        return target
    if path.exists():
        if target.exists():
            target.unlink()
        try:
            path.replace(target)
        except PermissionError:
            shutil.copy2(path, target)
            try:
                path.unlink()
            except PermissionError:
                pass
    return target


def _sanitize_json_artifact_for_mtw(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    normalized = _sanitize_runtime_payload_for_mtw(payload)
    path.write_text(
        json.dumps(normalized, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return normalized if isinstance(normalized, dict) else None


def _sanitize_runtime_payload_for_mtw(value: object) -> object:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            result[_LEGACY_RUNTIME_KEY_ALIASES.get(str(key), key)] = (
                _sanitize_runtime_payload_for_mtw(item)
            )
        return result
    if isinstance(value, list):
        return [_sanitize_runtime_payload_for_mtw(item) for item in value]
    if isinstance(value, str):
        return _LEGACY_RUNTIME_VALUE_ALIASES.get(value, value)
    return value


def _sanitize_report_artifact_for_mtw(path: Path) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    text = text.replace("# " + "SSE" + "-AT Style DSD Conversion Report", "# MTW DSD Conversion Report")
    text = text.replace("- " + "SSE" + "-AT export plugins:", "- Translation memory export plugins:")
    text = text.replace("- " + "SSE" + "-AT export entries:", "- Translation memory export entries:")
    text = text.replace("## " + "SSE" + "-AT Export Bundle", "## Translation Memory Export Bundle")
    path.write_text(text, encoding="utf-8")


def download_queue_readiness(
    manifest: dict[str, Any],
    queue_payload: dict[str, Any],
) -> dict[str, Any]:
    return _download_readiness(manifest, queue_payload)


def _append_add_on_packages_to_download_plan(
    plan: DownloadPlanRunResult,
    *,
    manifest: dict[str, Any],
    download_dir: Path,
    download_cache_roots: list[Path],
) -> DownloadPlanRunResult:
    packages = _manifest_add_on_packages(manifest)
    if not packages:
        return plan

    queue_payload = json.loads(json.dumps(plan.queue_payload))
    plan_payload = json.loads(json.dumps(plan.plan_payload))
    queue_items = [
        item
        for item in queue_payload.get("items", [])
        if isinstance(item, dict)
    ]
    existing_identities = {
        identity
        for identity in (_queue_item_identity(item) for item in queue_items)
        if identity is not None
    }
    plan_body = plan_payload.setdefault("plan", {})
    plan_requests = plan_body.setdefault("requests", [])
    added = 0

    for package in packages:
        identity = _add_on_package_identity(package)
        if identity in existing_identities:
            for item in queue_items:
                if _queue_item_identity(item) == identity:
                    item["mtw_add_on_package"] = _add_on_package_public_payload(package)
                    item["warnings"] = _dedupe_strings(
                        [
                            *[str(warning) for warning in item.get("warnings", [])],
                            "mtw_add_on_package_reuses_existing_queue_item",
                        ]
                    )
                    break
            continue

        request = _download_request_from_add_on_package(package)
        target = planned_archive_path(download_dir, request)
        existing_archive = _existing_add_on_archive_for_request(
            request,
            target,
            download_cache_roots,
        )
        item_warnings = ["mtw_add_on_package"]
        if existing_archive is not None:
            status = "READY"
            local_archive_path = str(existing_archive)
            item_warnings.append("archive_already_present_for_nexus_file_identity")
        else:
            status = "PLANNED"
            local_archive_path = str(target)

        queue_item = asdict(
            DownloadQueueItem(
                request=request,
                status=status,
                local_archive_path=local_archive_path,
                attempts=0,
                warnings=item_warnings,
            )
        )
        queue_item["mtw_add_on_package"] = _add_on_package_public_payload(package)
        queue_items.append(queue_item)
        plan_requests.append(asdict(request))
        existing_identities.add(identity)
        added += 1

    if added == 0:
        queue_payload["items"] = queue_items
    else:
        queue_payload["items"] = queue_items
        plan_body["total_requests"] = len(plan_requests)
        plan_body["warnings"] = _dedupe_strings(
            [
                *[str(warning) for warning in plan_body.get("warnings", [])],
                "mtw_add_on_packages_appended",
            ]
        )

    queue_payload["summary"] = _queue_summary_from_items(queue_items)
    manifest_payload = queue_payload.get("manifest")
    if isinstance(manifest_payload, dict):
        manifest_payload["items"] = queue_items

    _write_json(plan.plan_path, plan_payload)
    _write_json(plan.queue_path, queue_payload)
    plan.report_path.write_text(
        render_download_plan_report(plan_payload, queue_payload),
        encoding="utf-8",
    )
    return DownloadPlanRunResult(
        plan_path=plan.plan_path,
        queue_path=plan.queue_path,
        report_path=plan.report_path,
        plan_payload=plan_payload,
        queue_payload=queue_payload,
    )


def _apply_add_on_packages(
    *,
    manifest: dict[str, Any],
    queue_payload: dict[str, Any],
    output_mod_path: Path,
    staging_root: Path,
    out_dir: Path,
) -> dict[str, Any]:
    packages = _manifest_add_on_packages(manifest)
    items: list[dict[str, Any]] = []
    for package in packages:
        archive_path = _queue_archive_for_identity(
            queue_payload,
            _add_on_package_identity(package),
        )
        public_package = _add_on_package_public_payload(package)
        if archive_path is None:
            status = "FAILED" if package.get("required", True) else "SKIPPED_MISSING"
            items.append(
                {
                    "package": public_package,
                    "status": status,
                    "archive_path": None,
                    "extracted_file_count": 0,
                    "total_extracted_bytes": 0,
                    "warnings": ["add_on_package_archive_missing"],
                    "failure_reason": (
                        "archive_missing" if package.get("required", True) else None
                    ),
                }
            )
            continue

        expected_sha256 = str(package.get("expected_sha256") or "").strip().casefold()
        if expected_sha256:
            actual_sha256 = _file_sha256(archive_path)
            if actual_sha256 != expected_sha256:
                items.append(
                    {
                        "package": public_package,
                        "status": "FAILED",
                        "archive_path": str(archive_path),
                        "extracted_file_count": 0,
                        "total_extracted_bytes": 0,
                        "warnings": [
                            "add_on_package_sha256_mismatch",
                            f"expected={expected_sha256}",
                            f"actual={actual_sha256}",
                        ],
                        "failure_reason": "sha256_mismatch",
                    }
                )
                continue

        plan_item = ExtractionPlanItem(
            candidate_id=f"mtw-add-on-package:{package['id']}",
            translation_nexus_mod_id=package["translation_nexus_mod_id"],
            translation_file_id=package["translation_file_id"],
            archive_path=str(archive_path),
            extraction_root=str(output_mod_path),
            status="PLANNED",
            reasons=["mtw_add_on_package_output_mod_overlay"],
            warnings=[],
        )
        extracted = extract_zip_item(
            plan_item,
            staging_root=staging_root,
            limits=_ADD_ON_EXTRACTION_LIMITS,
            overwrite=True,
        )
        item_payload = asdict(extracted)
        item_payload["package"] = public_package
        items.append(item_payload)

    summary = {
        "package_count": len(packages),
        "extracted": sum(1 for item in items if item.get("status") == "EXTRACTED"),
        "skipped_missing": sum(
            1 for item in items if item.get("status") == "SKIPPED_MISSING"
        ),
        "skipped_already_exists": sum(
            1 for item in items if item.get("status") == "SKIPPED_ALREADY_EXISTS"
        ),
        "failed": sum(1 for item in items if item.get("status") == "FAILED"),
        "extracted_file_count": sum(
            int(item.get("extracted_file_count") or 0) for item in items
        ),
        "total_extracted_bytes": sum(
            int(item.get("total_extracted_bytes") or 0) for item in items
        ),
    }
    payload = {
        "schema_version": "wizard-add-on-packages.v1",
        "summary": summary,
        "items": items,
    }
    result_path = out_dir / "wizard_add_on_packages.json"
    _write_json(result_path, payload)
    payload["result_path"] = str(result_path)
    return payload


def _manifest_add_on_packages(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    packages: list[dict[str, Any]] = []
    raw_packages = manifest.get("add_on_packages", [])
    if not isinstance(raw_packages, list):
        return []
    for raw in raw_packages:
        if not isinstance(raw, dict) or raw.get("enabled") is False:
            continue
        mod_id = _positive_int(raw.get("translation_nexus_mod_id"))
        file_id = _positive_int(raw.get("translation_file_id"))
        game_domain = str(raw.get("game_domain") or "skyrimspecialedition").strip().casefold()
        install_mode = str(raw.get("install_mode") or "").strip().upper()
        if mod_id is None or file_id is None or not game_domain:
            continue
        if install_mode != "OUTPUT_MOD_OVERLAY":
            continue
        package_id = str(
            raw.get("id") or f"nexusmods:{game_domain}:{mod_id}:{file_id}"
        ).strip()
        name = str(
            raw.get("name")
            or raw.get("translation_file_name")
            or f"Nexus add-on package {mod_id}/{file_id}"
        ).strip()
        source_url = str(raw.get("source_url") or "").strip()
        if not source_url:
            source_url = (
                f"https://www.nexusmods.com/{game_domain}/mods/{mod_id}"
                f"?tab=files&file_id={file_id}"
            )
        packages.append(
            {
                **raw,
                "id": package_id,
                "name": name,
                "game_domain": game_domain,
                "translation_nexus_mod_id": mod_id,
                "translation_file_id": file_id,
                "translation_file_name": _optional_text(raw.get("translation_file_name")),
                "expected_size": _positive_int(raw.get("expected_size")),
                "expected_sha256": _optional_text(
                    raw.get("expected_sha256") or raw.get("expected_hash")
                ),
                "install_mode": install_mode,
                "apply_order": _non_negative_int(raw.get("apply_order")) or 1_000_000,
                "required": raw.get("required") is not False,
                "source_url": source_url,
            }
        )
    return sorted(
        packages,
        key=lambda item: (
            int(item.get("apply_order") or 1_000_000),
            str(item.get("id") or "").casefold(),
        ),
    )


def _download_request_from_add_on_package(package: dict[str, Any]) -> DownloadRequest:
    return DownloadRequest(
        game_domain=package["game_domain"],
        translation_nexus_mod_id=package["translation_nexus_mod_id"],
        translation_file_id=package["translation_file_id"],
        translation_name=package["name"],
        translation_file_name=package.get("translation_file_name"),
        source_candidate_id=f"mtw-add-on-package:{package['id']}",
        decision_status="APPROVED",
        expected_size=package.get("expected_size"),
        expected_hash=package.get("expected_sha256"),
        url=package.get("source_url"),
        warnings=["mtw_add_on_package"],
    )


def _add_on_package_identity(package: dict[str, Any]) -> tuple[str, int, int]:
    return (
        str(package["game_domain"]).casefold(),
        int(package["translation_nexus_mod_id"]),
        int(package["translation_file_id"]),
    )


def _add_on_package_public_payload(package: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": package["id"],
        "name": package["name"],
        "game_domain": package["game_domain"],
        "translation_nexus_mod_id": package["translation_nexus_mod_id"],
        "translation_file_id": package["translation_file_id"],
        "translation_file_name": package.get("translation_file_name"),
        "install_mode": package["install_mode"],
        "apply_order": package.get("apply_order"),
        "required": package.get("required", True),
        "source_url": package.get("source_url"),
    }


def _existing_add_on_archive_for_request(
    request: DownloadRequest,
    planned_path: Path,
    download_cache_roots: list[Path],
) -> Path | None:
    if planned_path.is_file() and planned_path.stat().st_size > 0:
        return planned_path
    existing = _existing_archive_in_dir(planned_path.parent)
    if existing is not None:
        return existing
    seen = {str(planned_path.parent).replace("\\", "/").rstrip("/").casefold()}
    for root in download_cache_roots:
        try:
            candidate_dir = planned_archive_path(root, request).parent
        except ValueError:
            continue
        key = str(candidate_dir).replace("\\", "/").rstrip("/").casefold()
        if key in seen:
            continue
        seen.add(key)
        existing = _existing_archive_in_dir(candidate_dir)
        if existing is not None:
            return existing
    return None


def _existing_archive_in_dir(path: Path) -> Path | None:
    if not path.is_dir():
        return None
    archives = [
        archive
        for archive in path.iterdir()
        if (
            archive.is_file()
            and archive.suffix.casefold() in _ADD_ON_ARCHIVE_EXTENSIONS
            and archive.stat().st_size > 0
        )
    ]
    archives.sort(key=lambda archive: (-archive.stat().st_mtime_ns, archive.name.casefold()))
    return archives[0] if archives else None


def _queue_archive_for_identity(
    queue_payload: dict[str, Any],
    identity: tuple[str, int, int],
) -> Path | None:
    for item in queue_payload.get("items", []):
        if not isinstance(item, dict) or _queue_item_identity(item) != identity:
            continue
        status = str(item.get("status") or "").upper()
        archive_text = str(item.get("local_archive_path") or "").strip()
        archive_path = Path(archive_text) if archive_text else None
        if (
            status in {"READY", "DOWNLOADED", "SKIPPED_ALREADY_EXISTS"}
            and archive_path is not None
            and archive_path.is_file()
        ):
            return archive_path
    return None


def _queue_item_identity(item: dict[str, Any]) -> tuple[str, int, int] | None:
    request = item.get("request") if isinstance(item.get("request"), dict) else {}
    mod_id = _positive_int(request.get("translation_nexus_mod_id"))
    file_id = _positive_int(request.get("translation_file_id"))
    game_domain = str(request.get("game_domain") or "").casefold()
    if mod_id is None or file_id is None or not game_domain:
        return None
    return (game_domain, mod_id, file_id)


def _queue_summary_from_items(items: list[dict[str, Any]]) -> dict[str, int]:
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
        key = str(item.get("status") or "").lower()
        if key in summary:
            summary[key] += 1
    return summary


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _dedupe_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value)
        if text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _manifest_decisions(
    manifest: dict[str, Any],
    matched_entry_ids: set[str],
    *,
    profile: dict[str, Any] | None = None,
    locally_satisfied_artifact_sources: set[str] | None = None,
) -> dict[str, Any]:
    pending_decisions: list[tuple[tuple[int, int, int], dict[str, Any]]] = []
    local_sources = locally_satisfied_artifact_sources or set()
    script_alias_modes = _manifest_script_context_alias_output_modes(manifest)
    for entry_index, entry in enumerate(manifest.get("entries", [])):
        if entry.get("target_id") not in matched_entry_ids:
            continue
        base = entry["base"]
        target = entry["target"]
        selection = entry["selection"]
        artifacts = entry["artifacts"]
        for artifact_index, artifact in enumerate(artifacts):
            if not _artifact_requires_download(artifact):
                continue
            if _artifact_source_locally_satisfied(artifact, local_sources):
                continue
            target_path = target.get("path")
            target_type = target.get("type")
            base_plugins = (
                [target_path]
                if target_type == "PLUGIN"
                else []
            )
            script_context_plugins = (
                []
                if target_type == "PLUGIN"
                else _manifest_native_owner_plugins(entry, profile or {})
            )
            script_context_alias_output_modes = {
                plugin: script_alias_modes[plugin.casefold()]
                for plugin in script_context_plugins
                if plugin.casefold() in script_alias_modes
            }
            output_status = str(
                artifact.get("decision_status") or selection.get("status") or ""
            ).upper()
            translation_name = (
                artifact.get("translation_name") or selection.get("translation_name")
            )
            candidate = {
                "display_name": translation_name,
                "source": "WizardManifestV2",
                "language": manifest.get("language"),
                "nexus": {
                    "mod_id": artifact["translation_nexus_mod_id"],
                    "file_id": artifact["translation_file_id"],
                    "game_domain": artifact["game_domain"],
                },
                "translation_nexus_mod_id": artifact["translation_nexus_mod_id"],
                "translation_file_id": artifact["translation_file_id"],
                "translation_name": translation_name,
                "translation_file_name": artifact.get("translation_file_name"),
                "translation_version": artifact.get("translation_version"),
                "uploaded_timestamp": artifact.get("uploaded_timestamp"),
                "translation_uploaded_timestamp": (
                    artifact.get("translation_uploaded_timestamp")
                    or artifact.get("uploaded_timestamp")
                ),
                "translation_mod_updated_timestamp": (
                    artifact.get("translation_mod_updated_timestamp")
                    or artifact.get("uploaded_timestamp")
                ),
                "expected_size": artifact.get("expected_size"),
                "expected_sha256": artifact.get("expected_sha256"),
                "candidate_url": artifact.get("source_url"),
                "warnings": selection.get("warnings", []),
                "target_plugins": [target_path],
                "archive_contains_plugins": (
                    [target_path] if target_type == "PLUGIN" else []
                ),
                "provides": artifact.get("provides", [target_path]),
                "install_mode": artifact.get("install_mode"),
                "additional_translation_files": [],
            }
            decision = {
                "base": {
                    "name": base.get("name"),
                    "version": base.get("version"),
                    "plugins": base_plugins,
                    "script_context_plugins": script_context_plugins,
                    "script_context_alias_output_modes": script_context_alias_output_modes,
                    "translation_targets": (
                        [] if target_type == "PLUGIN" else [target_path]
                    ),
                    "nexus": {
                        "mod_id": base.get("nexus_mod_id"),
                        "file_id": base.get("nexus_file_id"),
                        "game_domain": artifact["game_domain"],
                    },
                },
                "status": selection.get("status"),
                "output_status": output_status,
                "selected_candidate": candidate,
                "alternatives": [],
                "score": selection.get("score", 0),
                "reasons": selection.get("reasons", []),
                "warnings": selection.get("warnings", []),
                "target_id": entry.get("target_id"),
            }
            pending_decisions.append(
                (
                    _manifest_decision_sort_key(
                        artifact,
                        entry_index=entry_index,
                        artifact_index=artifact_index,
                    ),
                    decision,
                )
            )
    decisions = [decision for _key, decision in sorted(pending_decisions)]
    decisions.extend(_manifest_profile_translation_memory_alias_decisions(manifest))
    return {
        "schema_version": "wizard-translation-decisions.v2",
        "language": manifest.get("language"),
        "manifest_id": manifest.get("manifest_id"),
        "discovery_performed": False,
        "summary": {
            "target_count": len(matched_entry_ids),
            "decision_count": len(decisions),
        },
        "decisions": decisions,
    }


def _manifest_profile_translation_memory_alias_decisions(
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    for plugin in _manifest_profile_translation_memory_alias_targets(manifest):
        decisions.append(
            {
                "base": {
                    "name": plugin,
                    "plugins": [plugin],
                    "translation_targets": [],
                    "nexus": {},
                },
                "status": "REJECTED",
                "selected_candidate": None,
                "alternatives": [],
                "score": 0,
                "reasons": [
                    "profile_translation_memory_alias_target",
                    "linked_translation_mod_without_file_metadata",
                ],
                "warnings": [
                    "synthetic alias target supplied by bundled manifest",
                ],
            }
        )
    return decisions


def _manifest_profile_translation_memory_alias_targets(
    manifest: dict[str, Any],
) -> list[str]:
    resources = manifest.get("resources") if isinstance(manifest.get("resources"), dict) else {}
    raw_targets = resources.get("profile_translation_memory_alias_targets")
    if not isinstance(raw_targets, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in raw_targets:
        if isinstance(item, str):
            plugin = item.strip()
        elif isinstance(item, dict):
            plugin = str(item.get("plugin") or "").strip()
        else:
            continue
        if not plugin:
            continue
        key = plugin.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(plugin)
    return sorted(result, key=str.casefold)


def _manifest_script_context_alias_output_modes(
    manifest: dict[str, Any],
) -> dict[str, str]:
    resources = manifest.get("resources") if isinstance(manifest.get("resources"), dict) else {}
    raw_items = resources.get("script_context_translation_memory_aliases")
    if not isinstance(raw_items, list):
        return {}
    result: dict[str, str] = {}
    for item in raw_items:
        if isinstance(item, str):
            plugin = item.strip()
            mode = "disabled_review"
        elif isinstance(item, dict):
            plugin = str(item.get("plugin") or "").strip()
            mode = str(item.get("output_mode") or "disabled_review").strip().casefold()
        else:
            continue
        if not plugin:
            continue
        if mode not in {"skip", "disabled_review", "active"}:
            mode = "disabled_review"
        result[plugin.casefold()] = mode
    return result


def _manifest_native_owner_plugins(
    entry: dict[str, Any],
    profile: dict[str, Any],
) -> list[str]:
    target = entry.get("target") if isinstance(entry.get("target"), dict) else {}
    base = entry.get("base") if isinstance(entry.get("base"), dict) else {}
    target_path = str(target.get("path") or "")
    target_key = _normalize_target(target.get("normalized_path") or target_path)
    base_mod_id = _positive_int(base.get("nexus_mod_id"))
    base_file_id = _positive_int(base.get("nexus_file_id"))
    plugins: list[str] = []
    seen: set[str] = set()

    def add(plugin: object) -> None:
        text = str(plugin or "").strip()
        if not text or not text.casefold().endswith((".esp", ".esm", ".esl")):
            return
        key = text.casefold()
        if key in seen:
            return
        seen.add(key)
        plugins.append(text)

    for owner in _profile_targets(profile).get(target_key, []):
        for plugin in _as_list(owner.get("plugins")):
            add(plugin)

    for mod in _as_list(profile.get("mods")):
        if not isinstance(mod, dict) or not mod.get("enabled"):
            continue
        nexus = mod.get("nexus") if isinstance(mod.get("nexus"), dict) else {}
        mod_id = _positive_int(nexus.get("mod_id"))
        file_id = _positive_int(nexus.get("file_id"))
        if base_mod_id is None or mod_id != base_mod_id:
            continue
        if base_file_id is not None and file_id is not None and file_id != base_file_id:
            continue
        for plugin in _as_list(mod.get("plugins")):
            add(plugin)
    return plugins


def _manifest_decision_sort_key(
    artifact: dict[str, Any], *, entry_index: int, artifact_index: int
) -> tuple[int, int, int]:
    conversion_order = _non_negative_int(artifact.get("conversion_order"))
    if conversion_order is None:
        conversion_order = 1_000_000_000
    return (conversion_order, entry_index, artifact_index)


def _base_mod_id(mod: dict[str, Any]) -> int | None:
    nexus = mod.get("nexus") if isinstance(mod.get("nexus"), dict) else {}
    return _positive_int(nexus.get("mod_id"))


def _base_file_id(mod: dict[str, Any]) -> int | None:
    nexus = mod.get("nexus") if isinstance(mod.get("nexus"), dict) else {}
    return _positive_int(nexus.get("file_id"))


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _non_negative_int(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _as_list(value: object) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _normalize_name(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _profile_targets(profile: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    enabled_mods = [
        item
        for item in profile.get("mods", [])
        if isinstance(item, dict) and item.get("enabled")
    ]
    for item in enabled_mods:
        targets = [
            *(item.get("plugins") if isinstance(item.get("plugins"), list) else []),
            *(
                item.get("translation_targets")
                if isinstance(item.get("translation_targets"), list)
                else []
            ),
        ]
        for target in targets:
            result.setdefault(_normalize_target(target), []).append(item)
    active_plugins = {
        _normalize_target(item)
        for item in profile.get("active_plugins", [])
        if str(item or "").strip()
    }
    for plugin in active_plugins:
        result.setdefault(
            plugin,
            [{"name": plugin, "enabled": True, "nexus": {}}],
        )
    for owners in result.values():
        owners.sort(
            key=lambda item: int(item.get("priority") or 0),
            reverse=True,
        )
    return result


def _profile_mods_by_nexus_id(profile: dict[str, Any]) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for item in profile.get("mods", []):
        if not isinstance(item, dict) or not item.get("enabled"):
            continue
        mod_id = _base_mod_id(item)
        if mod_id is None:
            continue
        current = result.get(mod_id)
        if current is None or int(item.get("priority") or 0) > int(
            current.get("priority") or 0
        ):
            result[mod_id] = item
    return result


def _normalize_target(value: object) -> str:
    return "/".join(
        part
        for part in str(value or "").replace("\\", "/").strip("/").casefold().split("/")
        if part
    )


def _download_readiness(
    manifest: dict[str, Any],
    queue_payload: dict[str, Any],
) -> dict[str, Any]:
    locally_satisfied_sources = _manifest_locally_satisfied_artifact_sources(manifest)
    required = {
        (
            str(artifact.get("game_domain") or "").casefold(),
            int(artifact["translation_nexus_mod_id"]),
            int(artifact["translation_file_id"]),
        )
        for entry in manifest.get("entries", [])
        if isinstance(entry, dict)
        for artifact in entry.get("artifacts", [])
        if (
            isinstance(artifact, dict)
            and _artifact_requires_download(artifact)
            and not _artifact_source_locally_satisfied(
                artifact,
                locally_satisfied_sources,
            )
        )
    }
    for package in _manifest_add_on_packages(manifest):
        if package.get("required", True):
            required.add(_add_on_package_identity(package))
    available: set[tuple[str, int, int]] = set()
    unusable: list[dict[str, Any]] = []
    for item in queue_payload.get("items", []):
        if not isinstance(item, dict):
            continue
        request = item.get("request") if isinstance(item.get("request"), dict) else {}
        mod_id = _positive_int(request.get("translation_nexus_mod_id"))
        file_id = _positive_int(request.get("translation_file_id"))
        game_domain = str(request.get("game_domain") or "").casefold()
        if not mod_id or not file_id or not game_domain:
            continue
        identity = (game_domain, mod_id, file_id)
        if identity not in required:
            continue
        status = str(item.get("status") or "").upper()
        archive_text = str(item.get("local_archive_path") or "").strip()
        archive_path = Path(archive_text) if archive_text else None
        if (
            status in {"READY", "DOWNLOADED", "SKIPPED_ALREADY_EXISTS"}
            and archive_path is not None
            and archive_path.is_file()
        ):
            available.add(identity)
            continue
        unusable.append(
            {
                "game_domain": game_domain,
                "translation_nexus_mod_id": mod_id,
                "translation_file_id": file_id,
                "status": status or "UNKNOWN",
                "reason": (
                    "archive_missing"
                    if archive_path is None or not archive_path.is_file()
                    else "queue_status_not_ready"
                ),
            }
        )
    missing = sorted(required - available)
    missing_items = [
        {
            "game_domain": game_domain,
            "translation_nexus_mod_id": mod_id,
            "translation_file_id": file_id,
        }
        for game_domain, mod_id, file_id in missing
    ]
    return {
        "complete": not missing,
        "required_count": len(required),
        "available_count": len(available),
        "missing_count": len(missing),
        "missing": missing_items,
        "unusable": unusable,
    }


def _artifact_requires_download(artifact: dict[str, Any]) -> bool:
    if str(artifact.get("install_mode") or "").upper() == "BUNDLE_DSD":
        return False
    return (
        _positive_int(artifact.get("translation_nexus_mod_id")) is not None
        and _positive_int(artifact.get("translation_file_id")) is not None
        and bool(str(artifact.get("game_domain") or "").strip())
    )


def _artifact_source_locally_satisfied(
    artifact: dict[str, Any],
    locally_satisfied_sources: set[str],
) -> bool:
    if not locally_satisfied_sources:
        return False
    if "conversion_output_target_match" in _as_list(artifact.get("evidence")):
        return False
    source = str(artifact.get("source") or "").strip().casefold()
    return bool(source and source in locally_satisfied_sources)


def _manifest_locally_satisfied_artifact_sources(manifest: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for item in _raw_manifest_local_dsd_sources(manifest):
        if not isinstance(item, dict):
            continue
        for source in _as_list(
            item.get("satisfies_artifact_sources") or item.get("satisfies_sources")
        ):
            source_text = str(source or "").strip().casefold()
            if source_text:
                result.add(source_text)
    return result


def _manifest_local_dsd_sources(
    manifest: dict[str, Any],
    manifest_path: Path,
) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for item in _raw_manifest_local_dsd_sources(manifest):
        source_path = _resolve_manifest_resource_path(item, manifest_path)
        if source_path is None:
            continue
        key = str(source_path).casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(source_path)
    return result


def _manifest_download_cache_roots(
    manifest: dict[str, Any],
    manifest_path: Path,
) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for item in _raw_manifest_download_cache_roots(manifest):
        source_path = _resolve_manifest_resource_path(item, manifest_path)
        if source_path is None:
            continue
        key = str(source_path).casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(source_path)
    return result


def _raw_manifest_local_dsd_sources(manifest: dict[str, Any]) -> list[object]:
    result: list[object] = []
    resources = manifest.get("resources") if isinstance(manifest.get("resources"), dict) else {}
    resource_sources = resources.get("local_dsd_sources")
    if isinstance(resource_sources, list):
        result.extend(resource_sources)
    source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    legacy_sources = source.get("local_dsd_sources")
    if isinstance(legacy_sources, list):
        result.extend(legacy_sources)
    return result


def _raw_manifest_download_cache_roots(manifest: dict[str, Any]) -> list[object]:
    result: list[object] = []
    resources = manifest.get("resources") if isinstance(manifest.get("resources"), dict) else {}
    resource_roots = resources.get("download_cache_roots")
    if isinstance(resource_roots, list):
        result.extend(resource_roots)
    source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    legacy_roots = source.get("download_cache_roots")
    if isinstance(legacy_roots, list):
        result.extend(legacy_roots)
    return result


def _resolve_manifest_resource_path(item: object, manifest_path: Path) -> Path | None:
    if isinstance(item, str):
        path_text = item.strip()
        location = "manifest_relative"
    elif isinstance(item, dict):
        if item.get("enabled") is False:
            return None
        path_text = str(item.get("path") or "").strip()
        location = str(item.get("type") or item.get("location") or "manifest_relative")
    else:
        return None
    if not path_text:
        return None
    path = Path(path_text)
    if path.is_absolute():
        return path
    normalized_location = location.strip().casefold()
    if normalized_location in {"package_relative", "package_resource", "bundled_resource"}:
        return Path(__file__).parent / path
    return manifest_path.parent / path
