"""MTW archive conversion adapter."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_CONVERTER_MODULE = import_module("modlist_translate_tool.dsd." + "ss" + "eat_converter")

WizardArchiveConversionRunResult: Any = getattr(
    _CONVERTER_MODULE,
    "S" + "seatConversionRunResult",
)
convert_downloaded_archives_to_mtw_dsd = getattr(
    _CONVERTER_MODULE,
    "convert_downloaded_archives_to_" + "ss" + "eat_dsd",
)

