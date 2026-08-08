"""End-user installer GUI for one bundled modlist translation release."""

from __future__ import annotations

import ctypes
import json
import os
import queue
import threading
import tkinter as tk
import traceback
import time
import webbrowser
from datetime import datetime, timezone
from importlib.resources import files
from io import BytesIO
from math import ceil
from pathlib import Path
from tkinter import filedialog
from typing import Any, Callable

import customtkinter as ctk
from PIL import Image, ImageTk, UnidentifiedImageError

from modlist_translate_tool.app.workflow import scan_profile
from modlist_translate_tool.nexus.api_client import NexusRateLimit
from modlist_translate_tool.nexus.downloader import urllib_download_to_file

from modlist_translation_wizard.bundled import (
    MANIFEST_MODE_LOCAL,
    MANIFEST_MODE_OTA,
    clear_default_remote_manifest_cache,
    default_manifest_source_info,
    default_release_info,
    external_release_dirs,
    load_default_bundled_manifest,
)
from modlist_translation_wizard.conversion_worker import run_conversion_in_worker
from modlist_translation_wizard.credential_store import (
    CredentialStore,
    CredentialStoreError,
    MemoryCredentialStore,
    WindowsCredentialStore,
)
from modlist_translation_wizard.download_cache import (
    DownloadCacheClearResult,
    DownloadCacheSummary,
    clear_download_cache,
    format_cache_size,
    inspect_download_cache,
)
from modlist_translation_wizard.endorsement import (
    BulkEndorsementSummary,
    NexusEndorsementError,
    ReleaseEndorsementTarget,
    collect_manifest_endorsement_targets,
    endorse_manifest_targets,
    merge_remaining_endorsement_targets,
    wait_required_endorsement_targets,
)
from modlist_translation_wizard.gui_model import (
    NON_PREMIUM_DELIVERY_LABEL,
    PREMIUM_DELIVERY_LABEL,
    api_key_notice,
    api_settings_url,
    default_workspace_root,
    delivery_mode_value,
    discover_mo2_profiles,
    estimated_remaining_seconds,
    format_eta,
    installer_button_state,
    load_release_branding,
    manifest_summary,
    preflight_summary,
    release_branding_asset_bytes,
    release_branding_asset_path,
    resolve_installer_mo2_location,
    run_workspace_for_manifest,
    translation_output_mod_name,
)
from modlist_translation_wizard.nexus_auth import (
    NexusPremiumRequiredError,
    api_key_status,
    clear_api_key,
    fetch_official_api_usage,
    load_api_key,
    require_premium_api_key,
    store_manual_api_key,
)
from modlist_translation_wizard.manifest import write_wizard_manifest
from modlist_translation_wizard.non_premium import (
    failed_non_premium_downloads,
    next_non_premium_download,
    run_non_premium_nxm_download,
    unavailable_non_premium_downloads,
)
from modlist_translation_wizard.nxm_capture import (
    NxmCaptureError,
    NxmCaptureServer,
    WindowsNxmProtocolBinding,
)
from modlist_translation_wizard.runtime import (
    build_wizard_preflight,
    download_queue_readiness,
    plan_downloads_from_manifest,
    run_premium_downloads_from_plan,
)
from modlist_translation_wizard.themed_dialog import (
    show_themed_dialog,
    show_themed_toast,
)
from modlist_translation_wizard.windows_long_paths import (
    WindowsLongPathEnableResult,
    enable_windows_long_paths,
    windows_long_path_status,
)


BANNER_IMAGE_MAX_WIDTH = 1040
BANNER_IMAGE_MAX_HEIGHT = 138
BANNER_TEXT_COLUMN_WIDTH = 430
BANNER_WINDOW_PADDING = 120
C0KADAM_DISCORD_SUPPORT_URL = "https://discordapp.com/users/279006796524421130"
NEGATRM_DISCORD_SUPPORT_URL = "https://discord.gg/4cHCUGkEP"
ENDORSE_BUTTON_LABEL = "👍 Çevirileri Beğen / Endorse Et"
WINDOWS_APP_USER_MODEL_ID = "c0kadam.NexusmodsTranslationDownloadWizard"
CONVERSION_RETRY_COOLDOWN_SECONDS = 12
ENDORSEMENT_AUTO_RETRY_SECONDS = 15 * 60
ENDORSEMENT_BUSY_RETRY_SECONDS = 30


def _configure_windows_app_identity() -> bool:
    if os.name != "nt":
        return False
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(  # type: ignore[attr-defined]
            WINDOWS_APP_USER_MODEL_ID
        )
    except (AttributeError, OSError):
        return False
    return True


def _packaged_release_icon_path(list_id: str, icon_name: str) -> Path | None:
    safe_list_id = Path(str(list_id or "")).name
    safe_icon_name = Path(str(icon_name or "")).name
    if not safe_list_id or not safe_icon_name:
        return None
    candidate = files("modlist_translation_wizard").joinpath(
        "resources",
        "releases",
        safe_list_id,
        safe_icon_name,
    )
    try:
        return Path(str(candidate)) if candidate.is_file() else None
    except OSError:
        return None


def _default_release_icon_path() -> Path | None:
    release_id = str(default_release_info().get("release_id") or "")
    for release_dir in external_release_dirs(release_id):
        candidate = release_dir / "icon.ico"
        if candidate.is_file():
            return candidate
    return _packaged_release_icon_path(release_id, "icon.ico")


def _apply_window_icon_asset(window: tk.Misc, icon_path: Path) -> ImageTk.PhotoImage | None:
    try:
        window.iconbitmap(str(icon_path))  # type: ignore[attr-defined]
    except (OSError, tk.TclError):
        pass
    try:
        with Image.open(icon_path) as icon:
            icon_image = icon.convert("RGBA")
        icon_image.thumbnail((256, 256), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(icon_image)
        window.iconphoto(True, photo)  # type: ignore[attr-defined]
    except (OSError, ValueError, UnidentifiedImageError, tk.TclError):
        return None
    return photo


def _is_conversion_worker_failure(error: BaseException) -> bool:
    text = str(error).casefold()
    return (
        "worker process" in text
        and ("cikis kodu:" in text or "çıkış kodu:" in text)
    ) or "workerfailed" in text


def _conversion_retry_seconds_remaining(*, ready_at: float, now: float) -> int:
    return max(0, ceil(float(ready_at) - float(now)))


def _endorsement_button_presentation(
    target_count: int,
    *,
    busy: bool,
) -> tuple[str, bool]:
    """Return the shared label and enabled state for endorsement actions."""

    if busy:
        return "Gönderiliyor...", False
    if target_count <= 0:
        return "Beğeniler gönderildi", False
    return ENDORSE_BUTTON_LABEL, True


def _banner_title_font_size(title: str) -> int:
    length = len(title.strip())
    if length <= 30:
        return 24
    if length <= 38:
        return 21
    if length <= 48:
        return 18
    return 16


def _archive_path_key(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _download_item_lookup(queue_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    pending_items = unavailable_non_premium_downloads(queue_payload)
    total = len(pending_items)
    pending_by_ids = {
        (
            int(item["translation_nexus_mod_id"]),
            int(item["translation_file_id"]),
        ): item
        for item in pending_items
    }
    positions = {
        (
            int(item["translation_nexus_mod_id"]),
            int(item["translation_file_id"]),
        ): position
        for position, item in enumerate(pending_items, start=1)
    }
    for queue_item in queue_payload.get("items", []):
        if not isinstance(queue_item, dict):
            continue
        request = queue_item.get("request") if isinstance(queue_item.get("request"), dict) else {}
        try:
            identity = (
                int(request.get("translation_nexus_mod_id")),
                int(request.get("translation_file_id")),
            )
        except (TypeError, ValueError):
            continue
        summary = pending_by_ids.get(identity)
        archive_path = str(queue_item.get("local_archive_path") or "").strip()
        if summary is None or not archive_path:
            continue
        lookup[_archive_path_key(archive_path)] = {
            **summary,
            "position": positions[identity],
            "total": total,
        }
    return lookup


def _download_item_for_part_path(
    part_path: Path,
    lookup: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    path_text = str(part_path)
    archive_path = path_text[:-5] if path_text.casefold().endswith(".part") else path_text
    item = lookup.get(_archive_path_key(archive_path))
    if item is not None:
        return item
    try:
        identity = (int(part_path.parent.parent.name), int(part_path.parent.name))
    except (IndexError, TypeError, ValueError):
        identity = None
    if identity is not None:
        for candidate in lookup.values():
            if (
                int(candidate["translation_nexus_mod_id"]),
                int(candidate["translation_file_id"]),
            ) == identity:
                return candidate
    return {
        "translation_name": "Nexus çeviri dosyası",
        "translation_file_name": part_path.name.removesuffix(".part"),
        "translation_nexus_mod_id": "?",
        "translation_file_id": "?",
        "position": 1,
        "total": max(len(lookup), 1),
    }


def _conversion_archive_information(
    payload: dict[str, Any],
) -> tuple[tuple[str, str, str], str] | None:
    archive_text = str(payload.get("archive_path") or "").strip()
    if not archive_text:
        return None
    archive_name = Path(archive_text).name or archive_text
    stage = str(payload.get("stage") or "")
    mod_id = str(payload.get("translation_nexus_mod_id") or "?")
    file_id = str(payload.get("translation_file_id") or "?")
    display_name = str(payload.get("display_name") or "").strip()
    if stage == "extracting_add_on_package":
        position = max(1, int(payload.get("processed_packages") or 1))
        total = max(1, int(payload.get("total_packages") or 1))
        action = f"Ek paket çıkarılıyor: {position}/{total}"
    elif stage == "extracting_native_binary_asset":
        position = max(1, int(payload.get("processed_assets") or 1))
        total = max(1, int(payload.get("total_assets") or 1))
        action = f"Ek dosya çıkarılıyor: {position}/{total}"
    else:
        position = max(1, int(payload.get("processed_archives") or 1))
        total = max(1, int(payload.get("total_archives") or 1))
        action = f"Arşiv çıkarılıyor ve dönüştürülüyor: {position}/{total}"
    name_line = f" · {display_name}" if display_name else ""
    text = f"{action}{name_line}\nArşiv: {archive_name}\nNexus: {mod_id}/{file_id}"
    return (archive_text.casefold(), mod_id, file_id), text


def _format_nexus_api_usage(rate_limit: NexusRateLimit) -> str:
    hourly = _format_quota_pair(
        rate_limit.hourly_remaining,
        rate_limit.hourly_limit,
    )
    daily = _format_quota_pair(
        rate_limit.daily_remaining,
        rate_limit.daily_limit,
    )
    resets = [
        text
        for text in (
            _format_quota_reset("saatlik", rate_limit.hourly_reset),
            _format_quota_reset("günlük", rate_limit.daily_reset),
        )
        if text
    ]
    reset_text = f" · Sıfırlama: {', '.join(resets)}" if resets else ""
    return (
        f"Resmî Nexus API kotası · Saatlik: {hourly} · Günlük: {daily}"
        f"{reset_text}"
    )


def _bulk_endorsement_log_summary(result: BulkEndorsementSummary) -> str:
    return (
        "Nexus toplu endorse tamamlandı: "
        f"toplam={result.total}, desteklenen={result.completed}, "
        f"yeni={result.endorsed}, zaten={result.already_endorsed}, "
        f"15dk_bekleyen={result.wait_required}, hata={result.failed}, "
        f"geçici_hata={result.transient_error}, kota={result.rate_limited}, "
        f"denenmeyen={result.not_attempted}."
    )


def _bulk_endorsement_user_message(
    result: BulkEndorsementSummary,
    *,
    auto_retry_scheduled: bool = False,
) -> str:
    lines = [
        "Çeviri sayfalarını beğenme (endorse) işlemi tamamlandı.",
        "",
        f"Desteklenen sayfa: {result.completed}/{result.total}",
    ]
    if result.endorsed:
        lines.append(f"Yeni endorse edilen: {result.endorsed}")
    if result.already_endorsed:
        lines.append(f"Zaten endorse edilmiş: {result.already_endorsed}")
    if result.wait_required:
        lines.extend(
            [
                "",
                (
                    f"{result.wait_required} sayfa, Nexus'un 15 dakika bekleme "
                    "süresi nedeniyle endorse edilemedi."
                ),
            ]
        )
        if auto_retry_scheduled:
            lines.append(
                "Araç açık kalırsa bu sayfalar 15 dakika sonra arka planda "
                "otomatik olarak yeniden denenecek."
            )
        else:
            lines.append(
                "Bu sayfaları daha sonra Çevirileri Beğen düğmesiyle yeniden "
                "deneyebilirsiniz."
            )
    if result.rate_limited:
        lines.append("Nexus API kotası nedeniyle işlem durdu; kota yenilenince tekrar deneyin.")
    if result.transient_error:
        lines.append("Geçici bir Nexus bağlantı sorunu nedeniyle işlem durdu.")
    if result.unauthorized:
        lines.append("API anahtarı endorse yetkisine sahip değil veya geçersiz görünüyor.")
    if result.not_attempted:
        lines.append(f"Bu nedenle {result.not_attempted} sayfa henüz denenmedi.")
    if result.disabled or result.own_file or result.abstained or result.failed:
        lines.append(
            "Bazı sayfalar Nexus kuralları veya sayfa ayarları nedeniyle atlandı."
        )
    lines.extend(
        [
            "",
            (
                "Verdiğiniz beğeniler çevirmenlerin emeğini daha görünür kılacak "
                "ve gelecekteki çalışmalar için değerli bir motivasyon sağlayacaktır."
            ),
            "Katkınız için teşekkür ederiz. İyi oyunlar.",
        ]
    )
    return "\n".join(lines)


def _format_quota_pair(remaining: int | None, limit: int | None) -> str:
    if remaining is None and limit is None:
        return "bilinmiyor"
    remaining_text = _format_quota_number(remaining)
    if limit is None:
        return remaining_text
    return f"{remaining_text} / {_format_quota_number(limit)}"


def _format_quota_number(value: int | None) -> str:
    if value is None:
        return "?"
    return f"{max(0, int(value)):,}".replace(",", ".")


def _format_quota_reset(label: str, timestamp: int | None) -> str:
    if timestamp is None:
        return ""
    try:
        value = datetime.fromtimestamp(int(timestamp), tz=timezone.utc).astimezone()
    except (OSError, OverflowError, ValueError):
        return ""
    return f"{label} {value:%H:%M}"


def _initial_window_size(
    *,
    preferred_width: int,
    required_width: int,
    required_height: int,
    screen_width: int,
    screen_height: int,
) -> tuple[int, int]:
    available_width = max(760, int(screen_width) - 72)
    available_height = max(640, int(screen_height) - 96)
    width = min(
        max(MIN_WINDOW_WIDTH, int(preferred_width), int(required_width)),
        available_width,
        MAX_WINDOW_WIDTH,
    )
    height = min(
        max(MIN_WINDOW_HEIGHT, DEFAULT_WINDOW_HEIGHT, int(required_height)),
        available_height,
    )
    return width, height


DEFAULT_WINDOW_HEIGHT = 980
MIN_WINDOW_HEIGHT = 740
MIN_WINDOW_WIDTH = 1040
MAX_WINDOW_WIDTH = 1750
MANIFEST_MODE_OTA_LABEL = "OTA (Güncel)"
MANIFEST_MODE_LOCAL_LABEL = "Yerel"


class ModlistTranslationInstallerApp:
    """Single-release, discovery-free installer surface."""

    def __init__(
        self,
        root: ctk.CTk,
        *,
        initial_manifest: dict[str, Any] | None = None,
        initial_manifest_mode: str = MANIFEST_MODE_OTA,
        initial_source_info: dict[str, str | None] | None = None,
    ) -> None:
        self.root = root
        self.manifest_mode = tk.StringVar(value=initial_manifest_mode)
        self.manifest = initial_manifest or load_default_bundled_manifest(
            manifest_mode=self.manifest_mode.get()
        )
        self.manifest_source_info = (
            dict(initial_source_info)
            if initial_source_info is not None
            else default_manifest_source_info()
        )
        self.summary = manifest_summary(self.manifest)
        self.branding = load_release_branding(self.manifest)
        self.endorsement_target = self.branding.endorsement
        self.endorsement_targets = collect_manifest_endorsement_targets(
            self.manifest,
            extra_targets=(
                [self.endorsement_target] if self.endorsement_target is not None else []
            ),
        )
        self.endorsement_available = bool(self.endorsement_targets)
        self.app_id = self.summary["registered_app_id"]
        self.store: CredentialStore = _create_credential_store()
        self.window_icon_photo: ImageTk.PhotoImage | None = None

        self.root.title(self.branding.display_name)
        self._apply_window_icon()
        initial_width = self._initial_window_width()
        self.root.geometry(f"{initial_width}x{DEFAULT_WINDOW_HEIGHT}")
        self.root.minsize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)

        self.delivery_mode = tk.StringVar(value=PREMIUM_DELIVERY_LABEL)
        self.api_key = tk.StringVar()
        self.mo2_root = tk.StringVar()
        self.mo2_profile = tk.StringVar()
        self.status_text = tk.StringVar(value="Hazır.")
        self.profile_status_text = tk.StringVar(value="Modlist klasörü seçin.")
        self.auth_status_text = tk.StringVar(value="Nexus API anahtarı kontrol ediliyor.")
        self.api_usage_text = tk.StringVar(
            value="Resmî Nexus API kotası için API anahtarınızı kaydedin."
        )
        self.long_paths_status_text = tk.StringVar(value="Windows ayarı kontrol ediliyor.")
        self.download_status_text = tk.StringVar(value="Profil hazırlanınca indirme açılır.")
        self.current_download_text = tk.StringVar(
            value="İndirme veya hazırlama başlayınca işlem bilgisi burada görünür."
        )
        self.prepare_status_text = tk.StringVar(
            value="Çeviri hazırlanınca çıktı klasöründe oluşur."
        )
        self.output_folder_text = tk.StringVar(value=self._output_folder_display())
        self.nxm_status_text = tk.StringVar(value="")
        self.nxm_link = tk.StringVar()
        self.auto_open_next_nxm = tk.BooleanVar(value=True)
        self.details_visible = tk.BooleanVar(value=False)
        self.progress_value = tk.IntVar(value=0)
        self.progress_percent = tk.StringVar(value="0%")
        self.progress_label = tk.StringVar(value="Başlamadı")
        self.eta_text = tk.StringVar(value="")
        self.release_summary_text = tk.StringVar(value=self._release_summary_display())
        source_text, _source_tone = self._manifest_source_notice()
        self.manifest_source_text = tk.StringVar(value=source_text)
        self.endorsement_status_text = tk.StringVar(
            value=(
                f"{len(self.endorsement_targets)} Nexus çeviri sayfasını beğenebilirsiniz."
                if self.endorsement_targets
                else ""
            )
        )

        self.profile_scan_path: Path | None = None
        self.preflight_payload: dict[str, Any] | None = None
        self.profile_override_accepted = False
        self.premium_plan_result: Any | None = None
        self.premium_download_result: Any | None = None
        self.non_premium_download_result: Any | None = None
        self.conversion_result: Any | None = None
        self.premium_api_validated = False
        self.use_manifest_download_cache_roots = True
        self.pending_nxm_url: str | None = None
        self.nxm_capture_server: NxmCaptureServer | None = None
        self.nxm_protocol_binding = WindowsNxmProtocolBinding()
        self.task_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.busy = False
        self.endorsement_busy = False
        self._endorsement_retry_after: str | None = None
        self._endorsement_retry_due_at = 0.0
        self._endorsement_retry_targets: tuple[ReleaseEndorsementTarget, ...] = ()
        self.api_usage_busy = False
        self._busy_progress_after: str | None = None
        self._busy_progress_label = ""
        self._busy_progress_cap = 0
        self._busy_progress_dots = 0
        self._eta_after: str | None = None
        self._eta_phase: str | None = None
        self._eta_started_at = 0.0
        self._eta_completed = 0
        self._eta_total = 0
        self._eta_progress_base = 0
        self._eta_progress_span = 0
        self._eta_status_path: Path | None = None
        self._last_conversion_archive_key: tuple[str, str, str] | None = None
        self._conversion_retry_after: str | None = None
        self._conversion_retry_ready_at = 0.0
        self.banner_image: ctk.CTkImage | None = None
        self.discord_support_icon: ctk.CTkImage | None = None
        self.endorsement_button: ctk.CTkButton | None = None
        self.completion_endorsement_button: ctk.CTkButton | None = None
        self.completion_popup: ctk.CTkToplevel | None = None
        self.download_recovery_popup: ctk.CTkToplevel | None = None
        self.download_cache_popup: ctk.CTkToplevel | None = None
        self.active_toast: ctk.CTkFrame | None = None

        try:
            self.nxm_protocol_binding.recover_stale_binding()
        except NxmCaptureError:
            pass

        self._configure_style()
        self._build()
        self._fit_initial_window_to_content(initial_width)
        self._refresh_windows_long_paths_status()
        self._refresh_auth_status()
        self._refresh_pipeline_buttons()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(100, self._poll_task_queue)
        self.root.after(350, lambda: self._refresh_api_usage(silent=True))

    def _configure_style(self) -> None:
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")
        self.colors = {
            "bg": ("#f4f6f8", "#101114"),
            "panel": ("#ffffff", "#181a1f"),
            "panel_alt": ("#eef1f5", "#20232a"),
            "line": ("#d7dde5", "#30343d"),
            "text": ("#17202a", "#f3f4f6"),
            "muted": ("#5b6470", "#a4acb9"),
            "accent": self.branding.accent_color,
            "accent_hover": "#7a4422",
            "warning": ("#b45309", "#fbbf24"),
            "danger": ("#b91c1c", "#f87171"),
            "success": ("#047857", "#34d399"),
            "premium": ("#d97706", "#f28c28"),
            "free": ("#047857", "#6ee7b7"),
            "link": ("#2563eb", "#60a5fa"),
            "button": ("#e5e7eb", "#2a2f39"),
            "button_hover": ("#d1d5db", "#343a46"),
        }
        self.root.configure(fg_color=self.colors["bg"])

    def _notify(
        self,
        title: str,
        message: str,
        *,
        tone: str = "info",
        action_label: str = "Anladım",
        parent: tk.Misc | None = None,
    ) -> None:
        show_themed_dialog(
            parent or self.root,
            palette=self.colors,
            title=title,
            message=message,
            tone=tone,
            primary_label=action_label,
        )

    def _confirm(
        self,
        title: str,
        message: str,
        *,
        action_label: str,
        cancel_label: str = "Vazgeç",
        tone: str = "question",
        parent: tk.Misc | None = None,
    ) -> bool:
        return show_themed_dialog(
            parent or self.root,
            palette=self.colors,
            title=title,
            message=message,
            tone=tone,
            primary_label=action_label,
            secondary_label=cancel_label,
        )

    def _toast(
        self,
        title: str,
        message: str,
        *,
        tone: str = "info",
        duration_ms: int = 6500,
    ) -> None:
        if self.active_toast is not None and self.active_toast.winfo_exists():
            self.active_toast.destroy()
        self.active_toast = show_themed_toast(
            self.root,
            palette=self.colors,
            title=title,
            message=message,
            tone=tone,
            duration_ms=duration_ms,
        )

    def _apply_window_icon(self) -> None:
        icon_path = release_branding_asset_path(self.manifest, self.branding.icon)
        if icon_path is None:
            modlist = (
                self.manifest.get("modlist")
                if isinstance(self.manifest.get("modlist"), dict)
                else {}
            )
            icon_path = _packaged_release_icon_path(
                str(modlist.get("id") or ""),
                str(self.branding.icon or "icon.ico"),
            )
        if icon_path is None:
            return
        self.window_icon_photo = _apply_window_icon_asset(self.root, icon_path)

    def _build(self) -> None:
        shell = ctk.CTkScrollableFrame(
            self.root,
            fg_color=self.colors["bg"],
            corner_radius=0,
        )
        shell.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(3, weight=1)

        self._build_banner(shell)
        self._build_release_summary(shell)
        self._build_main_form(shell)
        self._log("Hazır.")
        self._log_manifest_source()

    def _build_banner(self, parent: ctk.CTkFrame) -> None:
        banner = ctk.CTkFrame(
            parent,
            fg_color=self.colors["accent"],
            corner_radius=18,
            height=154,
            border_width=1,
            border_color=self.branding.warm_glow,
        )
        banner.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 8))
        banner.grid_propagate(False)
        banner.columnconfigure(0, weight=0)
        banner.columnconfigure(1, weight=1)
        banner.rowconfigure(0, weight=1)

        text_frame = ctk.CTkFrame(banner, fg_color="transparent")
        text_frame.grid(row=0, column=0, sticky="w", padx=28, pady=24)
        title_layer = ctk.CTkFrame(
            text_frame,
            width=BANNER_TEXT_COLUMN_WIDTH - 56,
            height=36,
            fg_color="transparent",
        )
        title_layer.pack(anchor="w")
        title_layer.pack_propagate(False)
        title_font = ctk.CTkFont(
            family="Segoe UI",
            size=_banner_title_font_size(self.branding.display_name),
            weight="bold",
        )
        ctk.CTkLabel(
            title_layer,
            text=self.branding.display_name,
            text_color=self.branding.font_shadow,
            font=title_font,
            anchor="w",
        ).place(x=2, y=2, relwidth=1, relheight=1)
        ctk.CTkLabel(
            title_layer,
            text=self.branding.display_name,
            text_color=self.branding.font_color,
            font=title_font,
            anchor="w",
        ).place(x=0, y=0, relwidth=1, relheight=1)
        ctk.CTkLabel(
            text_frame,
            text=self.branding.subtitle,
            text_color=self.branding.font_color,
            font=ctk.CTkFont(family="Segoe UI", size=12),
        ).pack(anchor="w", pady=(4, 0))
        mode_row = ctk.CTkFrame(text_frame, fg_color="transparent")
        mode_row.pack(anchor="w", pady=(14, 0))
        ctk.CTkLabel(
            mode_row,
            text="Görünüm",
            text_color=self.branding.font_color,
            font=ctk.CTkFont(family="Segoe UI", size=11),
        ).pack(side=tk.LEFT, padx=(0, 8))
        self.appearance_mode = ctk.CTkSegmentedButton(
            mode_row,
            values=["Koyu", "Açık"],
            command=self._on_appearance_changed,
            selected_color=self.branding.font_shadow,
            selected_hover_color=self.branding.warm_glow,
            unselected_color=self.branding.warm_glow,
            unselected_hover_color=self.branding.accent_color,
            text_color=self.branding.font_color,
            corner_radius=8,
            height=28,
        )
        self.appearance_mode.pack(side=tk.LEFT)
        self.appearance_mode.set("Koyu")

        banner_bytes = release_branding_asset_bytes(self.manifest, self.branding.banner)
        if banner_bytes:
            try:
                image = Image.open(BytesIO(banner_bytes))
                max_width = BANNER_IMAGE_MAX_WIDTH
                max_height = BANNER_IMAGE_MAX_HEIGHT
                ratio = min(max_width / image.width, max_height / image.height, 1)
                size = (max(1, int(image.width * ratio)), max(1, int(image.height * ratio)))
                self.banner_image = ctk.CTkImage(
                    light_image=image,
                    dark_image=image,
                    size=size,
                )
                ctk.CTkLabel(
                    banner,
                    text="",
                    image=self.banner_image,
                    fg_color="transparent",
                ).grid(
                    row=0,
                    column=1,
                    sticky="e",
                    padx=(20, 26),
                    pady=12,
                )
            except Exception:
                self.banner_image = None

    def _initial_window_width(self) -> int:
        image_size = self._banner_display_size()
        if image_size is None:
            return 1180
        width = image_size[0] + BANNER_TEXT_COLUMN_WIDTH + BANNER_WINDOW_PADDING
        return max(MIN_WINDOW_WIDTH, min(MAX_WINDOW_WIDTH, width))

    def _fit_initial_window_to_content(self, preferred_width: int) -> None:
        self.root.update_idletasks()
        width, height = _initial_window_size(
            preferred_width=preferred_width,
            required_width=self.root.winfo_reqwidth(),
            required_height=self.root.winfo_reqheight(),
            screen_width=self.root.winfo_screenwidth(),
            screen_height=self.root.winfo_screenheight(),
        )
        x = max(0, (self.root.winfo_screenwidth() - width) // 2)
        y = max(0, (self.root.winfo_screenheight() - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _banner_display_size(self) -> tuple[int, int] | None:
        banner_bytes = release_branding_asset_bytes(self.manifest, self.branding.banner)
        if not banner_bytes:
            return None
        try:
            image = Image.open(BytesIO(banner_bytes))
        except Exception:
            return None
        ratio = min(
            BANNER_IMAGE_MAX_WIDTH / image.width,
            BANNER_IMAGE_MAX_HEIGHT / image.height,
            1,
        )
        return (
            max(1, int(image.width * ratio)),
            max(1, int(image.height * ratio)),
        )

    def _build_release_summary(self, parent: ctk.CTkFrame) -> None:
        card = ctk.CTkFrame(
            parent,
            fg_color=self.colors["panel"],
            corner_radius=14,
            border_width=1,
            border_color=self.colors["line"],
        )
        card.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 10))
        card.columnconfigure(0, weight=1)
        card.columnconfigure(1, weight=0, minsize=440)
        ctk.CTkLabel(
            card,
            textvariable=self.release_summary_text,
            text_color=self.colors["text"],
            anchor="w",
            wraplength=600,
            font=ctk.CTkFont(family="Segoe UI", size=13),
        ).grid(row=0, column=0, sticky="ew", padx=(18, 12), pady=(14, 8))

        source_row = ctk.CTkFrame(card, fg_color="transparent")
        source_row.grid(row=1, column=0, sticky="ew", padx=(18, 12), pady=(0, 14))
        ctk.CTkLabel(
            source_row,
            text="Çeviri listesi",
            text_color=self.colors["muted"],
            font=ctk.CTkFont(family="Segoe UI", size=12),
        ).grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.manifest_mode_control = ctk.CTkSegmentedButton(
            source_row,
            values=[MANIFEST_MODE_OTA_LABEL, MANIFEST_MODE_LOCAL_LABEL],
            command=self._on_manifest_mode_changed,
            selected_color=self.colors["accent"],
            selected_hover_color=self.colors["accent_hover"],
            unselected_color=self.colors["button"],
            unselected_hover_color=self.colors["button_hover"],
            corner_radius=8,
            height=30,
        )
        self.manifest_mode_control.grid(row=0, column=1, sticky="w")
        self.manifest_mode_control.set(self._manifest_mode_label())

        _source_text, source_tone = self._manifest_source_display()
        self.manifest_source_frame = ctk.CTkFrame(
            card,
            fg_color=self.colors["panel_alt"],
            corner_radius=10,
            border_width=1,
            border_color=self.colors[source_tone],
        )
        self.manifest_source_frame.grid(
            row=0,
            column=1,
            rowspan=2,
            sticky="nsew",
            padx=(6, 14),
            pady=12,
        )
        self.manifest_source_frame.columnconfigure(0, weight=1)
        ctk.CTkLabel(
            self.manifest_source_frame,
            text="Çeviri listesi durumu",
            text_color=self.colors["muted"],
            anchor="w",
            font=ctk.CTkFont(family="Segoe UI", size=11),
        ).grid(row=0, column=0, sticky="ew", padx=14, pady=(10, 2))
        self.manifest_source_label = ctk.CTkLabel(
            self.manifest_source_frame,
            textvariable=self.manifest_source_text,
            text_color=self.colors[source_tone],
            anchor="w",
            justify="left",
            wraplength=380,
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
        )
        self.manifest_source_label.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 10))
        if self.endorsement_targets:
            ctk.CTkFrame(
                self.manifest_source_frame,
                height=1,
                fg_color=self.colors["line"],
            ).grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 10))
            endorsement_row = ctk.CTkFrame(
                self.manifest_source_frame,
                fg_color="transparent",
            )
            endorsement_row.grid(row=3, column=0, sticky="ew", padx=14, pady=(0, 12))
            endorsement_row.columnconfigure(0, weight=1)
            ctk.CTkLabel(
                endorsement_row,
                textvariable=self.endorsement_status_text,
                text_color=self.colors["text"],
                anchor="w",
                justify="left",
                wraplength=220,
                font=ctk.CTkFont(family="Segoe UI", size=12),
            ).grid(row=0, column=0, sticky="ew", padx=(0, 10))
            self.endorsement_button = ctk.CTkButton(
                endorsement_row,
                text=ENDORSE_BUTTON_LABEL,
                command=self._endorse_release,
                fg_color=self.colors["premium"],
                hover_color="#ffad4d",
                text_color=("#ffffff", "#ffffff"),
                corner_radius=9,
                height=38,
                width=210,
                font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            )
            self.endorsement_button.grid(row=0, column=1, sticky="e")

    def _log_manifest_source(self) -> None:
        info = self.manifest_source_info
        source = str(info.get("source") or "")
        version = str(info.get("version") or "").strip()
        warning = str(info.get("warning") or "").strip()
        if source == "remote_download":
            suffix = f" ({version})" if version else ""
            self._log(f"Çeviri listesi güncellendi{suffix}.")
        elif source == "remote_cache":
            suffix = f" ({version})" if version else ""
            self._log(f"Çeviri listesi önbellekten kullanıldı{suffix}.")
        elif source == "external":
            self._log("Çeviri listesi yerel release klasöründen yüklendi.")
        elif source == "bundled":
            self._log("Çeviri listesi uygulamadaki yerel paketten yüklendi.")
        elif source == "remote_failed":
            self._log("Çeviri listesi güncellemesi alınamadı; yerel liste kullanılacak.")
        if warning:
            self._log(f"Çeviri listesi uyarısı: {warning}")

    def _release_summary_display(self) -> str:
        add_on_count = int(self.summary.get("add_on_package_count") or 0)
        add_on_text = f" / {add_on_count} ek paket" if add_on_count else ""
        return (
            f"{self.summary['modlist_name']}  |  "
            f"{self.summary['modlist_version']}  |  "
            f"{self.summary['language'].upper()}  |  "
            f"{self.summary['entry_count']} çeviri / "
            f"{self.summary['unique_download_count']} dosya"
            f"{add_on_text}"
        )

    def _manifest_mode_label(self) -> str:
        if self.manifest_mode.get() == MANIFEST_MODE_LOCAL:
            return MANIFEST_MODE_LOCAL_LABEL
        return MANIFEST_MODE_OTA_LABEL

    def _manifest_source_display(self) -> tuple[str, str]:
        info = self.manifest_source_info
        source = str(info.get("source") or "")
        version = str(info.get("version") or "").strip()
        fallback = str(info.get("fallback_from") or "").upper() == MANIFEST_MODE_OTA
        suffix = f" • {version}" if version else ""
        if source == "remote_download":
            return f"Aktif kaynak: OTA • GitHub'dan güncellendi{suffix}", "success"
        if source == "remote_cache":
            warning = str(info.get("warning") or "").strip()
            if warning:
                return f"Aktif kaynak: OTA önbelleği{suffix} • Ağ güncellemesi alınamadı", "warning"
            return f"Aktif kaynak: OTA önbelleği{suffix}", "success"
        if source == "external":
            if fallback:
                return "Aktif kaynak: Yerel release • OTA kullanılamadı", "warning"
            return "Aktif kaynak: Yerel release", "muted"
        if source == "bundled":
            if fallback:
                return "Aktif kaynak: Yerel paket • OTA kullanılamadı", "warning"
            return "Aktif kaynak: Uygulamaya gömülü yerel paket", "muted"
        return "Aktif manifest kaynağı belirlenemedi", "warning"

    def _manifest_source_notice(self) -> tuple[str, str]:
        source_text, tone = self._manifest_source_display()
        updated_at = str(self.summary.get("manifest_updated_at") or "Bilinmiyor")
        return f"{source_text}\nSon güncelleme: {updated_at}", tone

    def _set_manifest_source_visual(self, text: str, tone: str) -> None:
        self.manifest_source_text.set(text)
        color = self.colors.get(tone, self.colors["warning"])
        label = getattr(self, "manifest_source_label", None)
        if label is not None:
            label.configure(text_color=color)
        frame = getattr(self, "manifest_source_frame", None)
        if frame is not None:
            frame.configure(border_color=color)

    def _on_manifest_mode_changed(self, selected_label: str) -> None:
        requested_mode = (
            MANIFEST_MODE_LOCAL
            if selected_label == MANIFEST_MODE_LOCAL_LABEL
            else MANIFEST_MODE_OTA
        )
        previous_mode = self.manifest_mode.get()
        if requested_mode == previous_mode:
            return
        if self.busy:
            self.manifest_mode_control.set(self._manifest_mode_label())
            self._toast(
                "İşlem hâlâ devam ediyor",
                "Çeviri listesi kaynağını mevcut işlem tamamlandıktan sonra değiştirebilirsiniz.",
                tone="info",
            )
            return

        self.manifest_mode.set(requested_mode)
        self._set_manifest_source_visual(
            "Çeviri listesi kaynağı değiştiriliyor...",
            "warning",
        )

        def work() -> dict[str, Any]:
            try:
                manifest = load_default_bundled_manifest(manifest_mode=requested_mode)
                return {
                    "manifest": manifest,
                    "source_info": default_manifest_source_info(),
                }
            except Exception as exc:  # noqa: BLE001 - restored safely in GUI callback.
                return {
                    "error": exc,
                    "traceback": traceback.format_exc(),
                }

        def done(result: dict[str, Any]) -> None:
            error = result.get("error")
            if isinstance(error, BaseException):
                self.manifest_mode.set(previous_mode)
                self.manifest_mode_control.set(self._manifest_mode_label())
                source_text, source_tone = self._manifest_source_notice()
                self._set_manifest_source_visual(source_text, source_tone)
                self._handle_task_error(error, str(result.get("traceback") or ""))
                return

            self.manifest = result["manifest"]
            self._cancel_endorsement_auto_retry()
            self.manifest_source_info = dict(result["source_info"])
            self.summary = manifest_summary(self.manifest)
            self.branding = load_release_branding(self.manifest)
            self.endorsement_target = self.branding.endorsement
            self.endorsement_targets = collect_manifest_endorsement_targets(
                self.manifest,
                extra_targets=(
                    [self.endorsement_target]
                    if self.endorsement_target is not None
                    else []
                ),
            )
            self.endorsement_available = bool(self.endorsement_targets)
            self.endorsement_status_text.set(
                (
                    f"{len(self.endorsement_targets)} Nexus çeviri sayfasını beğenebilirsiniz."
                    if self.endorsement_targets
                    else ""
                )
            )
            self._sync_endorsement_buttons()
            self.app_id = self.summary["registered_app_id"]
            self.release_summary_text.set(self._release_summary_display())
            source_text, source_tone = self._manifest_source_notice()
            self._set_manifest_source_visual(source_text, source_tone)
            self._reset_for_profile_change()
            self._log(
                "Çeviri listesi kaynağı değiştirildi: "
                + ("OTA" if requested_mode == MANIFEST_MODE_OTA else "Yerel")
            )
            self._log_manifest_source()
            if self.mo2_root.get().strip():
                self._find_profiles()
            else:
                self._refresh_pipeline_buttons()

        self._run_task("Çeviri listesi yükleniyor", work, done)

    def _build_main_form(self, parent: ctk.CTkFrame) -> None:
        main = ctk.CTkFrame(
            parent,
            fg_color=self.colors["panel"],
            corner_radius=14,
            border_width=1,
            border_color=self.colors["line"],
        )
        main.grid(row=2, column=0, sticky="ew", padx=18, pady=(0, 10))
        main.columnconfigure(1, weight=1)
        main.columnconfigure(3, weight=1)

        ctk.CTkLabel(
            main,
            text="Kurulum",
            text_color=self.colors["text"],
            font=ctk.CTkFont(family="Segoe UI", size=16, weight="bold"),
        ).grid(row=0, column=0, columnspan=4, sticky="w", padx=18, pady=(16, 12))
        ctk.CTkLabel(
            main,
            text="Modlist klasörü",
            text_color=self.colors["text"],
            anchor="w",
        ).grid(row=1, column=0, sticky="w", padx=(18, 10), pady=6)
        ctk.CTkEntry(
            main,
            textvariable=self.mo2_root,
            corner_radius=8,
            fg_color=self.colors["panel_alt"],
            border_color=self.colors["line"],
        ).grid(row=1, column=1, sticky="ew", pady=6)
        ctk.CTkButton(
            main,
            text="Seç",
            command=self._browse_mo2_root,
            corner_radius=8,
            fg_color=self.colors["button"],
            hover_color=self.colors["button_hover"],
        ).grid(row=1, column=2, sticky="ew", padx=(10, 0), pady=6)
        ctk.CTkButton(
            main,
            text="Profilleri yenile",
            command=self._find_profiles,
            corner_radius=8,
            fg_color=self.colors["button"],
            hover_color=self.colors["button_hover"],
        ).grid(row=1, column=3, sticky="w", padx=(10, 18), pady=6)

        ctk.CTkLabel(
            main,
            text="Profil",
            text_color=self.colors["text"],
            anchor="w",
        ).grid(row=2, column=0, sticky="w", padx=(18, 10), pady=6)
        self.profile_combo = ctk.CTkComboBox(
            main,
            variable=self.mo2_profile,
            values=[],
            command=lambda _choice: self._on_profile_selected(),
            corner_radius=8,
            fg_color=self.colors["panel_alt"],
            border_color=self.colors["line"],
            button_color=self.colors["button"],
            button_hover_color=self.colors["button_hover"],
        )
        self.profile_combo.grid(row=2, column=1, sticky="ew", pady=6)
        self.profile_status_label = ctk.CTkLabel(
            main,
            textvariable=self.profile_status_text,
            text_color=self.colors["muted"],
            anchor="w",
        )
        self.profile_status_label.grid(
            row=2,
            column=2,
            columnspan=2,
            sticky="w",
            padx=(12, 18),
            pady=6,
        )

        ctk.CTkLabel(
            main,
            text="Çıktı klasörü",
            text_color=self.colors["text"],
            anchor="w",
        ).grid(row=3, column=0, sticky="w", padx=(18, 10), pady=6)
        ctk.CTkEntry(
            main,
            textvariable=self.output_folder_text,
            state="disabled",
            corner_radius=8,
            fg_color=self.colors["panel_alt"],
            border_color=self.colors["line"],
        ).grid(row=3, column=1, columnspan=2, sticky="ew", pady=6)
        folder_actions = ctk.CTkFrame(main, fg_color="transparent")
        folder_actions.grid(row=3, column=3, sticky="w", padx=(10, 18), pady=6)
        ctk.CTkButton(
            folder_actions,
            text="Çıktıyı aç",
            command=self._open_output_folder,
            corner_radius=8,
            fg_color=self.colors["button"],
            hover_color=self.colors["button_hover"],
            width=104,
        ).pack(side=tk.LEFT)
        ctk.CTkButton(
            folder_actions,
            text="Uygulama verilerini aç",
            command=self._open_app_data_folder,
            corner_radius=8,
            fg_color=self.colors["button"],
            hover_color=self.colors["button_hover"],
            width=166,
        ).pack(side=tk.LEFT, padx=(8, 0))

        ctk.CTkLabel(
            main,
            text="Windows uzun yol desteği",
            text_color=self.colors["text"],
            anchor="w",
        ).grid(row=4, column=0, sticky="w", padx=(18, 10), pady=(8, 6))
        self.long_paths_status_label = ctk.CTkLabel(
            main,
            textvariable=self.long_paths_status_text,
            text_color=self.colors["muted"],
            anchor="w",
        )
        self.long_paths_status_label.grid(
            row=4,
            column=1,
            columnspan=2,
            sticky="w",
            pady=(8, 6),
        )
        self.long_paths_button = ctk.CTkButton(
            main,
            text="Etkinleştir",
            command=self._enable_windows_long_paths,
            corner_radius=8,
            fg_color=self.colors["button"],
            hover_color=self.colors["button_hover"],
            width=136,
        )
        self.long_paths_button.grid(
            row=4,
            column=3,
            sticky="w",
            padx=(10, 18),
            pady=(8, 6),
        )

        ctk.CTkLabel(
            main,
            text="Nexus API anahtarı",
            text_color=self.colors["text"],
            anchor="w",
        ).grid(row=5, column=0, sticky="w", padx=(18, 10), pady=(12, 6))
        self.api_key_entry = ctk.CTkEntry(
            main,
            textvariable=self.api_key,
            show="*",
            corner_radius=8,
            fg_color=self.colors["panel_alt"],
            border_color=self.colors["line"],
        )
        self.api_key_entry.grid(row=5, column=1, sticky="ew", pady=(12, 6))
        api_actions = ctk.CTkFrame(main, fg_color="transparent")
        api_actions.grid(
            row=5,
            column=2,
            columnspan=2,
            sticky="w",
            padx=(10, 18),
            pady=(12, 6),
        )
        ctk.CTkButton(
            api_actions,
            text="Kaydet",
            command=self._save_api_key,
            corner_radius=8,
            fg_color=self.colors["button"],
            hover_color=self.colors["button_hover"],
            width=86,
        ).pack(side=tk.LEFT)
        ctk.CTkButton(
            api_actions,
            text="Sil",
            command=self._clear_api_key,
            corner_radius=8,
            fg_color=self.colors["button"],
            hover_color=("#fee2e2", "#4c1d1d"),
            text_color=self.colors["danger"],
            width=72,
        ).pack(side=tk.LEFT, padx=(8, 0))
        ctk.CTkButton(
            api_actions,
            text="API anahtarı sayfasını aç",
            command=self._open_api_key_page,
            corner_radius=8,
            fg_color=self.colors["button"],
            hover_color=self.colors["button_hover"],
            width=190,
        ).pack(side=tk.LEFT, padx=(8, 0))
        self.auth_status_label = ctk.CTkLabel(
            main,
            textvariable=self.auth_status_text,
            text_color=self.colors["muted"],
            font=ctk.CTkFont(family="Segoe UI", size=12),
            anchor="w",
        )
        self.auth_status_label.grid(
            row=6,
            column=1,
            columnspan=3,
            sticky="w",
            pady=(0, 6),
        )

        ctk.CTkLabel(
            main,
            text="API kullanım durumu",
            text_color=self.colors["text"],
            anchor="w",
        ).grid(row=7, column=0, sticky="w", padx=(18, 10), pady=(6, 8))
        api_usage_frame = ctk.CTkFrame(
            main,
            fg_color=self.colors["panel_alt"],
            corner_radius=9,
            border_width=1,
            border_color=self.colors["line"],
        )
        api_usage_frame.grid(
            row=7,
            column=1,
            columnspan=3,
            sticky="ew",
            padx=(0, 18),
            pady=(6, 8),
        )
        api_usage_frame.columnconfigure(0, weight=1)
        self.api_usage_label = ctk.CTkLabel(
            api_usage_frame,
            textvariable=self.api_usage_text,
            text_color=self.colors["muted"],
            anchor="w",
            justify=tk.LEFT,
            wraplength=880,
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
        )
        self.api_usage_label.grid(row=0, column=0, sticky="ew", padx=12, pady=9)
        self.api_usage_refresh_button = ctk.CTkButton(
            api_usage_frame,
            text="Yenile",
            command=self._refresh_api_usage,
            corner_radius=8,
            fg_color=self.colors["button"],
            hover_color=self.colors["button_hover"],
            width=76,
            height=30,
        )
        self.api_usage_refresh_button.grid(row=0, column=1, sticky="e", padx=(6, 9), pady=6)

        ctk.CTkLabel(
            main,
            text="İndirme yöntemi",
            text_color=self.colors["text"],
            anchor="w",
        ).grid(row=8, column=0, sticky="w", padx=(18, 10), pady=(12, 8))
        methods = ctk.CTkFrame(main, fg_color="transparent")
        methods.grid(row=8, column=1, columnspan=3, sticky="w", pady=(12, 8))
        ctk.CTkRadioButton(
            methods,
            text=PREMIUM_DELIVERY_LABEL,
            value=PREMIUM_DELIVERY_LABEL,
            variable=self.delivery_mode,
            command=self._on_delivery_mode_changed,
            text_color=self.colors["premium"],
            fg_color=self.colors["premium"],
            hover_color="#ffad4d",
            border_color=self.colors["premium"],
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
        ).pack(side=tk.LEFT, padx=(0, 18))
        ctk.CTkRadioButton(
            methods,
            text=NON_PREMIUM_DELIVERY_LABEL,
            value=NON_PREMIUM_DELIVERY_LABEL,
            variable=self.delivery_mode,
            command=self._on_delivery_mode_changed,
            text_color=self.colors["free"],
            fg_color=self.colors["free"],
            hover_color="#8cf7cf",
            border_color=self.colors["free"],
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
        ).pack(side=tk.LEFT, padx=(0, 16))
        self.discord_support_icon = self._load_discord_support_icon()
        support = ctk.CTkButton(
            methods,
            text="Download Destek: c0kadam",
            image=self.discord_support_icon,
            command=self._open_discord_support,
            compound="left",
            text_color=self.colors["link"],
            cursor="hand2",
            font=ctk.CTkFont(family="Segoe UI", size=13, underline=True),
            fg_color="transparent",
            hover_color=self.colors["panel_alt"],
            corner_radius=7,
            height=28,
            width=232,
            anchor="w",
        )
        support.pack(side=tk.LEFT)
        ctk.CTkButton(
            methods,
            text="/ Negatrm Discord",
            command=self._open_negatrm_discord,
            text_color=self.colors["link"],
            cursor="hand2",
            font=ctk.CTkFont(family="Segoe UI", size=13, underline=True),
            fg_color="transparent",
            hover_color=self.colors["panel_alt"],
            corner_radius=7,
            height=28,
            width=142,
            anchor="w",
        ).pack(side=tk.LEFT, padx=(4, 0))

        self.nxm_frame = ctk.CTkFrame(main, fg_color="transparent")
        self.nxm_frame.grid(row=9, column=0, columnspan=4, sticky="ew", padx=18, pady=(6, 16))
        self.nxm_frame.columnconfigure(1, weight=1)
        ctk.CTkLabel(
            self.nxm_frame,
            textvariable=self.nxm_status_text,
            text_color=self.colors["muted"],
            font=ctk.CTkFont(family="Segoe UI", size=12),
            anchor="w",
            wraplength=820,
        ).grid(row=0, column=0, columnspan=4, sticky="w")
        self.nxm_link_entry = ctk.CTkEntry(self.nxm_frame, textvariable=self.nxm_link)
        self.nxm_submit_button = ctk.CTkButton(
            self.nxm_frame,
            text="Linki kullan",
            command=self._submit_nxm_link,
            state=tk.DISABLED,
            corner_radius=8,
        )
        self.nxm_clipboard_button = ctk.CTkButton(
            self.nxm_frame,
            text="Panodan al",
            command=self._submit_nxm_from_clipboard,
            state=tk.DISABLED,
            corner_radius=8,
        )
        ctk.CTkCheckBox(
            self.nxm_frame,
            text="Sonraki dosyayı otomatik aç",
            variable=self.auto_open_next_nxm,
            text_color=self.colors["text"],
            fg_color=self.colors["accent"],
            hover_color=self.colors["accent_hover"],
            border_color=self.colors["line"],
            corner_radius=6,
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(8, 0))

        actions = ctk.CTkFrame(
            parent,
            fg_color=self.colors["panel"],
            corner_radius=14,
            border_width=1,
            border_color=self.colors["line"],
        )
        actions.grid(row=3, column=0, sticky="ew", padx=18, pady=(0, 16))
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=1)
        self.status_label_default_font = ctk.CTkFont(family="Segoe UI", size=13)
        self.status_label_prominent_font = ctk.CTkFont(family="Segoe UI", size=20, weight="bold")
        self.status_label = ctk.CTkLabel(
            actions,
            textvariable=self.status_text,
            text_color=self.colors["text"],
            anchor="w",
            font=self.status_label_default_font,
        )
        self.status_label.grid(row=0, column=0, sticky="w", padx=18, pady=(16, 6))
        action_tools = ctk.CTkFrame(actions, fg_color="transparent")
        action_tools.grid(row=0, column=1, sticky="e", padx=18, pady=(16, 6))
        self.download_cache_button = ctk.CTkButton(
            action_tools,
            text="İndirme arşivleri",
            command=self._show_download_cache_manager,
            corner_radius=8,
            fg_color=self.colors["button"],
            hover_color=self.colors["button_hover"],
            width=142,
        )
        self.download_cache_button.pack(side=tk.LEFT, padx=(0, 8))
        self.details_button = ctk.CTkButton(
            action_tools,
            text="Detayları göster",
            command=self._toggle_details,
            corner_radius=8,
            fg_color=self.colors["button"],
            hover_color=self.colors["button_hover"],
        )
        self.details_button.pack(side=tk.LEFT)
        progress_row = ctk.CTkFrame(actions, fg_color="transparent")
        progress_row.grid(row=1, column=0, columnspan=2, sticky="ew", padx=18, pady=(6, 2))
        progress_row.columnconfigure(0, weight=1)
        self.progress_bar = ctk.CTkProgressBar(
            progress_row,
            corner_radius=8,
            height=14,
            fg_color=self.colors["panel_alt"],
            progress_color=self.colors["accent"],
            mode="determinate",
        )
        self.progress_bar.grid(row=0, column=0, sticky="ew")
        self.progress_bar.set(0)
        ctk.CTkLabel(
            progress_row,
            textvariable=self.progress_percent,
            text_color=self.colors["muted"],
            width=54,
            anchor="e",
            font=ctk.CTkFont(family="Segoe UI", size=12),
        ).grid(row=0, column=1, sticky="e", padx=(10, 0))
        ctk.CTkLabel(
            actions,
            textvariable=self.progress_label,
            text_color=self.colors["muted"],
            anchor="w",
            font=ctk.CTkFont(family="Segoe UI", size=12),
        ).grid(row=2, column=0, sticky="w", padx=(18, 8))
        self.eta_label = ctk.CTkLabel(
            actions,
            textvariable=self.eta_text,
            text_color=self.colors["warning"],
            anchor="e",
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
        )
        self.eta_label.grid(row=2, column=1, sticky="e", padx=(8, 18))
        current_download = ctk.CTkFrame(
            actions,
            fg_color=self.colors["panel_alt"],
            corner_radius=10,
            border_width=1,
            border_color=self.colors["line"],
        )
        current_download.grid(
            row=3,
            column=0,
            columnspan=2,
            sticky="ew",
            padx=18,
            pady=(12, 0),
        )
        ctk.CTkLabel(
            current_download,
            text="İşlem bilgisi",
            text_color=self.colors["muted"],
            anchor="w",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
        ).pack(fill=tk.X, padx=12, pady=(8, 1))
        ctk.CTkLabel(
            current_download,
            textvariable=self.current_download_text,
            text_color=self.colors["text"],
            anchor="w",
            justify=tk.LEFT,
            wraplength=1060,
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
        ).pack(fill=tk.X, padx=12, pady=(0, 9))
        self.download_button = ctk.CTkButton(
            actions,
            text="Çevirileri indir",
            command=self._download_action_clicked,
            state=tk.DISABLED,
            corner_radius=10,
            fg_color=self.colors["button"],
            hover_color=self.colors["button_hover"],
            height=42,
        )
        self.download_button.grid(row=4, column=0, sticky="ew", padx=(18, 8), pady=(14, 0))
        self.prepare_button = ctk.CTkButton(
            actions,
            text="Çeviriyi hazırla",
            command=self._prepare_translation,
            state=tk.DISABLED,
            corner_radius=10,
            fg_color=self.colors["accent"],
            hover_color=self.colors["accent_hover"],
            height=42,
        )
        self.prepare_button.grid(row=4, column=1, sticky="ew", padx=(8, 18), pady=(14, 0))
        ctk.CTkLabel(
            actions,
            textvariable=self.download_status_text,
            text_color=self.colors["muted"],
            anchor="w",
            font=ctk.CTkFont(family="Segoe UI", size=12),
        ).grid(row=5, column=0, sticky="ew", padx=(18, 8), pady=(8, 16))
        self.prepare_status_label = ctk.CTkLabel(
            actions,
            textvariable=self.prepare_status_text,
            text_color=self.colors["muted"],
            anchor="w",
            font=ctk.CTkFont(family="Segoe UI", size=12),
        )
        self.prepare_status_label.grid(row=5, column=1, sticky="ew", padx=(8, 18), pady=(8, 16))
        self.details_frame = ctk.CTkFrame(actions, fg_color="transparent")
        self.details_frame.columnconfigure(0, weight=1)
        self.details_frame.rowconfigure(0, weight=1)
        self.log = ctk.CTkTextbox(
            self.details_frame,
            height=140,
            fg_color="#111318",
            text_color=self.colors["text"],
            border_width=1,
            border_color=self.colors["line"],
            corner_radius=10,
            font=ctk.CTkFont(family="Cascadia Mono", size=11),
            wrap=tk.WORD,
        )
        self.log.grid(row=0, column=0, sticky="nsew")

    def _browse_mo2_root(self) -> None:
        selected = filedialog.askdirectory(title="Modlist klasörü seç")
        if selected:
            self.mo2_root.set(selected)
            self._reset_for_profile_change()
            self._find_profiles()

    def _find_profiles(self) -> None:
        self._reset_for_profile_change()
        root = self.mo2_root.get().strip()
        profiles = discover_mo2_profiles(root)
        self.profile_combo.configure(values=profiles)
        if not profiles:
            self.mo2_profile.set("")
            self._set_profile_status("MO2 profili bulunamadı.", "danger")
            self._set_progress(0, "Profil bekleniyor")
            self._log("Profil arama: profil bulunamadı.")
            self._refresh_pipeline_buttons()
            return
        selected_profile = self._preferred_profile(profiles)
        self.mo2_profile.set(selected_profile)
        if hasattr(self.profile_combo, "set"):
            self.profile_combo.set(selected_profile)
        self.output_folder_text.set(self._output_folder_display())
        location = resolve_installer_mo2_location(root, selected_profile)
        if location is not None:
            self._log(
                f"MO2 yapısı: {location.layout_kind}; veri kökü: {location.data_root}; "
                f"mods: {location.mods_dir}."
            )
        self._set_profile_status(
            f"{len(profiles)} profil bulundu. {selected_profile} kontrol ediliyor."
        )
        self._log(f"Profil arama: {len(profiles)} profil bulundu; {selected_profile} seçildi.")
        self._scan_profile()

    def _on_profile_selected(self, _event: object | None = None) -> None:
        self._reset_for_profile_change()
        if self.mo2_root.get().strip() and self.mo2_profile.get().strip():
            self._scan_profile()

    def _preferred_profile(self, profiles: list[str]) -> str:
        modlist = self.manifest.get("modlist") if isinstance(self.manifest.get("modlist"), dict) else {}
        supported_profiles = [
            str(profile).strip()
            for profile in modlist.get("supported_profiles", [])
            if str(profile).strip()
        ]
        for supported in supported_profiles:
            for discovered in profiles:
                if discovered.casefold() == supported.casefold():
                    return discovered
        return profiles[0]

    def _scan_profile(self) -> None:
        mo2_root = self.mo2_root.get().strip()
        profile = self.mo2_profile.get().strip()
        if not mo2_root or not profile:
            self._set_profile_status("Modlist klasörü ve profil seçilmeli.", "danger")
            self._refresh_pipeline_buttons()
            return
        out_dir = self._run_workspace() / "profile-scan"

        def work() -> Any:
            return scan_profile(mo2_root, profile, out_dir)

        def done(result: Any) -> None:
            self.profile_scan_path = result.json_path
            self.output_folder_text.set(self._output_folder_display())
            summary = result.payload.get("summary", {})
            self._set_profile_status(
                f"Profil okundu: {summary.get('enabled_mod_count', 0)} mod, "
                f"{summary.get('active_plugin_count', 0)} plugin."
            )
            self._log(f"Profil tarama çıktısı: {result.json_path}")
            self._run_preflight()

        self._set_progress(10, "Profil kontrol ediliyor")
        self._run_task("Profil kontrol ediliyor", work, done)

    def _run_preflight(self) -> None:
        if not self.profile_scan_path:
            return

        def work() -> dict[str, Any]:
            profile = json.loads(self.profile_scan_path.read_text(encoding="utf-8"))
            return build_wizard_preflight(
                self.manifest,
                profile,
                delivery_mode=self._delivery_mode_value(),
            )

        def done(preflight: dict[str, Any]) -> None:
            self.preflight_payload = preflight
            self._reset_download_state()
            self._write_preflight_snapshot(preflight)
            summary = preflight_summary(preflight)
            exact_match = self._profile_exact_match_for(preflight)
            if preflight.get("status") == "READY":
                warning_count = int(summary["compatibility_warnings"])
                warning_suffix = f", {warning_count} uyarı" if warning_count else ""
                if exact_match:
                    self._set_profile_status(
                        f"Profil hazır: {summary['matched_entries']}/"
                        f"{summary['manifest_entries']} çeviri eşleşti{warning_suffix}.",
                        "success",
                    )
                    self._set_status(
                        "Profil hazır, küçük uyumluluk uyarıları var."
                        if warning_count
                        else "Profil hazır.",
                        "success",
                    )
                    self._set_progress(30, "Profil hazır")
                else:
                    self._set_profile_status(
                        f"Profil hazır, stok paketle birebir aynı değil: {summary['matched_entries']}/"
                        f"{summary['manifest_entries']} çeviri eşleşti{warning_suffix}. "
                        "Devam etmek için onay istenecek.",
                        "warning",
                    )
                    self._set_status(
                        "Profil hazır; stok paketten farklı olduğu için devam etmeden önce onay istenecek.",
                        "warning",
                    )
                    self._set_progress(25, "Profil onay bekliyor")
            else:
                self._set_profile_status(
                    f"Profil kontrol edilmeli: {summary['missing_entries']} eksik, "
                    f"{summary['compatibility_warnings']} uyarı.",
                    "danger",
                )
                self._set_status("Profil bu paketle tam uyumlu görünmüyor.", "danger")
                self._set_progress(20, "Profil kontrol edilmeli")
            readable_status = "Hazır" if summary["status"] == "READY" else "Kontrol gerekli"
            self._log(f"Profil uyumluluk durumu: {readable_status}")
            self._refresh_pipeline_buttons()

        self._set_progress(18, "Profil kontrol ediliyor")
        self._run_task("Profil kontrol ediliyor", work, done)

    def _write_preflight_snapshot(self, preflight: dict[str, Any]) -> None:
        runtime_dir = self._run_workspace() / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        (runtime_dir / "wizard_preflight.json").write_text(
            json.dumps(preflight, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def _save_api_key(self) -> None:
        try:
            store_manual_api_key(
                self.store,
                app_id=self.app_id,
                api_key=self.api_key.get(),
            )
        except Exception as exc:  # noqa: BLE001 - GUI boundary reports sanitized text.
            self._notify(
                "API anahtarı kaydedilemedi",
                str(exc),
                tone="danger",
                action_label="API alanına dön",
            )
            return
        self.api_key.set("")
        self.premium_api_validated = False
        self._log("Nexus API anahtarı kaydedildi.")
        self._refresh_auth_status()
        self._refresh_api_usage()

    def _show_api_required(self, purpose: str = "Bu işleme devam etmek") -> None:
        self.auth_status_text.set("Devam etmek için Nexus API anahtarınızı kaydedin.")
        if hasattr(self, "auth_status_label"):
            self.auth_status_label.configure(text_color=self.colors["warning"])
        self._toast(
            "Nexus API anahtarı gerekli",
            f"{purpose} için Nexus API anahtarınızı girip Kaydet düğmesine basın.",
            tone="warning",
        )
        if hasattr(self, "api_key_entry"):
            self.api_key_entry.focus_set()

    def _clear_api_key(self) -> None:
        if not self._api_key():
            self._cancel_endorsement_auto_retry()
            self.api_key.set("")
            self.api_usage_text.set("Resmî Nexus API kotası için API anahtarınızı kaydedin.")
            if hasattr(self, "api_usage_label"):
                self.api_usage_label.configure(text_color=self.colors["muted"])
            self._refresh_auth_status()
            return
        confirmed = self._confirm(
            "API anahtarını sil",
            "Kaydedilmiş Nexus API anahtarı bu bilgisayardan silinsin mi?",
            action_label="Anahtarı sil",
            tone="warning",
        )
        if not confirmed:
            return
        try:
            clear_api_key(self.store, app_id=self.app_id)
        except Exception as exc:  # noqa: BLE001 - GUI boundary reports sanitized text.
            self._notify(
                "API anahtarı silinemedi",
                str(exc),
                tone="danger",
            )
            return
        self.api_key.set("")
        self.premium_api_validated = False
        self._cancel_endorsement_auto_retry()
        self.api_usage_text.set("Resmî Nexus API kotası için API anahtarınızı kaydedin.")
        if hasattr(self, "api_usage_label"):
            self.api_usage_label.configure(text_color=self.colors["muted"])
        self._log("Kaydedilmiş Nexus API anahtarı silindi.")
        self._refresh_auth_status()

    def _open_api_key_page(self) -> None:
        webbrowser.open(api_settings_url(), new=2, autoraise=True)

    def _open_discord_support(self) -> None:
        webbrowser.open(C0KADAM_DISCORD_SUPPORT_URL, new=2, autoraise=True)

    def _open_negatrm_discord(self) -> None:
        webbrowser.open(NEGATRM_DISCORD_SUPPORT_URL, new=2, autoraise=True)

    def _endorsement_buttons(self) -> tuple[ctk.CTkButton, ...]:
        buttons: list[ctk.CTkButton] = []
        for button in (
            self.endorsement_button,
            self.completion_endorsement_button,
        ):
            if button is not None and button.winfo_exists():
                buttons.append(button)
        return tuple(buttons)

    def _sync_endorsement_buttons(self) -> None:
        text, enabled = _endorsement_button_presentation(
            len(self.endorsement_targets),
            busy=self.endorsement_busy,
        )
        for button in self._endorsement_buttons():
            button.configure(
                text=text,
                state=(tk.NORMAL if enabled else tk.DISABLED),
                fg_color=self.colors["premium"],
                hover_color="#ffad4d",
            )

    def _cancel_endorsement_auto_retry(self) -> None:
        if self._endorsement_retry_after is not None:
            try:
                self.root.after_cancel(self._endorsement_retry_after)
            except tk.TclError:
                pass
        self._endorsement_retry_after = None
        self._endorsement_retry_due_at = 0.0
        self._endorsement_retry_targets = ()

    def _schedule_endorsement_auto_retry(
        self,
        targets: tuple[ReleaseEndorsementTarget, ...],
    ) -> bool:
        self._cancel_endorsement_auto_retry()
        if not targets:
            return False
        self._endorsement_retry_targets = targets
        self._endorsement_retry_due_at = time.monotonic() + ENDORSEMENT_AUTO_RETRY_SECONDS
        self._update_endorsement_auto_retry()
        return True

    def _update_endorsement_auto_retry(self) -> None:
        self._endorsement_retry_after = None
        targets = self._endorsement_retry_targets
        if not targets:
            return
        remaining = max(0, ceil(self._endorsement_retry_due_at - time.monotonic()))
        if remaining > 0:
            minutes, seconds = divmod(remaining, 60)
            self.endorsement_status_text.set(
                f"{len(targets)} sayfa {minutes:02d}:{seconds:02d} sonra otomatik "
                "yeniden denenecek. Aracı açık bırakabilirsiniz."
            )
            self._endorsement_retry_after = self.root.after(
                1000,
                self._update_endorsement_auto_retry,
            )
            return
        if self.busy or self.endorsement_busy:
            self._endorsement_retry_due_at = (
                time.monotonic() + ENDORSEMENT_BUSY_RETRY_SECONDS
            )
            self.endorsement_status_text.set(
                "Otomatik beğeni denemesi devam eden işlem tamamlanınca başlayacak."
            )
            self._endorsement_retry_after = self.root.after(
                1000,
                self._update_endorsement_auto_retry,
            )
            return
        api_key = self._api_key()
        if not api_key:
            self._cancel_endorsement_auto_retry()
            self.endorsement_status_text.set(
                "Otomatik beğeni denemesi için kayıtlı Nexus API anahtarı bulunamadı."
            )
            return
        self._endorsement_retry_after = None
        self._endorsement_retry_due_at = 0.0
        self._endorsement_retry_targets = ()
        self._start_endorsement_attempt(
            api_key,
            targets,
            automatic_retry=True,
        )

    def _endorse_release(self, *, parent: tk.Misc | None = None) -> None:
        targets = self.endorsement_targets
        if not targets or self.endorsement_busy:
            return
        api_key = self._api_key()
        if not api_key:
            if parent is not None and parent.winfo_exists():
                self._notify(
                    "Nexus API anahtarı gerekli",
                    (
                        "Çeviri sayfalarını beğenmek için Nexus API anahtarınızı "
                        "ana ekrandaki alana girip Kaydet düğmesine basın."
                    ),
                    tone="warning",
                    action_label="Anladım",
                    parent=parent,
                )
            else:
                self._show_api_required("Çeviri sayfalarını beğenmek")
            return

        if not self._confirm(
            "Çevirileri beğen / endorse et",
            (
                f"Bu işlem {len(targets)} tekil Nexus çeviri sayfasına "
                "hesabınızla endorse göndermeyi deneyecek.\n\n"
                "Bu bir bağış veya ödeme işlemi değildir.\n\n"
                "Nexus kuralları gereği yalnızca indirilmiş ve üzerinden "
                "en az 15 dakika geçmiş dosyalar kabul edilir. Bekleme süresine "
                "takılan sayfalar, araç açık kalırsa 15 dakika sonra otomatik "
                "olarak yeniden denenecek.\n\n"
                "İşlem arka planda sürer; aracı arka plana alıp oyuna "
                "başlayabilirsiniz. Devam edilsin mi?"
            ),
            action_label="Beğenileri gönder",
            cancel_label="Şimdi değil",
            parent=parent,
        ):
            return

        self._cancel_endorsement_auto_retry()
        self._start_endorsement_attempt(api_key, targets, automatic_retry=False)

    def _start_endorsement_attempt(
        self,
        api_key: str,
        targets: tuple[ReleaseEndorsementTarget, ...],
        *,
        automatic_retry: bool,
    ) -> None:
        self.endorsement_busy = True
        self._sync_endorsement_buttons()
        self.endorsement_status_text.set(
            "15 dakika bekleyen sayfalar otomatik yeniden deneniyor."
            if automatic_retry
            else "Beğeniler gönderiliyor; işlem arka planda devam ediyor."
        )

        def work() -> dict[str, object]:
            def progress(
                done_count: int,
                total_count: int,
                target: ReleaseEndorsementTarget,
                status: str,
                message: str,
            ) -> None:
                self.task_queue.put(
                    (
                        "endorsement_progress",
                        {
                            "done": done_count,
                            "total": total_count,
                            "target": target,
                            "status": status,
                            "message": message,
                        },
                    )
                )

            try:
                return {
                    "result": endorse_manifest_targets(
                        api_key,
                        targets,
                        progress_callback=progress,
                    )
                }
            except Exception as exc:  # noqa: BLE001 - converted to a user-facing GUI result.
                return {"error": exc}

        def done(payload: dict[str, object]) -> None:
            notification_parent = (
                self.completion_popup
                if self.completion_popup is not None
                and self.completion_popup.winfo_exists()
                else None
            )
            result = payload.get("result")
            if isinstance(result, BulkEndorsementSummary):
                self.endorsement_targets = merge_remaining_endorsement_targets(
                    self.endorsement_targets,
                    targets,
                    result,
                )
                wait_targets = wait_required_endorsement_targets(result)
                auto_retry_scheduled = (
                    not automatic_retry
                    and self._schedule_endorsement_auto_retry(wait_targets)
                )
                self._sync_endorsement_buttons()
                self._log(_bulk_endorsement_log_summary(result))
                if automatic_retry:
                    if result.wait_required:
                        self.endorsement_status_text.set(
                            f"Otomatik deneme tamamlandı; {result.wait_required} sayfa "
                            "hâlâ bekleme süresinde. Daha sonra elle deneyebilirsiniz."
                        )
                    elif self.endorsement_targets:
                        self.endorsement_status_text.set(
                            f"Otomatik deneme tamamlandı; {len(self.endorsement_targets)} "
                            "sayfa elle yeniden denenebilir."
                        )
                    else:
                        self.endorsement_status_text.set(
                            "Otomatik beğeni denemesi tamamlandı."
                        )
                    return
                if not auto_retry_scheduled:
                    if self.endorsement_targets:
                        self.endorsement_status_text.set(
                            f"Beğeni işlemi tamamlandı: {result.completed}/{result.total}; "
                            f"{len(self.endorsement_targets)} sayfa yeniden denenebilir."
                        )
                    else:
                        self.endorsement_status_text.set(
                            f"Beğeni işlemi tamamlandı: {result.completed}/{result.total}."
                        )
                self._notify(
                    "Çeviriler beğenildi",
                    _bulk_endorsement_user_message(
                        result,
                        auto_retry_scheduled=auto_retry_scheduled,
                    ),
                    tone=("success" if not self.endorsement_targets else "info"),
                    action_label="Tamam",
                    parent=notification_parent,
                )
                return

            error = payload.get("error")
            message = (
                str(error)
                if isinstance(error, NexusEndorsementError)
                else "Nexus endorse işlemi tamamlanamadı. Daha sonra tekrar deneyin."
            )
            self._sync_endorsement_buttons()
            self.endorsement_status_text.set(
                "Otomatik beğeni denemesi tamamlanamadı; daha sonra elle deneyebilirsiniz."
                if automatic_retry
                else "Beğeniler gönderilemedi; tekrar deneyebilirsiniz."
            )
            self._log(f"Nexus endorsement başarısız: {message}")
            if automatic_retry:
                return
            self._notify(
                "Beğeni işlemi tamamlanamadı",
                message,
                tone="danger",
                action_label="Daha sonra dene",
                parent=notification_parent,
            )

        self._run_auxiliary_task("endorsement_done", work, done)

    def _refresh_api_usage(self, *, silent: bool = False) -> None:
        if self.api_usage_busy:
            return
        api_key = self._api_key()
        if not api_key:
            self.api_usage_text.set("Resmî Nexus API kotası için API anahtarınızı kaydedin.")
            if hasattr(self, "api_usage_label"):
                self.api_usage_label.configure(text_color=self.colors["muted"])
            return

        self.api_usage_busy = True
        self.api_usage_text.set("Nexus API kota bilgisi alınıyor...")
        if hasattr(self, "api_usage_refresh_button"):
            self.api_usage_refresh_button.configure(state=tk.DISABLED)

        def work() -> dict[str, object]:
            try:
                return {"rate_limit": fetch_official_api_usage(api_key)}
            except Exception as exc:  # noqa: BLE001 - auxiliary GUI boundary.
                return {"error": exc}

        def done(payload: dict[str, object]) -> None:
            if hasattr(self, "api_usage_refresh_button"):
                self.api_usage_refresh_button.configure(state=tk.NORMAL)
            if self._api_key() != api_key:
                self.api_usage_text.set(
                    "Resmî Nexus API kotası için API anahtarınızı kaydedin."
                )
                if hasattr(self, "api_usage_label"):
                    self.api_usage_label.configure(text_color=self.colors["muted"])
                return
            rate_limit = payload.get("rate_limit")
            if isinstance(rate_limit, NexusRateLimit):
                self.api_usage_text.set(_format_nexus_api_usage(rate_limit))
                if hasattr(self, "api_usage_label"):
                    self.api_usage_label.configure(text_color=self.colors["success"])
                self._log("Resmî Nexus API kota bilgisi güncellendi.")
                return

            self.api_usage_text.set("Nexus API kota bilgisi şu anda alınamadı.")
            if hasattr(self, "api_usage_label"):
                self.api_usage_label.configure(text_color=self.colors["warning"])
            if not silent:
                error = payload.get("error")
                self._log(f"Nexus API kota bilgisi alınamadı: {error}")

        self._run_auxiliary_task("api_usage_done", work, done)

    @staticmethod
    def _load_discord_support_icon() -> ctk.CTkImage | None:
        try:
            asset = files("modlist_translation_wizard").joinpath(
                "resources", "assets", "discord-seeklogo.png"
            )
            image = Image.open(BytesIO(asset.read_bytes())).convert("RGBA")
        except (FileNotFoundError, OSError, UnidentifiedImageError):
            return None
        return ctk.CTkImage(light_image=image, dark_image=image, size=(42, 21))

    def _on_appearance_changed(self, value: str) -> None:
        ctk.set_appearance_mode("light" if value == "Açık" else "dark")

    def _open_output_folder(self) -> None:
        path = self._current_output_folder()
        if path is None:
            self._toast(
                "Çıktı klasörü",
                "Modlist klasörü seçilince çıktı konumu otomatik belirlenecek.",
                tone="info",
            )
            return
        if not path.exists():
            self._toast(
                "Çıktı klasörü henüz oluşturulmadı",
                f"Çeviri hazırlanınca bu klasör oluşacak:\n{path}",
                tone="info",
            )
            return
        webbrowser.open(path.resolve().as_uri(), new=2, autoraise=True)

    def _open_app_data_folder(self) -> None:
        path = default_workspace_root()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._notify(
                "Uygulama verileri açılamadı",
                f"Uygulama veri klasörü oluşturulamadı:\n{path}\n\n{exc}",
                tone="danger",
            )
            return
        webbrowser.open(path.resolve().as_uri(), new=2, autoraise=True)

    def _show_download_cache_manager(self) -> None:
        if self.busy:
            self._toast(
                "İşlem hâlâ devam ediyor",
                "İndirme arşivleri devam eden işlem tamamlandıktan sonra temizlenebilir.",
                tone="info",
            )
            return

        current = inspect_download_cache(self.manifest, scope="current")
        all_runs = inspect_download_cache(self.manifest, scope="all")
        if self.download_cache_popup is not None and self.download_cache_popup.winfo_exists():
            self.download_cache_popup.destroy()

        popup = ctk.CTkToplevel(self.root)
        self.download_cache_popup = popup
        popup.title("İndirme arşivleri")
        popup.geometry("640x390")
        popup.minsize(590, 360)
        popup.transient(self.root)
        popup.configure(fg_color=self.colors["bg"])
        popup.columnconfigure(0, weight=1)

        ctk.CTkLabel(
            popup,
            text="İndirme önbelleğini yönet",
            text_color=self.colors["text"],
            anchor="w",
            font=ctk.CTkFont(family="Segoe UI", size=20, weight="bold"),
        ).grid(row=0, column=0, sticky="ew", padx=22, pady=(22, 6))
        ctk.CTkLabel(
            popup,
            text=(
                "Yalnızca indirilen ZIP, 7Z, RAR arşivleri ve yarım kalmış indirmeler "
                "silinir. Manifestler, loglar ve hazırlanmış çeviri klasörleri korunur."
            ),
            text_color=self.colors["muted"],
            anchor="w",
            justify=tk.LEFT,
            wraplength=590,
        ).grid(row=1, column=0, sticky="ew", padx=22, pady=(0, 14))

        self._build_download_cache_scope_card(
            popup,
            row=2,
            title="Bu çeviri paketi",
            detail="Yalnızca açık olan modlist ve paket sürümünün arşivlerini siler.",
            summary=current,
        )
        self._build_download_cache_scope_card(
            popup,
            row=3,
            title="Tüm çeviri paketleri",
            detail="Bu bilgisayarda çeviri aracıyla indirilen bütün paket arşivlerini siler.",
            summary=all_runs,
        )
        ctk.CTkButton(
            popup,
            text="Kapat",
            command=popup.destroy,
            corner_radius=8,
            fg_color=self.colors["button"],
            hover_color=self.colors["button_hover"],
            width=110,
        ).grid(row=4, column=0, sticky="e", padx=22, pady=(14, 20))
        popup.lift()
        popup.focus_force()

    def _build_download_cache_scope_card(
        self,
        popup: ctk.CTkToplevel,
        *,
        row: int,
        title: str,
        detail: str,
        summary: DownloadCacheSummary,
    ) -> None:
        card = ctk.CTkFrame(
            popup,
            fg_color=self.colors["panel"],
            corner_radius=10,
            border_width=1,
            border_color=self.colors["line"],
        )
        card.grid(row=row, column=0, sticky="ew", padx=22, pady=5)
        card.columnconfigure(0, weight=1)
        ctk.CTkLabel(
            card,
            text=title,
            text_color=self.colors["text"],
            anchor="w",
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=14, pady=(11, 1))
        ctk.CTkLabel(
            card,
            text=(
                f"{detail}\n{summary.file_count} arşiv, "
                f"{format_cache_size(summary.total_bytes)}"
            ),
            text_color=self.colors["muted"],
            anchor="w",
            justify=tk.LEFT,
        ).grid(row=1, column=0, sticky="w", padx=14, pady=(0, 11))
        ctk.CTkButton(
            card,
            text="Temizle",
            command=lambda: self._confirm_clear_download_cache(summary),
            state=tk.NORMAL,
            corner_radius=8,
            fg_color=self.colors["button"],
            hover_color=("#fee2e2", "#4c1d1d"),
            text_color=self.colors["danger"],
            width=104,
        ).grid(row=0, column=1, rowspan=2, sticky="e", padx=14, pady=12)

    def _confirm_clear_download_cache(self, summary: DownloadCacheSummary) -> None:
        scope_label = "bu çeviri paketinin" if summary.scope == "current" else "tüm paketlerin"
        confirmed = self._confirm(
            "İndirme arşivlerini temizle",
            f"{scope_label.capitalize()} {summary.file_count} indirme arşivi "
            f"({format_cache_size(summary.total_bytes)}) silinecek.\n\n"
            "Hazırlanmış çeviri çıktıları silinmez. Bu dosyalar daha sonra yeniden "
            "indirilmek zorunda kalabilir. Devam edilsin mi?",
            action_label="Arşivleri temizle",
            tone="warning",
            parent=self.download_cache_popup or self.root,
        )
        if not confirmed:
            return
        if self.download_cache_popup is not None and self.download_cache_popup.winfo_exists():
            self.download_cache_popup.destroy()
        self.download_cache_popup = None

        def work() -> DownloadCacheClearResult:
            return clear_download_cache(self.manifest, scope=summary.scope)

        def done(result: DownloadCacheClearResult) -> None:
            self.use_manifest_download_cache_roots = False
            self._reset_download_state()
            self._set_progress(0, "İndirme planı yeniden hazırlanacak")
            self._set_status("İndirme arşivleri temizlendi.", "success")
            self._log(
                f"İndirme arşivleri temizlendi: {result.deleted_files} dosya, "
                f"{format_cache_size(result.deleted_bytes)}."
            )
            if result.failures:
                self._log("Silinemeyen arşivler:")
                for failure in result.failures:
                    self._log(f"- {failure}")
                self._notify(
                    "Temizlik kısmen tamamlandı",
                    f"{result.deleted_files} arşiv silindi. "
                    f"{len(result.failures)} dosya silinemedi; ayrıntıları kontrol edin.",
                    tone="warning",
                    action_label="Detayları kontrol et",
                )
                return
            self._toast(
                "İndirme arşivleri temizlendi",
                f"{result.deleted_files} arşiv silindi ve "
                f"{format_cache_size(result.deleted_bytes)} alan boşaltıldı.\n\n"
                "Bu oturumdaki sonraki işlem dosyaları yeniden indirecek.",
                tone="success",
                duration_ms=8500,
            )

        self._run_task("İndirme arşivleri temizleniyor", work, done)

    def _refresh_windows_long_paths_status(self) -> None:
        status = windows_long_path_status()
        if not status.available:
            self.long_paths_status_text.set("Bu ayar yalnızca Windows üzerinde kullanılabilir.")
            self.long_paths_status_label.configure(text_color=self.colors["muted"])
            self.long_paths_button.configure(text="Kullanılamıyor", state=tk.DISABLED)
            return
        if status.enabled:
            self.long_paths_status_text.set("Etkin. Uzun çıktı yolları destekleniyor.")
            self.long_paths_status_label.configure(text_color=self.colors["success"])
            self.long_paths_button.configure(text="Etkin", state=tk.DISABLED)
            return
        if status.error:
            self.long_paths_status_text.set("Durum okunamadı; yönetici onayıyla yeniden deneyebilirsiniz.")
        else:
            self.long_paths_status_text.set("Kapalı. Uzun plugin adlarında dönüştürme durabilir.")
        self.long_paths_status_label.configure(text_color=self.colors["warning"])
        self.long_paths_button.configure(text="Etkinleştir", state=tk.NORMAL)

    def _enable_windows_long_paths(self) -> None:
        if windows_long_path_status().enabled:
            self._refresh_windows_long_paths_status()
            return
        confirmed = self._confirm(
            "Windows uzun yol desteği",
            "Windows uzun yol desteği etkinleştirilsin mi?\n\n"
            "Bu işlem LongPathsEnabled kayıt değerini 1 yapar ve Windows yönetici onayı ister. "
            "Değişikliğin tüm uygulamalarda geçerli olması için bilgisayarı yeniden başlatmanız önerilir.",
            action_label="Windows ayarını etkinleştir",
            tone="warning",
        )
        if not confirmed:
            return

        def done(result: WindowsLongPathEnableResult) -> None:
            self._refresh_windows_long_paths_status()
            if result.cancelled:
                self._set_status("Windows uzun yol ayarı değiştirilmedi.", "warning")
                self._log("Windows uzun yol desteği için yönetici onayı verilmedi.")
                return
            self._set_status("Windows uzun yol desteği etkin.", "success")
            self._log("Windows uzun yol desteği etkinleştirildi.")
            self._notify(
                "Windows uzun yol desteği etkin",
                "Uzun yol desteği etkinleştirildi.\n\n"
                "Değişikliğin bütün uygulamalarda geçerli olması için bilgisayarı yeniden başlatmanız önerilir.",
                tone="success",
                action_label="Anladım",
            )

        self._run_task(
            "Windows uzun yol desteği etkinleştiriliyor",
            enable_windows_long_paths,
            done,
        )

    def _refresh_auth_status(self) -> None:
        try:
            status = api_key_status(self.store, app_id=self.app_id)
        except Exception as exc:  # noqa: BLE001 - GUI boundary reports sanitized text.
            self.auth_status_text.set(str(exc))
            label = getattr(self, "auth_status_label", None)
            if label is not None:
                label.configure(text_color=self.colors["danger"])
            return
        notice, tone = api_key_notice(
            has_api_key=status.has_api_key,
            delivery_mode=self._delivery_mode_value(),
        )
        self.auth_status_text.set(notice)
        label = getattr(self, "auth_status_label", None)
        if label is not None:
            label.configure(text_color=self.colors[tone])
        self._refresh_pipeline_buttons()

    def _set_profile_status(self, text: str, level: str = "muted") -> None:
        self.profile_status_text.set(text)
        label = getattr(self, "profile_status_label", None)
        if label is None:
            return
        color = {
            "success": self.colors["success"],
            "warning": self.colors["warning"],
            "danger": self.colors["danger"],
        }.get(level, self.colors["muted"])
        label.configure(text_color=color)

    def _set_status(self, text: str, level: str = "normal", *, prominent: bool = False) -> None:
        self.status_text.set(text)
        label = getattr(self, "status_label", None)
        if label is None:
            return
        color = {
            "success": self.colors["success"],
            "warning": self.colors["warning"],
            "danger": self.colors["danger"],
        }.get(level, self.colors["text"])
        font = self.status_label_prominent_font if prominent else self.status_label_default_font
        label.configure(text_color=color, font=font)

    def _reset_prepare_status_style(self) -> None:
        label = getattr(self, "prepare_status_label", None)
        if label is None:
            return
        label.configure(
            text_color=self.colors["muted"],
            font=ctk.CTkFont(family="Segoe UI", size=12),
        )

    def _on_delivery_mode_changed(self) -> None:
        self._stop_nxm_capture()
        self._reset_download_state()
        self._refresh_auth_status()
        self._refresh_non_premium_prompt()
        self._log(f"İndirme yöntemi: {self.delivery_mode.get()}")
        if self.profile_scan_path is not None:
            self._run_preflight()

    def _download_action_clicked(self) -> None:
        if not self._profile_check_completed():
            self._toast(
                "Önce profilinizi hazırlayın",
                "Modlist klasörünü ve MO2 profilini seçin. Profil kontrolü tamamlandığında indirme açılacak.",
                tone="warning",
            )
            return
        if not self._ensure_profile_continue_confirmed():
            return
        if not self._api_key():
            self._show_api_required("Çevirileri indirmek")
            return
        if self.premium_plan_result is None:
            self._plan_downloads(auto_start=True)
            return
        self._download_files()

    def _plan_downloads(self, *, auto_start: bool = False) -> None:
        if not self.profile_scan_path or not self._ensure_profile_continue_confirmed():
            return
        api_key = self._api_key()
        if not api_key:
            self._show_api_required("İndirme listesini hazırlamak")
            return
        out_dir = self._run_workspace() / "runtime"
        download_dir = self._run_workspace() / "downloads"

        def work() -> Any:
            if self._delivery_mode_value() == "PREMIUM_API":
                self._ensure_premium_api_key(api_key)
            manifest_path = self._write_runtime_manifest()
            return plan_downloads_from_manifest(
                manifest_path=manifest_path,
                profile_scan_path=self.profile_scan_path,
                download_dir=download_dir,
                out_dir=out_dir,
                delivery_mode=self._delivery_mode_value(),
                api_key=api_key,
                allow_profile_drift=self.profile_override_accepted,
                use_manifest_download_cache_roots=self.use_manifest_download_cache_roots,
            )

        def done(result: Any) -> None:
            self.premium_plan_result = result
            self.premium_download_result = None
            self.non_premium_download_result = None
            self.conversion_result = None
            readiness = download_queue_readiness(self.manifest, result.download_plan.queue_payload)
            if readiness["complete"]:
                self.download_status_text.set("Tüm gerekli dosyalar hazır.")
                self._set_status("İndirme hazır.")
                self._set_progress(75, "Çeviri hazırlanabilir")
            else:
                self.download_status_text.set(
                    f"{readiness['missing_count']} dosya indirilecek; "
                    f"{readiness['available_count']} dosya hazır."
                )
                self._set_status("İndirme hazırlandı.")
                self._set_progress(45, "İndirme hazırlanıyor")
            self._log(f"İndirme hazırlık dosyası: {result.download_plan.queue_path}")
            self._refresh_non_premium_prompt()
            self._refresh_pipeline_buttons()
            if auto_start and not readiness["complete"]:
                self._download_files()

        self._set_progress(40, "İndirme hazırlanıyor")
        self._run_task("İndirme hazırlanıyor", work, done)

    def _download_files(self) -> None:
        if self.premium_plan_result is None:
            self._plan_downloads(auto_start=True)
            return
        api_key = self._api_key()
        if not api_key:
            self._show_api_required("Çeviri dosyalarını indirmek")
            return
        if self._delivery_mode_value() == "NON_PREMIUM_NXM":
            self._open_next_non_premium_page()
            return
        queue_path = self._current_download_queue_path()
        queue_payload = json.loads(queue_path.read_text(encoding="utf-8"))
        readiness = download_queue_readiness(self.manifest, queue_payload)
        download_lookup = _download_item_lookup(queue_payload)

        def work() -> Any:
            self._ensure_premium_api_key(api_key)
            download_total = int(readiness.get("missing_count") or 0)
            attempts: dict[tuple[str, str], int] = {}
            completed: set[tuple[str, str]] = set()

            def tracked_downloader(url: str, part_path: Path) -> int:
                item = _download_item_for_part_path(part_path, download_lookup)
                identity = (
                    str(item.get("translation_nexus_mod_id") or "?"),
                    str(item.get("translation_file_id") or "?"),
                )
                attempts[identity] = attempts.get(identity, 0) + 1
                self.task_queue.put(
                    (
                        "download_progress",
                        {
                            **item,
                            "stage": "started",
                            "attempt": attempts[identity],
                            "completed": len(completed),
                            "total": download_total,
                        },
                    )
                )
                try:
                    bytes_written = urllib_download_to_file(url, part_path)
                except Exception as exc:
                    self.task_queue.put(
                        (
                            "download_progress",
                            {
                                **item,
                                "stage": "retry",
                                "attempt": attempts[identity],
                                "completed": len(completed),
                                "total": download_total,
                                "error_type": type(exc).__name__,
                            },
                        )
                    )
                    raise
                completed.add(identity)
                self.task_queue.put(
                    (
                        "download_progress",
                        {
                            **item,
                            "stage": "completed",
                            "attempt": attempts[identity],
                            "completed": len(completed),
                            "total": download_total,
                        },
                    )
                )
                return bytes_written

            return run_premium_downloads_from_plan(
                plan=self.premium_plan_result,
                api_key=api_key,
                queue_path=queue_path,
                file_downloader=tracked_downloader,
                max_attempts=2,
            )

        def done(result: Any) -> None:
            self.premium_download_result = result
            self.conversion_result = None
            readiness = download_queue_readiness(self.manifest, result.download_run.updated_queue_payload)
            run_summary = result.download_run.manifest_payload.get("summary", {})
            if readiness["complete"]:
                self.current_download_text.set("Tüm gerekli çeviri dosyaları hazır.")
                self.download_status_text.set(
                    f"Tamamlandi: {run_summary.get('downloaded', 0)} indirildi, "
                    f"{run_summary.get('already_present', 0)} zaten vardi."
                )
                self._set_status("Tüm dosyalar hazır.", "success")
                self._set_progress(75, "Çeviri hazırlanabilir")
            else:
                unavailable = unavailable_non_premium_downloads(
                    result.download_run.updated_queue_payload
                )
                premium_missing_count = int(readiness.get("missing_count") or 0)
                self.current_download_text.set(
                    f"İndirme tamamlandı; {len(unavailable)} dosya kullanıcı işlemi bekliyor."
                )
                if self._delivery_mode_value() != "NON_PREMIUM_NXM":
                    self.current_download_text.set(
                        f"İndirme tamamlandı; {premium_missing_count} dosya hâlâ eksik."
                    )
                self.download_status_text.set(
                    f"{readiness['missing_count']} dosya eksik, "
                    f"{run_summary.get('failed', 0)} hata."
                )
                self._set_status("İndirme tamamlanmadı.", "danger")
                self._set_progress(55, "İndirme devam etmeli")
            self._log(f"İndirme sonucu: {result.download_run.manifest_path}")
            self._refresh_pipeline_buttons()
            self._refresh_api_usage(silent=True)
            if not readiness["complete"]:
                self._show_download_recovery_popup(result.download_run.updated_queue_payload)

        self._set_progress(55, "Dosya indiriliyor")
        self.current_download_text.set(
            f"İndirme başlatılıyor: {readiness['missing_count']} dosya bekliyor."
        )
        self._start_time_estimate(
            phase="premium_download",
            total=int(readiness.get("missing_count") or 0),
            progress_base=55,
            progress_span=18,
        )
        self._run_task("Dosya indiriliyor", work, done)

    def _ensure_premium_api_key(self, api_key: str) -> None:
        if self.premium_api_validated:
            return
        require_premium_api_key(api_key)
        self.premium_api_validated = True

    def _open_next_non_premium_page(self) -> None:
        if self.premium_plan_result is None:
            self._plan_downloads(auto_start=True)
            return
        if not self._api_key():
            self._show_api_required("Ücretsiz tarayıcı indirmesini başlatmak")
            return
        queue_payload = json.loads(self._current_download_queue_path().read_text(encoding="utf-8"))
        item = next_non_premium_download(queue_payload)
        if item is None:
            retried_payload = self._reset_first_failed_non_premium_download(queue_payload)
            if retried_payload is not None:
                queue_payload = retried_payload
                item = next_non_premium_download(queue_payload)
        if item is None:
            self._refresh_non_premium_prompt()
            self._show_non_premium_failures(queue_payload, dialog=True)
            self._refresh_pipeline_buttons()
            return
        if not self._ensure_nxm_capture_active():
            return
        unavailable = unavailable_non_premium_downloads(queue_payload)
        position = next(
            (
                index
                for index, candidate in enumerate(unavailable, start=1)
                if candidate.get("translation_nexus_mod_id")
                == item.get("translation_nexus_mod_id")
                and candidate.get("translation_file_id") == item.get("translation_file_id")
            ),
            1,
        )
        label = item["translation_file_name"] or item["translation_name"] or "Nexus dosyası"
        self.current_download_text.set(
            f"{position}/{len(unavailable) or 1} · {label}\n"
            f"Nexus sayfası açık; Slow Download düğmesine tıklamanız bekleniyor."
        )
        webbrowser.open(item["page_url"], new=2, autoraise=True)
        self._set_status("Nexus sayfası açıldı, Slow Download'a tıklayın.")
        self.nxm_status_text.set(
            f"Nexus sayfası açıldı. Slow Download'a tıklayın: "
            f"{item['translation_nexus_mod_id']} / {item['translation_file_id']}"
        )
        self._set_progress(55, "Nexus sayfası açıldı")
        self._log(
            "Nexus sayfası açıldı: "
            f"{item['translation_nexus_mod_id']}/{item['translation_file_id']}"
        )
        self._refresh_pipeline_buttons()

    def _submit_nxm_from_clipboard(self) -> None:
        try:
            value = self.root.clipboard_get()
        except tk.TclError:
            self._toast(
                "Panoda indirme bağlantısı yok",
                "Önce Nexus Slow Download bağlantısını kopyalayın, ardından yeniden deneyin.",
                tone="warning",
            )
            return
        self.nxm_link.set(value)
        self._submit_nxm_link()

    def _submit_nxm_link(self) -> None:
        if self.premium_plan_result is None:
            self._toast(
                "İndirme henüz hazırlanmadı",
                "Önce Çevirileri indir düğmesiyle indirme listesini hazırlayın.",
                tone="warning",
            )
            return
        api_key = self._api_key()
        if not api_key:
            self._show_api_required("İndirme bağlantısını kullanmak")
            return
        nxm_url = self.nxm_link.get().strip()
        if not nxm_url:
            self._toast(
                "İndirme bağlantısı gerekli",
                "Slow Download ile oluşan indirme bağlantısı gerekli.",
                tone="warning",
            )
            return
        queue_path = self._current_download_queue_path()

        def work() -> Any:
            return run_non_premium_nxm_download(
                plan=self.premium_plan_result,
                api_key=api_key,
                nxm_url=nxm_url,
                queue_path=queue_path,
            )

        def done(result: Any) -> None:
            self.non_premium_download_result = result
            self.premium_download_result = None
            self.conversion_result = None
            self.nxm_link.set("")
            readiness = download_queue_readiness(self.manifest, result.updated_queue_payload)
            if result.result_payload.get("status") == "DOWNLOADED":
                authorization = result.result_payload.get("authorization", {})
                self.current_download_text.set(
                    "Dosya indirildi: "
                    f"Nexus {authorization.get('mod_id', '?')}/{authorization.get('file_id', '?')}"
                )
                self.download_status_text.set(
                    f"Dosya indirildi. Kalan: {readiness['missing_count']} / "
                    f"{readiness['required_count']}."
                )
            else:
                failed_label = self._first_non_premium_failure_label(result.updated_queue_payload)
                self.current_download_text.set(
                    f"Dosya indirilemedi: {failed_label or 'ayrıntılar kaydedildi'}."
                )
                failure_hint = self._first_non_premium_failure_hint(result.updated_queue_payload)
                self.download_status_text.set(
                    f"Dosya indirilemedi: {failed_label or 'hata ayrıntısı detaylarda'}."
                )
                if failure_hint:
                    self.nxm_status_text.set(failure_hint)
                self._show_non_premium_failures(result.updated_queue_payload)
            if readiness["complete"]:
                self._set_status("Tüm dosyalar hazır.", "success")
                self._set_progress(75, "Çeviri hazırlanabilir")
            else:
                self._set_status("Siradaki dosya bekleniyor.")
                self._set_progress(60, "Dosya indiriliyor")
            self._log(f"Tarayıcı indirme sonucu: {result.result_path}")
            self._refresh_non_premium_prompt()
            self._refresh_pipeline_buttons()
            result_status = str(result.result_payload.get("status") or "").upper()
            if (
                result_status in {"DOWNLOADED", "FAILED"}
                and not readiness["complete"]
                and self.auto_open_next_nxm.get()
                and next_non_premium_download(result.updated_queue_payload) is not None
            ):
                self._open_next_non_premium_page()

        self._set_status("Dosya indiriliyor.")
        self._set_progress(60, "Dosya indiriliyor")
        self._run_task("Dosya indiriliyor", work, done)

    def _ensure_nxm_capture_active(self) -> bool:
        if self.nxm_capture_server is not None and self.nxm_capture_server.active:
            return True
        server = NxmCaptureServer(lambda nxm_url: self.task_queue.put(("nxm", nxm_url)))
        try:
            server.start()
            status = self.nxm_protocol_binding.bind()
        except Exception as exc:  # noqa: BLE001 - GUI boundary reports safe setup failure.
            server.stop()
            self._notify(
                "Tarayıcı bağlantısı yakalanamadı",
                f"İndirme bağlantısı Çeviri aracı'na yönlendirilemedi: {exc}",
                tone="danger",
                action_label="Detayları kontrol et",
            )
            return False
        self.nxm_capture_server = server
        self._log(
            "İndirme bağlantısı yakalama etkin; önceki bağlantı yedeklendi."
            if status.previous_command
            else "İndirme bağlantısı yakalama etkin."
        )
        return True

    def _stop_nxm_capture(self) -> None:
        server = self.nxm_capture_server
        self.nxm_capture_server = None
        if server is not None:
            server.stop()
        try:
            restored = self.nxm_protocol_binding.restore()
        except NxmCaptureError:
            restored = False
        if restored:
            self._log("Önceki Windows indirme bağlantısı yönlendirmesi geri yüklendi.")

    def _handle_captured_nxm(self, nxm_url: str) -> None:
        if self._delivery_mode_value() != "NON_PREMIUM_NXM" or self.premium_plan_result is None:
            return
        if self.busy:
            self.pending_nxm_url = nxm_url
            self.nxm_status_text.set("İndirme bağlantısı yakalandı; mevcut işlem bekleniyor.")
            return
        self.nxm_link.set(nxm_url)
        self.nxm_status_text.set("Slow Download yakalandı; dosya indiriliyor.")
        self.current_download_text.set(
            f"{self.current_download_text.get()}\nİndirme bağlantısı yakalandı; dosya alınıyor."
        )
        self._submit_nxm_link()

    def _process_pending_nxm(self) -> None:
        if self.busy or not self.pending_nxm_url:
            return
        nxm_url = self.pending_nxm_url
        self.pending_nxm_url = None
        self._handle_captured_nxm(nxm_url)

    def _prepare_translation(self) -> None:
        retry_seconds = self._conversion_retry_seconds()
        if retry_seconds > 0:
            self._toast(
                "Kısa bir süre bekleyin",
                (
                    "Arka plan işlemlerinin kapanması için "
                    f"{retry_seconds} saniye daha bekleyin. Sayaç bittiğinde "
                    "Çeviriyi hazırla düğmesini yeniden kullanabilirsiniz."
                ),
                tone="warning",
            )
            return
        if not self.profile_scan_path or self.premium_plan_result is None:
            self._toast(
                "Önce çevirileri indirin",
                "Çeviri paketi hazırlanmadan önce gerekli Nexus dosyalarının indirilmesi gerekiyor.",
                tone="warning",
            )
            return
        if not self._ensure_profile_continue_confirmed():
            return
        queue_path = self._current_download_queue_path()
        queue_payload = json.loads(queue_path.read_text(encoding="utf-8"))
        readiness = download_queue_readiness(self.manifest, queue_payload)
        if not readiness["complete"]:
            self._toast(
                "Dosyalar eksik",
                f"{readiness['missing_count']} gerekli dosya henüz hazır değil.",
                tone="warning",
            )
            return
        run_workspace = self._run_workspace()
        manifest_path = self._write_runtime_manifest()
        selected_mods_root = self._selected_mods_root()
        if selected_mods_root is None:
            self._toast(
                "Modlist klasörü gerekli",
                "Çeviri çıktısının nereye kurulacağını belirlemek için önce modlist klasörünü seçin.",
                tone="warning",
            )
            return

        def work() -> Any:
            return run_conversion_in_worker(
                manifest_path=manifest_path,
                profile_scan_path=self.profile_scan_path,
                decisions_path=self.premium_plan_result.decisions_path,
                download_queue_path=queue_path,
                out_dir=run_workspace / "runtime",
                staging_root=selected_mods_root,
                output_mod_name_override=self._output_mod_name(),
                allow_profile_drift=self.profile_override_accepted,
            )

        def done(result: Any) -> None:
            self.conversion_result = result
            output_folder = self._current_output_folder()
            self.output_folder_text.set(
                str(output_folder) if output_folder is not None else self._output_folder_display()
            )
            summary = result.conversion.manifest_payload.get("summary", {})
            add_on_summary = result.result_payload.get("add_on_packages", {}).get("summary", {})
            failed_items = int(summary.get("failed_items") or 0)
            add_on_failed = int(add_on_summary.get("failed") or 0)
            add_on_extracted = int(add_on_summary.get("extracted") or 0)
            add_on_text = f", {add_on_extracted} ek paket" if add_on_extracted else ""
            if failed_items or add_on_failed:
                self.prepare_status_text.set(
                    f"Hazırlandı, ancak {failed_items} arşiv ve {add_on_failed} ek paket işlenemedi. Detayları kontrol edin."
                )
                self.prepare_status_label.configure(
                    text_color=self.colors["warning"],
                    font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
                )
                self._set_status("Çeviri paketi kısmen hazırlandı.", "warning", prominent=True)
                self._set_progress(96, "Kontrol gerekli")
            else:
                self.prepare_status_text.set(
                    f"Çeviri hazırlandı: {summary.get('plugin_outputs', 0)} plugin dosyası, "
                    f"{summary.get('native_outputs', 0)} ek dosya{add_on_text}."
                )
                self.prepare_status_label.configure(
                    text_color=self.colors["success"],
                    font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold"),
                )
                self._set_status("Çeviri paketi başarıyla hazırlandı.", "success", prominent=True)
                self._set_progress(100, "Kurulum tamamlandı")
                self.current_download_text.set(
                    f"Çeviri paketi hazırlandı.\nÇıktı: {result.conversion.output_mod_path}"
                )
                self._show_completion_popup(Path(result.conversion.output_mod_path))
            self._log(f"Çıktı klasörü: {result.conversion.output_mod_path}")
            self._log(f"Rapor: {result.conversion.report_path}")
            self._refresh_pipeline_buttons()

        self._reset_prepare_status_style()
        self._set_status("Çeviri hazırlanıyor.")
        self._set_progress(88, "Çeviri hazırlanıyor")
        self._start_conversion_time_estimate(run_workspace / "runtime" / "conversion-worker" / "progress.json")
        self._run_task("Çeviri hazırlanıyor", work, done)

    def _show_completion_popup(self, output_folder: Path) -> None:
        existing = getattr(self, "completion_popup", None)
        if existing is not None and existing.winfo_exists():
            existing.destroy()

        popup = ctk.CTkToplevel(self.root)
        self.completion_popup = popup
        popup.title("Çeviri hazır")
        popup.resizable(False, False)
        popup.transient(self.root)
        popup.configure(fg_color=self.colors["bg"])
        popup.columnconfigure(0, weight=1)

        def close_popup() -> None:
            try:
                popup.grab_release()
            except tk.TclError:
                pass
            self.completion_endorsement_button = None
            self.completion_popup = None
            popup.destroy()

        popup.protocol("WM_DELETE_WINDOW", close_popup)

        ctk.CTkLabel(
            popup,
            text="KURULUM TAMAMLANDI",
            text_color=self.colors["success"],
            anchor="w",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
        ).grid(row=0, column=0, sticky="ew", padx=28, pady=(24, 3))
        ctk.CTkLabel(
            popup,
            text="Çeviri paketi hazır",
            text_color=self.colors["text"],
            anchor="w",
            font=ctk.CTkFont(family="Segoe UI", size=24, weight="bold"),
        ).grid(row=1, column=0, sticky="ew", padx=28)
        ctk.CTkLabel(
            popup,
            text="Paket seçtiğiniz modlistin MO2 mods klasörüne oluşturuldu.",
            text_color=self.colors["muted"],
            anchor="w",
            font=ctk.CTkFont(family="Segoe UI", size=14),
        ).grid(row=2, column=0, sticky="ew", padx=28, pady=(5, 16))
        ctk.CTkLabel(
            popup,
            text=str(output_folder),
            text_color=self.colors["text"],
            fg_color=self.colors["panel_alt"],
            corner_radius=8,
            justify=tk.LEFT,
            anchor="w",
            wraplength=630,
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
        ).grid(row=3, column=0, sticky="ew", padx=28, ipady=9)
        ctk.CTkLabel(
            popup,
            text="MO2 içinde çeviri modunu aktif etmeyi unutmayın.",
            text_color=self.colors["warning"],
            anchor="w",
            font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold"),
        ).grid(row=4, column=0, sticky="ew", padx=28, pady=(14, 0))

        completion_notice = self.branding.completion_notice
        next_row = 5
        if completion_notice is not None:
            ctk.CTkFrame(
                popup,
                height=1,
                fg_color=self.colors["line"],
            ).grid(row=5, column=0, sticky="ew", padx=28, pady=(18, 12))
            ctk.CTkLabel(
                popup,
                text=completion_notice.text,
                text_color=self.colors["warning"],
                justify=tk.LEFT,
                anchor="w",
                wraplength=620,
                font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
            ).grid(row=6, column=0, sticky="ew", padx=28)
            next_row = 7
            if completion_notice.url:
                ctk.CTkButton(
                    popup,
                    text=completion_notice.action_label or "Mod sayfasını aç",
                    command=lambda url=completion_notice.url: webbrowser.open(
                        url,
                        new=2,
                        autoraise=True,
                    ),
                    corner_radius=7,
                    fg_color="transparent",
                    hover_color=self.colors["panel_alt"],
                    border_width=1,
                    border_color=self.colors["line"],
                    text_color=self.colors["text"],
                    height=34,
                    width=170,
                ).grid(row=7, column=0, sticky="w", padx=28, pady=(9, 0))
                next_row = 8

        ctk.CTkFrame(
            popup,
            height=1,
            fg_color=self.colors["line"],
        ).grid(row=next_row, column=0, sticky="ew", padx=28, pady=(18, 14))

        buttons = ctk.CTkFrame(popup, fg_color="transparent")
        buttons.grid(row=next_row + 1, column=0, sticky="ew", padx=28, pady=(0, 24))
        action_count = 3 if self.endorsement_available else 2
        for column in range(action_count):
            buttons.columnconfigure(column, weight=1)
        ctk.CTkButton(
            buttons,
            text="Klasörü aç",
            command=self._open_output_folder,
            corner_radius=10,
            fg_color=self.colors["button"],
            hover_color=self.colors["button_hover"],
            height=40,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 6))

        close_column = 1
        if self.endorsement_available:
            self.completion_endorsement_button = ctk.CTkButton(
                buttons,
                text=ENDORSE_BUTTON_LABEL,
                command=lambda: self._endorse_release(parent=popup),
                corner_radius=10,
                fg_color=self.colors["premium"],
                hover_color="#ffad4d",
                text_color=("#ffffff", "#ffffff"),
                height=40,
                font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            )
            self.completion_endorsement_button.grid(
                row=0,
                column=1,
                sticky="ew",
                padx=6,
            )
            close_column = 2
            self._sync_endorsement_buttons()

        ctk.CTkButton(
            buttons,
            text="Kapat",
            command=close_popup,
            corner_radius=10,
            fg_color=self.colors["accent"],
            hover_color=self.colors["accent_hover"],
            height=40,
        ).grid(row=0, column=close_column, sticky="ew", padx=(6, 0))

        popup.update_idletasks()
        width = max(700, popup.winfo_reqwidth())
        height = max(300, popup.winfo_reqheight())
        root_x = self.root.winfo_rootx()
        root_y = self.root.winfo_rooty()
        root_width = max(self.root.winfo_width(), 1)
        root_height = max(self.root.winfo_height(), 1)
        x = root_x + max((root_width - width) // 2, 0)
        y = root_y + max((root_height - height) // 2, 0)
        popup.geometry(f"{width}x{height}+{x}+{y}")
        popup.lift()
        popup.focus_force()
        try:
            popup.grab_set()
        except tk.TclError:
            pass

    def _write_runtime_manifest(self) -> Path:
        output_path = self._run_workspace() / "manifest" / "manifest.json"
        return write_wizard_manifest(self.manifest, output_path).manifest_path

    def _current_download_queue_path(self) -> Path:
        if self.non_premium_download_result is not None:
            return self.non_premium_download_result.updated_queue_path
        if self.premium_download_result is not None:
            return self.premium_download_result.download_run.updated_queue_path
        if self.premium_plan_result is not None:
            return self.premium_plan_result.download_plan.queue_path
        raise RuntimeError("download plan is not available")

    def _reset_first_failed_non_premium_download(
        self,
        queue_payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        for item in queue_payload.get("items", []):
            if not isinstance(item, dict):
                continue
            if str(item.get("status") or "").upper() != "FAILED":
                continue
            item["status"] = "PLANNED"
            item.pop("last_error", None)
            request = item.get("request") if isinstance(item.get("request"), dict) else {}
            label = request.get("translation_file_name") or request.get("translation_name") or "Nexus dosyası"
            queue_path = self._current_download_queue_path()
            queue_path.write_text(
                json.dumps(queue_payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            self._log(f"Başarısız indirme yeniden denemeye alındı: {label}")
            self.download_status_text.set(f"Yeniden denenecek: {label}")
            return queue_payload
        return None

    def _downloads_complete(self) -> bool:
        if self.premium_plan_result is None:
            return False
        try:
            queue_payload = json.loads(self._current_download_queue_path().read_text(encoding="utf-8"))
            return bool(download_queue_readiness(self.manifest, queue_payload)["complete"])
        except (OSError, ValueError, json.JSONDecodeError):
            return False

    def _profile_exact_match_for(self, preflight: dict[str, Any] | None) -> bool:
        if not preflight:
            return False
        profile = preflight.get("profile")
        return bool(isinstance(profile, dict) and profile.get("exact_match"))

    def _profile_check_completed(self) -> bool:
        if not self.preflight_payload:
            return False
        return str(self.preflight_payload.get("status") or "").upper() in {
            "READY",
            "REVIEW_REQUIRED",
        }

    def _profile_requires_confirmation(self) -> bool:
        if not self._profile_check_completed():
            return False
        status = str(self.preflight_payload.get("status") or "").upper()
        return status != "READY" or not self._profile_exact_match_for(self.preflight_payload)

    def _preflight_ready(self) -> bool:
        if not self._profile_check_completed():
            return False
        if not self._profile_requires_confirmation():
            return True
        return self.profile_override_accepted

    def _ensure_profile_continue_confirmed(self) -> bool:
        if not self.preflight_payload:
            self._toast(
                "Profil henüz hazır değil",
                "Modlist klasörünü ve MO2 profilini seçin. Kontrol tamamlandığında devam edebilirsiniz.",
                tone="warning",
            )
            return False
        if self._preflight_ready():
            return True
        if not self._profile_check_completed():
            self._toast(
                "Profil kontrolü tamamlanmadı",
                "Profil kontrolü tamamlanmadan indirme başlatılamaz.",
                tone="warning",
            )
            return False

        summary = preflight_summary(self.preflight_payload)
        accepted = self._confirm(
            "Profil birebir eşleşmiyor",
            "Seçili profil stok paketle birebir uyuşmuyor.\n\n"
            f"Eşleşen çeviri: {summary['matched_entries']}/{summary['manifest_entries']}\n"
            f"Eksik hedef: {summary['missing_entries']}\n"
            f"Uyarı: {summary['compatibility_warnings']}\n\n"
            "Bu durumda çeviri yine kurulabilir, ancak mod listenizde yaptığınız "
            "ekleme veya değişikliklerden kaynaklı bazı çeviriler uygulanmayabilir.\n\n"
            "Yine de devam etmek istiyor musunuz?",
            action_label="Yine de devam et",
            cancel_label="Profile geri dön",
            tone="warning",
        )
        if not accepted:
            self.download_status_text.set("Profil farkı onaylanmadan indirme başlatılamaz.")
            self._set_status("Profil farkı onaylanmadı.", "danger")
            self._refresh_pipeline_buttons()
            return False

        self.profile_override_accepted = True
        self.download_status_text.set("Profil farkı kullanıcı onayıyla kabul edildi.")
        self._set_status("Profil farkı kullanıcı onayıyla kabul edildi.")
        self._log("Profil uyuşmazlığı kullanıcı onayıyla geçildi.")
        self._refresh_pipeline_buttons()
        return True

    def _api_key(self) -> str | None:
        return load_api_key(self.store, app_id=self.app_id)

    def _refresh_pipeline_buttons(self) -> None:
        state = installer_button_state(
            preflight_ready=self._profile_check_completed(),
            has_api_key=bool(self._api_key()),
            has_download_plan=self.premium_plan_result is not None,
            downloads_complete=self._downloads_complete(),
            conversion_complete=self.conversion_result is not None,
            busy=self.busy,
            delivery_mode=self._delivery_mode_value(),
            real_install_supported=False,
        )
        self.download_button.configure(
            text=state.download_label,
            state=tk.NORMAL if state.can_download else tk.DISABLED,
        )
        self.prepare_button.configure(
            text=state.prepare_label,
            state=tk.NORMAL if state.can_prepare else tk.DISABLED,
        )
        retry_seconds = self._conversion_retry_seconds()
        if retry_seconds > 0:
            self.prepare_button.configure(
                text=f"Yeniden deneme: {retry_seconds} sn",
                state=tk.DISABLED,
            )
            self.prepare_status_text.set(
                "Arka plan işlemleri kapanıyor. Sayaç bitince yeniden deneyebilirsiniz."
            )
        if hasattr(self, "download_cache_button"):
            self.download_cache_button.configure(
                state=tk.DISABLED if self.busy else tk.NORMAL
            )
        if hasattr(self, "manifest_mode_control"):
            self.manifest_mode_control.configure(
                state=tk.DISABLED if self.busy else tk.NORMAL
            )
        if not self.download_status_text.get() or self.download_status_text.get().endswith(("açılır.", "acilir.")):
            self.download_status_text.set(state.download_hint)
        if (
            self._profile_requires_confirmation()
            and not self.profile_override_accepted
            and self.premium_plan_result is None
            and not self.busy
        ):
            self.download_status_text.set("Profil birebir uyuşmuyor; devam etmek için onay istenecek.")
        if not self.prepare_status_text.get() or "staging" in self.prepare_status_text.get():
            self.prepare_status_text.set(state.prepare_hint)

        non_premium = self._delivery_mode_value() == "NON_PREMIUM_NXM"
        if non_premium:
            self.nxm_frame.grid()
        else:
            self.nxm_frame.grid_remove()
        nxm_state = (
            tk.NORMAL
            if non_premium and self.premium_plan_result is not None and not self._downloads_complete()
            else tk.DISABLED
        )
        self.nxm_submit_button.configure(state=nxm_state)
        self.nxm_clipboard_button.configure(state=nxm_state)

    def _reset_for_profile_change(self) -> None:
        self._stop_nxm_capture()
        self.profile_scan_path = None
        self.preflight_payload = None
        self.profile_override_accepted = False
        self._reset_download_state()
        self._set_profile_status("Profil kontrolü bekleniyor.")
        self._set_status("Profil bekleniyor.")
        self._set_progress(0, "Profil bekleniyor")

    def _reset_download_state(self) -> None:
        self._stop_nxm_capture()
        self._stop_conversion_retry_cooldown()
        self.premium_plan_result = None
        self.premium_download_result = None
        self.non_premium_download_result = None
        self.conversion_result = None
        self.premium_api_validated = False
        self.pending_nxm_url = None
        self.nxm_link.set("")
        self.output_folder_text.set(self._output_folder_display())
        self.download_status_text.set("Profil hazırlanınca indirme açılır.")
        self.current_download_text.set(
            "İndirme veya hazırlama başlayınca işlem bilgisi burada görünür."
        )
        self.prepare_status_text.set("Tüm gerekli dosyalar hazır olmadan başlatılamaz.")
        self._reset_prepare_status_style()
        if self.completion_popup is not None and self.completion_popup.winfo_exists():
            self.completion_popup.destroy()
            self.completion_popup = None
        if (
            self.download_recovery_popup is not None
            and self.download_recovery_popup.winfo_exists()
        ):
            self.download_recovery_popup.destroy()
            self.download_recovery_popup = None
        self._refresh_non_premium_prompt()
        self._refresh_pipeline_buttons()

    def _refresh_non_premium_prompt(self) -> None:
        if self._delivery_mode_value() != "NON_PREMIUM_NXM":
            self.nxm_status_text.set("")
            return
        if self.premium_plan_result is None:
            self.nxm_status_text.set(
                "Ücretsiz modda Premium üyelik gerekmez; indirme için Nexus API anahtarı kaydedilmelidir."
            )
            return
        try:
            queue_payload = json.loads(self._current_download_queue_path().read_text(encoding="utf-8"))
            item = next_non_premium_download(queue_payload)
        except (OSError, ValueError, json.JSONDecodeError):
            item = None
            queue_payload = {}
        if item is None:
            readiness = download_queue_readiness(self.manifest, queue_payload)
            if readiness["complete"]:
                self.nxm_status_text.set("Tüm gerekli dosyalar hazır.")
                return
            failed = failed_non_premium_downloads(queue_payload)
            if failed:
                self.nxm_status_text.set(
                    f"Açılacak yeni dosya yok. Başarısız: {self._format_non_premium_item(failed[0])}."
                )
                return
            self.nxm_status_text.set(f"{readiness['missing_count']} dosya eksik.")
            return
        self.nxm_status_text.set(
            f"Sıradaki: {item['translation_file_name'] or item['translation_name'] or 'Nexus dosyası'} "
            f"({item['translation_nexus_mod_id']}/{item['translation_file_id']})"
        )

    def _first_non_premium_failure_label(self, queue_payload: dict[str, Any]) -> str:
        failed = failed_non_premium_downloads(queue_payload)
        return self._format_non_premium_item(failed[0]) if failed else ""

    def _first_non_premium_failure_hint(self, queue_payload: dict[str, Any]) -> str:
        failed = failed_non_premium_downloads(queue_payload)
        if not failed:
            return ""
        last_error = str(failed[0].get("last_error") or "").strip()
        if last_error == "nexus_api_http_401" and not self._api_key():
            return (
                "Nexus bu dosya için API anahtarı istiyor. API anahtarını kaydedip "
                "aynı dosyayı tekrar deneyin."
            )
        return self._download_error_hint(last_error)

    def _show_non_premium_failures(
        self,
        queue_payload: dict[str, Any],
        *,
        dialog: bool = False,
    ) -> None:
        failed = failed_non_premium_downloads(queue_payload)
        if not failed:
            if dialog:
                self._toast(
                    "Bekleyen dosya bulunamadı",
                    "Açılacak yeni bir Nexus indirme sayfası yok.",
                    tone="info",
                )
            return
        self._log("Başarısız tarayıcı indirmeleri:")
        lines = []
        for item in failed[:10]:
            label = self._format_non_premium_item(item)
            page_url = item.get("page_url") or ""
            last_error = item.get("last_error") or "hata ayrıntısı yok"
            self._log(f"- {label} | {last_error} | {page_url}")
            lines.append(f"- {label}\n  {last_error}\n  {page_url}")
        if len(failed) > 10:
            self._log(f"- ... {len(failed) - 10} ek başarısız dosya")
            lines.append(f"- ... {len(failed) - 10} ek başarısız dosya")
        if dialog:
            self._notify(
                "İndirilemeyen dosyalar",
                "\n\n".join(lines),
                tone="warning",
                action_label="Detayları kapat",
            )

    def _show_download_recovery_popup(self, queue_payload: dict[str, Any]) -> None:
        unavailable = unavailable_non_premium_downloads(queue_payload)
        if not unavailable:
            return
        existing = getattr(self, "download_recovery_popup", None)
        if existing is not None and existing.winfo_exists():
            existing.destroy()

        popup = ctk.CTkToplevel(self.root)
        self.download_recovery_popup = popup
        popup.title("Eksik indirmeleri tamamla")
        popup.geometry("760x520")
        popup.minsize(680, 430)
        popup.transient(self.root)
        popup.configure(fg_color=self.colors["bg"])

        def close_popup() -> None:
            try:
                popup.grab_release()
            except tk.TclError:
                pass
            self.download_recovery_popup = None
            popup.destroy()

        def start_browser_recovery() -> None:
            close_popup()
            self.delivery_mode.set(NON_PREMIUM_DELIVERY_LABEL)
            self._refresh_auth_status()
            self._refresh_non_premium_prompt()
            self._refresh_pipeline_buttons()
            self._log(
                f"Tarayıcıyla manuel tamamlama başlatıldı: {len(unavailable)} dosya bekliyor."
            )
            self._open_next_non_premium_page()

        popup.protocol("WM_DELETE_WINDOW", close_popup)
        card = ctk.CTkFrame(
            popup,
            fg_color=self.colors["panel"],
            corner_radius=14,
            border_width=1,
            border_color=self.colors["line"],
        )
        card.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)
        ctk.CTkLabel(
            card,
            text=f"{len(unavailable)} dosya otomatik indirilemedi",
            text_color=self.colors["warning"],
            anchor="w",
            font=ctk.CTkFont(family="Segoe UI", size=21, weight="bold"),
        ).pack(fill=tk.X, padx=20, pady=(18, 6))
        ctk.CTkLabel(
            card,
            text=(
                "İşleme tarayıcı üzerinden devam edebilirsiniz. Çeviri aracı sıradaki "
                "Nexus sayfasını açar ve Slow Download bağlantısını otomatik yakalar."
            ),
            text_color=self.colors["text"],
            justify=tk.LEFT,
            anchor="w",
            wraplength=680,
            font=ctk.CTkFont(family="Segoe UI", size=13),
        ).pack(fill=tk.X, padx=20, pady=(0, 12))
        details = ctk.CTkTextbox(
            card,
            fg_color=self.colors["panel_alt"],
            text_color=self.colors["text"],
            border_width=1,
            border_color=self.colors["line"],
            corner_radius=9,
            height=250,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            wrap=tk.WORD,
        )
        details.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 14))
        for position, item in enumerate(unavailable, start=1):
            error = str(item.get("last_error") or "otomatik indirme tamamlanamadı")
            details.insert(
                tk.END,
                f"{position}. {self._format_non_premium_item(item)}\n"
                f"   {error}\n   {item.get('page_url') or ''}\n\n",
            )
        details.configure(state=tk.DISABLED)
        buttons = ctk.CTkFrame(card, fg_color="transparent")
        buttons.pack(fill=tk.X, padx=20, pady=(0, 18))
        buttons.columnconfigure(0, weight=1)
        buttons.columnconfigure(1, weight=1)
        ctk.CTkButton(
            buttons,
            text="Daha sonra",
            command=close_popup,
            fg_color=self.colors["button"],
            hover_color=self.colors["button_hover"],
            corner_radius=9,
            height=40,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 7))
        ctk.CTkButton(
            buttons,
            text="Tarayıcıyla tamamla",
            command=start_browser_recovery,
            fg_color=self.colors["accent"],
            hover_color=self.colors["accent_hover"],
            corner_radius=9,
            height=40,
        ).grid(row=0, column=1, sticky="ew", padx=(7, 0))
        popup.lift()
        popup.focus_force()
        try:
            popup.grab_set()
        except tk.TclError:
            pass

    def _format_non_premium_item(self, item: dict[str, Any]) -> str:
        label = (
            str(item.get("translation_file_name") or "").strip()
            or str(item.get("translation_name") or "").strip()
            or "Nexus dosyası"
        )
        return (
            f"{label} "
            f"({item.get('translation_nexus_mod_id')}/{item.get('translation_file_id')})"
        )

    def _download_error_hint(self, reason: str) -> str:
        if reason == "nexus_api_http_401":
            return "Nexus oturumu veya API yetkisi bu dosya için geçerli değil."
        if reason.startswith("nexus_api_http_"):
            return f"Nexus indirme isteği başarısız oldu: {reason}."
        if reason:
            return f"İndirme hatası: {reason}."
        return ""

    def _delivery_mode_value(self) -> str:
        return delivery_mode_value(self.delivery_mode.get())

    def _run_workspace(self) -> Path:
        return run_workspace_for_manifest(self.manifest)

    def _output_mod_name(self) -> str:
        return translation_output_mod_name(
            self.mo2_root.get().strip(),
            fallback_modlist_name=self.summary.get("modlist_name") or "Modlist",
            profile_name=self.mo2_profile.get().strip() or None,
        )

    def _selected_mods_root(self) -> Path | None:
        root_text = self.mo2_root.get().strip()
        if not root_text:
            return None
        location = resolve_installer_mo2_location(
            root_text,
            self.mo2_profile.get().strip() or None,
        )
        if location is not None:
            return location.mods_dir
        return Path(root_text) / "mods"

    def _planned_output_folder(self) -> Path | None:
        mods_root = self._selected_mods_root()
        if mods_root is None:
            return None
        return mods_root / self._output_mod_name()

    def _current_output_folder(self) -> Path | None:
        conversion_result = getattr(self, "conversion_result", None)
        if conversion_result is not None:
            return Path(conversion_result.conversion.output_mod_path)
        return self._planned_output_folder()

    def _output_folder_display(self) -> str:
        output_folder = self._current_output_folder()
        if output_folder is None:
            return "Modlist klasörü seçilince otomatik belirlenecek."
        return str(output_folder)

    def _set_progress(self, value: int, label: str) -> None:
        clamped = max(0, min(100, int(value)))
        self.progress_value.set(clamped)
        self.progress_percent.set(f"{clamped}%")
        self.progress_label.set(label)
        if hasattr(self, "progress_bar"):
            self.progress_bar.set(clamped / 100)

    def _start_time_estimate(
        self,
        *,
        phase: str,
        total: int,
        progress_base: int,
        progress_span: int,
    ) -> None:
        self._stop_time_estimate()
        self._eta_phase = phase
        self._eta_started_at = time.monotonic()
        self._eta_completed = 0
        self._eta_total = max(0, int(total))
        self._eta_progress_base = max(0, min(100, int(progress_base)))
        self._eta_progress_span = max(0, min(100, int(progress_span)))
        self._eta_status_path = None
        if self._eta_total > 0:
            self.eta_text.set(format_eta(None))
        else:
            self.eta_text.set("")

    def _start_conversion_time_estimate(self, status_path: Path) -> None:
        self._start_time_estimate(
            phase="conversion",
            total=0,
            progress_base=88,
            progress_span=10,
        )
        self._eta_status_path = status_path
        self._last_conversion_archive_key = None
        self.current_download_text.set("Çeviri arşivleri hazırlanıyor...")
        self.eta_text.set(format_eta(None))
        self._eta_after = self.root.after(700, self._poll_conversion_time_estimate)

    def _stop_time_estimate(self) -> None:
        if self._eta_after is not None:
            try:
                self.root.after_cancel(self._eta_after)
            except tk.TclError:
                pass
        self._eta_after = None
        self._eta_phase = None
        self._eta_started_at = 0.0
        self._eta_completed = 0
        self._eta_total = 0
        self._eta_status_path = None
        self.eta_text.set("")

    def _handle_eta_event(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        if str(payload.get("phase") or "") != self._eta_phase:
            return
        delta = int(payload.get("completed_delta") or 0)
        total = int(payload.get("total") or self._eta_total or 0)
        label = str(payload.get("label") or self.progress_label.get() or "İşlem sürüyor")
        self._update_time_estimate(
            completed=self._eta_completed + max(0, delta),
            total=total,
            label=label,
        )

    def _handle_download_progress_event(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        stage = str(payload.get("stage") or "")
        position = max(1, int(payload.get("position") or 1))
        total = max(1, int(payload.get("total") or self._eta_total or 1))
        completed = max(0, int(payload.get("completed") or 0))
        attempt = max(1, int(payload.get("attempt") or 1))
        translation_name = str(payload.get("translation_name") or "").strip()
        file_name = str(payload.get("translation_file_name") or "").strip()
        mod_id = payload.get("translation_nexus_mod_id") or "?"
        file_id = payload.get("translation_file_id") or "?"
        display_name = translation_name or file_name or "Nexus çeviri dosyası"
        file_detail = f"\nDosya: {file_name}" if file_name and file_name != display_name else ""

        if stage == "started":
            retry_text = f" · {attempt}. deneme" if attempt > 1 else ""
            self.current_download_text.set(
                f"{position}/{total} · {display_name}{retry_text}{file_detail}\n"
                f"Nexus: {mod_id}/{file_id}"
            )
            self.download_status_text.set(f"İndiriliyor: {position}/{total} · {display_name}")
            self._update_time_estimate(
                completed=completed,
                total=total,
                label=f"Dosya indiriliyor: {position}/{total}",
            )
            self._log(
                f"İndiriliyor [{position}/{total}]: {display_name} "
                f"({mod_id}/{file_id}), deneme {attempt}."
            )
            return

        if stage == "retry":
            self.current_download_text.set(
                f"{position}/{total} · {display_name}\n"
                f"Bağlantı tamamlanamadı; güvenli yeniden deneme hazırlanıyor."
            )
            self.download_status_text.set(
                f"Yeniden denenecek: {display_name} ({attempt}. deneme tamamlanamadı)"
            )
            self._log(
                f"İndirme yeniden denenecek [{position}/{total}]: {display_name}; "
                f"{payload.get('error_type') or 'bağlantı hatası'}."
            )
            return

        if stage == "completed":
            self.current_download_text.set(
                f"İndirildi: {display_name}{file_detail}\nNexus: {mod_id}/{file_id}"
            )
            self.download_status_text.set(f"İndirildi: {completed}/{total} · {display_name}")
            self._update_time_estimate(
                completed=completed,
                total=total,
                label=f"Dosya indirildi: {completed}/{total}",
            )

    def _handle_endorsement_progress_event(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        target = payload.get("target")
        if not isinstance(target, ReleaseEndorsementTarget):
            return
        done = max(1, int(payload.get("done") or 1))
        total = max(1, int(payload.get("total") or 1))
        status = str(payload.get("status") or "")
        status_labels = {
            "endorsed": "beğenildi",
            "already_endorsed": "zaten beğenilmiş",
            "wait_required": "15 dakika bekliyor",
            "disabled": "endorse kapalı",
            "own_file": "kendi dosyası",
            "abstained": "abstain seçili",
            "rate_limited": "kota bekliyor",
            "unauthorized": "API yetkisi yok",
            "transient_error": "bağlantı bekliyor",
            "failed": "tamamlanamadı",
        }
        label = status_labels.get(status, "işleniyor")
        self.endorsement_status_text.set(
            f"Beğeni: {done}/{total} - {target.label} - {label}"
        )
        self._log(
            f"Endorse [{done}/{total}]: {target.label} ({target.mod_id}) - {label}."
        )

    def _poll_conversion_time_estimate(self) -> None:
        if self._eta_phase != "conversion" or self._eta_status_path is None or not self.busy:
            self._eta_after = None
            return
        try:
            payload = json.loads(self._eta_status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            payload = {}
        converter_stage = str(payload.get("converter_stage") or payload.get("stage") or "")
        runtime_stage = str(payload.get("stage") or "")
        alias_total = int(payload.get("total_alias_plugins") or 0)
        alias_processed = int(payload.get("processed_alias_plugins") or 0)
        total = int(payload.get("total_archives") or 0)
        processed = int(payload.get("processed_archives") or 0)
        archive_information = _conversion_archive_information(payload)
        if archive_information is not None:
            archive_key, archive_text = archive_information
            if archive_key != self._last_conversion_archive_key:
                self._last_conversion_archive_key = archive_key
                self.current_download_text.set(archive_text)
                self._log(archive_text.replace("\n", " · "))

        if runtime_stage == "extracting_add_on_package":
            position = max(1, int(payload.get("processed_packages") or 1))
            package_total = max(1, int(payload.get("total_packages") or 1))
            self.eta_text.set("Ek paket uygulanıyor")
            self._set_progress(
                max(int(self.progress_value.get()), 98),
                f"Ek paket çıkarılıyor: {position}/{package_total}",
            )
        elif runtime_stage == "extracting_native_binary_asset":
            position = max(1, int(payload.get("processed_assets") or 1))
            asset_total = max(1, int(payload.get("total_assets") or 1))
            self.eta_text.set("Ek dosya uygulanıyor")
            self._set_progress(
                max(int(self.progress_value.get()), 98),
                f"Ek dosya çıkarılıyor: {position}/{asset_total}",
            )
        elif alias_total > 0:
            display_processed = max(1, min(alias_processed, alias_total))
            self.eta_text.set("Profil eklentileri kontrol ediliyor")
            self._set_progress(
                max(int(self.progress_value.get()), 98),
                f"Ek plugin çevirileri hazırlanıyor: {display_processed}/{alias_total}",
            )
        elif total > 0:
            display_processed = max(1, min(processed, total))
            completed_for_eta = max(0, min(display_processed - 1, total))
            label = f"Çeviri hazırlanıyor: {display_processed}/{total} arşiv"
            self._update_time_estimate(
                completed=completed_for_eta,
                total=total,
                label=label,
                visual_completed=display_processed,
            )
        elif runtime_stage in {
            "applying_add_on_packages",
            "writing_result",
        } or payload.get("ok") is True:
            self.eta_text.set(format_eta(0))
            self._set_progress(
                max(int(self.progress_value.get()), 98),
                "Son kontroller yapılıyor",
            )
        elif converter_stage:
            self.eta_text.set(format_eta(None))
        self._eta_after = self.root.after(700, self._poll_conversion_time_estimate)

    def _update_time_estimate(
        self,
        *,
        completed: int,
        total: int,
        label: str,
        visual_completed: int | None = None,
    ) -> None:
        self._eta_completed = max(0, int(completed))
        self._eta_total = max(0, int(total))
        if self._eta_total <= 0:
            self.eta_text.set("")
            return
        elapsed = time.monotonic() - self._eta_started_at
        eta_seconds = estimated_remaining_seconds(
            elapsed_seconds=elapsed,
            completed=self._eta_completed,
            total=self._eta_total,
        )
        self.eta_text.set(format_eta(eta_seconds))
        progress_completed = (
            max(0, int(visual_completed))
            if visual_completed is not None
            else self._eta_completed
        )
        ratio = min(progress_completed / self._eta_total, 1.0)
        value = self._eta_progress_base + int(self._eta_progress_span * ratio)
        stable_value = max(int(self.progress_value.get()), min(98, value))
        self._set_progress(stable_value, label)

    def _start_busy_progress(self, label: str) -> None:
        self._stop_busy_progress()
        self._busy_progress_label = label
        current = int(self.progress_value.get())
        if current < 15:
            cap = 28
        elif current < 40:
            cap = 48
        elif current < 60:
            cap = 72
        elif current < 85:
            cap = 92
        else:
            cap = 98
        self._busy_progress_cap = max(current, cap)
        self._busy_progress_dots = 0
        self._busy_progress_after = self.root.after(350, self._busy_progress_tick)

    def _busy_progress_tick(self) -> None:
        if not self.busy:
            self._busy_progress_after = None
            return
        if self._eta_phase is not None:
            self._busy_progress_after = self.root.after(700, self._busy_progress_tick)
            return
        current = int(self.progress_value.get())
        if current < self._busy_progress_cap:
            current += 1
            self.progress_value.set(current)
            self.progress_percent.set(f"{current}%")
            if hasattr(self, "progress_bar"):
                self.progress_bar.set(current / 100)
        self._busy_progress_dots = (self._busy_progress_dots + 1) % 4
        dots = "." * self._busy_progress_dots
        self.progress_label.set(f"{self._busy_progress_label}{dots}")
        self._busy_progress_after = self.root.after(450, self._busy_progress_tick)

    def _stop_busy_progress(self) -> None:
        if self._busy_progress_after is not None:
            try:
                self.root.after_cancel(self._busy_progress_after)
            except tk.TclError:
                pass
        self._busy_progress_after = None

    def _conversion_retry_seconds(self) -> int:
        return _conversion_retry_seconds_remaining(
            ready_at=self._conversion_retry_ready_at,
            now=time.monotonic(),
        )

    def _start_conversion_retry_cooldown(self) -> None:
        self._stop_conversion_retry_cooldown()
        self._conversion_retry_ready_at = (
            time.monotonic() + CONVERSION_RETRY_COOLDOWN_SECONDS
        )
        self._update_conversion_retry_cooldown()

    def _update_conversion_retry_cooldown(self) -> None:
        self._conversion_retry_after = None
        remaining = self._conversion_retry_seconds()
        if remaining <= 0:
            self._conversion_retry_ready_at = 0.0
            self._refresh_pipeline_buttons()
            self.prepare_status_text.set(
                "Bekleme tamamlandı. Çeviriyi hazırla düğmesine yeniden basabilirsiniz."
            )
            self._set_status("Yeniden denemeye hazır.", "warning", prominent=True)
            return
        self._refresh_pipeline_buttons()
        self._set_status(
            f"Yeniden denemeden önce {remaining} saniye bekleyin.",
            "warning",
            prominent=True,
        )
        self._conversion_retry_after = self.root.after(
            1000,
            self._update_conversion_retry_cooldown,
        )

    def _stop_conversion_retry_cooldown(self) -> None:
        if self._conversion_retry_after is not None:
            try:
                self.root.after_cancel(self._conversion_retry_after)
            except tk.TclError:
                pass
        self._conversion_retry_after = None
        self._conversion_retry_ready_at = 0.0

    def _write_gui_error_log(self, error: BaseException, traceback_text: str) -> Path | None:
        local_app_data = os.environ.get("LOCALAPPDATA")
        log_root = (
            Path(local_app_data) / "Modlist Translation Wizard"
            if local_app_data
            else Path.home() / "AppData" / "Local" / "Modlist Translation Wizard"
        )
        log_path = log_root / "last_gui_error.log"
        try:
            log_root.mkdir(parents=True, exist_ok=True)
            log_path.write_text(
                "\n".join(
                    [
                        f"time_utc={datetime.now(timezone.utc).isoformat()}",
                        f"error_type={type(error).__name__}",
                        f"error={error}",
                        "",
                        traceback_text,
                    ]
                ),
                encoding="utf-8",
            )
        except OSError:
            return None
        return log_path

    def _handle_task_error(self, error: BaseException, traceback_text: str) -> None:
        self._stop_time_estimate()
        if isinstance(error, NexusPremiumRequiredError):
            self._set_status("Premium API gerekli.", "warning", prominent=True)
            self.progress_label.set("Premium indirme başlatılmadı")
            self.download_status_text.set(str(error).splitlines()[0])
            self._log(f"Uyarı: {error}")
            self._notify(
                "Premium indirme kullanılamıyor",
                str(error),
                tone="warning",
                action_label="İndirme yöntemine dön",
            )
            self._refresh_pipeline_buttons()
            return
        if _is_conversion_worker_failure(error):
            self._set_status(
                "Çeviri hazırlama geçici olarak durdu.",
                "warning",
                prominent=True,
            )
            self.progress_label.set("Kısa bir beklemeden sonra yeniden deneyin")
            self._log(f"Worker hatası: {error}")
            log_path = self._write_gui_error_log(error, traceback_text)
            if log_path is not None:
                self._log(f"Hata logu: {log_path}")
            log_text = f"\n\nHata günlüğü:\n{log_path}" if log_path is not None else ""
            self._notify(
                "Çeviri hazırlama yeniden denenebilir",
                (
                    "Çeviri worker işlemi geçici olarak tamamlanamadı. Programı kapatmayın.\n\n"
                    "Bu pencereyi kapattıktan sonra araç 12 saniyelik güvenli bir "
                    "bekleme başlatacak. Sayaç bittiğinde Çeviriyi hazırla düğmesine "
                    "yeniden basın.\n\n"
                    "Gerekirse bu adımı birkaç kez tekrarlayabilirsiniz. Sorun iki veya "
                    "üç denemeden sonra da sürerse hata günlüğünü paylaşın."
                    f"{log_text}"
                ),
                tone="warning",
                action_label="Bekleyip yeniden dene",
            )
            self._start_conversion_retry_cooldown()
            return
        self._set_status("Hata oluştu.", "danger", prominent=True)
        self.progress_label.set("İşlem hata verdi")
        self._log(f"Hata: {error}")
        log_path = self._write_gui_error_log(error, traceback_text)
        if log_path is not None:
            self._log(f"Hata logu: {log_path}")
            message = f"{error}\n\nHata logu:\n{log_path}"
        else:
            message = str(error)
        self._notify(
            "İşlem tamamlanamadı",
            message,
            tone="danger",
            action_label="Detayları kapat",
        )
        self._refresh_pipeline_buttons()

    def _run_task(self, label: str, work: Callable[[], Any], done: Callable[[Any], None]) -> None:
        if self.busy:
            self._toast(
                "İşlem hâlâ devam ediyor",
                "Yeni bir adım başlatmadan önce devam eden işlemin tamamlanmasını bekleyin.",
                tone="info",
            )
            return
        self.busy = True
        self._set_status(label)
        self._log(label)
        self._refresh_pipeline_buttons()
        self._start_busy_progress(label)

        def runner() -> None:
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001 - GUI boundary passes safe text.
                self.task_queue.put(("error", (exc, traceback.format_exc())))
            else:
                self.task_queue.put(("done", (done, result)))

        threading.Thread(target=runner, daemon=True).start()

    def _run_auxiliary_task(
        self,
        queue_kind: str,
        work: Callable[[], Any],
        done: Callable[[Any], None],
    ) -> None:
        def runner() -> None:
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001 - callback receives safe error payload.
                result = {"error": exc}
            self.task_queue.put((queue_kind, (done, result)))

        threading.Thread(target=runner, daemon=True).start()

    def _poll_task_queue(self) -> None:
        try:
            while True:
                kind, payload = self.task_queue.get_nowait()
                if kind == "nxm":
                    self._handle_captured_nxm(str(payload))
                    continue
                if kind == "eta":
                    self._handle_eta_event(payload)
                    continue
                if kind == "download_progress":
                    self._handle_download_progress_event(payload)
                    continue
                if kind == "endorsement_progress":
                    self._handle_endorsement_progress_event(payload)
                    continue
                if kind in {"endorsement_done", "api_usage_done"}:
                    callback, result = payload  # type: ignore[misc]
                    if kind == "endorsement_done":
                        self.endorsement_busy = False
                    else:
                        self.api_usage_busy = False
                    try:
                        callback(result)
                    except Exception as exc:  # noqa: BLE001 - keep GUI alive after auxiliary callbacks.
                        self._log(f"Yardımcı işlem hatası: {exc}")
                    continue
                self.busy = False
                self._stop_busy_progress()
                self._stop_time_estimate()
                if kind == "error":
                    error, traceback_text = payload  # type: ignore[misc]
                    self._handle_task_error(error, traceback_text)
                else:
                    callback, result = payload  # type: ignore[misc]
                    try:
                        callback(result)
                    except Exception as exc:  # noqa: BLE001 - keep GUI alive after callback errors.
                        self._handle_task_error(exc, traceback.format_exc())
                self._process_pending_nxm()
        except queue.Empty:
            pass
        self.root.after(100, self._poll_task_queue)

    def _toggle_details(self) -> None:
        if self.details_visible.get():
            self.details_frame.grid_remove()
            self.details_button.configure(text="Detayları göster")
            self.details_visible.set(False)
            return
        self.details_frame.grid(row=6, column=0, columnspan=2, sticky="nsew", pady=(8, 0))
        self.details_button.configure(text="Detayları gizle")
        self.details_visible.set(True)

    def _bind_mousewheel(self, _event: object | None = None) -> None:
        if hasattr(self, "scroll_canvas"):
            self.scroll_canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _unbind_mousewheel(self, _event: object | None = None) -> None:
        if hasattr(self, "scroll_canvas"):
            self.scroll_canvas.unbind_all("<MouseWheel>")

    def _on_mousewheel(self, event: tk.Event) -> None:
        delta = int(-1 * (event.delta / 120))
        if delta and hasattr(self, "scroll_canvas"):
            self.scroll_canvas.yview_scroll(delta, "units")

    def _log(self, text: str) -> None:
        if not hasattr(self, "log"):
            return
        self.log.insert(tk.END, f"{text}\n")
        self.log.see(tk.END)

    def _close(self) -> None:
        self._stop_time_estimate()
        self._stop_busy_progress()
        self._stop_conversion_retry_cooldown()
        self._cancel_endorsement_auto_retry()
        if hasattr(self, "scroll_canvas"):
            self.scroll_canvas.unbind_all("<MouseWheel>")
        self._stop_nxm_capture()
        clear_default_remote_manifest_cache()
        self.root.destroy()


class ManifestRecoveryApp:
    """Keep the application usable when neither OTA nor local data can load."""

    def __init__(self, root: ctk.CTk, *, initial_error: BaseException) -> None:
        self.root = root
        self.release_info = default_release_info()
        self.selected_mode = MANIFEST_MODE_OTA
        self.busy = False
        self.status_text = tk.StringVar(value="Çeviri listesi yüklenemedi.")
        self.detail_text = tk.StringVar(value=str(initial_error))

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")
        self.root.title("Çeviri listesi bağlantısı")
        self.root.geometry("780x470")
        self.root.minsize(680, 420)
        self.root.configure(fg_color="#101114")
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self._build()

    def _build(self) -> None:
        card = ctk.CTkFrame(
            self.root,
            fg_color="#181a1f",
            border_width=1,
            border_color="#30343d",
            corner_radius=16,
        )
        card.pack(fill=tk.BOTH, expand=True, padx=24, pady=24)
        card.columnconfigure(0, weight=1)

        ctk.CTkLabel(
            card,
            text="Çeviri listesi bağlantısı",
            text_color="#f3f4f6",
            font=ctk.CTkFont(family="Segoe UI", size=24, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=24, pady=(24, 6))
        ctk.CTkLabel(
            card,
            text=f"Paket: {self.release_info['release_id']}",
            text_color="#a4acb9",
            font=ctk.CTkFont(family="Segoe UI", size=13),
        ).grid(row=1, column=0, sticky="w", padx=24)
        ctk.CTkLabel(
            card,
            text=(
                "OTA kaynağını yeniden deneyebilir veya release klasöründeki "
                "yerel manifesti kullanabilirsiniz."
            ),
            text_color="#d1d5db",
            justify=tk.LEFT,
            anchor="w",
            wraplength=700,
            font=ctk.CTkFont(family="Segoe UI", size=14),
        ).grid(row=2, column=0, sticky="ew", padx=24, pady=(18, 12))

        self.mode_control = ctk.CTkSegmentedButton(
            card,
            values=[MANIFEST_MODE_OTA_LABEL, MANIFEST_MODE_LOCAL_LABEL],
            command=self._on_mode_changed,
            selected_color="#7c3f18",
            selected_hover_color="#965024",
            unselected_color="#2a2f39",
            unselected_hover_color="#343a46",
            corner_radius=8,
            height=34,
        )
        self.mode_control.grid(row=3, column=0, sticky="w", padx=24)
        self.mode_control.set(MANIFEST_MODE_OTA_LABEL)

        ctk.CTkLabel(
            card,
            textvariable=self.status_text,
            text_color="#fbbf24",
            anchor="w",
            font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold"),
        ).grid(row=4, column=0, sticky="ew", padx=24, pady=(20, 6))
        ctk.CTkLabel(
            card,
            textvariable=self.detail_text,
            text_color="#f87171",
            justify=tk.LEFT,
            anchor="nw",
            wraplength=700,
            font=ctk.CTkFont(family="Segoe UI", size=12),
        ).grid(row=5, column=0, sticky="nsew", padx=24, pady=(0, 18))
        card.rowconfigure(5, weight=1)

        buttons = ctk.CTkFrame(card, fg_color="transparent")
        buttons.grid(row=6, column=0, sticky="ew", padx=24, pady=(0, 24))
        buttons.columnconfigure(0, weight=1)
        buttons.columnconfigure(1, weight=0)
        self.retry_button = ctk.CTkButton(
            buttons,
            text="Seçili kaynağı yükle",
            command=self._retry,
            fg_color="#7c3f18",
            hover_color="#965024",
            corner_radius=9,
            height=42,
        )
        self.retry_button.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        ctk.CTkButton(
            buttons,
            text="Kapat",
            command=self._close,
            fg_color="#2a2f39",
            hover_color="#343a46",
            corner_radius=9,
            width=120,
            height=42,
        ).grid(row=0, column=1)

    def _close(self) -> None:
        clear_default_remote_manifest_cache()
        self.root.destroy()

    def _on_mode_changed(self, selected_label: str) -> None:
        self.selected_mode = (
            MANIFEST_MODE_LOCAL
            if selected_label == MANIFEST_MODE_LOCAL_LABEL
            else MANIFEST_MODE_OTA
        )
        if self.selected_mode == MANIFEST_MODE_OTA:
            self.status_text.set("OTA kaynağı seçildi.")
            self.detail_text.set("GitHub manifest kanalı ve doğrulanmış önbellek kontrol edilecek.")
        else:
            self.status_text.set("Yerel kaynak seçildi.")
            self.detail_text.set("release/manifest.json ve SHA-256 dosyası kullanılacak.")

    def _retry(self) -> None:
        if self.busy:
            return
        self.busy = True
        self.mode_control.configure(state=tk.DISABLED)
        self.retry_button.configure(state=tk.DISABLED)
        self.status_text.set(
            "OTA çeviri listesi kontrol ediliyor..."
            if self.selected_mode == MANIFEST_MODE_OTA
            else "Yerel çeviri listesi kontrol ediliyor..."
        )
        self.detail_text.set("")

        def worker() -> None:
            try:
                manifest = load_default_bundled_manifest(
                    manifest_mode=self.selected_mode
                )
                source_info = default_manifest_source_info()
            except Exception as exc:  # noqa: BLE001 - recovery screen reports safe text.
                self.root.after(0, lambda error=exc: self._load_failed(error))
                return
            self.root.after(
                0,
                lambda payload=manifest, info=source_info: self._load_succeeded(
                    payload,
                    info,
                ),
            )

        threading.Thread(target=worker, daemon=True).start()

    def _load_failed(self, error: BaseException) -> None:
        self.busy = False
        self.mode_control.configure(state=tk.NORMAL)
        self.retry_button.configure(state=tk.NORMAL)
        self.status_text.set("Çeviri listesi yüklenemedi.")
        self.detail_text.set(str(error))

    def _load_succeeded(
        self,
        manifest: dict[str, Any],
        source_info: dict[str, str | None],
    ) -> None:
        for child in self.root.winfo_children():
            child.destroy()
        ModlistTranslationInstallerApp(
            self.root,
            initial_manifest=manifest,
            initial_manifest_mode=self.selected_mode,
            initial_source_info=source_info,
        )


def _create_credential_store() -> CredentialStore:
    try:
        return WindowsCredentialStore()
    except (CredentialStoreError, OSError):
        return MemoryCredentialStore()


def _load_startup_manifest(
    loader: Callable[..., dict[str, Any]] = load_default_bundled_manifest,
    source_info_loader: Callable[[], dict[str, str | None]] = default_manifest_source_info,
) -> tuple[dict[str, Any], dict[str, str | None]]:
    manifest = loader(manifest_mode=MANIFEST_MODE_OTA)
    return manifest, dict(source_info_loader())


class StartupSplashApp:
    """Show a responsive window while the OTA manifest is verified."""

    _POLL_INTERVAL_MS = 50

    def __init__(self, root: ctk.CTk) -> None:
        self.root = root
        self.result_queue: queue.Queue[
            tuple[str, object, dict[str, str | None] | None]
        ] = queue.Queue()
        self.closed = False

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.root.title("Çeviri Aracı hazırlanıyor")
        self.window_icon_photo: ImageTk.PhotoImage | None = None
        startup_icon = _default_release_icon_path()
        if startup_icon is not None:
            self.window_icon_photo = _apply_window_icon_asset(self.root, startup_icon)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

        self.frame = ctk.CTkFrame(
            self.root,
            width=620,
            height=260,
            corner_radius=14,
            fg_color="#17191f",
            border_width=1,
            border_color="#353944",
        )
        self.frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=16)
        self.frame.pack_propagate(False)

        ctk.CTkLabel(
            self.frame,
            text="Çeviri Aracı hazırlanıyor",
            anchor="w",
            font=ctk.CTkFont(family="Segoe UI", size=25, weight="bold"),
            text_color="#f3f4f6",
        ).pack(fill=tk.X, padx=30, pady=(31, 8))
        ctk.CTkLabel(
            self.frame,
            text=(
                "Güncel çeviri listesi güvenli OTA kaynağından kontrol ediliyor.\n"
                "İnternet bağlantısına göre bu işlem kısa bir süre alabilir."
            ),
            anchor="w",
            justify=tk.LEFT,
            font=ctk.CTkFont(family="Segoe UI", size=14),
            text_color="#b8bdc7",
        ).pack(fill=tk.X, padx=30, pady=(0, 22))
        self.progress = ctk.CTkProgressBar(
            self.frame,
            mode="indeterminate",
            height=9,
            corner_radius=5,
            fg_color="#292d35",
            progress_color="#ff8a24",
        )
        self.progress.pack(fill=tk.X, padx=30)
        self.progress.start()
        self.status_text = tk.StringVar(value="Çeviri listesi doğrulanıyor...")
        ctk.CTkLabel(
            self.frame,
            textvariable=self.status_text,
            anchor="w",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color="#8f96a3",
        ).pack(fill=tk.X, padx=30, pady=(12, 0))

        self.root.update_idletasks()
        width = 652
        height = 292
        x = max((self.root.winfo_screenwidth() - width) // 2, 0)
        y = max((self.root.winfo_screenheight() - height) // 2, 0)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.deiconify()
        self.root.lift()

        threading.Thread(
            target=self._load_manifest,
            name="mtw-startup-manifest",
            daemon=True,
        ).start()
        self.root.after(self._POLL_INTERVAL_MS, self._poll_result)

    def _load_manifest(self) -> None:
        try:
            manifest, source_info = _load_startup_manifest()
        except Exception as exc:  # noqa: BLE001 - recovery UI handles startup failures.
            self.result_queue.put(("error", exc, None))
        else:
            self.result_queue.put(("ready", manifest, source_info))

    def _poll_result(self) -> None:
        if self.closed:
            return
        try:
            kind, payload, source_info = self.result_queue.get_nowait()
        except queue.Empty:
            self.root.after(self._POLL_INTERVAL_MS, self._poll_result)
            return

        self.progress.stop()
        self.frame.destroy()
        self.root.resizable(True, True)
        if kind == "ready" and isinstance(payload, dict):
            app: object = ModlistTranslationInstallerApp(
                self.root,
                initial_manifest=payload,
                initial_manifest_mode=MANIFEST_MODE_OTA,
                initial_source_info=source_info,
            )
        else:
            error = payload if isinstance(payload, BaseException) else RuntimeError(
                "OTA çeviri listesi yüklenemedi."
            )
            app = ManifestRecoveryApp(self.root, initial_error=error)
        setattr(self.root, "_mtw_application", app)

    def _close(self) -> None:
        self.closed = True
        self.progress.stop()
        self.root.destroy()


def main() -> None:
    _configure_windows_app_identity()
    root = ctk.CTk()
    setattr(root, "_mtw_application", StartupSplashApp(root))
    root.mainloop()


if __name__ == "__main__":
    main()
