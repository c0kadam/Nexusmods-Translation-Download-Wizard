from modlist_translation_wizard.installer_gui import (
    C0KADAM_DISCORD_SUPPORT_URL,
    ENDORSEMENT_MESSAGE,
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


def test_endorsement_message_is_user_visible_turkish_copy() -> None:
    assert ENDORSEMENT_MESSAGE == "Endorse etmeyi unutmayın, Esenlikler"
