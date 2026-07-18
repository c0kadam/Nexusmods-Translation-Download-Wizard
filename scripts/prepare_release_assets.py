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
    parser.add_argument("--remote-config", type=Path)
    parser.add_argument("--expected-list-id")
    args = parser.parse_args()

    manifest = load_wizard_manifest(args.manifest)
    _assert_release_manifest_safe(manifest)
    if args.expected_list_id:
        _assert_manifest_list_id(manifest, args.expected_list_id)
    output = args.out
    output.mkdir(parents=True, exist_ok=True)

    manifest_path = output / "manifest.json"
    shutil.copy2(args.manifest, manifest_path)
    _write_sha256(manifest_path)
    _write_release_config(output, manifest)
    _write_release_readme(output)

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
    if args.remote_config is not None and args.remote_config.is_file():
        shutil.copy2(args.remote_config, output / "remote_manifest.json")

    print(f"Release assets: {output}")
    print(f"Manifest: {manifest_path}")
    print(f"Manifest SHA-256: {manifest_path.with_suffix(manifest_path.suffix + '.sha256')}")
    if copied_assets:
        print("Assets:")
        for asset in copied_assets:
            print(f"- {asset}")
    return 0


def _assert_manifest_list_id(manifest: dict[str, Any], expected_list_id: str) -> None:
    modlist = manifest.get("modlist") if isinstance(manifest.get("modlist"), dict) else {}
    actual = str(modlist.get("id") or "").strip().casefold()
    expected = str(expected_list_id or "").strip().casefold()
    if not expected:
        return
    if actual != expected:
        raise ValueError(
            f"manifest modlist.id mismatch: expected {expected!r}, got {actual!r}"
        )


def _assert_release_manifest_safe(manifest: dict[str, Any]) -> None:
    violations: list[str] = []

    def visit(value: object, path: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                if str(key).casefold() in {"api_key", "manual_api_key"} and str(item or "").strip():
                    violations.append(f"secret-like field: {child_path}")
                visit(item, child_path)
            return
        if isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{path}[{index}]")
            return
        if isinstance(value, str) and _looks_like_local_absolute_path(value):
            violations.append(f"local absolute path: {path}")

    visit(manifest, "")
    if violations:
        details = "; ".join(sorted(set(violations)))
        raise ValueError(f"manifest is not safe for public release: {details}")


def _looks_like_local_absolute_path(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    return bool(
        len(text) >= 3
        and text[0].isalpha()
        and text[1] == ":"
        and text[2] in {"\\", "/"}
    ) or text.startswith("\\\\")


def _write_release_config(output: Path, manifest: dict[str, Any]) -> None:
    modlist = manifest.get("modlist") if isinstance(manifest.get("modlist"), dict) else {}
    release_id = str(modlist.get("id") or "").strip()
    if not release_id:
        raise ValueError("manifest modlist.id is required to write release config")
    payload = {
        "schema_version": "mtw-release-config.v1",
        "release_id": release_id,
        "manifest_name": "manifest.json",
    }
    (output / "release_config.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _write_release_readme(output: Path) -> None:
    (output / "README.txt").write_text(
        """Bu klasör çeviri paketinin kullanıcıya açık release verilerini içerir.

manifest.json ve manifest.json.sha256
  Yerel çeviri listesidir. GUI'de Yerel seçildiğinde kullanılır.

remote_manifest.json
  OTA manifest kanalının adresini ve güvenlik sınırlarını tanımlar.

release_config.json
  Paketin hangi mod listesine ait olduğunu tanımlar.

branding.json, banner ve icon
  Paketin görsel kimliğidir.

Yan taraftaki modlist_translation_wizard klasörü uygulamanın dahili çalışma
dosyalarını içerir. O klasördeki dosyaları değiştirmeyin veya silmeyin.
""",
        encoding="utf-8",
    )


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
