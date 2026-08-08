"""Safe inventory and cleanup helpers for MTW-managed download archives."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from modlist_translation_wizard.gui_model import (
    default_workspace_root,
    run_workspace_for_manifest,
)


DownloadCacheScope = Literal["current", "all"]

_ARCHIVE_SUFFIXES = frozenset({".7z", ".zip", ".rar", ".part", ".crdownload"})


@dataclass(frozen=True, slots=True)
class DownloadCacheSummary:
    scope: DownloadCacheScope
    root_count: int
    file_count: int
    total_bytes: int


@dataclass(frozen=True, slots=True)
class DownloadCacheClearResult:
    scope: DownloadCacheScope
    deleted_files: int
    deleted_bytes: int
    failures: tuple[str, ...]


def inspect_download_cache(
    manifest: dict[str, Any],
    *,
    scope: DownloadCacheScope = "current",
    workspace_root: Path | str | None = None,
) -> DownloadCacheSummary:
    roots = _download_roots(manifest, scope=scope, workspace_root=workspace_root)
    file_count = 0
    total_bytes = 0
    for root in roots:
        for archive in _archive_files(root):
            file_count += 1
            try:
                total_bytes += archive.stat().st_size
            except OSError:
                continue
    return DownloadCacheSummary(
        scope=scope,
        root_count=len(roots),
        file_count=file_count,
        total_bytes=total_bytes,
    )


def clear_download_cache(
    manifest: dict[str, Any],
    *,
    scope: DownloadCacheScope = "current",
    workspace_root: Path | str | None = None,
) -> DownloadCacheClearResult:
    """Delete only known archive and partial-download files under MTW run folders."""

    roots = _download_roots(manifest, scope=scope, workspace_root=workspace_root)
    deleted_files = 0
    deleted_bytes = 0
    failures: list[str] = []
    for root in roots:
        resolved_root = root.resolve(strict=False)
        for archive in _archive_files(root):
            try:
                resolved_archive = archive.resolve(strict=False)
                if not resolved_archive.is_relative_to(resolved_root):
                    failures.append(f"Güvenli klasör dışında: {archive}")
                    continue
                size = archive.stat().st_size
                archive.unlink()
            except OSError as exc:
                failures.append(f"{archive}: {exc}")
                continue
            deleted_files += 1
            deleted_bytes += size
    return DownloadCacheClearResult(
        scope=scope,
        deleted_files=deleted_files,
        deleted_bytes=deleted_bytes,
        failures=tuple(failures),
    )


def format_cache_size(size_bytes: int) -> str:
    value = max(0, int(size_bytes))
    if value < 1024:
        return f"{value} B"
    if value < 1024**2:
        return f"{value / 1024:.1f} KB"
    if value < 1024**3:
        return f"{value / (1024**2):.1f} MB"
    return f"{value / (1024**3):.2f} GB"


def _download_roots(
    manifest: dict[str, Any],
    *,
    scope: DownloadCacheScope,
    workspace_root: Path | str | None,
) -> tuple[Path, ...]:
    if scope not in {"current", "all"}:
        raise ValueError(f"Unsupported download cache scope: {scope}")

    workspace = (
        Path(workspace_root)
        if workspace_root is not None
        else default_workspace_root()
    )
    runs_root = workspace / "runs"
    if scope == "current":
        downloads = run_workspace_for_manifest(manifest, workspace) / "downloads"
        return (downloads,) if _is_managed_download_root(downloads, runs_root) else ()
    if not runs_root.is_dir():
        return ()

    roots: list[Path] = []
    try:
        list_dirs = tuple(runs_root.iterdir())
    except OSError:
        return ()
    for list_dir in list_dirs:
        if not list_dir.is_dir():
            continue
        try:
            release_dirs = tuple(list_dir.iterdir())
        except OSError:
            continue
        for release_dir in release_dirs:
            if not release_dir.is_dir():
                continue
            downloads = release_dir / "downloads"
            if downloads.is_dir() and _is_managed_download_root(downloads, runs_root):
                roots.append(downloads)
    return tuple(roots)


def _is_managed_download_root(downloads: Path, runs_root: Path) -> bool:
    try:
        return downloads.resolve(strict=False).is_relative_to(
            runs_root.resolve(strict=False)
        )
    except OSError:
        return False


def _archive_files(root: Path) -> tuple[Path, ...]:
    if not root.is_dir():
        return ()
    try:
        candidates = root.rglob("*")
        return tuple(
            path
            for path in candidates
            if path.is_file() and path.suffix.casefold() in _ARCHIVE_SUFFIXES
        )
    except OSError:
        return ()
