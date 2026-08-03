import json

from modlist_translation_wizard.gui_model import (
    NON_PREMIUM_DELIVERY_LABEL,
    NEXUS_API_KEYS_URL,
    api_key_notice,
    default_workspace_root,
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
    resolve_installer_mo2_location,
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


def test_nolvus_root_resolves_split_mo2_profiles_and_mods(tmp_path) -> None:
    nolvus_root = tmp_path / "NOLVUS"
    instance_root = nolvus_root / "Instances" / "Nolvus Awakening"
    mo2_root = instance_root / "MO2"
    data_root = instance_root / "MODS"
    profile_root = data_root / "profiles" / "Nolvus Awakening"
    mods_root = data_root / "mods"
    mo2_root.mkdir(parents=True)
    profile_root.mkdir(parents=True)
    mods_root.mkdir(parents=True)
    (profile_root / "modlist.txt").write_text("+Example", encoding="utf-8")
    (mo2_root / "ModOrganizer.ini").write_text(
        f"base_directory={data_root.resolve().as_posix()}\n",
        encoding="utf-8",
    )

    assert discover_mo2_profiles(nolvus_root) == ["Nolvus Awakening"]
    location = resolve_installer_mo2_location(nolvus_root, "Nolvus Awakening")

    assert location is not None
    assert location.instance_root == instance_root
    assert location.data_root == data_root
    assert location.profiles_dir == data_root / "profiles"
    assert location.mods_dir == mods_root
    assert location.display_name == "Nolvus Awakening"
    assert location.layout_kind == "modorganizer-ini"
    assert translation_output_mod_name(
        nolvus_root,
        profile_name="Nolvus Awakening",
    ) == "Nolvus Awakening - Turkce Ceviri"


def test_standard_mo2_root_still_resolves_direct_mods_directory(tmp_path) -> None:
    profile_root = tmp_path / "profiles" / "Default"
    mods_root = tmp_path / "mods"
    profile_root.mkdir(parents=True)
    mods_root.mkdir()
    (profile_root / "modlist.txt").write_text("+Example", encoding="utf-8")

    location = resolve_installer_mo2_location(tmp_path, "Default")

    assert location is not None
    assert location.data_root == tmp_path
    assert location.mods_dir == mods_root
    assert location.layout_kind == "standard"


def test_run_workspace_for_manifest_is_manifest_specific(tmp_path) -> None:
    workspace = run_workspace_for_manifest(_manifest(), tmp_path)

    assert workspace == tmp_path / "runs" / "lorerim" / "lorerim-tr-stable"


def test_default_workspace_root_uses_local_app_data(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert default_workspace_root() == tmp_path / "Modlist Translation Wizard"


def test_manifest_summary_uses_manifest_metadata() -> None:
    summary = manifest_summary(_manifest())

    assert summary["modlist_name"] == "LoreRim"
    assert summary["entry_count"] == "1"
    assert summary["unique_download_count"] == "2"
    assert summary["add_on_package_count"] == "0"
    assert summary["registered_app_id"] == "modlist-translation-wizard"
    assert summary["manifest_updated_at"] == "2026-06-25 00:00 UTC"


def test_manifest_summary_prefers_explicit_update_timestamp() -> None:
    manifest = _manifest()
    manifest["updated_at"] = "2026-07-17T14:35:00+00:00"

    summary = manifest_summary(manifest)

    assert summary["manifest_updated_at"] == "2026-07-17 14:35 UTC"


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
    assert branding.endorsement is not None
    assert branding.endorsement.game_domain == "skyrimspecialedition"
    assert branding.endorsement.mod_id == 158770
    assert release_branding_asset_bytes(_manifest(), branding.banner)


def test_release_branding_loads_nolvus_completion_notice() -> None:
    manifest = _manifest()
    manifest["modlist"]["id"] = "nolvus-awakening"
    manifest["modlist"]["name"] = "Nolvus Awakening"

    branding = load_release_branding(manifest)

    assert branding.display_name == "Nolvus Awakening Türkçe Çeviri Aracı"
    assert branding.banner == "nolvusTurkceBanner.png"
    assert branding.completion_notice is not None
    assert "Edge, Untarnished veya Oathvein" in branding.completion_notice.text
    assert branding.completion_notice.url == (
        "https://www.nexusmods.com/skyrimspecialedition/mods/164744?tab=files"
    )
    assert release_branding_asset_bytes(manifest, branding.banner)


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
                "font_color": "#ABCDEF",
                "font_shadow": "#010203",
                "warm_glow": "#A04020",
                "completion_notice": {
                    "text": "Kurulumdan sonra ek dosyayı kontrol edin.",
                    "action_label": "Mod sayfasını aç",
                    "url": "https://www.nexusmods.com/example",
                },
            }
        ),
        encoding="utf-8",
    )
    (release_dir / "banner.png").write_bytes(b"external-banner")
    monkeypatch.setenv("MTW_RELEASE_DIR", str(release_dir))

    branding = load_release_branding(_manifest())

    assert branding.display_name == "External Release"
    assert branding.accent_color == "#123456"
    assert branding.font_color == "#ABCDEF"
    assert branding.font_shadow == "#010203"
    assert branding.warm_glow == "#A04020"
    assert branding.completion_notice is not None
    assert branding.completion_notice.text == "Kurulumdan sonra ek dosyayı kontrol edin."
    assert branding.completion_notice.action_label == "Mod sayfasını aç"
    assert branding.completion_notice.url == "https://www.nexusmods.com/example"
    assert release_branding_asset_bytes(_manifest(), branding.banner) == b"external-banner"


def test_release_branding_rejects_unsafe_completion_notice_url(tmp_path, monkeypatch) -> None:
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    (release_dir / "branding.json").write_text(
        json.dumps(
            {
                "completion_notice": {
                    "text": "Ek bilgi",
                    "action_label": "Aç",
                    "url": "file:///tmp/example",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MTW_RELEASE_DIR", str(release_dir))

    branding = load_release_branding(_manifest())

    assert branding.completion_notice is not None
    assert branding.completion_notice.text == "Ek bilgi"
    assert branding.completion_notice.action_label is None
    assert branding.completion_notice.url is None


def test_release_branding_rejects_invalid_color_values(tmp_path, monkeypatch) -> None:
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    (release_dir / "branding.json").write_text(
        json.dumps(
            {
                "accent_color": "red; invalid",
                "font_color": "#12345",
                "font_shadow": None,
                "warm_glow": "#GGGGGG",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MTW_RELEASE_DIR", str(release_dir))

    branding = load_release_branding(_manifest())

    assert branding.accent_color == "#7C8F67"
    assert branding.font_color == "#FFFFFF"
    assert branding.font_shadow == "#28150C"
    assert branding.warm_glow == "#8A5030"


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
    assert controls["api_key_clear"] is True
    assert controls["api_key_settings_link"] is True
    assert controls["sso"] is False
    assert controls["registered_app_id"] is False
    assert NEXUS_API_KEYS_URL == "https://www.nexusmods.com/settings/api-keys"


def test_api_key_notice_warns_without_changing_delivery_mode() -> None:
    assert api_key_notice(has_api_key=True, delivery_mode="PREMIUM_API") == (
        "API anahtarı hazır.",
        "success",
    )
    assert api_key_notice(has_api_key=False, delivery_mode="PREMIUM_API") == (
        "Premium indirme için Nexus API anahtarı gerekli.",
        "warning",
    )
    assert api_key_notice(has_api_key=False, delivery_mode="NON_PREMIUM_NXM") == (
        "Ücretsiz / Tarayıcı indirmesi için Nexus API anahtarı gerekli.",
        "warning",
    )


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

    non_premium_without_api = installer_button_state(
        preflight_ready=True,
        has_api_key=False,
        has_download_plan=False,
        downloads_complete=False,
        conversion_complete=False,
        delivery_mode="NON_PREMIUM_NXM",
    )
    assert non_premium_without_api.can_download is False
    assert non_premium_without_api.download_hint == "Nexus API anahtarı kaydedilmeli."

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
