from urllib.parse import parse_qs

import pytest

from modlist_translate_tool.nexus.api_client import HttpResponse, NexusApiResponse
from modlist_translation_wizard.endorsement import (
    NexusEndorsementError,
    ReleaseEndorsementTarget,
    endorse_release_translation,
    release_endorsement_target,
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


class _FakeModClient:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def get_mod(self, game_domain: str, mod_id: int) -> NexusApiResponse:
        assert game_domain == "skyrimspecialedition"
        assert mod_id == 158770
        return NexusApiResponse(payload=self.payload)
