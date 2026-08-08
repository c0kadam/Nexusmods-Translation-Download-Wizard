from pathlib import Path

from PIL import Image

import modlist_translation_wizard.installer_gui as installer_gui
from modlist_translate_tool.nexus.api_client import NexusRateLimit
from modlist_translation_wizard.endorsement import (
    BulkEndorsementEntry,
    BulkEndorsementSummary,
    ReleaseEndorsementTarget,
)

from modlist_translation_wizard.installer_gui import (
    C0KADAM_DISCORD_SUPPORT_URL,
    ENDORSE_BUTTON_LABEL,
    ModlistTranslationInstallerApp,
    NEGATRM_DISCORD_SUPPORT_URL,
    _banner_title_font_size,
    _apply_window_icon_asset,
    _bulk_endorsement_user_message,
    _conversion_archive_information,
    _conversion_retry_seconds_remaining,
    _download_item_for_part_path,
    _download_item_lookup,
    _endorsement_button_presentation,
    _format_nexus_api_usage,
    _initial_window_size,
    _is_conversion_worker_failure,
    _load_startup_manifest,
)


def test_banner_title_font_size_keeps_long_release_names_inside_title_column() -> None:
    assert _banner_title_font_size("LoreRim Türkçe Çeviri Paketi") == 24
    assert _banner_title_font_size("Nordic Souls Türkçe Çeviri Aracı") == 21
    assert _banner_title_font_size("A" * 45) == 18
    assert _banner_title_font_size("A" * 60) == 16


def test_discord_support_links_use_expected_destinations() -> None:
    assert C0KADAM_DISCORD_SUPPORT_URL == (
        "https://discordapp.com/users/279006796524421130"
    )
    assert NEGATRM_DISCORD_SUPPORT_URL == "https://discord.gg/4cHCUGkEP"


def test_installer_uses_resolved_nolvus_mods_root(tmp_path) -> None:
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

    class Value:
        def __init__(self, value: str) -> None:
            self.value = value

        def get(self) -> str:
            return self.value

    app = object.__new__(ModlistTranslationInstallerApp)
    app.mo2_root = Value(str(nolvus_root))
    app.mo2_profile = Value("Nolvus Awakening")
    app.summary = {"modlist_name": "Nolvus Awakening"}

    assert app._selected_mods_root() == mods_root
    assert app._output_mod_name() == "Nolvus Awakening - Turkce Ceviri"


def test_endorsement_button_uses_thumb_up_action_label() -> None:
    assert ENDORSE_BUTTON_LABEL == "👍 Çevirileri Beğen / Endorse Et"


def test_endorsement_buttons_share_idle_busy_and_completed_states() -> None:
    assert _endorsement_button_presentation(12, busy=False) == (
        ENDORSE_BUTTON_LABEL,
        True,
    )
    assert _endorsement_button_presentation(12, busy=True) == (
        "Gönderiliyor...",
        False,
    )
    assert _endorsement_button_presentation(0, busy=False) == (
        "Beğeniler gönderildi",
        False,
    )


def test_release_icons_contain_windows_titlebar_and_taskbar_sizes() -> None:
    release_root = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "modlist_translation_wizard"
        / "resources"
        / "releases"
    )
    required_sizes = {(16, 16), (32, 32), (48, 48), (256, 256)}
    for release_id in ("lorerim", "nolvus-awakening", "nordicsouls"):
        with Image.open(release_root / release_id / "icon.ico") as icon:
            assert required_sizes <= set(icon.info.get("sizes", set()))


def test_window_icon_is_applied_to_the_current_window(monkeypatch, tmp_path) -> None:
    icon_path = tmp_path / "icon.ico"
    Image.new("RGBA", (32, 32), "#ff8800").save(icon_path, format="ICO")
    photo = object()
    monkeypatch.setattr(installer_gui.ImageTk, "PhotoImage", lambda _image: photo)

    class Window:
        bitmap: str | None = None
        icon_photo: tuple[bool, object] | None = None

        def iconbitmap(self, value: str) -> None:
            self.bitmap = value

        def iconphoto(self, default: bool, value: object) -> None:
            self.icon_photo = (default, value)

    window = Window()

    assert _apply_window_icon_asset(window, icon_path) is photo
    assert window.bitmap == str(icon_path)
    assert window.icon_photo == (True, photo)


def test_conversion_worker_failure_gets_a_visible_retry_cooldown() -> None:
    error = RuntimeError(
        "Ceviri hazirlama islemi ayri worker process icinde basarisiz oldu.\n"
        "Cikis kodu: 3\nDetay: WorkerFailed"
    )

    assert _is_conversion_worker_failure(error) is True
    assert _is_conversion_worker_failure(RuntimeError("ordinary error")) is False
    assert _conversion_retry_seconds_remaining(ready_at=112.0, now=100.0) == 12
    assert _conversion_retry_seconds_remaining(ready_at=112.0, now=112.0) == 0


def test_endorsement_message_explains_scheduled_15_minute_retry() -> None:
    target = ReleaseEndorsementTarget("skyrimspecialedition", 42, "Example")
    result = BulkEndorsementSummary(
        entries=(BulkEndorsementEntry(target, "wait_required", "wait"),),
        total=1,
        endorsed=0,
        already_endorsed=0,
        wait_required=1,
        disabled=0,
        own_file=0,
        abstained=0,
        rate_limited=0,
        unauthorized=0,
        transient_error=0,
        failed=0,
    )

    message = _bulk_endorsement_user_message(
        result,
        auto_retry_scheduled=True,
    )

    assert "15 dakika sonra" in message
    assert "araç açık kalırsa" in message.casefold()
    assert "otomatik olarak yeniden denenecek" in message


def test_download_progress_lookup_resolves_queue_item_from_part_path(tmp_path) -> None:
    archive = tmp_path / "downloads" / "123" / "456" / "translation.7z"
    queue = {
        "items": [
            {
                "status": "PLANNED",
                "local_archive_path": str(archive),
                "request": {
                    "game_domain": "skyrimspecialedition",
                    "translation_nexus_mod_id": 123,
                    "translation_file_id": 456,
                    "translation_name": "Example Turkish Translation",
                    "translation_file_name": "translation.7z",
                },
            }
        ]
    }

    item = _download_item_for_part_path(
        archive.with_name(f"{archive.name}.part"),
        _download_item_lookup(queue),
    )

    assert item["translation_name"] == "Example Turkish Translation"
    assert item["translation_file_name"] == "translation.7z"
    assert item["position"] == 1
    assert item["total"] == 1


def test_conversion_information_shows_current_archive_and_nexus_identity() -> None:
    result = _conversion_archive_information(
        {
            "stage": "extracting_archive",
            "processed_archives": 9,
            "total_archives": 589,
            "archive_path": r"C:\downloads\Example Turkish Translation.7z",
            "translation_nexus_mod_id": 123,
            "translation_file_id": 456,
        }
    )

    assert result is not None
    key, text = result
    assert key[1:] == ("123", "456")
    assert "9/589" in text
    assert "Example Turkish Translation.7z" in text
    assert "Nexus: 123/456" in text


def test_conversion_information_labels_add_on_package() -> None:
    result = _conversion_archive_information(
        {
            "stage": "extracting_add_on_package",
            "processed_packages": 1,
            "total_packages": 2,
            "archive_path": r"C:\downloads\LoreRim Add-on.zip",
            "display_name": "LoreRim Türkçe Ek Paketi",
            "translation_nexus_mod_id": 158770,
            "translation_file_id": 778351,
        }
    )

    assert result is not None
    assert "Ek paket çıkarılıyor: 1/2" in result[1]
    assert "LoreRim Türkçe Ek Paketi" in result[1]


def test_official_api_usage_formats_nexus_response_headers() -> None:
    text = _format_nexus_api_usage(
        NexusRateLimit(
            hourly_remaining=1967,
            hourly_limit=2000,
            daily_remaining=19940,
            daily_limit=20000,
        )
    )

    assert "Resmî Nexus API kotası" in text
    assert "1.967 / 2.000" in text
    assert "19.940 / 20.000" in text


def test_initial_window_size_fits_content_within_screen() -> None:
    assert _initial_window_size(
        preferred_width=1590,
        required_width=1510,
        required_height=955,
        screen_width=1920,
        screen_height=1080,
    ) == (1590, 980)

    assert _initial_window_size(
        preferred_width=1590,
        required_width=1510,
        required_height=955,
        screen_width=1366,
        screen_height=768,
    ) == (1294, 672)


def test_startup_manifest_loader_uses_ota_and_returns_source_details() -> None:
    requested_modes: list[str] = []

    def loader(*, manifest_mode: str):
        requested_modes.append(manifest_mode)
        return {"manifest_id": "remote-test"}

    manifest, source = _load_startup_manifest(
        loader=loader,
        source_info_loader=lambda: {"source": "remote_download"},
    )

    assert requested_modes == ["OTA"]
    assert manifest == {"manifest_id": "remote-test"}
    assert source == {"source": "remote_download"}
