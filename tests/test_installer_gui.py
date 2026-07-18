from modlist_translation_wizard.installer_gui import _banner_title_font_size


def test_banner_title_font_size_keeps_long_release_names_inside_title_column() -> None:
    assert _banner_title_font_size("LoreRim Türkçe Çeviri Paketi") == 24
    assert _banner_title_font_size("Nordic Souls Türkçe Çeviri Aracı") == 21
    assert _banner_title_font_size("A" * 45) == 18
    assert _banner_title_font_size("A" * 60) == 16
