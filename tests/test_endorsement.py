from urllib.parse import parse_qs

import pytest

from modlist_translate_tool.nexus.api_client import HttpResponse, NexusApiResponse
from modlist_translation_wizard.endorsement import (
    NexusEndorsementError,
    ReleaseEndorsementTarget,
    collect_manifest_endorsement_targets,
    endorse_manifest_targets,
    endorse_release_translation,
    merge_remaining_endorsement_targets,
    remaining_endorsement_targets,
    release_endorsement_target,
    wait_required_endorsement_targets,
)


def test_release_endorsement_target_is_optional_and_validated() -> None:
    assert release_endorsement_target(None, fallback_label="Example") is None
    assert (
        release_endorsement_target(
            {"enabled": False, "game_domain": "skyrimspecialedition", "mod_id": 10},
            fallback_label="Example",
        )
        is None
    )
    assert (
        release_endorsement_target(
            {"game_domain": "../unsafe", "mod_id": 10},
            fallback_label="Example",
        )
        is None
    )

    target = release_endorsement_target(
        {"game_domain": "SkyrimSpecialEdition", "mod_id": "158770"},
        fallback_label="LoreRim Türkçe Çeviri Paketi",
    )

    assert target == ReleaseEndorsementTarget(
        game_domain="skyrimspecialedition",
        mod_id=158770,
        label="LoreRim Türkçe Çeviri Paketi",
    )


def test_endorse_release_posts_current_mod_version_to_endorse_only() -> None:
    target = ReleaseEndorsementTarget("skyrimspecialedition", 158770, "LoreRim")
    requests: list[tuple[str, str, dict[str, str], bytes]] = []

    def post_transport(method, url, headers, body):
        requests.append((method, url, dict(headers), body))
        return HttpResponse(200, {}, b'{"status":"Endorsed"}')

    result = endorse_release_translation(
        "TEST_API_KEY",
        target,
        client_factory=lambda _key: _FakeModClient(
            {"version": "2.1.0", "allow_rating": True}
        ),
        post_transport=post_transport,
    )

    method, url, headers, body = requests[0]
    assert method == "POST"
    assert url.endswith("/games/skyrimspecialedition/mods/158770/endorse.json")
    assert "/abstain" not in url
    assert parse_qs(body.decode("utf-8")) == {"Version": ["2.1.0"]}
    assert headers["apikey"] == "TEST_API_KEY"
    assert headers["Application-Name"] == "modlist-translation-wizard"
    assert result.already_endorsed is False
    assert result.mod_version == "2.1.0"


def test_endorse_release_uses_manifest_version_without_metadata_request() -> None:
    target = ReleaseEndorsementTarget(
        "skyrimspecialedition",
        158770,
        "LoreRim",
        mod_version="2.1.0",
    )
    requests: list[bytes] = []

    result = endorse_release_translation(
        "TEST_API_KEY",
        target,
        client_factory=lambda _key: pytest.fail("metadata çağrısı yapılmamalı"),
        post_transport=lambda _method, _url, _headers, body: (
            requests.append(body) or HttpResponse(200, {}, b'{"status":"Endorsed"}')
        ),
    )

    assert parse_qs(requests[0].decode("utf-8")) == {"Version": ["2.1.0"]}
    assert result.mod_version == "2.1.0"


def test_endorse_release_treats_already_endorsed_as_success() -> None:
    target = ReleaseEndorsementTarget("skyrimspecialedition", 158770, "LoreRim")

    result = endorse_release_translation(
        "TEST_API_KEY",
        target,
        client_factory=lambda _key: _FakeModClient({"version": "2.1.0"}),
        post_transport=lambda *_args: HttpResponse(
            403,
            {},
            b'{"message":"You have already endorsed this mod."}',
        ),
    )

    assert result.already_endorsed is True


def test_endorse_release_reports_nexus_download_wait_rule() -> None:
    target = ReleaseEndorsementTarget("skyrimspecialedition", 158770, "LoreRim")

    with pytest.raises(NexusEndorsementError, match="en az 15 dakika"):
        endorse_release_translation(
            "TEST_API_KEY",
            target,
            client_factory=lambda _key: _FakeModClient({"version": "2.1.0"}),
            post_transport=lambda *_args: HttpResponse(
                403,
                {},
                b'{"message":"You must have downloaded this file at least 15 minutes ago."}',
            ),
        )


def test_endorse_release_rejects_pages_with_endorsements_disabled() -> None:
    target = ReleaseEndorsementTarget("skyrimspecialedition", 158770, "LoreRim")

    with pytest.raises(NexusEndorsementError, match="devre dışı"):
        endorse_release_translation(
            "TEST_API_KEY",
            target,
            client_factory=lambda _key: _FakeModClient(
                {"version": "2.1.0", "allow_rating": False}
            ),
            post_transport=lambda *_args: pytest.fail("POST yapılmamalı"),
        )


def test_collect_manifest_endorsement_targets_deduplicates_translation_pages() -> None:
    manifest = {
        "entries": [
            {
                "artifacts": [
                    {
                        "game_domain": "SkyrimSpecialEdition",
                        "translation_nexus_mod_id": 10,
                        "translation_file_id": 100,
                        "translation_name": "Example TR",
                        "translation_version": "1.2.0",
                        "install_mode": "DSD_CONVERT",
                    },
                    {
                        "game_domain": "skyrimspecialedition",
                        "translation_nexus_mod_id": 10,
                        "translation_file_id": 101,
                        "translation_name": "Duplicate page",
                        "install_mode": "NATIVE_INSTALL",
                    },
                    {
                        "game_domain": "skyrimspecialedition",
                        "translation_nexus_mod_id": 11,
                        "translation_file_id": 110,
                        "translation_file_name": "other.7z",
                        "install_mode": "BUNDLE_DSD",
                    },
                ]
            }
        ],
        "add_on_packages": [
            {
                "game_domain": "skyrimspecialedition",
                "translation_nexus_mod_id": 12,
                "translation_file_id": 120,
                "display_name": "Add-on",
            }
        ],
    }

    targets = collect_manifest_endorsement_targets(
        manifest,
        extra_targets=[ReleaseEndorsementTarget("skyrimspecialedition", 99, "Release")],
    )

    assert targets == (
        ReleaseEndorsementTarget("skyrimspecialedition", 99, "Release"),
        ReleaseEndorsementTarget(
            "skyrimspecialedition", 10, "Example TR", mod_version="1.2.0"
        ),
        ReleaseEndorsementTarget("skyrimspecialedition", 12, "Add-on"),
    )


def test_bulk_endorsement_keeps_15_minute_wait_as_retryable() -> None:
    targets = (
        ReleaseEndorsementTarget("skyrimspecialedition", 1, "Ready"),
        ReleaseEndorsementTarget("skyrimspecialedition", 2, "Waiting"),
        ReleaseEndorsementTarget("skyrimspecialedition", 3, "Already"),
    )
    progress: list[tuple[int, int, int, str]] = []

    def post_transport(_method, url, _headers, _body):
        if url.endswith("/mods/2/endorse.json"):
            return HttpResponse(
                403,
                {},
                b'{"message":"You must have downloaded this file at least 15 minutes ago."}',
            )
        if url.endswith("/mods/3/endorse.json"):
            return HttpResponse(
                403,
                {},
                b'{"message":"You have already endorsed this mod."}',
            )
        return HttpResponse(200, {}, b'{"status":"Endorsed"}')

    result = endorse_manifest_targets(
        "TEST_API_KEY",
        targets,
        delay_seconds=0,
        client_factory=lambda _key: _BulkFakeModClient(),
        post_transport=post_transport,
        progress_callback=lambda done, total, target, status, _message: progress.append(
            (done, total, target.mod_id, status)
        ),
    )

    assert result.total == 3
    assert result.endorsed == 1
    assert result.wait_required == 1
    assert result.already_endorsed == 1
    assert result.completed == 2
    assert wait_required_endorsement_targets(result) == (targets[1],)
    untouched = ReleaseEndorsementTarget("skyrimspecialedition", 4, "Untouched")
    assert merge_remaining_endorsement_targets(
        targets + (untouched,),
        targets,
        result,
    ) == (targets[1], untouched)
    assert progress == [
        (1, 3, 1, "endorsed"),
        (2, 3, 2, "wait_required"),
        (3, 3, 3, "already_endorsed"),
    ]


def test_bulk_endorsement_stops_on_rate_limit_and_keeps_remaining_targets() -> None:
    targets = (
        ReleaseEndorsementTarget("skyrimspecialedition", 1, "Ready", "1.0"),
        ReleaseEndorsementTarget("skyrimspecialedition", 2, "Limited", "1.0"),
        ReleaseEndorsementTarget("skyrimspecialedition", 3, "Later", "1.0"),
    )

    def post_transport(_method, url, _headers, _body):
        if url.endswith("/mods/2/endorse.json"):
            return HttpResponse(429, {}, b'{"message":"Rate limit exceeded"}')
        return HttpResponse(200, {}, b'{"status":"Endorsed"}')

    result = endorse_manifest_targets(
        "TEST_API_KEY",
        targets,
        delay_seconds=0,
        client_factory=lambda _key: pytest.fail("metadata çağrısı yapılmamalı"),
        post_transport=post_transport,
    )

    assert result.endorsed == 1
    assert result.rate_limited == 1
    assert result.attempted == 2
    assert result.not_attempted == 1
    assert remaining_endorsement_targets(targets, result) == targets[1:]


class _FakeModClient:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def get_mod(self, game_domain: str, mod_id: int) -> NexusApiResponse:
        assert game_domain == "skyrimspecialedition"
        assert mod_id == 158770
        return NexusApiResponse(payload=self.payload)


class _BulkFakeModClient:
    def get_mod(self, game_domain: str, mod_id: int) -> NexusApiResponse:
        assert game_domain == "skyrimspecialedition"
        assert mod_id in {1, 2, 3}
        return NexusApiResponse(payload={"version": "1.0.0", "allow_rating": True})
