from modlist_translation_wizard import windows_long_paths
from modlist_translation_wizard.windows_long_paths import WindowsLongPathStatus


def test_enable_windows_long_paths_skips_elevation_when_already_enabled(monkeypatch) -> None:
    enabled = WindowsLongPathStatus(available=True, enabled=True, raw_value=1)
    monkeypatch.setattr(windows_long_paths, "windows_long_path_status", lambda: enabled)

    result = windows_long_paths.enable_windows_long_paths()

    assert result.status == enabled
    assert result.changed is False
    assert result.cancelled is False


def test_enable_windows_long_paths_reports_cancelled_elevation(monkeypatch) -> None:
    disabled = WindowsLongPathStatus(available=True, enabled=False, raw_value=0)
    monkeypatch.setattr(windows_long_paths, "windows_long_path_status", lambda: disabled)
    monkeypatch.setattr(windows_long_paths, "_run_elevated_registry_update", lambda: None)

    result = windows_long_paths.enable_windows_long_paths()

    assert result.changed is False
    assert result.cancelled is True


def test_enable_windows_long_paths_verifies_registry_after_update(monkeypatch) -> None:
    states = iter(
        (
            WindowsLongPathStatus(available=True, enabled=False, raw_value=0),
            WindowsLongPathStatus(available=True, enabled=True, raw_value=1),
        )
    )
    monkeypatch.setattr(windows_long_paths, "windows_long_path_status", lambda: next(states))
    monkeypatch.setattr(windows_long_paths, "_run_elevated_registry_update", lambda: 0)

    result = windows_long_paths.enable_windows_long_paths()

    assert result.changed is True
    assert result.status.enabled is True
