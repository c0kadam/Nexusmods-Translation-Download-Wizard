import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from modlist_translation_wizard.archive_conversion import WizardArchiveConversionRunResult
from modlist_translation_wizard.bundled import (
    MANIFEST_MODE_LOCAL,
    copy_default_bundled_manifest,
    load_bundled_manifest,
    load_default_bundled_manifest,
)
from modlist_translation_wizard.manifest import (
    WizardManifestError,
    build_wizard_manifest,
    load_wizard_manifest,
    validate_wizard_manifest,
    write_wizard_manifest,
)
from modlist_translation_wizard.runtime import (
    build_wizard_preflight,
    convert_downloaded_translations_from_manifest,
    download_queue_readiness,
    plan_premium_downloads_from_manifest,
)


def test_stable_manifest_exports_only_complete_approved_candidates() -> None:
    payload = _manifest()

    assert payload["schema_version"] == "mtt-wizard-manifest.v2"
    assert payload["nexus"]["discovery_enabled"] is False
    assert payload["nexus"]["request_scope"] == "KNOWN_MOD_AND_FILE_IDS_ONLY"
    assert "manual_api_key" not in payload["nexus"]["authentication"]
    assert payload["summary"]["entry_count"] == 1
    assert payload["summary"]["artifact_reference_count"] == 2
    assert payload["summary"]["unique_download_count"] == 2
    assert payload["summary"]["skipped"]["status_needs_review"] == 1
    entry = payload["entries"][0]
    assert entry["target"]["path"] == "Example.esp"
    assert entry["base"]["nexus_mod_id"] == 111
    assert [item["translation_file_id"] for item in entry["artifacts"]] == [444, 445]
    assert all(item["provides"] == ["Example.esp"] for item in entry["artifacts"])
    assert all("key=" not in item["source_url"] for item in entry["artifacts"])


def test_bundled_manifest_is_valid_and_discovery_free(monkeypatch) -> None:
    monkeypatch.setenv("MTW_DISABLE_REMOTE_MANIFEST", "1")
    payload = load_bundled_manifest(
        list_id="lorerim",
        manifest_name="manifest.json",
    )
    default_payload = load_default_bundled_manifest(manifest_mode=MANIFEST_MODE_LOCAL)

    assert payload["manifest_id"] == "lorerim-tr-stable"
    assert default_payload["manifest_id"] == payload["manifest_id"]
    assert payload["release_state"] == "DRAFT"
    assert payload["nexus"]["discovery_enabled"] is False
    assert "manual_api_key" not in payload["nexus"]["authentication"]
    assert payload["schema_version"] == "mtt-wizard-manifest.v2"
    assert payload["output"]["mod_name"] == "LoreRim - Turkce Ceviri"
    assert payload["summary"]["entry_count"] > 0


def test_default_bundled_manifest_can_be_copied_for_runtime(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MTW_DISABLE_REMOTE_MANIFEST", "1")
    copied = copy_default_bundled_manifest(
        tmp_path,
        manifest_mode=MANIFEST_MODE_LOCAL,
    )

    assert copied.name == "manifest.json"
    assert copied.exists()
    assert copied.with_suffix(copied.suffix + ".sha256").exists()
    assert load_wizard_manifest(copied)["manifest_id"] == "lorerim-tr-stable"


def test_default_manifest_can_load_external_release_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MTW_DISABLE_REMOTE_MANIFEST", "1")
    release_dir = tmp_path / "release"
    payload = _manifest()
    payload["manifest_id"] = "external-release-test"
    write_wizard_manifest(payload, release_dir / "manifest.json")
    monkeypatch.setenv("MTW_RELEASE_DIR", str(release_dir))

    loaded = load_default_bundled_manifest(manifest_mode=MANIFEST_MODE_LOCAL)

    assert loaded["manifest_id"] == "external-release-test"


def test_default_manifest_uses_release_config_for_external_release_dir(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("MTW_DISABLE_REMOTE_MANIFEST", "1")
    monkeypatch.delenv("MTW_DEFAULT_RELEASE_ID", raising=False)
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    payload = _manifest()
    payload["manifest_id"] = "nordicsouls-release-config-test"
    payload["modlist"]["id"] = "nordicsouls"
    payload["modlist"]["name"] = "NordicSouls"
    write_wizard_manifest(payload, release_dir / "manifest.json")
    (release_dir / "release_config.json").write_text(
        json.dumps(
            {
                "schema_version": "mtw-release-config.v1",
                "release_id": "nordicsouls",
                "manifest_name": "manifest.json",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MTW_RELEASE_DIR", str(release_dir))

    loaded = load_default_bundled_manifest(manifest_mode=MANIFEST_MODE_LOCAL)

    assert loaded["manifest_id"] == "nordicsouls-release-config-test"
    assert loaded["modlist"]["id"] == "nordicsouls"


def test_manifest_digest_is_required_and_verified(tmp_path) -> None:
    result = write_wizard_manifest(_manifest(), tmp_path / "wizard.json")

    loaded = load_wizard_manifest(result.manifest_path)
    assert loaded["manifest_id"] == "lorerim-tr-stable"

    result.manifest_path.write_text("{}", encoding="utf-8")
    with pytest.raises(WizardManifestError, match="SHA-256 mismatch"):
        load_wizard_manifest(result.manifest_path)


def test_load_manifest_normalizes_legacy_source_vocabulary(tmp_path) -> None:
    payload = _manifest()
    entry = payload["entries"][0]
    legacy_confidence = "VERIFIED_" + "SSE" + "_AT"
    legacy_source = "SSE" + "_AT_DOWNLOAD_LIST"
    legacy_reason = "ss" + "eat_selected_download"
    entry["selection"]["confidence"] = legacy_confidence
    entry["selection"]["provenance"] = [legacy_source]
    entry["selection"]["reasons"] = [legacy_reason]
    entry["artifacts"][0]["source"] = legacy_source
    payload["source"]["ss" + "eat_download_list"] = "ss" + "eat_download_list.json"
    path = tmp_path / "wizard.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_suffix(path.suffix + ".sha256").write_text(
        f"{digest}  {path.name}\n",
        encoding="ascii",
    )

    loaded = load_wizard_manifest(path)

    loaded_entry = loaded["entries"][0]
    assert loaded_entry["selection"]["confidence"] == "VERIFIED_CURATED"
    assert loaded_entry["selection"]["provenance"] == ["CURATED_DOWNLOAD_LIST"]
    assert loaded_entry["selection"]["reasons"] == ["curated_selected_download"]
    assert loaded_entry["artifacts"][0]["source"] == "CURATED_DOWNLOAD_LIST"
    assert loaded["source"]["curated_download_list"] == "curated_download_list.json"


def test_manifest_validation_rejects_discovery_enabled() -> None:
    payload = _manifest()
    payload["nexus"]["discovery_enabled"] = True

    with pytest.raises(WizardManifestError, match="disable discovery"):
        validate_wizard_manifest(payload)


def test_manifest_validation_requires_artifact_to_provide_target() -> None:
    payload = _manifest()
    payload["entries"][0]["artifacts"][0]["provides"] = ["Other.esp"]

    with pytest.raises(WizardManifestError, match="does not provide target"):
        validate_wizard_manifest(payload)


def test_manifest_validation_accepts_add_on_packages() -> None:
    payload = _manifest()
    payload["add_on_packages"] = [_add_on_package()]

    validate_wizard_manifest(payload)


def test_manifest_validation_accepts_native_binary_install_entry() -> None:
    payload = _manifest()
    payload["entries"].append(_native_binary_entry(payload))

    validate_wizard_manifest(payload)


def test_manifest_validation_rejects_invalid_add_on_package() -> None:
    payload = _manifest()
    payload["add_on_packages"] = [
        _add_on_package(package_id="duplicate"),
        _add_on_package(package_id="duplicate"),
    ]

    with pytest.raises(WizardManifestError, match="duplicate add-on package id"):
        validate_wizard_manifest(payload)


def test_premium_preflight_requires_exact_curated_profile() -> None:
    profile = _profile()
    manifest = _manifest(profile=profile)

    ready = build_wizard_preflight(manifest, profile, delivery_mode="PREMIUM_API")
    non_premium = build_wizard_preflight(
        manifest, profile, delivery_mode="NON_PREMIUM_NXM"
    )

    assert ready["status"] == "READY"
    assert ready["discovery_performed"] is False
    assert non_premium["status"] == "READY"
    assert non_premium["non_premium"]["status"] == "SUPPORTED"
    assert non_premium["non_premium"]["requires_user_initiated_nxm_link"] is True


def test_preflight_ignores_mo2_priority_only_profile_drift() -> None:
    profile = _profile()
    manifest = _manifest(profile=profile)
    priority_shifted_profile = _profile()
    priority_shifted_profile["mods"][0]["priority"] = 999

    preflight = build_wizard_preflight(
        manifest,
        priority_shifted_profile,
        delivery_mode="PREMIUM_API",
    )

    assert preflight["status"] == "READY"
    assert preflight["profile"]["exact_match"] is True
    assert preflight["summary"]["missing_entries"] == 0


def test_preflight_accepts_manifest_owned_native_targets_for_exact_profile() -> None:
    profile = _profile()
    manifest = _manifest(profile=profile)
    extra_entry = {
        **manifest["entries"][0],
        "target_id": "target-native-owned",
        "target": {
            "path": "interface/example_extra.txt",
            "normalized_path": "interface/example_extra.txt",
            "type": "INTERFACE",
        },
        "install": {"mode": "NATIVE_INSTALL"},
        "artifacts": [
            {
                **manifest["entries"][0]["artifacts"][0],
                "provides": ["interface/example_extra.txt"],
                "install_mode": "NATIVE_INSTALL",
            }
        ],
    }
    manifest["entries"].append(extra_entry)

    preflight = build_wizard_preflight(manifest, profile, delivery_mode="PREMIUM_API")

    assert preflight["status"] == "READY"
    assert preflight["summary"]["matched_entries"] == 2
    assert preflight["summary"]["missing_entries"] == 0


def test_preflight_keeps_file_id_warnings_non_blocking_for_exact_profile() -> None:
    profile = _profile()
    manifest = _manifest(profile=profile)
    manifest["entries"][0]["base"]["nexus_file_id"] = 999999

    preflight = build_wizard_preflight(manifest, profile, delivery_mode="PREMIUM_API")

    assert preflight["status"] == "READY"
    assert preflight["summary"]["compatibility_warning_count"] == 1


def test_preflight_accepts_profile_drift_when_nexus_mod_metadata_matches() -> None:
    profile = _profile()
    manifest = _manifest(profile=profile)
    drifted_profile = _profile()
    drifted_profile["active_plugins"] = []
    drifted_profile["mods"][0]["plugins"] = []

    preflight = build_wizard_preflight(
        manifest,
        drifted_profile,
        delivery_mode="PREMIUM_API",
    )

    assert preflight["status"] == "READY"
    assert preflight["profile"]["exact_match"] is False
    assert preflight["summary"]["missing_entries"] == 0
    assert preflight["summary"]["compatibility_warning_count"] >= 1


def test_premium_plan_builds_download_queue_without_discovery(tmp_path) -> None:
    profile = _profile()
    manifest = _manifest(profile=profile)
    manifest["entries"][0]["artifacts"][1]["translation_name"] = "Example Mod Turkish Patch"
    manifest_result = write_wizard_manifest(manifest, tmp_path / "wizard.json")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    result = plan_premium_downloads_from_manifest(
        manifest_path=manifest_result.manifest_path,
        profile_scan_path=profile_path,
        download_dir=tmp_path / "downloads",
        out_dir=tmp_path / "out",
    )

    assert result.preflight_payload["status"] == "READY"
    decisions = json.loads(result.decisions_path.read_text(encoding="utf-8"))
    assert decisions["discovery_performed"] is False
    assert decisions["schema_version"] == "wizard-translation-decisions.v2"
    assert decisions["summary"]["target_count"] == 1
    assert decisions["summary"]["decision_count"] == 2
    assert decisions["decisions"][1]["selected_candidate"]["translation_name"] == (
        "Example Mod Turkish Patch"
    )
    assert result.download_plan.queue_payload["summary"]["item_count"] == 2


def test_premium_plan_appends_add_on_packages_last(tmp_path) -> None:
    profile = _profile()
    manifest = _manifest(profile=profile)
    manifest["add_on_packages"] = [_add_on_package()]
    manifest_result = write_wizard_manifest(manifest, tmp_path / "wizard.json")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    result = plan_premium_downloads_from_manifest(
        manifest_path=manifest_result.manifest_path,
        profile_scan_path=profile_path,
        download_dir=tmp_path / "downloads",
        out_dir=tmp_path / "out",
    )

    items = result.download_plan.queue_payload["items"]
    assert result.download_plan.queue_payload["summary"]["item_count"] == 3
    assert [item["request"]["translation_file_id"] for item in items] == [
        444,
        445,
        773329,
    ]
    assert items[-1]["mtw_add_on_package"]["id"] == "lorerim-extra-pack"
    readiness = download_queue_readiness(manifest, result.download_plan.queue_payload)
    assert readiness["required_count"] == 3
    assert readiness["missing_count"] == 3


def test_premium_plan_appends_native_binary_assets_without_conversion_decision(
    tmp_path,
) -> None:
    profile = _profile()
    manifest = _manifest(profile=profile)
    manifest["entries"].append(_native_binary_entry(manifest))
    manifest_result = write_wizard_manifest(manifest, tmp_path / "wizard.json")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    result = plan_premium_downloads_from_manifest(
        manifest_path=manifest_result.manifest_path,
        profile_scan_path=profile_path,
        download_dir=tmp_path / "downloads",
        out_dir=tmp_path / "out",
    )

    queue = result.download_plan.queue_payload
    assert queue["summary"]["item_count"] == 3
    assert queue["items"][-1]["request"]["translation_file_id"] == 695247
    assert queue["items"][-1]["mtw_native_binary_assets"][0]["target_path"] == (
        "SKSE/Plugins/Wheeler.dll"
    )
    decisions = json.loads(result.decisions_path.read_text(encoding="utf-8"))
    assert all(
        item.get("target_id") != "target-native-binary-wheeler"
        for item in decisions["decisions"]
    )


def test_premium_plan_orders_decisions_by_manifest_conversion_order(tmp_path) -> None:
    profile = _profile()
    manifest = _manifest(profile=profile)
    artifacts = manifest["entries"][0]["artifacts"]
    artifacts[0]["conversion_order"] = 20
    artifacts[1]["conversion_order"] = 10
    manifest_result = write_wizard_manifest(manifest, tmp_path / "wizard.json")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    result = plan_premium_downloads_from_manifest(
        manifest_path=manifest_result.manifest_path,
        profile_scan_path=profile_path,
        download_dir=tmp_path / "downloads",
        out_dir=tmp_path / "out",
    )

    decisions = json.loads(result.decisions_path.read_text(encoding="utf-8"))
    assert [
        item["selected_candidate"]["translation_file_id"]
        for item in decisions["decisions"]
    ] == [445, 444]
    assert [
        item["request"]["translation_file_id"]
        for item in result.download_plan.queue_payload["items"]
    ] == [445, 444]


def test_premium_plan_uses_manifest_download_cache_roots(tmp_path) -> None:
    profile = _profile()
    manifest = _manifest(profile=profile)
    shared_cache = tmp_path / "shared-cache"
    manifest["resources"] = {
        "download_cache_roots": [
            {"type": "manifest_relative", "path": shared_cache.name}
        ]
    }
    archive_dir = shared_cache / "nexusmods" / "skyrimspecialedition" / "333" / "444"
    archive_dir.mkdir(parents=True)
    archive = archive_dir / "renamed-existing-translation.7z"
    archive.write_bytes(b"archive")
    manifest_result = write_wizard_manifest(manifest, tmp_path / "wizard.json")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    result = plan_premium_downloads_from_manifest(
        manifest_path=manifest_result.manifest_path,
        profile_scan_path=profile_path,
        download_dir=tmp_path / "downloads",
        out_dir=tmp_path / "out",
    )

    item = result.download_plan.queue_payload["items"][0]
    assert item["status"] == "READY"
    assert item["local_archive_path"] == str(archive)
    assert "archive_already_present_for_nexus_file_identity" in item["warnings"]


def test_premium_plan_skips_artifacts_satisfied_by_local_dsd_source(tmp_path) -> None:
    profile = _profile()
    manifest = _manifest(profile=profile)
    artifacts = manifest["entries"][0]["artifacts"]
    artifacts[0]["source"] = "CURATED_DOWNLOAD_LIST"
    artifacts[1]["source"] = "MTT"
    manifest["resources"] = {
        "local_dsd_sources": [
            {
                "type": "absolute",
                "path": str(tmp_path / "curated-output"),
                "satisfies_artifact_sources": ["CURATED_DOWNLOAD_LIST"],
            }
        ]
    }
    manifest_result = write_wizard_manifest(manifest, tmp_path / "wizard.json")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    result = plan_premium_downloads_from_manifest(
        manifest_path=manifest_result.manifest_path,
        profile_scan_path=profile_path,
        download_dir=tmp_path / "downloads",
        out_dir=tmp_path / "out",
    )

    assert result.download_plan.queue_payload["summary"]["item_count"] == 1
    item = result.download_plan.queue_payload["items"][0]
    assert item["request"]["translation_file_id"] == 445


def test_premium_plan_preserves_profile_translation_memory_alias_targets(
    tmp_path,
) -> None:
    profile = _profile()
    manifest = _manifest(profile=profile)
    manifest["resources"] = {
        "profile_translation_memory_alias_targets": [
            {
                "plugin": "AliasTarget.esp",
                "reason": "mtt_conversion_profile_translation_memory_alias",
            }
        ]
    }
    manifest_result = write_wizard_manifest(manifest, tmp_path / "wizard.json")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    result = plan_premium_downloads_from_manifest(
        manifest_path=manifest_result.manifest_path,
        profile_scan_path=profile_path,
        download_dir=tmp_path / "downloads",
        out_dir=tmp_path / "out",
    )

    decisions = json.loads(result.decisions_path.read_text(encoding="utf-8"))
    alias_decision = decisions["decisions"][-1]
    assert alias_decision["status"] == "REJECTED"
    assert alias_decision["base"]["plugins"] == ["AliasTarget.esp"]
    assert "linked_translation_mod_without_file_metadata" in alias_decision["reasons"]
    assert result.download_plan.queue_payload["summary"]["item_count"] == 2


def test_premium_plan_preserves_native_owner_plugins_for_script_context(
    tmp_path,
) -> None:
    profile = _profile()
    manifest = _manifest(profile=profile)
    native_entry = {
        **manifest["entries"][0],
        "entry_id": "entry-native-script",
        "target_id": "target-native-script",
        "target": {
            "path": "scripts/example_script.pex",
            "normalized_path": "scripts/example_script.pex",
            "type": "NATIVE",
        },
        "install": {"mode": "NATIVE_INSTALL"},
        "artifacts": [
            {
                **manifest["entries"][0]["artifacts"][0],
                "provides": ["scripts/example_script.pex"],
                "install_mode": "NATIVE_INSTALL",
                "uploaded_timestamp": 123456789,
            }
        ],
    }
    manifest["entries"].append(native_entry)
    manifest["resources"] = {
        "script_context_translation_memory_aliases": [
            {"plugin": "Example.esp", "output_mode": "disabled_review"}
        ]
    }
    manifest_result = write_wizard_manifest(manifest, tmp_path / "wizard.json")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    result = plan_premium_downloads_from_manifest(
        manifest_path=manifest_result.manifest_path,
        profile_scan_path=profile_path,
        download_dir=tmp_path / "downloads",
        out_dir=tmp_path / "out",
    )

    decisions = json.loads(result.decisions_path.read_text(encoding="utf-8"))
    native_decision = next(
        item
        for item in decisions["decisions"]
        if item.get("target_id") == "target-native-script"
    )
    candidate = native_decision["selected_candidate"]
    assert native_decision["base"]["plugins"] == []
    assert native_decision["base"]["script_context_plugins"] == ["Example.esp"]
    assert native_decision["base"]["script_context_alias_output_modes"] == {
        "Example.esp": "disabled_review"
    }
    assert candidate["target_plugins"] == ["scripts/example_script.pex"]
    assert candidate["translation_uploaded_timestamp"] == 123456789
    assert candidate["translation_mod_updated_timestamp"] == 123456789


def test_premium_plan_preserves_artifact_output_status_for_conversion(
    tmp_path,
) -> None:
    profile = _profile()
    manifest = _manifest(profile=profile)
    manifest["entries"][0]["artifacts"][0]["decision_status"] = "NEEDS_REVIEW"
    manifest_result = write_wizard_manifest(manifest, tmp_path / "wizard.json")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    result = plan_premium_downloads_from_manifest(
        manifest_path=manifest_result.manifest_path,
        profile_scan_path=profile_path,
        download_dir=tmp_path / "downloads",
        out_dir=tmp_path / "out",
    )

    decisions = json.loads(result.decisions_path.read_text(encoding="utf-8"))
    first = decisions["decisions"][0]
    assert first["status"] == "APPROVED"
    assert first["output_status"] == "NEEDS_REVIEW"


def test_premium_plan_allows_resolvable_profile_drift_with_warning(tmp_path) -> None:
    manifest_result = write_wizard_manifest(_manifest(), tmp_path / "wizard.json")
    profile = _profile()
    profile["active_plugins"].append("Unexpected.esp")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    result = plan_premium_downloads_from_manifest(
        manifest_path=manifest_result.manifest_path,
        profile_scan_path=profile_path,
        download_dir=tmp_path / "downloads",
        out_dir=tmp_path / "out",
    )

    assert result.preflight_payload["status"] == "READY"
    assert result.preflight_payload["profile"]["exact_match"] is False
    assert result.preflight_payload["summary"]["compatibility_warning_count"] >= 1


def test_premium_plan_refuses_unresolved_profile_targets(tmp_path) -> None:
    manifest_result = write_wizard_manifest(_manifest(), tmp_path / "wizard.json")
    profile = _profile()
    profile["active_plugins"] = []
    profile["mods"] = []
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    with pytest.raises(WizardManifestError, match="preflight is not READY"):
        plan_premium_downloads_from_manifest(
            manifest_path=manifest_result.manifest_path,
            profile_scan_path=profile_path,
            download_dir=tmp_path / "downloads",
            out_dir=tmp_path / "out",
        )


def test_download_readiness_requires_every_curated_archive_on_disk(tmp_path) -> None:
    manifest = _manifest()
    first_archive = tmp_path / "444.7z"
    first_archive.write_bytes(b"archive")
    queue = _download_queue(
        first_archive=first_archive,
        second_archive=tmp_path / "missing.7z",
    )

    readiness = download_queue_readiness(manifest, queue)

    assert readiness["complete"] is False
    assert readiness["required_count"] == 2
    assert readiness["available_count"] == 1
    assert readiness["missing_count"] == 1
    assert readiness["missing"][0]["translation_file_id"] == 445


def test_download_readiness_ignores_bundle_dsd_artifacts() -> None:
    manifest = _manifest()
    artifact = {
        "artifact_id": "bundle:example",
        "source": "CURATED_DSD_BUNDLE",
        "provides": ["Example.esp"],
        "install_mode": "BUNDLE_DSD",
    }
    manifest["entries"][0]["artifacts"] = [artifact]

    readiness = download_queue_readiness(manifest, {"items": []})

    assert readiness["complete"] is True
    assert readiness["required_count"] == 0
    assert readiness["missing_count"] == 0


def test_conversion_refuses_incomplete_download_queue(tmp_path) -> None:
    profile = _profile()
    manifest_result = write_wizard_manifest(
        _manifest(profile=profile),
        tmp_path / "wizard.json",
    )
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    decisions_path = tmp_path / "decisions.json"
    decisions_path.write_text('{"decisions":[]}', encoding="utf-8")
    queue_path = tmp_path / "queue.json"
    queue_path.write_text(
        json.dumps(
            _download_queue(
                first_archive=tmp_path / "missing-444.7z",
                second_archive=tmp_path / "missing-445.7z",
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(WizardManifestError, match="download queue is incomplete"):
        convert_downloaded_translations_from_manifest(
            manifest_path=manifest_result.manifest_path,
            profile_scan_path=profile_path,
            decisions_path=decisions_path,
            download_queue_path=queue_path,
            out_dir=tmp_path / "runtime",
        )


def test_conversion_passes_manifest_local_dsd_sources(tmp_path, monkeypatch) -> None:
    profile = _profile()
    manifest = _manifest(profile=profile)
    local_source = tmp_path / "curated-dsd"
    local_source.mkdir()
    manifest["resources"] = {
        "local_dsd_sources": [
            {"type": "manifest_relative", "path": local_source.name}
        ]
    }
    manifest_result = write_wizard_manifest(manifest, tmp_path / "wizard.json")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    decisions_path = tmp_path / "decisions.json"
    decisions_path.write_text('{"decisions":[]}', encoding="utf-8")
    first_archive = tmp_path / "444.7z"
    second_archive = tmp_path / "445.7z"
    first_archive.write_bytes(b"archive-444")
    second_archive.write_bytes(b"archive-445")
    queue_path = tmp_path / "queue.json"
    queue_path.write_text(
        json.dumps(
            _download_queue(
                first_archive=first_archive,
                second_archive=second_archive,
            )
        ),
        encoding="utf-8",
    )
    captured = {}

    def fake_convert(**kwargs):
        captured["local_dsd_sources"] = kwargs["local_dsd_sources"]
        output_mod_path = Path(kwargs["output_root"]) / "LoreRim - Turkish DSD Output"
        output_mod_path.mkdir(parents=True)
        conversion_dir = Path(kwargs["out_dir"])
        conversion_dir.mkdir(parents=True)
        manifest_path = conversion_dir / "converter_manifest.json"
        report_path = conversion_dir / "converter_report.md"
        payload = {"summary": {"processed_archives": 2, "failed_items": 0}}
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        report_path.write_text("ok", encoding="utf-8")
        return WizardArchiveConversionRunResult(
            manifest_path=manifest_path,
            report_path=report_path,
            output_mod_path=output_mod_path,
            manifest_payload=payload,
        )

    monkeypatch.setattr(
        "modlist_translation_wizard.runtime.convert_downloaded_archives_to_mtw_dsd",
        fake_convert,
    )

    result = convert_downloaded_translations_from_manifest(
        manifest_path=manifest_result.manifest_path,
        profile_scan_path=profile_path,
        decisions_path=decisions_path,
        download_queue_path=queue_path,
        out_dir=tmp_path / "runtime",
    )

    assert captured["local_dsd_sources"] == [local_source]
    assert result.result_payload["local_dsd_sources"] == [str(local_source)]
    assert result.conversion.manifest_path.name == "mtw_dsd_conversion_manifest.json"
    assert result.conversion.report_path.name == "mtw_dsd_conversion_report.md"


def test_conversion_stages_output_without_installing_to_mo2(tmp_path, monkeypatch) -> None:
    profile = _profile()
    manifest_result = write_wizard_manifest(
        _manifest(profile=profile),
        tmp_path / "wizard.json",
    )
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    decisions_path = tmp_path / "decisions.json"
    decisions_path.write_text('{"decisions":[]}', encoding="utf-8")
    first_archive = tmp_path / "444.7z"
    second_archive = tmp_path / "445.7z"
    first_archive.write_bytes(b"archive-444")
    second_archive.write_bytes(b"archive-445")
    queue_path = tmp_path / "queue.json"
    queue_path.write_text(
        json.dumps(
            _download_queue(
                first_archive=first_archive,
                second_archive=second_archive,
            )
        ),
        encoding="utf-8",
    )
    staging_root = tmp_path / "staging" / "mods"

    def fake_convert(**kwargs):
        output_mod_path = Path(kwargs["output_root"]) / "LoreRim - Turkish DSD Output"
        output_mod_path.mkdir(parents=True)
        conversion_dir = Path(kwargs["out_dir"])
        conversion_dir.mkdir(parents=True)
        manifest_path = conversion_dir / "converter_manifest.json"
        report_path = conversion_dir / "converter_report.md"
        payload = {
            "summary": {
                "processed_archives": 2,
                "failed_items": 0,
                "plugin_outputs": 1,
                "native_outputs": 0,
                "entry_count": 3,
            }
        }
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        report_path.write_text("ok", encoding="utf-8")
        return WizardArchiveConversionRunResult(
            manifest_path=manifest_path,
            report_path=report_path,
            output_mod_path=output_mod_path,
            manifest_payload=payload,
        )

    monkeypatch.setattr(
        "modlist_translation_wizard.runtime.convert_downloaded_archives_to_mtw_dsd",
        fake_convert,
    )

    result = convert_downloaded_translations_from_manifest(
        manifest_path=manifest_result.manifest_path,
        profile_scan_path=profile_path,
        decisions_path=decisions_path,
        download_queue_path=queue_path,
        out_dir=tmp_path / "runtime",
        staging_root=staging_root,
    )

    assert result.result_payload["status"] == "COMPLETED"
    assert result.result_payload["install_state"] == "STAGED_NOT_INSTALLED"
    assert result.conversion.output_mod_path == (
        staging_root / "LoreRim - Turkish DSD Output"
    )
    assert not (tmp_path / "profiles").exists()


def test_conversion_overlays_add_on_package_after_converter(tmp_path, monkeypatch) -> None:
    profile = _profile()
    add_on_archive = tmp_path / "LoreRim - Turkce Ek Paketi.zip"
    with zipfile.ZipFile(add_on_archive, "w") as archive:
        archive.writestr("Interface/marker.txt", "addon")
        archive.writestr(
            "SKSE/Plugins/DynamicStringDistributor/Extra.esp/Extra.esp_CKDM.json",
            "{}",
        )
    manifest = _manifest(profile=profile)
    manifest["add_on_packages"] = [
        _add_on_package(expected_sha256=_sha256(add_on_archive))
    ]
    manifest_result = write_wizard_manifest(manifest, tmp_path / "wizard.json")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    decisions_path = tmp_path / "decisions.json"
    decisions_path.write_text('{"decisions":[]}', encoding="utf-8")
    first_archive = tmp_path / "444.7z"
    second_archive = tmp_path / "445.7z"
    first_archive.write_bytes(b"archive-444")
    second_archive.write_bytes(b"archive-445")
    queue = _download_queue(
        first_archive=first_archive,
        second_archive=second_archive,
    )
    queue["items"].append(
        {
            "status": "READY",
            "local_archive_path": str(add_on_archive),
            "request": {
                "game_domain": "skyrimspecialedition",
                "translation_nexus_mod_id": 158770,
                "translation_file_id": 773329,
            },
            "mtw_add_on_package": {"id": "lorerim-extra-pack"},
        }
    )
    queue_path = tmp_path / "queue.json"
    queue_path.write_text(json.dumps(queue), encoding="utf-8")
    staging_root = tmp_path / "selected-modlist" / "mods"

    def fake_convert(**kwargs):
        output_mod_path = Path(kwargs["output_root"]) / "LoreRim - Turkish DSD Output"
        (output_mod_path / "Interface").mkdir(parents=True)
        (output_mod_path / "Interface" / "marker.txt").write_text(
            "base",
            encoding="utf-8",
        )
        conversion_dir = Path(kwargs["out_dir"])
        conversion_dir.mkdir(parents=True)
        manifest_path = conversion_dir / "converter_manifest.json"
        report_path = conversion_dir / "converter_report.md"
        payload = {"summary": {"processed_archives": 2, "failed_items": 0}}
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        report_path.write_text("ok", encoding="utf-8")
        return WizardArchiveConversionRunResult(
            manifest_path=manifest_path,
            report_path=report_path,
            output_mod_path=output_mod_path,
            manifest_payload=payload,
        )

    monkeypatch.setattr(
        "modlist_translation_wizard.runtime.convert_downloaded_archives_to_mtw_dsd",
        fake_convert,
    )

    result = convert_downloaded_translations_from_manifest(
        manifest_path=manifest_result.manifest_path,
        profile_scan_path=profile_path,
        decisions_path=decisions_path,
        download_queue_path=queue_path,
        out_dir=tmp_path / "runtime",
        staging_root=staging_root,
    )

    marker = result.conversion.output_mod_path / "Interface" / "marker.txt"
    assert marker.read_text(encoding="utf-8") == "addon"
    assert result.result_payload["status"] == "COMPLETED"
    add_on_summary = result.result_payload["add_on_packages"]["summary"]
    assert add_on_summary["extracted"] == 1
    assert add_on_summary["extracted_file_count"] == 2


def test_conversion_extracts_manifest_native_binary_asset_after_converter(
    tmp_path,
    monkeypatch,
) -> None:
    profile = _profile()
    binary_archive = tmp_path / "wheeler-dll.zip"
    with zipfile.ZipFile(binary_archive, "w") as archive:
        archive.writestr("SKSE/Plugins/Wheeler.dll", b"latin-extended-dll")
        archive.writestr("Data/SKSE/Plugins/ignored.dll", b"ignored")
    manifest = _manifest(profile=profile)
    manifest["entries"].append(_native_binary_entry(manifest))
    manifest_result = write_wizard_manifest(manifest, tmp_path / "wizard.json")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    decisions_path = tmp_path / "decisions.json"
    decisions_path.write_text('{"decisions":[]}', encoding="utf-8")
    first_archive = tmp_path / "444.7z"
    second_archive = tmp_path / "445.7z"
    first_archive.write_bytes(b"archive-444")
    second_archive.write_bytes(b"archive-445")
    queue = _download_queue(
        first_archive=first_archive,
        second_archive=second_archive,
    )
    queue["items"].append(
        {
            "status": "READY",
            "local_archive_path": str(binary_archive),
            "request": {
                "game_domain": "skyrimspecialedition",
                "translation_nexus_mod_id": 166452,
                "translation_file_id": 695247,
            },
        }
    )
    queue_path = tmp_path / "queue.json"
    queue_path.write_text(json.dumps(queue), encoding="utf-8")
    staging_root = tmp_path / "selected-modlist" / "mods"

    def fake_convert(**kwargs):
        output_mod_path = Path(kwargs["output_root"]) / "LoreRim - Turkish DSD Output"
        output_mod_path.mkdir(parents=True)
        conversion_dir = Path(kwargs["out_dir"])
        conversion_dir.mkdir(parents=True)
        manifest_path = conversion_dir / "converter_manifest.json"
        report_path = conversion_dir / "converter_report.md"
        payload = {"summary": {"processed_archives": 2, "failed_items": 0}}
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        report_path.write_text("ok", encoding="utf-8")
        return WizardArchiveConversionRunResult(
            manifest_path=manifest_path,
            report_path=report_path,
            output_mod_path=output_mod_path,
            manifest_payload=payload,
        )

    monkeypatch.setattr(
        "modlist_translation_wizard.runtime.convert_downloaded_archives_to_mtw_dsd",
        fake_convert,
    )

    result = convert_downloaded_translations_from_manifest(
        manifest_path=manifest_result.manifest_path,
        profile_scan_path=profile_path,
        decisions_path=decisions_path,
        download_queue_path=queue_path,
        out_dir=tmp_path / "runtime",
        staging_root=staging_root,
    )

    output_mod = result.conversion.output_mod_path
    assert (output_mod / "SKSE" / "Plugins" / "Wheeler.dll").read_bytes() == (
        b"latin-extended-dll"
    )
    assert not (output_mod / "SKSE" / "Plugins" / "ignored.dll").exists()
    assert result.result_payload["status"] == "COMPLETED"
    binary_summary = result.result_payload["native_binary_assets"]["summary"]
    assert binary_summary["extracted"] == 1
    managed_state = json.loads(
        (output_mod / ".mtw" / "managed_native_binary_assets.json").read_text(
            encoding="utf-8"
        )
    )
    assert managed_state["items"][0]["archive_member"] == "SKSE/Plugins/Wheeler.dll"


def test_conversion_removes_stale_managed_native_binary_when_manifest_entry_is_removed(
    tmp_path,
    monkeypatch,
) -> None:
    profile = _profile()
    manifest_result = write_wizard_manifest(
        _manifest(profile=profile),
        tmp_path / "wizard.json",
    )
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    decisions_path = tmp_path / "decisions.json"
    decisions_path.write_text('{"decisions":[]}', encoding="utf-8")
    first_archive = tmp_path / "444.7z"
    second_archive = tmp_path / "445.7z"
    first_archive.write_bytes(b"archive-444")
    second_archive.write_bytes(b"archive-445")
    queue_path = tmp_path / "queue.json"
    queue_path.write_text(
        json.dumps(
            _download_queue(
                first_archive=first_archive,
                second_archive=second_archive,
            )
        ),
        encoding="utf-8",
    )
    staging_root = tmp_path / "selected-modlist" / "mods"

    def fake_convert(**kwargs):
        output_mod_path = Path(kwargs["output_root"]) / "LoreRim - Turkish DSD Output"
        stale = output_mod_path / "SKSE" / "Plugins" / "Wheeler.dll"
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b"old-managed-dll")
        state = output_mod_path / ".mtw" / "managed_native_binary_assets.json"
        state.parent.mkdir(parents=True)
        state.write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "target_path": "SKSE/Plugins/Wheeler.dll",
                            "normalized_target_path": "skse/plugins/wheeler.dll",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        conversion_dir = Path(kwargs["out_dir"])
        conversion_dir.mkdir(parents=True)
        manifest_path = conversion_dir / "converter_manifest.json"
        report_path = conversion_dir / "converter_report.md"
        payload = {"summary": {"processed_archives": 2, "failed_items": 0}}
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        report_path.write_text("ok", encoding="utf-8")
        return WizardArchiveConversionRunResult(
            manifest_path=manifest_path,
            report_path=report_path,
            output_mod_path=output_mod_path,
            manifest_payload=payload,
        )

    monkeypatch.setattr(
        "modlist_translation_wizard.runtime.convert_downloaded_archives_to_mtw_dsd",
        fake_convert,
    )

    result = convert_downloaded_translations_from_manifest(
        manifest_path=manifest_result.manifest_path,
        profile_scan_path=profile_path,
        decisions_path=decisions_path,
        download_queue_path=queue_path,
        out_dir=tmp_path / "runtime",
        staging_root=staging_root,
    )

    stale = result.conversion.output_mod_path / "SKSE" / "Plugins" / "Wheeler.dll"
    assert not stale.exists()
    assert result.result_payload["native_binary_assets"]["summary"]["cleanup_removed"] == 1


def test_conversion_uses_requested_output_mod_name_override(tmp_path, monkeypatch) -> None:
    profile = _profile()
    manifest_result = write_wizard_manifest(
        _manifest(profile=profile, output_mod_name="LoreRim - Turkce Ceviri"),
        tmp_path / "wizard.json",
    )
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    decisions_path = tmp_path / "decisions.json"
    decisions_path.write_text('{"decisions":[]}', encoding="utf-8")
    first_archive = tmp_path / "444.7z"
    second_archive = tmp_path / "445.7z"
    first_archive.write_bytes(b"archive-444")
    second_archive.write_bytes(b"archive-445")
    queue_path = tmp_path / "queue.json"
    queue_path.write_text(
        json.dumps(
            _download_queue(
                first_archive=first_archive,
                second_archive=second_archive,
            )
        ),
        encoding="utf-8",
    )
    staging_root = tmp_path / "selected-modlist" / "mods"
    captured = {}

    def fake_convert(**kwargs):
        captured["output_mod_name_override"] = kwargs["output_mod_name_override"]
        output_mod_path = Path(kwargs["output_root"]) / kwargs["output_mod_name_override"]
        output_mod_path.mkdir(parents=True)
        conversion_dir = Path(kwargs["out_dir"])
        conversion_dir.mkdir(parents=True)
        manifest_path = conversion_dir / "converter_manifest.json"
        report_path = conversion_dir / "converter_report.md"
        payload = {"summary": {"processed_archives": 2, "failed_items": 0}}
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        report_path.write_text("ok", encoding="utf-8")
        return WizardArchiveConversionRunResult(
            manifest_path=manifest_path,
            report_path=report_path,
            output_mod_path=output_mod_path,
            manifest_payload=payload,
        )

    monkeypatch.setattr(
        "modlist_translation_wizard.runtime.convert_downloaded_archives_to_mtw_dsd",
        fake_convert,
    )

    result = convert_downloaded_translations_from_manifest(
        manifest_path=manifest_result.manifest_path,
        profile_scan_path=profile_path,
        decisions_path=decisions_path,
        download_queue_path=queue_path,
        out_dir=tmp_path / "runtime",
        staging_root=staging_root,
        output_mod_name_override="Nordic Souls - Turkce Ceviri",
    )

    assert captured["output_mod_name_override"] == "Nordic Souls - Turkce Ceviri"
    assert result.conversion.output_mod_path == staging_root / "Nordic Souls - Turkce Ceviri"


def _manifest(profile=None, output_mod_name="LoreRim - Turkish DSD Output"):
    profile = profile or _profile()
    return build_wizard_manifest(
        profile=profile,
        decisions=_decisions(),
        list_id="lorerim",
        list_name="LoreRim",
        list_version="test-1",
        output_mod_name=output_mod_name,
        channel="stable",
        release_state="DRAFT",
        created_at="2026-06-25T00:00:00+00:00",
    )


def _profile():
    return {
        "schema_version": "profile-scan.v1",
        "mo2": {"root": "FixtureRoots/LoreRim", "profile": "Ultra"},
        "active_plugins": ["Example.esp"],
        "mods": [
            {
                "name": "Example Mod",
                "enabled": True,
                "priority": 1,
                "version": "1.2.3",
                "nexus": {"mod_id": 111, "file_id": 222},
                "plugins": ["Example.esp"],
            }
        ],
    }


def _decisions():
    approved_candidate = {
        "display_name": "Example Mod Turkish",
        "source": "ManualTranslationOverride",
        "language": "tr",
        "nexus": {
            "mod_id": 333,
            "file_id": 444,
            "game_domain": "skyrimspecialedition",
        },
        "translation_nexus_mod_id": 333,
        "translation_file_id": 444,
        "translation_name": "Example Mod Turkish",
        "translation_file_name": "example-tr.7z",
        "additional_translation_files": [
            {
                "translation_file_id": 445,
                "translation_file_name": "example-tr-patch.7z",
                "reason": "required_multipart_translation_file",
            }
        ],
    }
    review_candidate = dict(approved_candidate)
    review_candidate["translation_file_id"] = 446
    review_candidate["nexus"] = dict(approved_candidate["nexus"], file_id=446)
    return {
        "schema_version": "translation-decisions.v1",
        "language": "tr",
        "decisions": [
            {
                "base": {
                    "name": "Example Mod",
                    "version": "1.2.3",
                    "plugins": ["Example.esp"],
                    "nexus": {"mod_id": 111, "file_id": 222},
                },
                "status": "APPROVED",
                "selected_candidate": approved_candidate,
                "score": 100,
                "reasons": ["manual_override_preferred"],
                "warnings": [],
            },
            {
                "base": {
                    "name": "Review Mod",
                    "nexus": {"mod_id": 112, "file_id": 223},
                },
                "status": "NEEDS_REVIEW",
                "selected_candidate": review_candidate,
                "score": 50,
                "reasons": ["version_mismatch"],
                "warnings": [],
            },
        ],
    }


def _download_queue(*, first_archive: Path, second_archive: Path) -> dict:
    return {
        "schema_version": "download-queue.v1",
        "items": [
            {
                "status": "READY",
                "local_archive_path": str(first_archive),
                "request": {
                    "game_domain": "skyrimspecialedition",
                    "translation_nexus_mod_id": 333,
                    "translation_file_id": 444,
                },
            },
            {
                "status": "DOWNLOADED",
                "local_archive_path": str(second_archive),
                "request": {
                    "game_domain": "skyrimspecialedition",
                    "translation_nexus_mod_id": 333,
                    "translation_file_id": 445,
                },
            },
        ],
    }


def _add_on_package(
    *,
    package_id: str = "lorerim-extra-pack",
    expected_sha256: str | None = None,
) -> dict:
    return {
        "id": package_id,
        "name": "LoreRim Turkce Ek Paketi",
        "enabled": True,
        "required": True,
        "game_domain": "skyrimspecialedition",
        "translation_nexus_mod_id": 158770,
        "translation_file_id": 773329,
        "translation_file_name": "LoreRim - Turkce Ek Paketi.zip",
        "expected_size": 26231510,
        "expected_sha256": expected_sha256,
        "install_mode": "OUTPUT_MOD_OVERLAY",
        "apply_order": 100000,
        "source_url": (
            "https://www.nexusmods.com/skyrimspecialedition/mods/158770"
            "?tab=files&file_id=773329"
        ),
    }


def _native_binary_entry(manifest: dict) -> dict:
    base_entry = manifest["entries"][0]
    artifact = base_entry["artifacts"][0]
    return {
        "target_id": "target-native-binary-wheeler",
        "target": {
            "path": "SKSE/Plugins/Wheeler.dll",
            "normalized_path": "skse/plugins/wheeler.dll",
            "type": "NATIVE_BINARY",
        },
        "base": dict(base_entry["base"]),
        "selection": {
            "status": "APPROVED",
            "confidence": "VERIFIED_METADATA",
            "translation_name": "Wheeler Turkish Latin Extended DLL",
            "score": 100,
            "provenance": ["MTT_BINARY_ASSET_CATALOG"],
            "reasons": ["binary_asset_catalog"],
            "warnings": [],
        },
        "install": {"mode": "NATIVE_BINARY_INSTALL"},
        "artifacts": [
            {
                **artifact,
                "artifact_id": "nexusmods:skyrimspecialedition:166452:695247",
                "source": "MTT_BINARY_ASSET_CATALOG",
                "game_domain": "skyrimspecialedition",
                "translation_nexus_mod_id": 166452,
                "translation_file_id": 695247,
                "translation_name": "Wheeler Turkish Latin Extended DLL",
                "translation_file_name": "wheeler-dll.zip",
                "required": True,
                "provides": ["SKSE/Plugins/Wheeler.dll"],
                "evidence": ["binary_asset_catalog"],
                "install_mode": "NATIVE_BINARY_INSTALL",
                "archive_member": "Data/SKSE/Plugins/Wheeler.dll",
                "archive_member_candidates": [
                    "Data/SKSE/Plugins/Wheeler.dll",
                    "SKSE/Plugins/Wheeler.dll",
                ],
                "managed": True,
                "binary_asset_id": "wheeler-tr-latin-extended-dll",
                "source_url": (
                    "https://www.nexusmods.com/skyrimspecialedition/mods/166452"
                    "?tab=files&file_id=695247"
                ),
            }
        ],
        "alternatives": [],
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
