import json
import os

import pytest

from modlist_translation_wizard.credential_store import MemoryCredentialStore
from modlist_translation_wizard.manifest import write_wizard_manifest
from modlist_translation_wizard.nexus_auth import (
    NexusAuthError,
    api_key_status,
    auth_report_payload,
    clear_api_key,
    create_sso_handshake,
    load_api_key,
    store_manual_api_key,
    temporary_nexus_api_key_env,
)
from modlist_translation_wizard.runtime import (
    plan_premium_downloads_from_manifest,
    run_premium_downloads_from_manifest,
)
from tests.test_wizard_manifest import _manifest, _profile


def test_sso_handshake_uses_registered_app_id_without_secret() -> None:
    handshake = create_sso_handshake(
        app_id="modlist-translation-wizard",
        session_id="4c694264-1fdb-48c6-a5a0-8edd9e53c7a6",
    )

    assert handshake.websocket_url == "wss://sso.nexusmods.com"
    assert handshake.authorize_url.endswith("?id=4c694264-1fdb-48c6-a5a0-8edd9e53c7a6")
    assert handshake.initial_payload == {
        "id": "4c694264-1fdb-48c6-a5a0-8edd9e53c7a6",
        "appid": "modlist-translation-wizard",
    }
    assert "api" not in json.dumps(handshake.safe_payload()).casefold()


def test_sso_handshake_rejects_invalid_app_id() -> None:
    with pytest.raises(NexusAuthError, match="app id"):
        create_sso_handshake(app_id="")

    with pytest.raises(NexusAuthError, match="whitespace"):
        create_sso_handshake(app_id="bad app")


def test_manual_api_key_roundtrip_reports_only_safe_metadata() -> None:
    store = MemoryCredentialStore()
    secret = "nxm_secret_value_123"

    status = store_manual_api_key(store, app_id="modlist-translation-wizard", api_key=secret)
    payload = auth_report_payload(status)

    assert load_api_key(store, app_id="modlist-translation-wizard") == secret
    assert status.has_api_key is True
    assert payload["secrets_written_to_output"] is False
    assert secret not in json.dumps(payload)

    clear_api_key(store, app_id="modlist-translation-wizard")
    missing = api_key_status(store, app_id="modlist-translation-wizard")
    assert missing.has_api_key is False


def test_temporary_api_key_env_restores_previous_value(monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_API_KEY", "previous")

    with temporary_nexus_api_key_env("current"):
        assert os.environ["NEXUS_API_KEY"] == "current"

    assert os.environ["NEXUS_API_KEY"] == "previous"


def test_temporary_api_key_env_removes_key_when_no_previous_value(monkeypatch) -> None:
    monkeypatch.delenv("NEXUS_API_KEY", raising=False)

    with temporary_nexus_api_key_env("current"):
        assert os.environ["NEXUS_API_KEY"] == "current"

    assert "NEXUS_API_KEY" not in os.environ


def test_premium_plan_can_use_temporary_api_key_without_leaking_secret(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("NEXUS_API_KEY", raising=False)
    secret = "nxm_secret_value_456"
    profile = _profile()
    manifest_result = write_wizard_manifest(_manifest(profile=profile), tmp_path / "wizard.json")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    result = plan_premium_downloads_from_manifest(
        manifest_path=manifest_result.manifest_path,
        profile_scan_path=profile_path,
        download_dir=tmp_path / "downloads",
        out_dir=tmp_path / "out",
        api_key=secret,
    )

    rendered_plan = json.dumps(result.download_plan.plan_payload)
    rendered_queue = json.dumps(result.download_plan.queue_payload)
    assert result.download_plan.plan_payload["plan"]["auth"]["auth_mode"] == "api_key_env"
    assert result.download_plan.plan_payload["plan"]["auth"]["env_var_name"] == "NEXUS_API_KEY"
    assert secret not in rendered_plan
    assert secret not in rendered_queue
    assert "NEXUS_API_KEY" not in os.environ


def test_premium_download_run_wrapper_uses_manifest_queue_without_discovery(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("NEXUS_API_KEY", raising=False)
    secret = "nxm_secret_value_789"
    profile = _profile()
    manifest_result = write_wizard_manifest(_manifest(profile=profile), tmp_path / "wizard.json")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    result = run_premium_downloads_from_manifest(
        manifest_path=manifest_result.manifest_path,
        profile_scan_path=profile_path,
        download_dir=tmp_path / "downloads",
        out_dir=tmp_path / "out",
        api_key=secret,
        max_items=0,
        client_factory=lambda api_key: _FakeNexusClient(api_key),
    )

    rendered = json.dumps(
        {
            "plan": result.plan.download_plan.plan_payload,
            "queue": result.plan.download_plan.queue_payload,
            "run": result.download_run.manifest_payload,
        }
    )
    assert result.plan.preflight_payload["discovery_performed"] is False
    assert result.download_run.manifest_payload["summary"]["skipped"] == 2
    assert result.download_run.manifest_payload["summary"]["failed"] == 0
    assert secret not in rendered
    assert "NEXUS_API_KEY" not in os.environ


class _FakeNexusClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def request_telemetry(self) -> dict[str, object]:
        return {"network_requests": 0, "cache_hits": 0}
