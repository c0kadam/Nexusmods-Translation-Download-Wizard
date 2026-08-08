"""User-initiated Nexus endorsement support."""

from __future__ import annotations

import json
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Callable, Iterable
from urllib.parse import urlencode

from modlist_translate_tool.nexus.api_client import (
    DEFAULT_NEXUS_API_BASE_URL,
    HttpResponse,
    NexusApiClient,
    NexusApiError,
    NexusPostTransport,
    urllib_post_transport,
)

from modlist_translation_wizard.version import TOOL_NAME, __version__


_GAME_DOMAIN_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{1,63}")
_WRAPPING_QUOTES = "\"'\u201c\u201d\u2018\u2019"


@dataclass(frozen=True, slots=True)
class ReleaseEndorsementTarget:
    game_domain: str
    mod_id: int
    label: str
    mod_version: str | None = None

    @property
    def page_url(self) -> str:
        return f"https://www.nexusmods.com/{self.game_domain}/mods/{self.mod_id}"


@dataclass(frozen=True, slots=True)
class NexusEndorsementResult:
    target: ReleaseEndorsementTarget
    mod_version: str
    already_endorsed: bool
    message: str


@dataclass(frozen=True, slots=True)
class BulkEndorsementEntry:
    target: ReleaseEndorsementTarget
    status: str
    message: str


@dataclass(frozen=True, slots=True)
class BulkEndorsementSummary:
    entries: tuple[BulkEndorsementEntry, ...]
    total: int
    endorsed: int
    already_endorsed: int
    wait_required: int
    disabled: int
    own_file: int
    abstained: int
    rate_limited: int
    unauthorized: int
    transient_error: int
    failed: int

    @property
    def completed(self) -> int:
        return self.endorsed + self.already_endorsed

    @property
    def retryable(self) -> int:
        return (
            self.wait_required
            + self.rate_limited
            + self.transient_error
            + self.not_attempted
        )

    @property
    def attempted(self) -> int:
        return len(self.entries)

    @property
    def not_attempted(self) -> int:
        return max(0, self.total - self.attempted)


class NexusEndorsementError(RuntimeError):
    """Raised when an explicit endorsement request cannot be completed."""


def release_endorsement_target(
    payload: object,
    *,
    fallback_label: str,
) -> ReleaseEndorsementTarget | None:
    if not isinstance(payload, dict) or payload.get("enabled") is False:
        return None
    game_domain = str(payload.get("game_domain") or "").strip().casefold()
    if not _GAME_DOMAIN_PATTERN.fullmatch(game_domain):
        return None
    mod_id = _positive_int(payload.get("mod_id"))
    if mod_id is None:
        return None
    label = str(payload.get("label") or fallback_label).strip() or fallback_label
    return ReleaseEndorsementTarget(
        game_domain=game_domain,
        mod_id=mod_id,
        label=label,
        mod_version=_clean_version(payload.get("mod_version") or payload.get("version")),
    )


def endorse_release_translation(
    api_key: str,
    target: ReleaseEndorsementTarget,
    *,
    client_factory: Callable[[str], NexusApiClient] | None = None,
    post_transport: NexusPostTransport = urllib_post_transport,
) -> NexusEndorsementResult:
    key = str(api_key or "").strip().strip(_WRAPPING_QUOTES)
    if not key or any(character.isspace() for character in key):
        raise NexusEndorsementError(
            "Endorse işlemi için geçerli bir Nexus API anahtarı gerekli."
        )

    mod_version = _clean_version(target.mod_version)
    if mod_version is None:
        factory = client_factory or (lambda value: NexusApiClient(value))
        try:
            mod_response = factory(key).get_mod(target.game_domain, target.mod_id)
        except NexusApiError as exc:
            raise NexusEndorsementError(
                _endorsement_error_message(exc.status_code or 0, str(exc))
            ) from exc
        mod_payload = mod_response.payload
        if not isinstance(mod_payload, dict):
            raise NexusEndorsementError("Nexus mod bilgisi beklenmeyen biçimde döndü.")
        if mod_payload.get("allow_rating") is False:
            raise NexusEndorsementError("Bu Nexus mod sayfasında endorsement devre dışı.")
        mod_version = _clean_version(mod_payload.get("version"))
        if mod_version is None:
            raise NexusEndorsementError(
                "Nexus mod sürümü okunamadığı için endorse gönderilemedi."
            )

    url = (
        f"{DEFAULT_NEXUS_API_BASE_URL}/games/{target.game_domain}/mods/"
        f"{target.mod_id}/endorse.json"
    )
    headers = {
        "apikey": key,
        "accept": "application/json",
        "content-type": "application/x-www-form-urlencoded",
        "User-Agent": f"{TOOL_NAME}/{__version__}",
        "Application-Name": TOOL_NAME,
        "Application-Version": __version__,
    }
    try:
        response = post_transport(
            "POST",
            url,
            headers,
            urlencode({"Version": mod_version}).encode("utf-8"),
        )
    except NexusApiError as exc:
        raise NexusEndorsementError(
            _endorsement_error_message(exc.status_code or 0, str(exc))
        ) from exc
    response_message = _response_message(response)
    if _already_endorsed(response_message):
        return NexusEndorsementResult(
            target=target,
            mod_version=mod_version,
            already_endorsed=True,
            message="Bu çeviri daha önce endorse edilmiş.",
        )
    if response.status_code < 200 or response.status_code >= 300:
        raise NexusEndorsementError(
            _endorsement_error_message(response.status_code, response_message)
        )
    return NexusEndorsementResult(
        target=target,
        mod_version=mod_version,
        already_endorsed=False,
        message="Çeviri Nexus Mods üzerinde endorse edildi.",
    )


def collect_manifest_endorsement_targets(
    manifest: dict[str, object],
    *,
    extra_targets: Iterable[ReleaseEndorsementTarget] = (),
) -> tuple[ReleaseEndorsementTarget, ...]:
    targets: list[ReleaseEndorsementTarget] = []
    indexes: dict[tuple[str, int], int] = {}

    def add(
        game_domain: object,
        mod_id: object,
        label: object,
        mod_version: object = None,
    ) -> None:
        normalized_game = str(game_domain or "skyrimspecialedition").strip().casefold()
        if not _GAME_DOMAIN_PATTERN.fullmatch(normalized_game):
            return
        normalized_mod_id = _positive_int(mod_id)
        if normalized_mod_id is None:
            return
        identity = (normalized_game, normalized_mod_id)
        normalized_version = _clean_version(mod_version)
        existing_index = indexes.get(identity)
        if existing_index is not None:
            existing = targets[existing_index]
            if existing.mod_version is None and normalized_version is not None:
                targets[existing_index] = ReleaseEndorsementTarget(
                    game_domain=existing.game_domain,
                    mod_id=existing.mod_id,
                    label=existing.label,
                    mod_version=normalized_version,
                )
            return
        indexes[identity] = len(targets)
        label_text = str(label or "").strip() or f"Nexus mod {normalized_mod_id}"
        targets.append(
            ReleaseEndorsementTarget(
                game_domain=normalized_game,
                mod_id=normalized_mod_id,
                label=label_text,
                mod_version=normalized_version,
            )
        )

    for target in extra_targets:
        add(target.game_domain, target.mod_id, target.label, target.mod_version)

    for entry in _as_dict_list(manifest.get("entries")):
        for artifact in _as_dict_list(entry.get("artifacts")):
            if str(artifact.get("install_mode") or "").upper() == "BUNDLE_DSD":
                continue
            if _positive_int(artifact.get("translation_file_id")) is None:
                continue
            add(
                artifact.get("game_domain"),
                artifact.get("translation_nexus_mod_id"),
                artifact.get("translation_name")
                or artifact.get("translation_file_name")
                or artifact.get("source_url"),
                artifact.get("translation_version"),
            )

    for package in _as_dict_list(manifest.get("add_on_packages")):
        if _positive_int(package.get("translation_file_id")) is None:
            continue
        add(
            package.get("game_domain"),
            package.get("translation_nexus_mod_id"),
            package.get("display_name") or package.get("translation_file_name"),
            package.get("translation_version"),
        )

    for asset in _as_dict_list(manifest.get("native_binary_assets")):
        if _positive_int(asset.get("translation_file_id")) is None:
            continue
        add(
            asset.get("game_domain"),
            asset.get("translation_nexus_mod_id"),
            asset.get("display_name") or asset.get("translation_file_name"),
            asset.get("translation_version"),
        )

    return tuple(targets)


def endorse_manifest_targets(
    api_key: str,
    targets: Iterable[ReleaseEndorsementTarget],
    *,
    delay_seconds: float = 0.35,
    client_factory: Callable[[str], NexusApiClient] | None = None,
    post_transport: NexusPostTransport = urllib_post_transport,
    progress_callback: Callable[
        [int, int, ReleaseEndorsementTarget, str, str], None
    ]
    | None = None,
) -> BulkEndorsementSummary:
    ordered_targets = _deduplicate_targets(targets)
    entries: list[BulkEndorsementEntry] = []
    total = len(ordered_targets)
    shared_client: NexusApiClient | None = None

    def shared_client_factory(value: str) -> NexusApiClient:
        nonlocal shared_client
        if shared_client is None:
            factory = client_factory or (lambda api_key: NexusApiClient(api_key))
            shared_client = factory(value)
        return shared_client

    for index, target in enumerate(ordered_targets, start=1):
        try:
            result = endorse_release_translation(
                api_key,
                target,
                client_factory=shared_client_factory,
                post_transport=post_transport,
            )
        except NexusEndorsementError as exc:
            status = _classify_endorsement_error(str(exc))
            message = str(exc)
        except Exception as exc:  # noqa: BLE001 - bulk operation returns visible status.
            status = "failed"
            message = str(exc) or "Beklenmeyen endorse hatası."
        else:
            status = "already_endorsed" if result.already_endorsed else "endorsed"
            message = result.message

        entries.append(BulkEndorsementEntry(target, status, message))
        if progress_callback is not None:
            progress_callback(index, total, target, status, message)

        if status in {"rate_limited", "unauthorized", "transient_error"}:
            break
        if delay_seconds > 0 and index < total:
            time.sleep(delay_seconds)

    return _bulk_summary(entries, total)


def remaining_endorsement_targets(
    targets: Iterable[ReleaseEndorsementTarget],
    result: BulkEndorsementSummary,
) -> tuple[ReleaseEndorsementTarget, ...]:
    """Return only targets that are useful to retry in the current session."""

    retryable_statuses = {
        "wait_required",
        "rate_limited",
        "unauthorized",
        "transient_error",
        "failed",
    }
    status_by_identity = {
        (entry.target.game_domain.casefold(), entry.target.mod_id): entry.status
        for entry in result.entries
    }
    return tuple(
        target
        for target in _deduplicate_targets(targets)
        if status_by_identity.get(
            (target.game_domain.casefold(), target.mod_id)
        )
        in retryable_statuses
        or (target.game_domain.casefold(), target.mod_id) not in status_by_identity
    )


def wait_required_endorsement_targets(
    result: BulkEndorsementSummary,
) -> tuple[ReleaseEndorsementTarget, ...]:
    """Return only pages blocked by Nexus' post-download waiting period."""

    return _deduplicate_targets(
        entry.target for entry in result.entries if entry.status == "wait_required"
    )


def merge_remaining_endorsement_targets(
    current_targets: Iterable[ReleaseEndorsementTarget],
    attempted_targets: Iterable[ReleaseEndorsementTarget],
    result: BulkEndorsementSummary,
) -> tuple[ReleaseEndorsementTarget, ...]:
    """Merge a partial retry result without discarding untouched targets."""

    attempted = _deduplicate_targets(attempted_targets)
    attempted_identities = {
        (target.game_domain.casefold(), target.mod_id) for target in attempted
    }
    retry_targets = remaining_endorsement_targets(attempted, result)
    retry_by_identity = {
        (target.game_domain.casefold(), target.mod_id): target
        for target in retry_targets
    }
    merged: list[ReleaseEndorsementTarget] = []
    seen: set[tuple[str, int]] = set()
    for target in _deduplicate_targets(current_targets):
        identity = (target.game_domain.casefold(), target.mod_id)
        if identity in attempted_identities:
            replacement = retry_by_identity.pop(identity, None)
            if replacement is None:
                continue
            target = replacement
        if identity in seen:
            continue
        seen.add(identity)
        merged.append(target)
    for identity, target in retry_by_identity.items():
        if identity not in seen:
            seen.add(identity)
            merged.append(target)
    return tuple(merged)


def _response_message(response: HttpResponse) -> str:
    if not response.body:
        return ""
    try:
        payload = json.loads(response.body.decode("utf-8-sig", errors="replace"))
    except json.JSONDecodeError:
        return response.body.decode("utf-8", errors="replace").strip()
    if isinstance(payload, dict):
        return str(payload.get("message") or payload.get("status") or "").strip()
    return str(payload).strip()


def _already_endorsed(message: str) -> bool:
    normalized = _search_text(message)
    return "already endorsed" in normalized or "zaten endorse" in normalized


def _endorsement_error_message(status_code: int, message: str) -> str:
    normalized = _search_text(message)
    if status_code == 401:
        return "Nexus API anahtarı geçersiz veya endorse yetkisine sahip değil."
    if status_code == 429:
        return "Nexus API kotası dolu. Kota yenilendikten sonra tekrar deneyin."
    if "already abstained" in normalized:
        return "Bu mod için daha önce abstain seçilmiş. Bu araç bu tercihi değiştirmez."
    if "download" in normalized or "15 minute" in normalized:
        return (
            "Nexus endorse için dosyanın indirilmiş olmasını ve indirmeden sonra "
            "en az 15 dakika geçmesini istiyor."
        )
    if "own mod" in normalized or "own file" in normalized or "your own" in normalized:
        return "Nexus, kullanıcıların kendi yüklemelerini endorse etmesine izin vermiyor."
    if "disabled" in normalized:
        return "Bu Nexus mod sayfasında endorsement devre dışı."
    if status_code == 403:
        return "Nexus endorse isteğini reddetti (HTTP 403)."
    if status_code <= 0:
        detail = message.strip() or "bağlantı kurulamadı"
        return f"Geçici Nexus bağlantı hatası: {detail}"
    return f"Nexus endorse isteği başarısız oldu (HTTP {status_code})."


def _classify_endorsement_error(message: str) -> str:
    normalized = _search_text(message)
    if "gecersiz" in normalized or "401" in normalized:
        return "unauthorized"
    if "kota" in normalized or "rate" in normalized or "429" in normalized:
        return "rate_limited"
    if any(
        marker in normalized
        for marker in (
            "baglanti hatasi",
            "timed out",
            "zaman asimi",
            "incomplete response",
            "connection",
            "temporary failure",
        )
    ):
        return "transient_error"
    if (
        "15 dakika" in normalized
        or "15 minute" in normalized
        or "download" in normalized
        or "indiril" in normalized
        or "indirilm" in normalized
    ):
        return "wait_required"
    if "devre disi" in normalized or "disabled" in normalized:
        return "disabled"
    if "kendi" in normalized or "own" in normalized:
        return "own_file"
    if "abstain" in normalized:
        return "abstained"
    return "failed"


def _bulk_summary(
    entries: Iterable[BulkEndorsementEntry],
    total: int,
) -> BulkEndorsementSummary:
    entry_tuple = tuple(entries)

    def count(status: str) -> int:
        return sum(1 for entry in entry_tuple if entry.status == status)

    return BulkEndorsementSummary(
        entries=entry_tuple,
        total=total,
        endorsed=count("endorsed"),
        already_endorsed=count("already_endorsed"),
        wait_required=count("wait_required"),
        disabled=count("disabled"),
        own_file=count("own_file"),
        abstained=count("abstained"),
        rate_limited=count("rate_limited"),
        unauthorized=count("unauthorized"),
        transient_error=count("transient_error"),
        failed=count("failed"),
    )


def _deduplicate_targets(
    targets: Iterable[ReleaseEndorsementTarget],
) -> tuple[ReleaseEndorsementTarget, ...]:
    ordered: list[ReleaseEndorsementTarget] = []
    indexes: dict[tuple[str, int], int] = {}
    for target in targets:
        identity = (target.game_domain.casefold(), target.mod_id)
        existing_index = indexes.get(identity)
        if existing_index is None:
            indexes[identity] = len(ordered)
            ordered.append(target)
            continue
        existing = ordered[existing_index]
        if existing.mod_version is None and target.mod_version is not None:
            ordered[existing_index] = ReleaseEndorsementTarget(
                game_domain=existing.game_domain,
                mod_id=existing.mod_id,
                label=existing.label,
                mod_version=target.mod_version,
            )
    return tuple(ordered)


def _as_dict_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _positive_int(value: object) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _clean_version(value: object) -> str | None:
    version = str(value or "").strip()
    if (
        not version
        or len(version) > 128
        or any(ord(character) < 32 for character in version)
    ):
        return None
    return version


def _search_text(value: object) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return "".join(
        character
        for character in normalized
        if not unicodedata.combining(character)
    )
