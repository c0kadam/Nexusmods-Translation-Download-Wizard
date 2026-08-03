"""Resolve and validate the external archive backend used by MTW."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping, Sequence

SEVEN_ZIP_ENV_VAR = "MODLIST_TRANSLATE_TOOL_7Z_EXE"
SEVEN_ZIP_RUNTIME_VERSION = "26.02"
SEVEN_ZIP_RELATIVE_PATH = Path("tools") / "7zip" / "7z.exe"

_BUNDLED_SHA256 = {
    "7z.exe": "83967f1b02b43c4efeda302795722c809e0e81b8307de73558d10484d5676a7d",
    "7z.dll": "69fd4df057985c40e510e2fac182881c7f85e90aa13ec703f763a8fdb2ce61f8",
}
_VERSION_PATTERN = re.compile(r"7-Zip(?: \(r\))?\s+([0-9]+(?:\.[0-9]+)+)", re.IGNORECASE)

_Runner = Callable[..., Any]
_Which = Callable[[str], str | None]


@dataclass(frozen=True, slots=True)
class ArchiveToolAttempt:
    source: str
    path: str
    status: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ArchiveToolResolution:
    status: str
    path: str | None
    source: str | None
    version: str | None
    attempts: tuple[ArchiveToolAttempt, ...]

    @property
    def available(self) -> bool:
        return self.status == "AVAILABLE" and self.path is not None

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": "mtw-archive-tool-diagnostic.v1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": self.status,
            "path": self.path,
            "source": self.source,
            "version": self.version,
            "attempts": [asdict(attempt) for attempt in self.attempts],
        }


@dataclass(frozen=True, slots=True)
class _Candidate:
    path: Path
    source: str
    verify_bundled_hashes: bool = False


def activate_archive_tool(
    explicit_path: Path | str | None = None,
    *,
    environ: MutableMapping[str, str] | None = None,
    runner: _Runner = subprocess.run,
    runtime_roots: Sequence[Path | str] | None = None,
    which: _Which = shutil.which,
) -> ArchiveToolResolution:
    """Resolve a working 7-Zip backend and expose it to the shared converter."""

    environment = environ if environ is not None else os.environ
    resolution = resolve_archive_tool(
        explicit_path,
        environ=environment,
        runner=runner,
        runtime_roots=runtime_roots,
        which=which,
    )
    if resolution.available:
        environment[SEVEN_ZIP_ENV_VAR] = str(resolution.path)
    return resolution


def resolve_archive_tool(
    explicit_path: Path | str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    runner: _Runner = subprocess.run,
    runtime_roots: Sequence[Path | str] | None = None,
    which: _Which = shutil.which,
) -> ArchiveToolResolution:
    """Return the first verified and launchable 7-Zip candidate."""

    environment = environ if environ is not None else os.environ
    attempts: list[ArchiveToolAttempt] = []
    for candidate in _archive_tool_candidates(
        explicit_path,
        environ=environment,
        runtime_roots=runtime_roots,
        which=which,
    ):
        path = candidate.path
        if not path.is_file():
            attempts.append(
                ArchiveToolAttempt(
                    source=candidate.source,
                    path=str(path),
                    status="REJECTED",
                    reason="file_not_found",
                )
            )
            continue

        if candidate.verify_bundled_hashes:
            reason = _bundled_integrity_failure(path)
            if reason is not None:
                attempts.append(
                    ArchiveToolAttempt(
                        source=candidate.source,
                        path=str(path),
                        status="REJECTED",
                        reason=reason,
                    )
                )
                continue

        version, failure = _probe_seven_zip(path, runner=runner)
        if failure is not None:
            attempts.append(
                ArchiveToolAttempt(
                    source=candidate.source,
                    path=str(path),
                    status="REJECTED",
                    reason=failure,
                )
            )
            continue

        attempts.append(
            ArchiveToolAttempt(
                source=candidate.source,
                path=str(path),
                status="AVAILABLE",
            )
        )
        return ArchiveToolResolution(
            status="AVAILABLE",
            path=str(path),
            source=candidate.source,
            version=version,
            attempts=tuple(attempts),
        )

    return ArchiveToolResolution(
        status="UNAVAILABLE",
        path=None,
        source=None,
        version=None,
        attempts=tuple(attempts),
    )


def write_archive_tool_diagnostic(
    path: Path | str,
    resolution: ArchiveToolResolution,
) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(
        json.dumps(resolution.to_payload(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
    return target


def _archive_tool_candidates(
    explicit_path: Path | str | None,
    *,
    environ: Mapping[str, str],
    runtime_roots: Sequence[Path | str] | None,
    which: _Which,
) -> list[_Candidate]:
    candidates: list[_Candidate] = []
    if explicit_path is not None and str(explicit_path).strip():
        candidates.append(_Candidate(Path(explicit_path), "explicit"))

    roots = (
        [Path(root) for root in runtime_roots]
        if runtime_roots is not None
        else _default_runtime_roots()
    )
    candidates.extend(
        _Candidate(root / SEVEN_ZIP_RELATIVE_PATH, "bundled", True)
        for root in roots
    )

    configured = str(environ.get(SEVEN_ZIP_ENV_VAR) or "").strip()
    if configured:
        candidates.append(_Candidate(Path(configured), "environment"))

    for command in ("7z", "7za"):
        found = which(command)
        if found:
            candidates.append(_Candidate(Path(found), "path"))

    program_files = str(environ.get("ProgramFiles") or r"C:\Program Files").strip()
    program_files_x86 = str(
        environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    ).strip()
    candidates.extend(
        [
            _Candidate(Path(program_files) / "7-Zip" / "7z.exe", "installed"),
            _Candidate(Path(program_files_x86) / "7-Zip" / "7z.exe", "installed"),
        ]
    )
    return _deduplicate_candidates(candidates)


def _default_runtime_roots() -> list[Path]:
    roots: list[Path] = []
    for value in (sys.argv[0] if sys.argv else None, sys.executable):
        if not value:
            continue
        try:
            roots.append(Path(value).resolve().parent)
        except OSError:
            continue

    try:
        module_path = Path(__file__).resolve()
    except OSError:
        module_path = Path(__file__)
    roots.extend([module_path.parent, module_path.parent.parent.parent, Path.cwd()])
    return _deduplicate_paths(roots)


def _deduplicate_candidates(candidates: Sequence[_Candidate]) -> list[_Candidate]:
    unique: list[_Candidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(os.path.abspath(str(candidate.path)))
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def _deduplicate_paths(paths: Sequence[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = os.path.normcase(os.path.abspath(str(path)))
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _bundled_integrity_failure(seven_zip: Path) -> str | None:
    for name, expected in _BUNDLED_SHA256.items():
        path = seven_zip.parent / name
        if not path.is_file():
            return f"bundled_{name.replace('.', '_')}_missing"
        try:
            actual = _sha256(path)
        except OSError as exc:
            return f"bundled_integrity_read_failed:{type(exc).__name__}"
        if actual != expected:
            return f"bundled_{name.replace('.', '_')}_sha256_mismatch"
    return None


def _probe_seven_zip(path: Path, *, runner: _Runner) -> tuple[str | None, str | None]:
    kwargs: dict[str, Any] = {
        "check": False,
        "capture_output": True,
        "timeout": 12,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = startupinfo
    try:
        completed = runner([str(path), "i", "-sccUTF-8"], **kwargs)
    except subprocess.TimeoutExpired:
        return None, "self_test_timeout"
    except OSError as exc:
        return None, f"launch_failed:{type(exc).__name__}:{exc}"

    if int(getattr(completed, "returncode", 1)) != 0:
        return None, f"self_test_exit_{getattr(completed, 'returncode', 'unknown')}"

    output = _decode_output(getattr(completed, "stdout", b""))
    output += "\n" + _decode_output(getattr(completed, "stderr", b""))
    match = _VERSION_PATTERN.search(output)
    if match is None:
        return None, "self_test_version_not_detected"
    return match.group(1), None


def _decode_output(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8-sig", errors="replace")
    return str(value)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
