import json
import zipfile
from contextlib import nullcontext
from pathlib import Path

import pytest

from modlist_translate_tool.nexus.api_client import (
    NexusApiResponse,
    NexusRateLimit,
)
from modlist_translation_wizard.manifest import WizardManifestError, write_wizard_manifest
from modlist_translation_wizard.non_premium import (
    NxmLinkError,
    failed_non_premium_downloads,
    next_non_premium_download,
    parse_nxm_download_link,
    run_non_premium_nxm_download,
    unavailable_non_premium_downloads,
)
from modlist_translation_wizard.runtime import plan_downloads_from_manifest
from tests.test_wizard_manifest import _manifest, _profile


def test_parse_nxm_download_link_extracts_transient_authorization() -> None:
    authorization = parse_nxm_download_link(
        "nxm://skyrimspecialedition/mods/333/files/444"
        "?key=temporary-secret&expires=2000000000&user_id=12",
        now=1900000000,
    )

    assert authorization.game_domain == "skyrimspecialedition"
    assert authorization.mod_id == 333
    assert authorization.file_id == 444
    assert authorization.expires == 2000000000
    assert "temporary-secret" not in repr(authorization)
    assert authorization.safe_payload()["key_present"] is True


def test_parse_nxm_download_link_rejects_expired_or_malformed_links() -> None:
    with pytest.raises(NxmLinkError, match="expired"):
        parse_nxm_download_link(
            "nxm://skyrimspecialedition/mods/333/files/444"
            "?key=temporary-secret&expires=100",
            now=101,
        )

    with pytest.raises(NxmLinkError, match="nxm"):
        parse_nxm_download_link("https://example.test/file", now=1)


def test_next_non_premium_download_skips_archives_already_on_disk(tmp_path) -> None:
    ready_archive = tmp_path / "ready.zip"
    ready_archive.write_bytes(b"ready")
    queue = {
        "items": [
            {
                "status": "READY",
                "local_archive_path": str(ready_archive),
                "request": {
                    "game_domain": "skyrimspecialedition",
                    "translation_nexus_mod_id": 333,
                    "translation_file_id": 444,
                },
            },
            {
                "status": "PLANNED",
                "local_archive_path": str(tmp_path / "missing.zip"),
                "request": {
                    "game_domain": "skyrimspecialedition",
                    "translation_nexus_mod_id": 333,
                    "translation_file_id": 445,
                    "translation_file_name": "patch.zip",
                },
            },
        ]
    }

    item = next_non_premium_download(queue)

    assert item is not None
    assert item["translation_file_id"] == 445
    assert item["page_url"].endswith("?tab=files&file_id=445&nmm=1")


def test_next_non_premium_download_skips_failed_items(tmp_path) -> None:
    queue = {
        "items": [
            {
                "status": "FAILED",
                "local_archive_path": str(tmp_path / "failed.zip"),
                "last_error": "downloaded_size_mismatch",
                "request": {
                    "game_domain": "skyrimspecialedition",
                    "translation_nexus_mod_id": 333,
                    "translation_file_id": 444,
                },
            },
            {
                "status": "PLANNED",
                "local_archive_path": str(tmp_path / "next.zip"),
                "request": {
                    "game_domain": "skyrimspecialedition",
                    "translation_nexus_mod_id": 333,
                    "translation_file_id": 445,
                    "translation_file_name": "next.zip",
                },
            },
        ]
    }

    item = next_non_premium_download(queue)

    assert item is not None
    assert item["translation_file_id"] == 445


def test_failed_non_premium_downloads_include_page_url_and_error(tmp_path) -> None:
    queue = {
        "items": [
            {
                "status": "FAILED",
                "last_error": "downloaded_size_mismatch",
                "local_archive_path": str(tmp_path / "failed.zip"),
                "request": {
                    "game_domain": "skyrimspecialedition",
                    "translation_nexus_mod_id": 333,
                    "translation_file_id": 444,
                    "translation_file_name": "failed-file.zip",
                },
            },
        ]
    }

    failed = failed_non_premium_downloads(queue)

    assert failed == [
        {
            "game_domain": "skyrimspecialedition",
            "translation_nexus_mod_id": 333,
            "translation_file_id": 444,
            "translation_name": "",
            "translation_file_name": "failed-file.zip",
            "status": "FAILED",
            "page_url": (
                "https://www.nexusmods.com/skyrimspecialedition/mods/333"
                "?tab=files&file_id=444&nmm=1"
            ),
            "last_error": "downloaded_size_mismatch",
        }
    ]


def test_unavailable_non_premium_downloads_includes_planned_and_failed(tmp_path) -> None:
    ready_archive = tmp_path / "ready.zip"
    ready_archive.write_bytes(b"ready")
    queue = {
        "items": [
            {
                "status": "READY",
                "local_archive_path": str(ready_archive),
                "request": {
                    "game_domain": "skyrimspecialedition",
                    "translation_nexus_mod_id": 1,
                    "translation_file_id": 10,
                },
            },
            {
                "status": "PLANNED",
                "local_archive_path": str(tmp_path / "planned.zip"),
                "request": {
                    "game_domain": "skyrimspecialedition",
                    "translation_nexus_mod_id": 2,
                    "translation_file_id": 20,
                    "translation_name": "Planned translation",
                },
            },
            {
                "status": "FAILED",
                "last_error": "url_error_timeout",
                "local_archive_path": str(tmp_path / "failed.zip"),
                "request": {
                    "game_domain": "skyrimspecialedition",
                    "translation_nexus_mod_id": 3,
                    "translation_file_id": 30,
                    "translation_file_name": "failed.zip",
                },
            },
        ]
    }

    unavailable = unavailable_non_premium_downloads(queue)

    assert [item["translation_file_id"] for item in unavailable] == [20, 30]
    assert unavailable[0]["page_url"].endswith("file_id=20&nmm=1")
    assert unavailable[1]["last_error"] == "url_error_timeout"


def test_non_premium_download_uses_nxm_authorization_without_persisting_it(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("NEXUS_API_KEY", raising=False)
    plan = _non_premium_plan(tmp_path)
    first_item = plan.download_plan.queue_payload["items"][0]
    first_item["request"]["translation_file_name"] = "example-tr.zip"
    first_item["local_archive_path"] = str(tmp_path / "downloads" / "example-tr.zip")
    plan.download_plan.queue_path.write_text(
        json.dumps(plan.download_plan.queue_payload),
        encoding="utf-8",
    )
    transient_secret = "temporary-nxm-secret"
    fake_client = _FakeNonPremiumClient()

    received_api_keys: list[str | None] = []

    def client_factory(api_key: str | None):
        received_api_keys.append(api_key)
        return fake_client

    result = run_non_premium_nxm_download(
        plan=plan,
        api_key="NON_PREMIUM_API_KEY",
        nxm_url=(
            "nxm://skyrimspecialedition/mods/333/files/444"
            f"?key={transient_secret}&expires=2000000000"
        ),
        file_downloader=_write_test_zip,
        client_factory=client_factory,
        now=1900000000,
    )

    assert result.result_payload["status"] == "DOWNLOADED"
    assert result.result_payload["secrets_persisted"] is False
    assert result.updated_queue_payload["summary"]["downloaded"] == 1
    assert result.updated_queue_payload["summary"]["planned"] == 1
    assert received_api_keys == ["NON_PREMIUM_API_KEY"]
    assert "key=temporary-nxm-secret" in fake_client.download_link_path
    for path in tmp_path.rglob("*"):
        if path.is_file() and path.suffix.casefold() in {".json", ".md", ".queue"}:
            content = path.read_text(encoding="utf-8")
            assert transient_secret not in content
            assert "2000000000" not in content


def test_non_premium_download_requires_api_key(tmp_path) -> None:
    plan = _non_premium_plan(tmp_path)

    with pytest.raises(WizardManifestError, match="API key is required"):
        run_non_premium_nxm_download(
            plan=plan,
            api_key=None,
            nxm_url=(
                "nxm://skyrimspecialedition/mods/333/files/444"
                "?key=temporary-nxm-secret&expires=2000000000"
            ),
            now=1900000000,
        )


def test_non_premium_failed_download_leaves_queue_on_next_file(tmp_path) -> None:
    plan = _non_premium_plan(tmp_path)

    result = run_non_premium_nxm_download(
        plan=plan,
        api_key="NON_PREMIUM_API_KEY",
        nxm_url=(
            "nxm://skyrimspecialedition/mods/333/files/444"
            "?key=temporary-nxm-secret&expires=2000000000"
        ),
        file_downloader=_fail_test_download,
        client_factory=lambda _api_key: _FakeNonPremiumClient(),
        now=1900000000,
    )

    assert result.result_payload["status"] == "FAILED"
    assert result.updated_queue_payload["summary"]["failed"] == 1
    next_item = next_non_premium_download(result.updated_queue_payload)
    assert next_item is not None
    assert next_item["translation_file_id"] == 445


def test_non_premium_download_rejects_link_for_another_queue_item(tmp_path) -> None:
    plan = _non_premium_plan(tmp_path)

    with pytest.raises(NxmLinkError, match="does not match"):
        run_non_premium_nxm_download(
            plan=plan,
            api_key="NON_PREMIUM_API_KEY",
            nxm_url=(
                "nxm://skyrimspecialedition/mods/999/files/888"
                "?key=temporary-nxm-secret&expires=2000000000"
            ),
            client_factory=lambda _api_key: _FakeNonPremiumClient(),
            now=1900000000,
        )


def _non_premium_plan(tmp_path):
    profile = _profile()
    manifest_result = write_wizard_manifest(
        _manifest(profile=profile),
        tmp_path / "wizard.json",
    )
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    return plan_downloads_from_manifest(
        manifest_path=manifest_result.manifest_path,
        profile_scan_path=profile_path,
        download_dir=tmp_path / "downloads",
        out_dir=tmp_path / "runtime",
        delivery_mode="NON_PREMIUM_NXM",
        api_key="NON_PREMIUM_API_KEY",
    )


def _write_test_zip(_url: str, part_path: Path) -> int:
    part_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(part_path, "w") as archive:
        archive.writestr("translated.txt", "ok")
    return part_path.stat().st_size


def _fail_test_download(_url: str, _part_path: Path) -> int:
    raise RuntimeError("simulated download failure")


class _FakeNonPremiumClient:
    def __init__(self) -> None:
        self.download_link_path = ""

    def get_json(self, path: str) -> NexusApiResponse:
        self.download_link_path = path
        return NexusApiResponse(
            payload=[
                {
                    "name": "Primary",
                    "short_name": "Nexus CDN",
                    "URI": "https://downloads.example.test/example-tr.zip",
                }
            ],
            rate_limit=NexusRateLimit(hourly_remaining=99),
        )

    def get_mod_file(self, _game_domain: str, _mod_id: int, _file_id: int):
        return NexusApiResponse(payload={}, rate_limit=NexusRateLimit())

    def operation_lock(self, _operation_name: str):
        return nullcontext()

    def request_telemetry(self) -> dict[str, int]:
        return {"network_requests": 1, "cache_hits": 0}
