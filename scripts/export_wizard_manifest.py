"""Export a list-specific, discovery-free wizard manifest."""

from __future__ import annotations

import argparse
from pathlib import Path

from modlist_translation_wizard.manifest import export_wizard_manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile-scan", required=True, type=Path)
    parser.add_argument("--decisions", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--list-id", required=True)
    parser.add_argument("--list-name", required=True)
    parser.add_argument("--list-version", required=True)
    parser.add_argument("--output-mod-name", required=True)
    parser.add_argument("--channel", choices=("stable", "extended"), default="stable")
    parser.add_argument("--release-state", default="DRAFT")
    parser.add_argument("--registered-app-slug")
    args = parser.parse_args()
    result = export_wizard_manifest(
        profile_scan_path=args.profile_scan,
        decisions_path=args.decisions,
        output_path=args.out,
        list_id=args.list_id,
        list_name=args.list_name,
        list_version=args.list_version,
        output_mod_name=args.output_mod_name,
        channel=args.channel,
        release_state=args.release_state,
        registered_app_slug=args.registered_app_slug,
    )
    print(f"Wrote {result.manifest_path}")
    print(f"SHA-256 {result.sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
