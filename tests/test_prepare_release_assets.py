import pytest

from scripts.prepare_release_assets import _assert_release_manifest_safe


def test_release_manifest_safety_accepts_portable_metadata() -> None:
    _assert_release_manifest_safe(
        {
            "nexus": {
                "authentication": {
                    "primary": "REGISTERED_APPLICATION_SSO",
                    "secret_storage": "OS_CREDENTIAL_STORE",
                }
            },
            "source_url": "https://www.nexusmods.com/example",
        }
    )


def test_release_manifest_safety_rejects_secret_like_fields() -> None:
    with pytest.raises(ValueError, match="secret-like field"):
        _assert_release_manifest_safe({"nexus": {"manual_api_key": "TESTING_ONLY"}})


def test_release_manifest_safety_rejects_local_absolute_paths() -> None:
    with pytest.raises(ValueError, match="local absolute path"):
        _assert_release_manifest_safe(
            {"resources": {"local_dsd_sources": [{"path": r"C:\PrivateData\curated"}]}}
        )
