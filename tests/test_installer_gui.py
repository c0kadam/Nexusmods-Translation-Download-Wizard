from modlist_translation_wizard.installer_gui import (
    C0KADAM_DISCORD_SUPPORT_URL,
    ENDORSE_BUTTON_LABEL,
    ModlistTranslationInstallerApp,
    NEGATRM_DISCORD_SUPPORT_URL,
    _banner_title_font_size,
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
    assert ENDORSE_BUTTON_LABEL == "👍 Endorse Et"
