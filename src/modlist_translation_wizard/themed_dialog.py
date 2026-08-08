"""CustomTkinter dialogs that follow the installer's visual language."""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from typing import Mapping

import customtkinter as ctk


@dataclass(frozen=True, slots=True)
class DialogPresentation:
    eyebrow: str
    symbol: str
    color_key: str


_PRESENTATIONS = {
    "info": DialogPresentation("BİLGİ", "i", "link"),
    "success": DialogPresentation("TAMAMLANDI", "✓", "success"),
    "warning": DialogPresentation("DİKKAT", "!", "warning"),
    "danger": DialogPresentation("İŞLEM TAMAMLANAMADI", "×", "danger"),
    "question": DialogPresentation("ONAYINIZ GEREKİYOR", "?", "accent"),
}


def dialog_presentation(tone: str) -> DialogPresentation:
    return _PRESENTATIONS.get(str(tone).casefold(), _PRESENTATIONS["info"])


def dialog_dimensions(message: str) -> tuple[int, int, bool]:
    text = str(message or "")
    line_count = text.count("\n") + 1
    long_content = len(text) > 760 or line_count > 13
    if long_content:
        return 760, min(620, max(440, 300 + line_count * 8)), True
    estimated_lines = line_count + max(0, len(text) // 78)
    return 650, min(560, max(390, 330 + estimated_lines * 12)), False


def show_themed_dialog(
    parent: tk.Misc,
    *,
    palette: Mapping[str, object],
    title: str,
    message: str,
    tone: str = "info",
    primary_label: str = "Anladım",
    secondary_label: str | None = None,
) -> bool:
    """Display a modal, branded dialog and return the selected action."""

    presentation = dialog_presentation(tone)
    width, height, use_textbox = dialog_dimensions(message)
    result = False
    popup = ctk.CTkToplevel(parent)
    popup.title(title)
    popup.geometry(f"{width}x{height}")
    popup.minsize(min(width, 600), min(height, 300))
    popup.transient(parent.winfo_toplevel())
    popup.configure(fg_color=palette["bg"])
    popup.columnconfigure(0, weight=1)
    popup.rowconfigure(1, weight=1)

    accent_color = palette.get(presentation.color_key, palette["accent"])
    primary_hover_color = (
        palette["accent_hover"]
        if presentation.color_key == "accent"
        else accent_color
    )
    previous_grab = popup.grab_current()

    def close(accepted: bool = False) -> None:
        nonlocal result
        result = accepted
        try:
            popup.grab_release()
        except tk.TclError:
            pass
        popup.destroy()
        if previous_grab is not None and previous_grab.winfo_exists():
            try:
                previous_grab.grab_set()
            except tk.TclError:
                pass

    header = ctk.CTkFrame(popup, fg_color="transparent")
    header.grid(row=0, column=0, sticky="ew", padx=26, pady=(24, 14))
    header.columnconfigure(1, weight=1)

    icon = ctk.CTkFrame(
        header,
        width=54,
        height=54,
        corner_radius=27,
        fg_color=palette["panel_alt"],
        border_width=2,
        border_color=accent_color,
    )
    icon.grid(row=0, column=0, rowspan=2, sticky="nw", padx=(0, 16))
    icon.grid_propagate(False)
    ctk.CTkLabel(
        icon,
        text=presentation.symbol,
        text_color=accent_color,
        font=ctk.CTkFont(family="Segoe UI Symbol", size=25, weight="bold"),
    ).place(relx=0.5, rely=0.5, anchor="center")

    ctk.CTkLabel(
        header,
        text=presentation.eyebrow,
        anchor="w",
        text_color=accent_color,
        font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
    ).grid(row=0, column=1, sticky="ew", pady=(1, 2))
    ctk.CTkLabel(
        header,
        text=title,
        anchor="w",
        text_color=palette["text"],
        font=ctk.CTkFont(family="Segoe UI", size=22, weight="bold"),
    ).grid(row=1, column=1, sticky="ew")

    body = ctk.CTkFrame(
        popup,
        corner_radius=10,
        fg_color=palette["panel"],
        border_width=1,
        border_color=palette["line"],
    )
    body.grid(row=1, column=0, sticky="nsew", padx=26, pady=(0, 18))
    body.columnconfigure(0, weight=1)
    body.rowconfigure(0, weight=1)

    if use_textbox:
        content = ctk.CTkTextbox(
            body,
            corner_radius=7,
            fg_color=palette["panel_alt"],
            border_width=0,
            text_color=palette["text"],
            font=ctk.CTkFont(family="Segoe UI", size=14),
            wrap=tk.WORD,
            activate_scrollbars=True,
        )
        content.grid(row=0, column=0, sticky="nsew", padx=16, pady=16)
        content.insert("1.0", message)
        content.configure(state=tk.DISABLED)
    else:
        ctk.CTkLabel(
            body,
            text=message,
            anchor="nw",
            justify=tk.LEFT,
            wraplength=width - 92,
            text_color=palette["text"],
            font=ctk.CTkFont(family="Segoe UI", size=15),
        ).grid(row=0, column=0, sticky="nsew", padx=20, pady=18)

    actions = ctk.CTkFrame(popup, fg_color="transparent")
    actions.grid(row=2, column=0, sticky="ew", padx=26, pady=(0, 24))
    actions.columnconfigure(0, weight=1)
    actions.columnconfigure(1, weight=0)
    actions.columnconfigure(2, weight=0)

    if secondary_label:
        ctk.CTkButton(
            actions,
            text=secondary_label,
            command=lambda: close(False),
            width=150,
            height=42,
            corner_radius=9,
            fg_color=palette["button"],
            hover_color=palette["button_hover"],
            text_color=palette["text"],
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
        ).grid(row=0, column=1, padx=(0, 10))

    ctk.CTkButton(
        actions,
        text=primary_label,
        command=lambda: close(True),
        width=170,
        height=42,
        corner_radius=9,
        fg_color=accent_color,
        hover_color=primary_hover_color,
        text_color=("#ffffff", "#ffffff"),
        font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
    ).grid(row=0, column=2)

    popup.protocol("WM_DELETE_WINDOW", lambda: close(False))
    popup.bind("<Escape>", lambda _event: close(False))
    popup.bind("<Return>", lambda _event: close(True))
    popup.update_idletasks()
    parent_window = parent.winfo_toplevel()
    parent_x = parent_window.winfo_rootx()
    parent_y = parent_window.winfo_rooty()
    parent_width = max(parent_window.winfo_width(), 1)
    parent_height = max(parent_window.winfo_height(), 1)
    x = parent_x + max((parent_width - width) // 2, 0)
    y = parent_y + max((parent_height - height) // 2, 0)
    popup.geometry(f"{width}x{height}+{x}+{y}")
    popup.lift()
    popup.focus_force()
    popup.grab_set()
    popup.wait_window()
    return result


def show_themed_toast(
    parent: tk.Misc,
    *,
    palette: Mapping[str, object],
    title: str,
    message: str,
    tone: str = "info",
    duration_ms: int = 6500,
) -> ctk.CTkFrame:
    """Show a non-blocking in-app guidance card without an acknowledgement step."""

    presentation = dialog_presentation(tone)
    accent_color = palette.get(presentation.color_key, palette["accent"])
    toast = ctk.CTkFrame(
        parent,
        width=500,
        corner_radius=11,
        fg_color=palette["panel"],
        border_width=1,
        border_color=accent_color,
    )
    toast.place(relx=1.0, x=-24, y=24, width=500, anchor="ne")
    toast.columnconfigure(1, weight=1)

    symbol = ctk.CTkLabel(
        toast,
        text=presentation.symbol,
        width=34,
        height=34,
        corner_radius=17,
        fg_color=palette["panel_alt"],
        text_color=accent_color,
        font=ctk.CTkFont(family="Segoe UI Symbol", size=17, weight="bold"),
    )
    symbol.grid(row=0, column=0, rowspan=2, sticky="n", padx=(14, 10), pady=14)
    ctk.CTkLabel(
        toast,
        text=title,
        anchor="w",
        text_color=palette["text"],
        font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold"),
    ).grid(row=0, column=1, sticky="ew", pady=(13, 2))
    ctk.CTkLabel(
        toast,
        text=message,
        anchor="w",
        justify=tk.LEFT,
        wraplength=385,
        text_color=palette["muted"],
        font=ctk.CTkFont(family="Segoe UI", size=13),
    ).grid(row=1, column=1, sticky="ew", pady=(0, 13))

    def close() -> None:
        if toast.winfo_exists():
            toast.destroy()

    ctk.CTkButton(
        toast,
        text="×",
        command=close,
        width=30,
        height=30,
        corner_radius=7,
        fg_color="transparent",
        hover_color=palette["panel_alt"],
        text_color=palette["muted"],
        font=ctk.CTkFont(family="Segoe UI Symbol", size=18),
    ).grid(row=0, column=2, sticky="ne", padx=(6, 8), pady=(8, 0))

    toast.update_idletasks()
    toast.lift()
    toast.after(max(1500, int(duration_ms)), close)
    return toast
