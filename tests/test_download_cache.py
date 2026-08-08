from pathlib import Path

from modlist_translation_wizard.download_cache import (
    clear_download_cache,
    format_cache_size,
    inspect_download_cache,
)
from modlist_translation_wizard.gui_model import run_workspace_for_manifest


def _manifest(list_id: str, manifest_id: str) -> dict:
    return {
        "manifest_id": manifest_id,
        "modlist": {"id": list_id},
    }


def _write(path: Path, size: int = 16) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def test_current_cache_cleanup_removes_only_download_archives(tmp_path) -> None:
    manifest = _manifest("lorerim", "lorerim-stable")
    run_root = run_workspace_for_manifest(manifest, tmp_path)
    archive = _write(run_root / "downloads" / "100" / "200" / "translation.7z", 20)
    partial = _write(archive.with_name(f"{archive.name}.part"), 7)
    queue = _write(run_root / "runtime" / "download_queue.json", 11)
    extracted = _write(run_root / "staging" / "translation.esp", 13)

    summary = inspect_download_cache(manifest, workspace_root=tmp_path)
    result = clear_download_cache(manifest, workspace_root=tmp_path)

    assert summary.file_count == 2
    assert summary.total_bytes == 27
    assert result.deleted_files == 2
    assert result.deleted_bytes == 27
    assert result.failures == ()
    assert not archive.exists()
    assert not partial.exists()
    assert queue.exists()
    assert extracted.exists()


def test_all_cache_cleanup_covers_other_manifests_but_not_unmanaged_files(tmp_path) -> None:
    current = _manifest("nordicsouls", "nordicsouls-stable")
    other = _manifest("lorerim", "lorerim-stable")
    current_archive = _write(
        run_workspace_for_manifest(current, tmp_path) / "downloads" / "current.zip",
        9,
    )
    other_archive = _write(
        run_workspace_for_manifest(other, tmp_path) / "downloads" / "other.rar",
        12,
    )
    remote_manifest = _write(tmp_path / "remote" / "manifest.zip", 18)
    output_archive = _write(tmp_path / "exports" / "translation.7z", 21)

    summary = inspect_download_cache(current, scope="all", workspace_root=tmp_path)
    result = clear_download_cache(current, scope="all", workspace_root=tmp_path)

    assert summary.root_count == 2
    assert summary.file_count == 2
    assert result.deleted_files == 2
    assert not current_archive.exists()
    assert not other_archive.exists()
    assert remote_manifest.exists()
    assert output_archive.exists()


def test_cache_inventory_ignores_non_archive_download_state(tmp_path) -> None:
    manifest = _manifest("example", "example-stable")
    downloads = run_workspace_for_manifest(manifest, tmp_path) / "downloads"
    _write(downloads / "download_queue.json", 30)
    _write(downloads / "notes.txt", 40)

    summary = inspect_download_cache(manifest, workspace_root=tmp_path)

    assert summary.file_count == 0
    assert summary.total_bytes == 0


def test_format_cache_size_uses_readable_units() -> None:
    assert format_cache_size(0) == "0 B"
    assert format_cache_size(1024) == "1.0 KB"
    assert format_cache_size(1024**2) == "1.0 MB"
    assert format_cache_size(1024**3) == "1.00 GB"
