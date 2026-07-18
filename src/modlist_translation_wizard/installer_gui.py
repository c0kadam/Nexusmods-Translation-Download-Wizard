"""End-user installer GUI for one bundled modlist translation release."""

from __future__ import annotations

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
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any, Callable

import customtkinter as ctk
from PIL import Image, ImageTk, UnidentifiedImageError

from modlist_translate_tool.app.workflow import scan_profile
from modlist_translate_tool.nexus.downloader import urllib_download_to_file

from modlist_translation_wizard.bundled import (
    MANIFEST_MODE_LOCAL,
    MANIFEST_MODE_OTA,
    clear_default_remote_manifest_cache,
    default_manifest_source_info,
    default_release_info,
    load_default_bundled_manifest,
)
from modlist_translation_wizard.conversion_worker import run_conversion_in_worker
from modlist_translation_wizard.credential_store import (
    CredentialStore,
    CredentialStoreError,
    MemoryCredentialStore,
    WindowsCredentialStore,
)
from modlist_translation_wizard.gui_model import (
    NON_PREMIUM_DELIVERY_LABEL,
    PREMIUM_DELIVERY_LABEL,
    api_key_notice,
    api_settings_url,
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
    run_workspace_for_manifest,
    translation_output_mod_name,
)
from modlist_translation_wizard.nexus_auth import (
    api_key_status,
    clear_api_key,
    load_api_key,
    store_manual_api_key,
)
from modlist_translation_wizard.manifest import write_wizard_manifest
from modlist_translation_wizard.non_premium import (
    failed_non_premium_downloads,
    next_non_premium_download,
    run_non_premium_nxm_download,
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


BANNER_IMAGE_MAX_WIDTH = 820
BANNER_IMAGE_MAX_HEIGHT = 126
BANNER_TEXT_COLUMN_WIDTH = 430
BANNER_WINDOW_PADDING = 140


def _banner_title_font_size(title: str) -> int:
    length = len(title.strip())
    if length <= 30:
        return 24
    if length <= 38:
        return 21
    if length <= 48:
        return 18
    return 16
DEFAULT_WINDOW_HEIGHT = 760
MIN_WINDOW_WIDTH = 1040
MAX_WINDOW_WIDTH = 1500
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
        self.app_id = self.summary["registered_app_id"]
        self.store: CredentialStore = _create_credential_store()
        self.window_icon_photo: ImageTk.PhotoImage | None = None

        self.root.title(self.branding.display_name)
        self._apply_window_icon()
        initial_width = self._initial_window_width()
        self.root.geometry(f"{initial_width}x{DEFAULT_WINDOW_HEIGHT}")
        self.root.minsize(MIN_WINDOW_WIDTH, 640)

        self.delivery_mode = tk.StringVar(value=PREMIUM_DELIVERY_LABEL)
        self.api_key = tk.StringVar()
        self.mo2_root = tk.StringVar()
        self.mo2_profile = tk.StringVar()
        self.status_text = tk.StringVar(value="Hazır.")
        self.profile_status_text = tk.StringVar(value="Modlist klasörü seçin.")
        self.auth_status_text = tk.StringVar(value="Nexus API anahtarı kontrol ediliyor.")
        self.download_status_text = tk.StringVar(value="Profil hazırlanınca indirme açılır.")
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

        self.profile_scan_path: Path | None = None
        self.preflight_payload: dict[str, Any] | None = None
        self.profile_override_accepted = False
        self.premium_plan_result: Any | None = None
        self.premium_download_result: Any | None = None
        self.non_premium_download_result: Any | None = None
        self.conversion_result: Any | None = None
        self.pending_nxm_url: str | None = None
        self.nxm_capture_server: NxmCaptureServer | None = None
        self.nxm_protocol_binding = WindowsNxmProtocolBinding()
        self.task_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.busy = False
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
        self.banner_image: ctk.CTkImage | None = None
        self.discord_support_icon: ctk.CTkImage | None = None
        self.completion_popup: ctk.CTkToplevel | None = None

        try:
            self.nxm_protocol_binding.recover_stale_binding()
        except NxmCaptureError:
            pass

        self._configure_style()
        self._build()
        self._refresh_auth_status()
        self._refresh_pipeline_buttons()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(100, self._poll_task_queue)

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

    def _apply_window_icon(self) -> None:
        icon_path = release_branding_asset_path(self.manifest, self.branding.icon)
        if icon_path is None:
            return
        try:
            self.root.iconbitmap(default=str(icon_path))
        except (OSError, tk.TclError):
            pass
        try:
            with Image.open(icon_path) as icon:
                icon_image = icon.copy()
            icon_image.thumbnail((256, 256), Image.Resampling.LANCZOS)
            self.window_icon_photo = ImageTk.PhotoImage(icon_image)
            self.root.iconphoto(True, self.window_icon_photo)
        except (OSError, ValueError, UnidentifiedImageError, tk.TclError):
            pass

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
        card.columnconfigure(0, weight=3)
        card.columnconfigure(1, weight=2, minsize=360)
        ctk.CTkLabel(
            card,
            textvariable=self.release_summary_text,
            text_color=self.colors["text"],
            anchor="w",
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
            wraplength=410,
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
        )
        self.manifest_source_label.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 10))

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
            messagebox.showinfo("İşlem sürüyor", "Manifest kaynağı işlem tamamlanınca değiştirilebilir.")
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
            self.manifest_source_info = dict(result["source_info"])
            self.summary = manifest_summary(self.manifest)
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
        ctk.CTkButton(
            main,
            text="Klasörü aç",
            command=self._open_output_folder,
            corner_radius=8,
            fg_color=self.colors["button"],
            hover_color=self.colors["button_hover"],
        ).grid(row=3, column=3, sticky="w", padx=(10, 18), pady=6)

        ctk.CTkLabel(
            main,
            text="Nexus API anahtarı",
            text_color=self.colors["text"],
            anchor="w",
        ).grid(row=4, column=0, sticky="w", padx=(18, 10), pady=(12, 6))
        ctk.CTkEntry(
            main,
            textvariable=self.api_key,
            show="*",
            corner_radius=8,
            fg_color=self.colors["panel_alt"],
            border_color=self.colors["line"],
        ).grid(row=4, column=1, sticky="ew", pady=(12, 6))
        api_actions = ctk.CTkFrame(main, fg_color="transparent")
        api_actions.grid(
            row=4,
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
            row=5,
            column=1,
            columnspan=3,
            sticky="w",
            pady=(0, 6),
        )

        ctk.CTkLabel(
            main,
            text="İndirme yöntemi",
            text_color=self.colors["text"],
            anchor="w",
        ).grid(row=6, column=0, sticky="w", padx=(18, 10), pady=(12, 8))
        methods = ctk.CTkFrame(main, fg_color="transparent")
        methods.grid(row=6, column=1, columnspan=3, sticky="w", pady=(12, 8))
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

        self.nxm_frame = ctk.CTkFrame(main, fg_color="transparent")
        self.nxm_frame.grid(row=7, column=0, columnspan=4, sticky="ew", padx=18, pady=(6, 16))
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
        self.details_button = ctk.CTkButton(
            actions,
            text="Detayları göster",
            command=self._toggle_details,
            corner_radius=8,
            fg_color=self.colors["button"],
            hover_color=self.colors["button_hover"],
        )
        self.details_button.grid(row=0, column=1, sticky="e", padx=18, pady=(16, 6))
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
        self.download_button.grid(row=3, column=0, sticky="ew", padx=(18, 8), pady=(14, 0))
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
        self.prepare_button.grid(row=3, column=1, sticky="ew", padx=(8, 18), pady=(14, 0))
        ctk.CTkLabel(
            actions,
            textvariable=self.download_status_text,
            text_color=self.colors["muted"],
            anchor="w",
            font=ctk.CTkFont(family="Segoe UI", size=12),
        ).grid(row=4, column=0, sticky="ew", padx=(18, 8), pady=(8, 16))
        self.prepare_status_label = ctk.CTkLabel(
            actions,
            textvariable=self.prepare_status_text,
            text_color=self.colors["muted"],
            anchor="w",
            font=ctk.CTkFont(family="Segoe UI", size=12),
        )
        self.prepare_status_label.grid(row=4, column=1, sticky="ew", padx=(8, 18), pady=(8, 16))
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
            messagebox.showerror("API kaydedilemedi", str(exc))
            return
        self.api_key.set("")
        self._log("Nexus API anahtarı kaydedildi.")
        self._refresh_auth_status()

    def _clear_api_key(self) -> None:
        if not self._api_key():
            self.api_key.set("")
            self._refresh_auth_status()
            return
        confirmed = messagebox.askyesno(
            "API anahtarını sil",
            "Kaydedilmiş Nexus API anahtarı bu bilgisayardan silinsin mi?",
        )
        if not confirmed:
            return
        try:
            clear_api_key(self.store, app_id=self.app_id)
        except Exception as exc:  # noqa: BLE001 - GUI boundary reports sanitized text.
            messagebox.showerror("API anahtarı silinemedi", str(exc))
            return
        self.api_key.set("")
        self._log("Kaydedilmiş Nexus API anahtarı silindi.")
        self._refresh_auth_status()

    def _open_api_key_page(self) -> None:
        webbrowser.open(api_settings_url(), new=2, autoraise=True)

    def _open_discord_support(self) -> None:
        webbrowser.open("https://discordapp.com/users/279006796524421130", new=2, autoraise=True)

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
            messagebox.showinfo(
                "Çıktı klasörü",
                "Modlist klasörü seçilince çıktı konumu otomatik belirlenecek.",
            )
            return
        if not path.exists():
            messagebox.showinfo(
                "Çıktı klasörü henüz yok",
                f"Çeviri hazırlanınca bu klasör oluşacak:\n{path}",
            )
            return
        webbrowser.open(path.resolve().as_uri(), new=2, autoraise=True)

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
            messagebox.showwarning("Profil hazır değil", "Modlist klasörü ve profil kontrolü tamamlanmalı.")
            return
        if not self._ensure_profile_continue_confirmed():
            return
        if self._delivery_mode_value() == "PREMIUM_API" and not self._api_key():
            messagebox.showwarning("API gerekli", "Nexus API anahtarı kaydedilmeli.")
            return
        if self.premium_plan_result is None:
            self._plan_downloads(auto_start=True)
            return
        self._download_files()

    def _plan_downloads(self, *, auto_start: bool = False) -> None:
        if not self.profile_scan_path or not self._ensure_profile_continue_confirmed():
            return
        api_key = self._api_key()
        if self._delivery_mode_value() == "PREMIUM_API" and not api_key:
            messagebox.showwarning("API gerekli", "Nexus API anahtarı kaydedilmeli.")
            return
        out_dir = self._run_workspace() / "runtime"
        download_dir = self._run_workspace() / "downloads"

        def work() -> Any:
            manifest_path = self._write_runtime_manifest()
            return plan_downloads_from_manifest(
                manifest_path=manifest_path,
                profile_scan_path=self.profile_scan_path,
                download_dir=download_dir,
                out_dir=out_dir,
                delivery_mode=self._delivery_mode_value(),
                api_key=api_key,
                allow_profile_drift=self.profile_override_accepted,
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
        if self._delivery_mode_value() == "NON_PREMIUM_NXM":
            self._open_next_non_premium_page()
            return
        api_key = self._api_key()
        if not api_key:
            messagebox.showwarning("API gerekli", "Nexus API anahtarı kaydedilmeli.")
            return
        queue_path = self._current_download_queue_path()

        def work() -> Any:
            download_total = self._eta_total

            def tracked_downloader(url: str, part_path: Path) -> int:
                bytes_written = urllib_download_to_file(url, part_path)
                self.task_queue.put(
                    (
                        "eta",
                        {
                            "phase": "premium_download",
                            "completed_delta": 1,
                            "total": download_total,
                            "label": "Dosya indiriliyor",
                        },
                    )
                )
                return bytes_written

            return run_premium_downloads_from_plan(
                plan=self.premium_plan_result,
                api_key=api_key,
                queue_path=queue_path,
                file_downloader=tracked_downloader,
            )

        def done(result: Any) -> None:
            self.premium_download_result = result
            self.conversion_result = None
            readiness = download_queue_readiness(self.manifest, result.download_run.updated_queue_payload)
            run_summary = result.download_run.manifest_payload.get("summary", {})
            if readiness["complete"]:
                self.download_status_text.set(
                    f"Tamamlandi: {run_summary.get('downloaded', 0)} indirildi, "
                    f"{run_summary.get('already_present', 0)} zaten vardi."
                )
                self._set_status("Tüm dosyalar hazır.", "success")
                self._set_progress(75, "Çeviri hazırlanabilir")
            else:
                self.download_status_text.set(
                    f"{readiness['missing_count']} dosya eksik, "
                    f"{run_summary.get('failed', 0)} hata."
                )
                self._set_status("İndirme tamamlanmadı.", "danger")
                self._set_progress(55, "İndirme devam etmeli")
            self._log(f"İndirme sonucu: {result.download_run.manifest_path}")
            self._refresh_pipeline_buttons()

        self._set_progress(55, "Dosya indiriliyor")
        queue_payload = json.loads(queue_path.read_text(encoding="utf-8"))
        readiness = download_queue_readiness(self.manifest, queue_payload)
        self._start_time_estimate(
            phase="premium_download",
            total=int(readiness.get("missing_count") or 0),
            progress_base=55,
            progress_span=18,
        )
        self._run_task("Dosya indiriliyor", work, done)

    def _open_next_non_premium_page(self) -> None:
        if self.premium_plan_result is None:
            self._plan_downloads(auto_start=True)
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
            messagebox.showwarning("Pano boş", "Panoda okunabilir indirme bağlantısı bulunamadı.")
            return
        self.nxm_link.set(value)
        self._submit_nxm_link()

    def _submit_nxm_link(self) -> None:
        if self.premium_plan_result is None:
            messagebox.showwarning("İndirme hazır değil", "Önce indirme hazırlığı yapılmalı.")
            return
        api_key = self._api_key()
        nxm_url = self.nxm_link.get().strip()
        if not nxm_url:
            messagebox.showwarning(
                "İndirme bağlantısı gerekli",
                "Slow Download ile oluşan indirme bağlantısı gerekli.",
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
                self.download_status_text.set(
                    f"Dosya indirildi. Kalan: {readiness['missing_count']} / "
                    f"{readiness['required_count']}."
                )
            else:
                failed_label = self._first_non_premium_failure_label(result.updated_queue_payload)
                self.download_status_text.set(
                f"Dosya indirilemedi: {failed_label or 'hata ayrıntısı detaylarda'}."
                )
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
            messagebox.showerror(
                "Tarayıcı indirme yakalama başlatılamadı",
                f"İndirme bağlantısı Çeviri aracı'na yönlendirilemedi: {exc}",
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
        self._submit_nxm_link()

    def _process_pending_nxm(self) -> None:
        if self.busy or not self.pending_nxm_url:
            return
        nxm_url = self.pending_nxm_url
        self.pending_nxm_url = None
        self._handle_captured_nxm(nxm_url)

    def _prepare_translation(self) -> None:
        if not self.profile_scan_path or self.premium_plan_result is None:
            messagebox.showwarning("Hazırlık gerekli", "Önce çevirileri indirin.")
            return
        if not self._ensure_profile_continue_confirmed():
            return
        queue_path = self._current_download_queue_path()
        queue_payload = json.loads(queue_path.read_text(encoding="utf-8"))
        readiness = download_queue_readiness(self.manifest, queue_payload)
        if not readiness["complete"]:
            messagebox.showwarning(
                "Dosyalar eksik",
                f"{readiness['missing_count']} gerekli dosya henüz hazır değil.",
            )
            return
        run_workspace = self._run_workspace()
        manifest_path = self._write_runtime_manifest()
        selected_mods_root = self._selected_mods_root()
        if selected_mods_root is None:
            messagebox.showwarning("Modlist klasörü gerekli", "Önce modlist klasörünü seçin.")
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
        popup.title("Çeviri paketi hazır")
        popup.resizable(False, False)
        popup.transient(self.root)
        popup.configure(fg_color=self.colors["bg"])

        def close_popup() -> None:
            try:
                popup.grab_release()
            except tk.TclError:
                pass
            self.completion_popup = None
            popup.destroy()

        popup.protocol("WM_DELETE_WINDOW", close_popup)

        card = ctk.CTkFrame(
            popup,
            fg_color=self.colors["panel"],
            corner_radius=16,
            border_width=1,
            border_color=self.colors["line"],
        )
        card.pack(fill=tk.BOTH, expand=True, padx=22, pady=22)

        ctk.CTkLabel(
            card,
            text="Çeviri Paketi Başarıyla Kuruldu",
            text_color=self.colors["success"],
            font=ctk.CTkFont(family="Segoe UI", size=24, weight="bold"),
        ).pack(anchor="w", padx=24, pady=(22, 10))
        ctk.CTkLabel(
            card,
            text="Çeviri paketi başarıyla bu hedefe kuruldu:",
            text_color=self.colors["text"],
            font=ctk.CTkFont(family="Segoe UI", size=15),
        ).pack(anchor="w", padx=24)
        ctk.CTkLabel(
            card,
            text=str(output_folder),
            text_color=self.colors["text"],
            fg_color=self.colors["panel_alt"],
            corner_radius=10,
            justify=tk.LEFT,
            anchor="w",
            wraplength=620,
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
        ).pack(fill=tk.X, padx=24, pady=(10, 18), ipady=10)
        ctk.CTkLabel(
            card,
            text="MO2'de çeviriyi aktif etmeyi unutmayın.",
            text_color=self.colors["warning"],
            font=ctk.CTkFont(family="Segoe UI", size=17, weight="bold"),
        ).pack(anchor="w", padx=24, pady=(0, 22))

        buttons = ctk.CTkFrame(card, fg_color="transparent")
        buttons.pack(fill=tk.X, padx=24, pady=(0, 22))
        buttons.columnconfigure(0, weight=1)
        buttons.columnconfigure(1, weight=1)
        ctk.CTkButton(
            buttons,
            text="Klasörü aç",
            command=self._open_output_folder,
            corner_radius=10,
            fg_color=self.colors["button"],
            hover_color=self.colors["button_hover"],
            height=40,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ctk.CTkButton(
            buttons,
            text="Tamam",
            command=close_popup,
            corner_radius=10,
            fg_color=self.colors["accent"],
            hover_color=self.colors["accent_hover"],
            height=40,
        ).grid(row=0, column=1, sticky="ew", padx=(8, 0))

        popup.update_idletasks()
        width = max(700, popup.winfo_reqwidth())
        height = max(320, popup.winfo_reqheight())
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
            messagebox.showwarning(
                "Profil hazır değil",
                "Modlist klasörü ve profil kontrolü tamamlanmalı.",
            )
            return False
        if self._preflight_ready():
            return True
        if not self._profile_check_completed():
            messagebox.showwarning(
                "Profil hazır değil",
                "Profil kontrolü tamamlanmadan indirme başlatılamaz.",
            )
            return False

        summary = preflight_summary(self.preflight_payload)
        accepted = messagebox.askyesno(
            "Profil birebir eşleşmiyor",
            "Seçili profil stok paketle birebir uyuşmuyor.\n\n"
            f"Eşleşen çeviri: {summary['matched_entries']}/{summary['manifest_entries']}\n"
            f"Eksik hedef: {summary['missing_entries']}\n"
            f"Uyarı: {summary['compatibility_warnings']}\n\n"
            "Bu durumda çeviri yine kurulabilir, ancak mod listenizde yaptığınız "
            "ekleme veya değişikliklerden kaynaklı bazı çeviriler uygulanmayabilir.\n\n"
            "Yine de devam etmek istiyor musunuz?",
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
        self.premium_plan_result = None
        self.premium_download_result = None
        self.non_premium_download_result = None
        self.conversion_result = None
        self.pending_nxm_url = None
        self.nxm_link.set("")
        self.output_folder_text.set(self._output_folder_display())
        self.download_status_text.set("Profil hazırlanınca indirme açılır.")
        self.prepare_status_text.set("Tüm gerekli dosyalar hazır olmadan başlatılamaz.")
        self._reset_prepare_status_style()
        if self.completion_popup is not None and self.completion_popup.winfo_exists():
            self.completion_popup.destroy()
            self.completion_popup = None
        self._refresh_non_premium_prompt()
        self._refresh_pipeline_buttons()

    def _refresh_non_premium_prompt(self) -> None:
        if self._delivery_mode_value() != "NON_PREMIUM_NXM":
            self.nxm_status_text.set("")
            return
        if self.premium_plan_result is None:
            self.nxm_status_text.set(
                "Ücretsiz modda Nexus sayfası açılır; Slow Download'a tıklayınca Çeviri aracı linki yakalar."
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

    def _show_non_premium_failures(
        self,
        queue_payload: dict[str, Any],
        *,
        dialog: bool = False,
    ) -> None:
        failed = failed_non_premium_downloads(queue_payload)
        if not failed:
            if dialog:
                messagebox.showinfo("Dosya yok", "Açılacak yeni Nexus dosyası bulunamadı.")
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
            messagebox.showwarning("İndirilemeyen dosyalar", "\n\n".join(lines))

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

    def _delivery_mode_value(self) -> str:
        return delivery_mode_value(self.delivery_mode.get())

    def _run_workspace(self) -> Path:
        return run_workspace_for_manifest(self.manifest)

    def _output_mod_name(self) -> str:
        return translation_output_mod_name(
            self.mo2_root.get().strip(),
            fallback_modlist_name=self.summary.get("modlist_name") or "Modlist",
        )

    def _selected_mods_root(self) -> Path | None:
        root_text = self.mo2_root.get().strip()
        if not root_text:
            return None
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
        total = int(payload.get("total_archives") or 0)
        processed = int(payload.get("processed_archives") or 0)
        if total > 0:
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
        self._set_status("Hata oluştu.", "danger", prominent=True)
        self.progress_label.set("İşlem hata verdi")
        self._log(f"Hata: {error}")
        log_path = self._write_gui_error_log(error, traceback_text)
        if log_path is not None:
            self._log(f"Hata logu: {log_path}")
            message = f"{error}\n\nHata logu:\n{log_path}"
        else:
            message = str(error)
        messagebox.showerror("İşlem hatası", message)
        self._refresh_pipeline_buttons()

    def _run_task(self, label: str, work: Callable[[], Any], done: Callable[[Any], None]) -> None:
        if self.busy:
            messagebox.showinfo("İşlem sürüyor", "Devam eden işlem tamamlanmalı.")
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
        self.details_frame.grid(row=5, column=0, columnspan=2, sticky="nsew", pady=(8, 0))
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


def main() -> None:
    root = ctk.CTk()
    try:
        manifest = load_default_bundled_manifest(manifest_mode=MANIFEST_MODE_OTA)
        source_info = default_manifest_source_info()
    except Exception as exc:  # noqa: BLE001 - recovery UI keeps startup usable.
        ManifestRecoveryApp(root, initial_error=exc)
    else:
        ModlistTranslationInstallerApp(
            root,
            initial_manifest=manifest,
            initial_manifest_mode=MANIFEST_MODE_OTA,
            initial_source_info=source_info,
        )
    root.mainloop()


if __name__ == "__main__":
    main()
