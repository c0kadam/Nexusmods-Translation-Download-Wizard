import hashlib
import json
from pathlib import Path

import pytest
import modlist_translation_wizard.remote_manifest as remote_manifest_module

from modlist_translation_wizard.bundled import (
    MANIFEST_MODE_LOCAL,
    MANIFEST_MODE_OTA,
    copy_default_bundled_manifest,
    default_manifest_source_info,
    load_default_bundled_manifest,
)
from modlist_translation_wizard.manifest import (
    WizardManifestError,
    build_wizard_manifest,
    load_wizard_manifest,
    write_wizard_manifest,
)
from modlist_translation_wizard.remote_manifest import (
    REMOTE_INDEX_SCHEMA_VERSION,
    RemoteManifestConfig,
    RemoteManifestError,
    clear_remote_manifest_cache,
    resolve_remote_manifest,
)


def test_remote_manifest_downloads_validates_and_caches(tmp_path) -> None:
    manifest_bytes = _manifest_bytes("remote-test")
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    index_url = "https://raw.githubusercontent.com/c0kadam/mtw-manifests/main/index.json"
    manifest_url = (
        "https://raw.githubusercontent.com/c0kadam/mtw-manifests/main/lorerim/stable/"
        "manifest.json"
    )
    fetcher = _fetcher(
        {
            index_url: _index_bytes(manifest_url=manifest_url, manifest_sha=manifest_sha),
            manifest_url: manifest_bytes,
        }
    )
    config = RemoteManifestConfig.from_payload(
        {
            "enabled": True,
            "list_id": "lorerim",
            "channel": "stable",
            "index_url": index_url,
            "allow_hosts": ["raw.githubusercontent.com"],
            "cache_root": str(tmp_path / "cache"),
        },
        default_list_id="lorerim",
        default_manifest_name="manifest.json",
    )

    result = resolve_remote_manifest(config, fetcher=fetcher)

    assert result is not None
    assert result.source == "remote_download"
    assert result.payload["manifest_id"] == "remote-test"
    assert result.manifest_path.exists()
    assert result.digest_path.read_text(encoding="ascii").startswith(manifest_sha)

    cached = resolve_remote_manifest(config, fetcher=lambda *_args: b"{}")

    assert cached is not None
    assert cached.source == "remote_cache"
    assert cached.payload["manifest_id"] == "remote-test"


def test_remote_manifest_cache_can_be_cleared_for_one_release(tmp_path) -> None:
    manifest_bytes = _manifest_bytes("remote-clear-test")
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    index_url = "https://raw.githubusercontent.com/c0kadam/mtw-manifests/main/index.json"
    manifest_url = (
        "https://raw.githubusercontent.com/c0kadam/mtw-manifests/main/lorerim/stable/"
        "manifest.json"
    )
    config = RemoteManifestConfig.from_payload(
        {
            "enabled": True,
            "list_id": "lorerim",
            "channel": "stable",
            "index_url": index_url,
            "allow_hosts": ["raw.githubusercontent.com"],
            "cache_root": str(tmp_path / "cache"),
        },
        default_list_id="lorerim",
        default_manifest_name="manifest.json",
    )
    result = resolve_remote_manifest(
        config,
        fetcher=_fetcher(
            {
                index_url: _index_bytes(
                    manifest_url=manifest_url,
                    manifest_sha=manifest_sha,
                ),
                manifest_url: manifest_bytes,
            }
        ),
    )

    assert result is not None
    assert result.manifest_path.exists()

    clear_remote_manifest_cache(config)

    assert not result.manifest_path.exists()
    assert not result.digest_path.exists()
    assert not result.manifest_path.parent.exists()
    assert not config.cache_root.exists()


def test_remote_manifest_can_use_modlist_scoped_github_index(tmp_path) -> None:
    manifest_bytes = _manifest_bytes("lorerim-scoped-index-test")
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    index_url = "https://raw.githubusercontent.com/c0kadam/NTDW-TranslationMAPS/main/Lorerim/index.json"
    manifest_url = (
        "https://raw.githubusercontent.com/c0kadam/NTDW-TranslationMAPS/main/Lorerim/"
        "stable/manifest.json"
    )
    fetcher = _fetcher(
        {
            index_url: json.dumps(
                {
                    "schema_version": REMOTE_INDEX_SCHEMA_VERSION,
                    "list_id": "lorerim",
                    "manifest": {
                        "channel": "stable",
                        "version": "2026-07-15",
                        "url": manifest_url,
                        "sha256": manifest_sha,
                    },
                }
            ).encode("utf-8"),
            manifest_url: manifest_bytes,
        }
    )
    config = RemoteManifestConfig.from_payload(
        {
            "enabled": True,
            "repository": "c0kadam/NTDW-TranslationMAPS",
            "branch": "main",
            "remote_list_id": "Lorerim",
            "cache_root": str(tmp_path / "cache"),
        },
        default_list_id="lorerim",
        default_manifest_name="manifest.json",
    )

    result = resolve_remote_manifest(config, fetcher=fetcher)

    assert result is not None
    assert result.payload["manifest_id"] == "lorerim-scoped-index-test"
    assert result.index_url == index_url


def test_nordicsouls_release_uses_modlist_scoped_ota_index() -> None:
    config_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "modlist_translation_wizard"
        / "resources"
        / "releases"
        / "nordicsouls"
        / "remote_manifest.json"
    )
    payload = json.loads(config_path.read_text(encoding="utf-8"))

    config = RemoteManifestConfig.from_payload(
        payload,
        default_list_id="nordicsouls",
        default_manifest_name="manifest.json",
    )

    assert config is not None
    assert config.index_url.endswith("/NordicSouls/index.json")


def test_default_loader_always_refreshes_configured_remote_manifest(
    tmp_path,
    monkeypatch,
) -> None:
    manifest_bytes = _manifest_bytes("remote-default-test")
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    index_url = "https://raw.githubusercontent.com/c0kadam/mtw-manifests/main/index.json"
    manifest_url = (
        "https://raw.githubusercontent.com/c0kadam/mtw-manifests/main/lorerim/stable/"
        "manifest.json"
    )
    config_path = tmp_path / "remote_manifest.json"
    config_path.write_text(
        json.dumps(
            {
                "enabled": True,
                "index_url": index_url,
                "allow_hosts": ["raw.githubusercontent.com"],
                "cache_root": str(tmp_path / "cache"),
            }
        ),
        encoding="utf-8",
    )

    requested_urls: list[str] = []

    def fake_fetch(url: str, _timeout: float, _max_bytes: int, _hosts: tuple[str, ...]):
        requested_urls.append(url)
        if url == index_url:
            return _index_bytes(manifest_url=manifest_url, manifest_sha=manifest_sha)
        if url == manifest_url:
            return manifest_bytes
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setenv("MTW_REMOTE_MANIFEST_CONFIG", str(config_path))
    monkeypatch.delenv("MTW_DISABLE_REMOTE_MANIFEST", raising=False)
    monkeypatch.setattr(
        "modlist_translation_wizard.remote_manifest._fetch_url_bytes",
        fake_fetch,
    )

    payload = load_default_bundled_manifest()
    copied = copy_default_bundled_manifest(tmp_path / "runtime")

    assert payload["manifest_id"] == "remote-default-test"
    assert load_wizard_manifest(copied)["manifest_id"] == "remote-default-test"
    assert default_manifest_source_info()["source"] == "remote_download"
    assert requested_urls == [index_url, manifest_url, index_url, manifest_url]


def test_local_manifest_mode_never_calls_remote_fetcher(tmp_path, monkeypatch) -> None:
    release_dir = tmp_path / "release"
    local_payload = json.loads(_manifest_bytes("local-mode-test").decode("utf-8"))
    write_wizard_manifest(local_payload, release_dir / "manifest.json")
    (release_dir / "remote_manifest.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "index_url": "https://raw.githubusercontent.com/c0kadam/mtw-manifests/main/index.json",
                "allow_hosts": ["raw.githubusercontent.com"],
                "cache_root": str(tmp_path / "cache"),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MTW_RELEASE_DIR", str(release_dir))
    monkeypatch.setattr(
        "modlist_translation_wizard.remote_manifest._fetch_url_bytes",
        lambda *_args: (_ for _ in ()).throw(AssertionError("remote fetch must not run")),
    )
    cache_dir = tmp_path / "cache" / "manifests" / "lorerim" / "stable"
    cache_dir.mkdir(parents=True)
    (cache_dir / "manifest.json").write_text("cached", encoding="utf-8")
    (cache_dir / "manifest.json.sha256").write_text("cached", encoding="ascii")
    (cache_dir / "metadata.json").write_text("{}", encoding="utf-8")

    payload = load_default_bundled_manifest(manifest_mode=MANIFEST_MODE_LOCAL)
    source = default_manifest_source_info()

    assert payload["manifest_id"] == "local-mode-test"
    assert source["source"] == "external"
    assert source["requested_mode"] == MANIFEST_MODE_LOCAL
    assert source["active_mode"] == MANIFEST_MODE_LOCAL
    assert source["fallback_from"] is None
    assert not cache_dir.exists()


def test_ota_failure_never_uses_cache_or_local_fallback(tmp_path, monkeypatch) -> None:
    release_dir = tmp_path / "release"
    local_payload = json.loads(_manifest_bytes("ota-fallback-test").decode("utf-8"))
    write_wizard_manifest(local_payload, release_dir / "manifest.json")
    (release_dir / "remote_manifest.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "index_url": "https://raw.githubusercontent.com/c0kadam/mtw-manifests/main/index.json",
                "allow_hosts": ["raw.githubusercontent.com"],
                "cache_root": str(tmp_path / "empty-cache"),
                "allow_stale_cache": True,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MTW_RELEASE_DIR", str(release_dir))
    monkeypatch.setattr(
        "modlist_translation_wizard.remote_manifest._fetch_url_bytes",
        lambda *_args: (_ for _ in ()).throw(RemoteManifestError("network unavailable")),
    )

    cache_dir = tmp_path / "empty-cache" / "manifests" / "lorerim" / "stable"
    cache_dir.mkdir(parents=True)
    cached_bytes = _manifest_bytes("stale-cache-test")
    cached_sha = hashlib.sha256(cached_bytes).hexdigest()
    (cache_dir / "manifest.json").write_bytes(cached_bytes)
    (cache_dir / "manifest.json.sha256").write_text(
        f"{cached_sha}  manifest.json\n",
        encoding="ascii",
    )
    (cache_dir / "metadata.json").write_text(
        json.dumps({"version": "stale-cache-test"}),
        encoding="utf-8",
    )

    with pytest.raises(WizardManifestError, match="Önbellek kullanılmadı"):
        load_default_bundled_manifest(manifest_mode=MANIFEST_MODE_OTA)
    source = default_manifest_source_info()

    assert source["source"] == "unavailable"
    assert source["requested_mode"] == MANIFEST_MODE_OTA
    assert source["active_mode"] is None
    assert "network unavailable" in str(source["warning"])


def test_missing_ota_and_local_manifest_reports_recoverable_error(
    tmp_path,
    monkeypatch,
) -> None:
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    (release_dir / "release_config.json").write_text(
        json.dumps(
            {
                "schema_version": "mtw-release-config.v1",
                "release_id": "missing-release-test",
                "manifest_name": "manifest.json",
            }
        ),
        encoding="utf-8",
    )
    (release_dir / "remote_manifest.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "list_id": "missing-release-test",
                "index_url": "https://raw.githubusercontent.com/c0kadam/mtw-manifests/main/missing/index.json",
                "allow_hosts": ["raw.githubusercontent.com"],
                "cache_root": str(tmp_path / "empty-cache"),
                "allow_stale_cache": False,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MTW_RELEASE_DIR", str(release_dir))
    monkeypatch.delenv("MTW_DEFAULT_RELEASE_ID", raising=False)
    monkeypatch.setattr(
        "modlist_translation_wizard.remote_manifest._fetch_url_bytes",
        lambda *_args: (_ for _ in ()).throw(RemoteManifestError("index not found")),
    )

    with pytest.raises(WizardManifestError, match="Önbellek kullanılmadı"):
        load_default_bundled_manifest(manifest_mode=MANIFEST_MODE_OTA)

    source = default_manifest_source_info()
    assert source["source"] == "unavailable"
    assert source["requested_mode"] == MANIFEST_MODE_OTA
    assert source["active_mode"] is None
    assert "index not found" in str(source["warning"])


def test_remote_manifest_rejects_untrusted_manifest_host(tmp_path) -> None:
    index_url = "https://raw.githubusercontent.com/c0kadam/mtw-manifests/main/index.json"
    manifest_url = "https://example.com/lorerim/stable/manifest.json"
    config = RemoteManifestConfig.from_payload(
        {
            "enabled": True,
            "list_id": "lorerim",
            "index_url": index_url,
            "allow_hosts": ["raw.githubusercontent.com"],
            "cache_root": str(tmp_path / "cache"),
        },
        default_list_id="lorerim",
        default_manifest_name="manifest.json",
    )
    fetcher = _fetcher(
        {
            index_url: _index_bytes(
                manifest_url=manifest_url,
                manifest_sha="0" * 64,
            )
        }
    )

    assert resolve_remote_manifest(config, fetcher=fetcher) is None


def test_http_fetch_retries_incomplete_response(monkeypatch) -> None:
    expected = b'{"complete": true}'
    calls = 0

    class Response:
        def __init__(self, data: bytes, *, start: int = 0) -> None:
            self.data = data
            self.headers = {"Content-Length": str(len(data))}
            if start:
                self.headers["Content-Range"] = (
                    f"bytes {start}-{len(expected) - 1}/{len(expected)}"
                )

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def geturl(self) -> str:
            return "https://raw.githubusercontent.com/example/manifest.json"

        def read(self, _limit: int) -> bytes:
            return self.data

    def urlopen(_request, *, timeout: float):
        nonlocal calls
        assert timeout == 30
        calls += 1
        if calls == 1:
            response = Response(expected[:5])
            response.headers["Content-Length"] = str(len(expected))
            return response
        assert _request.get_header("Range") == "bytes=5-"
        return Response(expected[5:], start=5)

    monkeypatch.setattr(remote_manifest_module, "urlopen", urlopen)
    monkeypatch.setattr(remote_manifest_module.time, "sleep", lambda _seconds: None)

    result = remote_manifest_module._fetch_url_bytes(
        "https://raw.githubusercontent.com/example/manifest.json",
        30,
        1024,
        ("raw.githubusercontent.com",),
    )

    assert result == expected
    assert calls == 2


def _fetcher(mapping: dict[str, bytes]):
    def fetch(url: str, _timeout: float, _max_bytes: int, _hosts: tuple[str, ...]) -> bytes:
        return mapping[url]

    return fetch


def _index_bytes(*, manifest_url: str, manifest_sha: str) -> bytes:
    return json.dumps(
        {
            "schema_version": REMOTE_INDEX_SCHEMA_VERSION,
            "manifests": [
                {
                    "list_id": "lorerim",
                    "channel": "stable",
                    "version": "remote-test-version",
                    "url": manifest_url,
                    "sha256": manifest_sha,
                    "min_app_version": "0.1.0",
                }
            ],
        }
    ).encode("utf-8")


def _manifest_bytes(manifest_id: str) -> bytes:
    payload = build_wizard_manifest(
        profile={
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
        },
        decisions={
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
                    "selected_candidate": {
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
                    },
                    "score": 100,
                    "reasons": ["manual_override_preferred"],
                    "warnings": [],
                }
            ],
        },
        list_id="lorerim",
        list_name="LoreRim",
        list_version="test-remote",
        output_mod_name="LoreRim - Turkce Ceviri",
        channel="stable",
        release_state="DRAFT",
        created_at="2026-07-15T00:00:00+00:00",
    )
    payload["manifest_id"] = manifest_id
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
