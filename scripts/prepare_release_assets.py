"""Prepare build-external assets for a curated release."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from modlist_translation_wizard.manifest import load_wizard_manifest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a release/ folder that can be shipped next to Ceviri Araci."
    )
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--branding", type=Path)
    parser.add_argument("--banner", type=Path)
    parser.add_argument("--icon", type=Path)
    args = parser.parse_args()

    manifest = load_wizard_manifest(args.manifest)
    output = args.out
    output.mkdir(parents=True, exist_ok=True)

    manifest_path = output / "manifest.json"
    shutil.copy2(args.manifest, manifest_path)
    _write_sha256(manifest_path)

    branding_payload = _load_or_create_branding(
        args.branding,
        manifest=manifest,
        banner=args.banner,
        icon=args.icon,
    )
    branding_path = output / "branding.json"
    branding_path.write_text(
        json.dumps(branding_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    copied_assets: list[Path] = []
    for source, key in ((args.banner, "banner"), (args.icon, "icon")):
        source = source or _asset_next_to_branding(args.branding, branding_payload.get(key))
        if source is None:
            continue
        target = output / Path(source).name
        shutil.copy2(source, target)
        copied_assets.append(target)
        branding_payload[key] = target.name

    if copied_assets:
        branding_path.write_text(
            json.dumps(branding_payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    print(f"Release assets: {output}")
    print(f"Manifest: {manifest_path}")
    print(f"Manifest SHA-256: {manifest_path.with_suffix(manifest_path.suffix + '.sha256')}")
    if copied_assets:
        print("Assets:")
        for asset in copied_assets:
            print(f"- {asset}")
    return 0


def _load_or_create_branding(
    branding_path: Path | None,
    *,
    manifest: dict[str, Any],
    banner: Path | None,
    icon: Path | None,
) -> dict[str, Any]:
    if branding_path is not None:
        payload = json.loads(branding_path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise ValueError("branding JSON must be an object")
    else:
        modlist = manifest.get("modlist") if isinstance(manifest.get("modlist"), dict) else {}
        display_name = str(modlist.get("name") or "Modlist").strip()
        payload = {
            "display_name": f"{display_name} Turkce Ceviri Paketi",
            "subtitle": f"{display_name} icin hazirlanmis ceviri araci",
            "accent_color": "#603415",
        }
    if banner is not None:
        payload["banner"] = banner.name
    if icon is not None:
        payload["icon"] = icon.name
    return payload


def _asset_next_to_branding(branding_path: Path | None, asset_name: object) -> Path | None:
    if branding_path is None:
        return None
    name = Path(str(asset_name or "")).name
    if not name:
        return None
    candidate = branding_path.parent / name
    return candidate if candidate.is_file() else None


def _write_sha256(path: Path) -> None:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_suffix(path.suffix + ".sha256").write_text(
        f"{digest}  {path.name}\n",
        encoding="ascii",
    )


if __name__ == "__main__":
    raise SystemExit(main())
