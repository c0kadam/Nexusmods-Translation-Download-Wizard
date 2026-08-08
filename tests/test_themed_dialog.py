from modlist_translation_wizard.themed_dialog import (
    dialog_dimensions,
    dialog_presentation,
)


def test_dialog_presentations_keep_information_and_errors_visually_distinct() -> None:
    assert dialog_presentation("info").color_key == "link"
    assert dialog_presentation("warning").symbol == "!"
    assert dialog_presentation("danger").eyebrow == "İŞLEM TAMAMLANAMADI"
    assert dialog_presentation("success").symbol == "✓"
    assert dialog_presentation("question").eyebrow == "ONAYINIZ GEREKİYOR"


def test_dialog_dimensions_use_scrollable_content_only_for_long_messages() -> None:
    assert dialog_dimensions("Kısa ve okunabilir bir yönlendirme.") == (650, 390, False)

    width, height, scrollable = dialog_dimensions("Satır\n" * 20)

    assert width == 760
    assert 440 <= height <= 620
    assert scrollable is True


def test_unknown_dialog_tone_falls_back_to_information_style() -> None:
    assert dialog_presentation("unknown") == dialog_presentation("info")
