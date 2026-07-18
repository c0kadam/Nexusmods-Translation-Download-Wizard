"""Small view-model helpers for the desktop wizard."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from math import ceil
from pathlib import Path
from typing import Any
from importlib.resources import files

from modlist_translation_wizard.bundled import external_release_dirs
from modlist_translation_wizard.version import TOOL_NAME

DEFAULT_BUNDLED_MANIFEST_LIST_ID = "lorerim"
DEFAULT_BUNDLED_MANIFEST_NAME = "manifest.json"
NEXUS_API_KEYS_URL = "https://www.nexusmods.com/settings/api-keys"
PREMIUM_DELIVERY_LABEL = "Premium"
NON_PREMIUM_DELIVERY_LABEL = "Ücretsiz / Tarayıcı"
PREPARE_TRANSLATION_LABEL = "Çeviriyi hazırla"


@dataclass(frozen=True, slots=True)
class ReleaseBranding:
    display_name: str
    subtitle: str
    accent_color: str
    font_color: str = "#FFFFFF"
    font_shadow: str = "#28150C"
    warm_glow: str = "#8A5030"
    banner: str | None = None
    icon: str | None = None


@dataclass(frozen=True, slots=True)
class InstallerButtonState:
    can_download: bool
    can_prepare: bool
    download_label: str
    prepare_label: str
    download_hint: str
    prepare_hint: str
    staging_only: bool


def default_workspace_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "Modlist Translation Wizard"
    return Path.home() / "AppData" / "Local" / "Modlist Translation Wizard"


def run_workspace_for_manifest(manifest: dict[str, Any], workspace_root: Path | str | None = None) -> Path:
    root = Path(workspace_root) if workspace_root is not None else default_workspace_root()
    modlist = manifest.get("modlist") if isinstance(manifest.get("modlist"), dict) else {}
    manifest_id = _safe_component(manifest.get("manifest_id")) or "manifest"
    list_id = _safe_component(modlist.get("id")) or "modlist"
    return root / "runs" / list_id / manifest_id


def discover_mo2_profiles(mo2_root: Path | str) -> list[str]:
    profiles_dir = Path(mo2_root) / "profiles"
    if not profiles_dir.is_dir():
        return []
    profiles = [
        path.name
        for path in profiles_dir.iterdir()
        if path.is_dir() and (path / "modlist.txt").exists()
    ]
    return sorted(profiles, key=str.casefold)


def manifest_summary(manifest: dict[str, Any]) -> dict[str, str]:
    modlist = manifest.get("modlist") if isinstance(manifest.get("modlist"), dict) else {}
    output = manifest.get("output") if isinstance(manifest.get("output"), dict) else {}
    summary = manifest.get("summary") if isinstance(manifest.get("summary"), dict) else {}
    nexus = manifest.get("nexus") if isinstance(manifest.get("nexus"), dict) else {}
    auth = nexus.get("authentication") if isinstance(nexus.get("authentication"), dict) else {}
    app_id = str(auth.get("registered_app_slug") or TOOL_NAME)
    add_on_count = len(
        [
            item
            for item in manifest.get("add_on_packages", [])
            if isinstance(item, dict) and item.get("enabled") is not False
        ]
    )
    unique_download_count = int(summary.get("unique_download_count") or 0) + add_on_count
    return {
        "manifest_id": str(manifest.get("manifest_id") or ""),
        "modlist_name": str(modlist.get("name") or "Unknown modlist"),
        "modlist_version": str(modlist.get("version") or "Unknown version"),
        "supported_profiles": ", ".join(str(item) for item in modlist.get("supported_profiles", [])),
        "language": str(manifest.get("language") or ""),
        "channel": str(manifest.get("channel") or ""),
        "release_state": str(manifest.get("release_state") or ""),
        "manifest_updated_at": _manifest_updated_at_display(manifest),
        "entry_count": str(summary.get("entry_count") or 0),
        "unique_download_count": str(unique_download_count),
        "base_download_count": str(summary.get("unique_download_count") or 0),
        "add_on_package_count": str(add_on_count),
        "output_mod_name": str(output.get("mod_name") or ""),
        "registered_app_id": app_id,
    }


def _manifest_updated_at_display(manifest: dict[str, Any]) -> str:
    value = str(manifest.get("updated_at") or manifest.get("created_at") or "").strip()
    if not value:
        return "bilinmiyor"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def load_release_branding(manifest: dict[str, Any]) -> ReleaseBranding:
    modlist = manifest.get("modlist") if isinstance(manifest.get("modlist"), dict) else {}
    list_id = _safe_component(modlist.get("id")) or DEFAULT_BUNDLED_MANIFEST_LIST_ID
    fallback_name = str(modlist.get("name") or "Modlist Türkçe Çeviri Paketi")
    fallback_subtitle = f"{fallback_name} için hazırlanmış kurulum aracı"
    for release_dir in external_release_dirs(list_id):
        branding_json = release_dir / "branding.json"
        if branding_json.is_file():
            return _release_branding_from_json(
                branding_json,
                fallback_name=fallback_name,
                fallback_subtitle=fallback_subtitle,
            )
    package_root = files("modlist_translation_wizard")
    branding_root = package_root.joinpath("resources", "releases", list_id)
    branding_json = branding_root.joinpath("branding.json")
    if not branding_json.is_file():
        branding_root = package_root.joinpath("resources", "branding", list_id)
        branding_json = branding_root.joinpath("branding.json")
        if not branding_json.is_file():
            return ReleaseBranding(
                display_name=f"{fallback_name} Türkçe Çeviri Paketi",
                subtitle=fallback_subtitle,
                accent_color="#7c8f67",
            )
    return _release_branding_from_json(
        branding_json,
        fallback_name=fallback_name,
        fallback_subtitle=fallback_subtitle,
    )


def _release_branding_from_json(
    path: Any,
    *,
    fallback_name: str,
    fallback_subtitle: str,
) -> ReleaseBranding:
    payload = json.loads(path.read_text(encoding="utf-8"))
    banner = str(payload.get("banner") or "").strip() or None
    icon = str(payload.get("icon") or "").strip() or None
    return ReleaseBranding(
        display_name=str(payload.get("display_name") or fallback_name),
        subtitle=str(payload.get("subtitle") or fallback_subtitle),
        accent_color=_branding_color(payload.get("accent_color"), "#7C8F67"),
        font_color=_branding_color(payload.get("font_color"), "#FFFFFF"),
        font_shadow=_branding_color(payload.get("font_shadow"), "#28150C"),
        warm_glow=_branding_color(payload.get("warm_glow"), "#8A5030"),
        banner=banner,
        icon=icon,
    )


def _branding_color(value: object, fallback: str) -> str:
    color = str(value or "").strip()
    if re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
        return color.upper()
    return fallback


def release_branding_asset_bytes(
    manifest: dict[str, Any],
    asset_name: str | None,
) -> bytes | None:
    modlist = manifest.get("modlist") if isinstance(manifest.get("modlist"), dict) else {}
    list_id = _safe_component(modlist.get("id")) or DEFAULT_BUNDLED_MANIFEST_LIST_ID
    package_root = files("modlist_translation_wizard")
    roots = (
        *external_release_dirs(list_id),
        package_root.joinpath("resources", "releases", list_id),
        package_root.joinpath("resources", "branding", list_id),
    )
    names: list[str] = []
    if asset_name:
        names.append(Path(str(asset_name)).name)
    names.extend((f"{list_id}.png", "banner.png", "banner.jpg", "banner.jpeg", "banner.gif"))
    seen: set[str] = set()
    for name in names:
        if not name or name in seen:
            continue
        seen.add(name)
        for root in roots:
            asset = root.joinpath(name)
            if asset.is_file():
                return asset.read_bytes()
    return None


def release_branding_asset_path(
    manifest: dict[str, Any],
    asset_name: str | None,
) -> Path | None:
    """Return a filesystem path for externally replaceable release assets."""

    name = Path(str(asset_name or "")).name
    if not name:
        return None
    modlist = manifest.get("modlist") if isinstance(manifest.get("modlist"), dict) else {}
    list_id = _safe_component(modlist.get("id")) or DEFAULT_BUNDLED_MANIFEST_LIST_ID
    for root in external_release_dirs(list_id):
        asset = root / name
        if asset.is_file():
            return asset
    return None


def preflight_summary(preflight: dict[str, Any]) -> dict[str, str]:
    summary = preflight.get("summary") if isinstance(preflight.get("summary"), dict) else {}
    profile = preflight.get("profile") if isinstance(preflight.get("profile"), dict) else {}
    return {
        "status": str(preflight.get("status") or "UNKNOWN"),
        "profile_name": str(profile.get("name") or ""),
        "exact_match": "Yes" if profile.get("exact_match") else "No",
        "manifest_entries": str(summary.get("manifest_entries") or 0),
        "matched_entries": str(summary.get("matched_entries") or 0),
        "missing_entries": str(summary.get("missing_entries") or 0),
        "compatibility_warnings": str(summary.get("compatibility_warning_count") or 0),
    }


def delivery_mode_options() -> tuple[str, str]:
    return (PREMIUM_DELIVERY_LABEL, NON_PREMIUM_DELIVERY_LABEL)


def delivery_mode_value(label: str) -> str:
    return "NON_PREMIUM_NXM" if label == NON_PREMIUM_DELIVERY_LABEL else "PREMIUM_API"


def api_settings_url() -> str:
    return NEXUS_API_KEYS_URL


def api_key_notice(*, has_api_key: bool, delivery_mode: str) -> tuple[str, str]:
    if has_api_key:
        return "API anahtarı hazır.", "success"
    if delivery_mode == "PREMIUM_API":
        return "Premium indirme için Nexus API anahtarı gerekli.", "warning"
    return "Ücretsiz / Tarayıcı indirmesinde API anahtarı gerekmez.", "muted"


def visible_auth_controls() -> dict[str, bool]:
    return {
        "api_key": True,
        "api_key_clear": True,
        "api_key_settings_link": True,
        "sso": False,
        "registered_app_id": False,
    }


def estimated_remaining_seconds(
    *,
    elapsed_seconds: float,
    completed: int,
    total: int,
    min_completed: int = 1,
) -> float | None:
    if total <= 0 or completed < min_completed or elapsed_seconds <= 0:
        return None
    if completed >= total:
        return 0.0
    average_seconds = elapsed_seconds / completed
    return max(0.0, average_seconds * (total - completed))


def format_eta(seconds: float | None) -> str:
    if seconds is None:
        return "Yaklaşık kalan süre: hesaplanıyor"
    if seconds <= 0:
        return "Yaklaşık kalan süre: tamamlanıyor"
    if seconds < 60:
        return "Yaklaşık kalan süre: 1 dk'dan az"
    if seconds < 3600:
        return f"Yaklaşık kalan süre: {ceil(seconds / 60)} dk"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    if minutes:
        return f"Yaklaşık kalan süre: {hours} sa {minutes} dk"
    return f"Yaklaşık kalan süre: {hours} sa"


def installer_button_state(
    *,
    preflight_ready: bool,
    has_api_key: bool,
    has_download_plan: bool,
    downloads_complete: bool,
    conversion_complete: bool,
    busy: bool = False,
    delivery_mode: str = "PREMIUM_API",
    real_install_supported: bool = False,
) -> InstallerButtonState:
    if conversion_complete:
        return InstallerButtonState(
            can_download=False,
            can_prepare=False,
            download_label="Çeviriler indirildi",
            prepare_label="Hazırlandı",
            download_hint="Gerekli dosyalar hazır.",
            prepare_hint="Çeviri dosyaları hazırlandı.",
            staging_only=not real_install_supported,
        )
    if busy:
        return InstallerButtonState(
            can_download=False,
            can_prepare=False,
            download_label="İşlem sürüyor",
            prepare_label=_prepare_label(real_install_supported),
            download_hint="Devam eden işlem tamamlanmalı.",
            prepare_hint="Devam eden işlem tamamlanmalı.",
            staging_only=not real_install_supported,
        )
    if not preflight_ready:
        return InstallerButtonState(
            can_download=False,
            can_prepare=False,
            download_label="Çevirileri indir",
            prepare_label=_prepare_label(real_install_supported),
            download_hint="Profil hazır olmadan indirme başlatılamaz.",
            prepare_hint="Tüm gerekli arşivler hazır olmadan başlatılamaz.",
            staging_only=not real_install_supported,
        )
    if not has_api_key and delivery_mode == "PREMIUM_API":
        return InstallerButtonState(
            can_download=False,
            can_prepare=False,
            download_label="Çevirileri indir",
            prepare_label=_prepare_label(real_install_supported),
            download_hint="Nexus API anahtarı kaydedilmeli.",
            prepare_hint="Tüm gerekli arşivler hazır olmadan başlatılamaz.",
            staging_only=not real_install_supported,
        )
    if downloads_complete:
        return InstallerButtonState(
            can_download=False,
            can_prepare=True,
            download_label="Çeviriler indirildi",
            prepare_label=_prepare_label(real_install_supported),
            download_hint="Tüm gerekli arşivler hazır.",
            prepare_hint=(
                "MO2 mods klasörüne kurulacak."
                if real_install_supported
                else "Çıktı hazırlama alanında oluşturulacak."
            ),
            staging_only=not real_install_supported,
        )
    mode_hint = (
        "Nexus sayfası açılacak; Slow Download'a tıklayın."
        if delivery_mode == "NON_PREMIUM_NXM"
        else "Gerekli dosyalar Nexus API ile indirilecek."
    )
    if not has_download_plan:
        mode_hint = "İndirme hazırlığı otomatik yapılacak. " + mode_hint
    return InstallerButtonState(
        can_download=True,
        can_prepare=False,
        download_label="Çevirileri indir",
        prepare_label=_prepare_label(real_install_supported),
        download_hint=mode_hint,
        prepare_hint="Tüm gerekli arşivler hazır olmadan başlatılamaz.",
        staging_only=not real_install_supported,
    )


def _prepare_label(real_install_supported: bool) -> str:
    return "Kuruluma başla" if real_install_supported else PREPARE_TRANSLATION_LABEL


def smart_modlist_display_name(mo2_root: Path | str, fallback: str = "Modlist") -> str:
    raw_name = Path(str(mo2_root or "").strip()).name.strip()
    if not raw_name:
        return fallback
    compact_key = re.sub(r"[\s_-]+", "", raw_name).casefold()
    known = {
        "lorerim": "LoreRim",
        "nordicsouls": "Nordic Souls",
    }
    if compact_key in known:
        return known[compact_key]
    name = re.sub(r"[_-]+", " ", raw_name).strip()
    if " " not in name:
        name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name or fallback


def translation_output_mod_name(
    mo2_root: Path | str,
    fallback_modlist_name: str = "Modlist",
) -> str:
    modlist_name = smart_modlist_display_name(mo2_root, fallback=fallback_modlist_name)
    safe_name = _safe_windows_component(f"{modlist_name} - Turkce Ceviri")
    return safe_name or "Modlist - Turkce Ceviri"


def primary_action_label(
    preflight: dict[str, Any] | None,
    has_api_key: bool,
    *,
    has_download_plan: bool = False,
    downloads_complete: bool = False,
    conversion_complete: bool = False,
    delivery_mode: str = "PREMIUM_API",
) -> str:
    if preflight is None:
        return "Kontrol Et"
    if preflight.get("status") != "READY":
        return "Kontrol Gerekli"
    if conversion_complete:
        return "Tamamlandı"
    if downloads_complete:
        return "Çeviriyi hazırla"
    if not has_api_key and delivery_mode == "PREMIUM_API":
        return "API Gerekli"
    if has_download_plan:
        if delivery_mode == "NON_PREMIUM_NXM":
            return "Nexus dosyasını aç"
        return "Dosyaları İndir"
    return "İndirme hazırlığı yap"


def _safe_component(value: object) -> str:
    text = str(value or "").strip().casefold()
    text = re.sub(r"[^a-z0-9._-]+", "-", text)
    text = "-".join(part for part in text.split("-") if part)
    return text.strip(".-_")


def _safe_windows_component(value: object) -> str:
    text = str(value or "").strip().replace("\\", "/")
    text = Path(text).name
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", text).strip(" .")
    text = re.sub(r"\s+", " ", text)
    return text[:160].rstrip(" .")
