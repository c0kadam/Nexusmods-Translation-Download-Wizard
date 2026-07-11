import json

from modlist_translation_wizard.gui_model import (
    NON_PREMIUM_DELIVERY_LABEL,
    NEXUS_API_KEYS_URL,
    discover_mo2_profiles,
    delivery_mode_options,
    delivery_mode_value,
    estimated_remaining_seconds,
    format_eta,
    installer_button_state,
    load_release_branding,
    manifest_summary,
    preflight_summary,
    release_branding_asset_bytes,
    release_branding_asset_path,
    run_workspace_for_manifest,
    smart_modlist_display_name,
    translation_output_mod_name,
    visible_auth_controls,
)
from tests.test_wizard_manifest import _manifest


def test_discover_mo2_profiles_returns_profiles_with_modlist(tmp_path) -> None:
    profiles = tmp_path / "profiles"
    (profiles / "Default").mkdir(parents=True)
    (profiles / "Default" / "modlist.txt").write_text("+Example", encoding="utf-8")
    (profiles / "NoModlist").mkdir()
    (profiles / "Ultra").mkdir()
    (profiles / "Ultra" / "modlist.txt").write_text("+Example", encoding="utf-8")

    assert discover_mo2_profiles(tmp_path) == ["Default", "Ultra"]


def test_run_workspace_for_manifest_is_manifest_specific(tmp_path) -> None:
    workspace = run_workspace_for_manifest(_manifest(), tmp_path)

    assert workspace == tmp_path / "runs" / "lorerim" / "lorerim-tr-stable"


def test_manifest_summary_uses_manifest_metadata() -> None:
    summary = manifest_summary(_manifest())

    assert summary["modlist_name"] == "LoreRim"
    assert summary["entry_count"] == "1"
    assert summary["unique_download_count"] == "2"
    assert summary["add_on_package_count"] == "0"
    assert summary["registered_app_id"] == "modlist-translation-wizard"


def test_manifest_summary_counts_add_on_packages() -> None:
    manifest = _manifest()
    manifest["add_on_packages"] = [
        {
            "id": "lorerim-extra-pack",
            "name": "LoreRim Turkce Ek Paketi",
            "enabled": True,
            "required": True,
            "game_domain": "skyrimspecialedition",
            "translation_nexus_mod_id": 158770,
            "translation_file_id": 773329,
            "install_mode": "OUTPUT_MOD_OVERLAY",
        }
    ]

    summary = manifest_summary(manifest)

    assert summary["unique_download_count"] == "3"
    assert summary["base_download_count"] == "2"
    assert summary["add_on_package_count"] == "1"


def test_release_branding_loads_bundled_lorerim_defaults() -> None:
    branding = load_release_branding(_manifest())

    assert branding.display_name == "LoreRim Türkçe Çeviri Paketi"
    assert branding.subtitle
    assert branding.accent_color == "#603415"
    assert branding.banner == "lorerim.png"
    assert release_branding_asset_bytes(_manifest(), branding.banner)


def test_release_branding_loads_external_release_assets(tmp_path, monkeypatch) -> None:
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    (release_dir / "branding.json").write_text(
        json.dumps(
            {
                "display_name": "External Release",
                "subtitle": "External subtitle",
                "banner": "banner.png",
                "accent_color": "#123456",
            }
        ),
        encoding="utf-8",
    )
    (release_dir / "banner.png").write_bytes(b"external-banner")
    monkeypatch.setenv("MTW_RELEASE_DIR", str(release_dir))

    branding = load_release_branding(_manifest())

    assert branding.display_name == "External Release"
    assert branding.accent_color == "#123456"
    assert release_branding_asset_bytes(_manifest(), branding.banner) == b"external-banner"


def test_release_branding_asset_path_prefers_external_release_icon(tmp_path, monkeypatch) -> None:
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    icon = release_dir / "icon.ico"
    icon.write_bytes(b"fake-icon")
    monkeypatch.setenv("MTW_RELEASE_DIR", str(release_dir))

    assert release_branding_asset_path(_manifest(), "icon.ico") == icon


def test_translation_output_mod_name_tracks_selected_modlist_root() -> None:
    assert smart_modlist_display_name("FixtureRoots/LoreRim") == "LoreRim"
    assert smart_modlist_display_name("FixtureRoots/NordicSouls") == "Nordic Souls"
    assert smart_modlist_display_name("FixtureRoots/MagesAndVikings") == "Mages And Vikings"
    assert translation_output_mod_name("FixtureRoots/LoreRim") == "LoreRim - Turkce Ceviri"
    assert (
        translation_output_mod_name("FixtureRoots/NordicSouls")
        == "Nordic Souls - Turkce Ceviri"
    )


def test_auth_controls_hide_sso_and_expose_api_settings_link() -> None:
    controls = visible_auth_controls()

    assert controls["api_key"] is True
    assert controls["api_key_settings_link"] is True
    assert controls["sso"] is False
    assert controls["registered_app_id"] is False
    assert NEXUS_API_KEYS_URL == "https://www.nexusmods.com/settings/api-keys"


def test_delivery_mode_labels_are_end_user_facing() -> None:
    assert delivery_mode_options() == ("Premium", "Ücretsiz / Tarayıcı")
    assert delivery_mode_value("Premium") == "PREMIUM_API"
    assert delivery_mode_value(NON_PREMIUM_DELIVERY_LABEL) == "NON_PREMIUM_NXM"


def test_eta_helpers_format_conservative_remaining_time() -> None:
    assert estimated_remaining_seconds(elapsed_seconds=0, completed=1, total=10) is None
    assert estimated_remaining_seconds(elapsed_seconds=30, completed=0, total=10) is None
    assert estimated_remaining_seconds(elapsed_seconds=30, completed=3, total=10) == 70
    assert estimated_remaining_seconds(elapsed_seconds=30, completed=10, total=10) == 0
    assert format_eta(None) == "Yaklaşık kalan süre: hesaplanıyor"
    assert format_eta(0) == "Yaklaşık kalan süre: tamamlanıyor"
    assert format_eta(30) == "Yaklaşık kalan süre: 1 dk'dan az"
    assert format_eta(61) == "Yaklaşık kalan süre: 2 dk"
    assert format_eta(3660) == "Yaklaşık kalan süre: 1 sa 1 dk"


def test_preflight_summary_and_installer_button_states() -> None:
    preflight = {
        "status": "READY",
        "profile": {"name": "Ultra", "exact_match": True},
        "summary": {
            "manifest_entries": 10,
            "matched_entries": 10,
            "missing_entries": 0,
            "compatibility_warning_count": 0,
        },
    }

    summary = preflight_summary(preflight)

    assert summary["status"] == "READY"
    assert summary["exact_match"] == "Yes"

    blocked = installer_button_state(
        preflight_ready=False,
        has_api_key=True,
        has_download_plan=False,
        downloads_complete=False,
        conversion_complete=False,
    )
    assert blocked.can_download is False
    assert blocked.can_prepare is False

    missing_api = installer_button_state(
        preflight_ready=True,
        has_api_key=False,
        has_download_plan=False,
        downloads_complete=False,
        conversion_complete=False,
    )
    assert missing_api.can_download is False
    assert missing_api.download_hint == "Nexus API anahtarı kaydedilmeli."

    ready = installer_button_state(
        preflight_ready=True,
        has_api_key=True,
        has_download_plan=False,
        downloads_complete=False,
        conversion_complete=False,
        delivery_mode="NON_PREMIUM_NXM",
    )
    assert ready.can_download is True
    assert ready.download_label == "Çevirileri indir"
    assert "Slow Download" in ready.download_hint

    downloads_done = installer_button_state(
        preflight_ready=True,
        has_api_key=True,
        has_download_plan=True,
        downloads_complete=True,
        conversion_complete=False,
    )
    assert downloads_done.can_download is False
    assert downloads_done.can_prepare is True
    assert downloads_done.prepare_label == "Çeviriyi hazırla"
    assert downloads_done.staging_only is True

    completed = installer_button_state(
        preflight_ready=True,
        has_api_key=True,
        has_download_plan=True,
        downloads_complete=True,
        conversion_complete=True,
    )
    assert completed.can_download is False
    assert completed.can_prepare is False
