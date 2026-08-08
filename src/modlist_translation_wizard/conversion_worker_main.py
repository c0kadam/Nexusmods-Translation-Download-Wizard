"""Minimal standalone entry point for MTW conversion work."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main(arguments: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if arguments is None else arguments)
    if len(args) != 2:
        return 2

    mode, request_path = args
    if mode == "--convert-worker":
        from modlist_translation_wizard.conversion_worker import run_conversion_worker

        return run_conversion_worker(Path(request_path))
    if mode == "--plugin-convert-worker":
        from modlist_translate_tool.dsd.dynamic_string_converter import (
            run_plugin_pair_conversion_worker,
        )

        return run_plugin_pair_conversion_worker(Path(request_path))
    return 2


if __name__ == "__main__":
    os._exit(main())
