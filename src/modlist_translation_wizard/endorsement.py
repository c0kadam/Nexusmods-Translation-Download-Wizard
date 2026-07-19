"""User-initiated Nexus endorsement support for a configured release page."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable
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

    @property
    def page_url(self) -> str:
        return f"https://www.nexusmods.com/{self.game_domain}/mods/{self.mod_id}"


@dataclass(frozen=True, slots=True)
class NexusEndorsementResult:
    target: ReleaseEndorsementTarget
    mod_version: str
    already_endorsed: bool
    message: str


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
    try:
        mod_id = int(payload.get("mod_id"))
    except (TypeError, ValueError):
        return None
    if mod_id <= 0:
        return None
    label = str(payload.get("label") or fallback_label).strip() or fallback_label
    return ReleaseEndorsementTarget(
        game_domain=game_domain,
        mod_id=mod_id,
        label=label,
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
        raise NexusEndorsementError("Endorse işlemi için geçerli bir Nexus API anahtarı gerekli.")

    factory = client_factory or (lambda value: NexusApiClient(value))
    try:
        mod_response = factory(key).get_mod(target.game_domain, target.mod_id)
    except NexusApiError as exc:
        raise NexusEndorsementError(
            _endorsement_error_message(exc.status_code or 0, "")
        ) from exc
    mod_payload = mod_response.payload
    if not isinstance(mod_payload, dict):
        raise NexusEndorsementError("Nexus mod bilgisi beklenmeyen biçimde döndü.")
    if mod_payload.get("allow_rating") is False:
        raise NexusEndorsementError("Bu Nexus mod sayfasında endorsement devre dışı.")
    mod_version = str(mod_payload.get("version") or "").strip()
    if not mod_version:
        raise NexusEndorsementError("Nexus mod sürümü okunamadığı için endorse gönderilemedi.")

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
    response = post_transport(
        "POST",
        url,
        headers,
        urlencode({"Version": mod_version}).encode("utf-8"),
    )
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
    normalized = message.casefold()
    return "already endorsed" in normalized or "zaten endorse" in normalized


def _endorsement_error_message(status_code: int, message: str) -> str:
    normalized = message.casefold()
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
        return (
            "Nexus endorse isteğini reddetti. Dosyanın indirildiğini ve indirmeden sonra "
            "en az 15 dakika geçtiğini kontrol edin."
        )
    return f"Nexus endorse isteği başarısız oldu (HTTP {status_code})."
