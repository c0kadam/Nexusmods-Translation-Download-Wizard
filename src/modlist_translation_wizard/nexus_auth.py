"""Nexus authentication helpers for manifest-driven wizard installs."""

from __future__ import annotations

import os
import uuid
import webbrowser
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Callable, Iterator
from urllib.parse import urlencode

from modlist_translation_wizard.credential_store import CredentialStore
from modlist_translation_wizard.version import TOOL_NAME, __version__

NEXUS_SSO_URL = "wss://sso.nexusmods.com"
NEXUS_SSO_AUTHORIZE_URL = "https://www.nexusmods.com/sso"
NEXUS_API_SETTINGS_URL = "https://www.nexusmods.com/settings/api-keys"
DEFAULT_NEXUS_API_KEY_ENV = "NEXUS_API_KEY"


class NexusAuthError(ValueError):
    """Raised when Nexus authentication setup is invalid."""


@dataclass(frozen=True, slots=True)
class NexusSsoHandshake:
    session_id: str
    app_id: str
    websocket_url: str
    authorize_url: str
    initial_payload: dict[str, str]
    ping_interval_seconds: int = 30

    def safe_payload(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class NexusAuthStatus:
    target: str
    mode: str
    has_api_key: bool
    source: str | None = None
    env_var_name: str = DEFAULT_NEXUS_API_KEY_ENV
    settings_url: str = NEXUS_API_SETTINGS_URL
    warnings: list[str] = field(default_factory=list)

    def safe_payload(self) -> dict[str, object]:
        return asdict(self)


def auth_target_for_app(app_id: str | None) -> str:
    safe_app_id = _required_app_id(app_id)
    return f"{TOOL_NAME}/nexusmods/{safe_app_id}/api-key"


def registered_app_id_from_manifest(manifest: dict[str, object]) -> str | None:
    nexus = manifest.get("nexus") if isinstance(manifest.get("nexus"), dict) else {}
    auth = nexus.get("authentication") if isinstance(nexus.get("authentication"), dict) else {}
    value = auth.get("registered_app_slug")
    text = str(value or "").strip()
    return text or None


def create_sso_handshake(
    *,
    app_id: str,
    session_id: str | None = None,
    websocket_url: str = NEXUS_SSO_URL,
    authorize_url: str = NEXUS_SSO_AUTHORIZE_URL,
) -> NexusSsoHandshake:
    safe_app_id = _required_app_id(app_id)
    safe_session_id = _session_id(session_id)
    url = f"{authorize_url}?{urlencode({'id': safe_session_id})}"
    return NexusSsoHandshake(
        session_id=safe_session_id,
        app_id=safe_app_id,
        websocket_url=websocket_url,
        authorize_url=url,
        initial_payload={"id": safe_session_id, "appid": safe_app_id},
    )


def open_sso_authorization_page(
    handshake: NexusSsoHandshake,
    opener: Callable[[str], object] | None = None,
) -> None:
    open_url = opener or webbrowser.open
    open_url(handshake.authorize_url)


def api_key_status(
    store: CredentialStore,
    *,
    app_id: str,
    env_var_name: str = DEFAULT_NEXUS_API_KEY_ENV,
) -> NexusAuthStatus:
    target = auth_target_for_app(app_id)
    warnings: list[str] = []
    has_stored_key = bool(store.read_secret(target))
    has_env_key = bool(os.environ.get(env_var_name))
    if has_stored_key:
        return NexusAuthStatus(
            target=target,
            mode="credential_store",
            has_api_key=True,
            source="credential_store",
            env_var_name=env_var_name,
            warnings=warnings,
        )
    if has_env_key:
        warnings.append("Nexus API key is present in environment; it was not read into outputs.")
        return NexusAuthStatus(
            target=target,
            mode="environment",
            has_api_key=True,
            source="environment",
            env_var_name=env_var_name,
            warnings=warnings,
        )
    warnings.append("No Nexus API key is stored for this registered app.")
    return NexusAuthStatus(
        target=target,
        mode="missing",
        has_api_key=False,
        env_var_name=env_var_name,
        warnings=warnings,
    )


def store_manual_api_key(store: CredentialStore, *, app_id: str, api_key: str) -> NexusAuthStatus:
    key = _required_api_key(api_key)
    target = auth_target_for_app(app_id)
    store.write_secret(target, key)
    return NexusAuthStatus(
        target=target,
        mode="credential_store",
        has_api_key=True,
        source="manual_api_key",
    )


def store_sso_api_key(store: CredentialStore, *, app_id: str, api_key: str) -> NexusAuthStatus:
    key = _required_api_key(api_key)
    target = auth_target_for_app(app_id)
    store.write_secret(target, key)
    return NexusAuthStatus(
        target=target,
        mode="credential_store",
        has_api_key=True,
        source="registered_application_sso",
    )


def load_api_key(store: CredentialStore, *, app_id: str) -> str | None:
    return store.read_secret(auth_target_for_app(app_id))


def clear_api_key(store: CredentialStore, *, app_id: str) -> NexusAuthStatus:
    target = auth_target_for_app(app_id)
    store.delete_secret(target)
    return NexusAuthStatus(target=target, mode="missing", has_api_key=False)


@contextmanager
def temporary_nexus_api_key_env(
    api_key: str | None,
    *,
    env_var_name: str = DEFAULT_NEXUS_API_KEY_ENV,
) -> Iterator[None]:
    if api_key is None:
        yield
        return
    key = _required_api_key(api_key)
    previous = os.environ.get(env_var_name)
    had_previous = env_var_name in os.environ
    os.environ[env_var_name] = key
    try:
        yield
    finally:
        if had_previous:
            assert previous is not None
            os.environ[env_var_name] = previous
        else:
            os.environ.pop(env_var_name, None)


def auth_report_payload(status: NexusAuthStatus) -> dict[str, object]:
    payload = status.safe_payload()
    payload["tool"] = {"name": TOOL_NAME, "version": __version__}
    payload["secrets_written_to_output"] = False
    return payload


def _required_app_id(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        raise NexusAuthError("A registered Nexus app id is required.")
    if any(character.isspace() for character in text):
        raise NexusAuthError("Nexus app id must not contain whitespace.")
    return text


def _required_api_key(value: str | None) -> str:
    text = str(value or "").strip().strip("\"'\u201c\u201d\u2018\u2019")
    if not text:
        raise NexusAuthError("Nexus API key is required.")
    if any(character.isspace() for character in text):
        raise NexusAuthError("Nexus API key must not contain whitespace.")
    return text


def _session_id(value: str | None) -> str:
    if value is None:
        return str(uuid.uuid4())
    try:
        return str(uuid.UUID(str(value)))
    except ValueError as exc:
        raise NexusAuthError("SSO session id must be a UUID.") from exc
