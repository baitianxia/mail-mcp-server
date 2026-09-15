#!/usr/bin/env python3
"""Build the user-facing Windows package for the mail assistant.

The builder is deliberately allowlisted.  It never archives the worktree as a
whole and it never downloads a runtime.  A local build is labelled
``UNVERIFIED``; a formal Windows package is accepted only when it is built on a
Windows x64 runner with an explicitly supplied, copied runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable


VERSION = "0.9.0"
PACKAGE_NAME = "mail-mcp-server"
DISPLAY_NAME = "邮件助手"
MCP_SERVER_NAME = "mail-mcp"
BUNDLE_NAME = f"{PACKAGE_NAME}-{VERSION}"
LOCAL_ARCHIVE_NAME = f"{BUNDLE_NAME}-windows-x64-UNVERIFIED.zip"
GATED_ARCHIVE_NAME = f"{BUNDLE_NAME}-windows-x64.zip"
ARCHIVE_NAME = LOCAL_ARCHIVE_NAME
GENERATED_FILES = ("release-manifest.json", "SHA256SUMS.txt")
RUNTIME_MANIFEST = "payload/runtime/runtime-manifest.json"
SBOM_PATH = "payload/sbom.json"

# This is the public package allowlist.  Publisher-only tests and build
# plumbing stay out of the user ZIP.  Keep paths POSIX-style even on Windows.
EXACT_FILES = (
    "README.md",
    "START-HERE.html",
    "INSTALL.cmd",
    "CONFIGURE.cmd",
    "OPEN-CONFIG.cmd",
    "UNINSTALL.cmd",
    "LICENSE",
    "CHANGELOG.md",
    "config/settings.example.json",
    ".claude-plugin/plugin.json",
    ".mcp.json",
    "SKILL.md",
    "docs/architecture.md",
    "docs/browser-orchestration.md",
    "mcp/check-python.py",
    "mcp/coremail_backend.py",
    "mcp/describe-python.py",
    "mcp/local_discovery.py",
    "mcp/run-server.ps1",
    "mcp/server.py",
    "mcp/validate-config.py",
    "mcp/windows_mapi.py",
    "scripts/configure-account.ps1",
    "scripts/install.ps1",
    "scripts/register_claude_user_mcp.py",
    "scripts/setup-account.ps1",
    "scripts/uninstall.ps1",
    "scripts/verify-release.py",
    "scripts/windows-credential.ps1",
    "scripts/windows-lifecycle-common.ps1",
    "scripts/windows-tool-discovery.ps1",
    "skills/coremail/SKILL.md",
    "skills/web-to-coremail/SKILL.md",
    "scripts/mcp-healthcheck.ps1",
)

COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RUNTIME_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
FORBIDDEN_PARTS = {
    ".git",
    "node_modules",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".tox",
}
FORBIDDEN_RUNTIME_NAMES = {
    "npm",
    "npm.cmd",
    "pnpm",
    "pnpm.cmd",
    "npx",
    "npx.cmd",
    "corepack",
    "corepack.cmd",
}
FORBIDDEN_RUNTIME_PARTS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".tox",
    "doc",
    "docs",
    "ensurepip",
    "idlelib",
    "include",
    "includes",
    "lib2to3",
    "libs",
    "site-packages",
    "scripts",
    "tcl",
    "test",
    "tests",
    "tools",
    "turtledemo",
    "venv",
}
FORBIDDEN_RUNTIME_SUFFIXES = {".h", ".lib", ".pdb", ".pyc", ".pyo", ".pyi"}
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class ReleaseError(RuntimeError):
    """Raised when a release cannot be built safely."""


def normalized_system() -> str:
    value = platform.system().lower()
    return "windows" if value.startswith("win") else value


def normalized_machine() -> str:
    value = platform.machine().lower()
    if value in {"amd64", "x86_64"}:
        return "x64"
    if value in {"aarch64", "arm64"}:
        return "arm64"
    return value


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bytes_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _pe_pointer_bits(payload: bytes) -> int | None:
    """Return the PE architecture for a Windows executable, if recognizable."""
    if len(payload) < 0x40 or payload[:2] != b"MZ":
        return None
    pe_offset = int.from_bytes(payload[0x3C:0x40], "little")
    if pe_offset < 0 or pe_offset + 26 > len(payload) or payload[pe_offset : pe_offset + 4] != b"PE\0\0":
        return None
    machine = int.from_bytes(payload[pe_offset + 4 : pe_offset + 6], "little")
    optional_magic = int.from_bytes(payload[pe_offset + 24 : pe_offset + 26], "little")
    if machine == 0x8664 and optional_magic == 0x20B:
        return 64
    if machine == 0x14C and optional_magic == 0x10B:
        return 32
    return None


def json_bytes(payload: object) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def _safe_relative(value: str, *, label: str = "path") -> PurePosixPath:
    if (
        not value
        or "\\" in value
        or "\x00" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(character in value for character in '<>:"|?*')
    ):
        raise ReleaseError(f"{label} must be a non-empty POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ReleaseError(f"unsafe {label}: {value}")
    if path.as_posix() != value:
        raise ReleaseError(f"non-canonical {label}: {value}")
    for part in path.parts:
        if not part or part.endswith((" ", ".")):
            raise ReleaseError(f"unsafe {label}: {value}")
        if part.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
            raise ReleaseError(f"Windows reserved name in {label}: {value}")
    return path


def _assert_regular(path: Path, relative: str) -> None:
    try:
        metadata = path.lstat()
        if path.is_symlink() or bool(getattr(metadata, "st_file_attributes", 0) & 0x400):
            raise ReleaseError(f"reparse point is forbidden in release input: {relative}")
        if not path.is_file():
            raise ReleaseError(f"required release file is missing or not regular: {relative}")
    except OSError as exc:
        raise ReleaseError(f"cannot inspect release input {relative}: {exc}") from exc


def _is_link_or_reparse(path: Path, relative: str) -> bool:
    """Return whether an input path can redirect outside the release tree.

    ``Path.is_symlink`` is sufficient on POSIX, while Windows junctions and
    other reparse points are exposed through ``st_file_attributes``.  An
    inspection failure is unsafe and therefore becomes a build error instead
    of being treated as an ordinary file.
    """
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ReleaseError(f"cannot inspect release input {relative}: {exc}") from exc
    return path.is_symlink() or bool(getattr(metadata, "st_file_attributes", 0) & 0x400)


def _assert_path_chain_has_no_links(path: Path, label: str) -> None:
    """Reject a Windows junction/symlink in any existing ancestor.

    Resolving a source directory before this check would make a junction look
    like an ordinary directory and could copy files from outside the intended
    runtime tree.  The public builder runs on POSIX hosts too; the stricter
    ancestor walk is therefore enabled only for a real Windows build host,
    while direct link entries remain rejected on every host.
    """
    if os.name != "nt":
        return
    try:
        current = Path(os.path.abspath(str(path)))
    except (OSError, ValueError) as exc:
        raise ReleaseError(f"{label} is not a valid path: {path}") from exc
    while True:
        if _is_link_or_reparse(current, str(current)):
            raise ReleaseError(f"{label} traverses a link or reparse point: {current}")
        parent = current.parent
        if parent == current:
            return
        current = parent


def validated_files(project_root: Path) -> list[Path]:
    """Return the reviewed source allowlist after identity and secret checks."""
    project_root = Path(project_root)
    if _is_link_or_reparse(project_root, str(project_root)):
        raise ReleaseError(f"project root is a link or reparse point: {project_root}")
    _assert_path_chain_has_no_links(project_root, "project root")
    project_root = project_root.resolve()
    files: list[Path] = []
    for value in EXACT_FILES:
        relative = _safe_relative(value).as_posix()
        source = project_root / Path(*PurePosixPath(relative).parts)
        _assert_path_chain_has_no_links(source, f"release input {relative}")
        _assert_regular(source, relative)
        files.append(Path(relative))

    try:
        plugin = json.loads((project_root / ".claude-plugin" / "plugin.json").read_text("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"plugin manifest is unavailable or invalid: {exc}") from exc
    if not isinstance(plugin, dict) or plugin.get("name") != PACKAGE_NAME:
        raise ReleaseError("unexpected plugin identity")
    if plugin.get("version") != VERSION:
        raise ReleaseError(
            f"release version {VERSION} does not match plugin version {plugin.get('version')}"
        )
    if plugin.get("displayName") != DISPLAY_NAME:
        raise ReleaseError("plugin displayName must be the generic mail assistant name")
    try:
        mcp_config = json.loads((project_root / ".mcp.json").read_text("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"MCP package declaration is unavailable or invalid: {exc}") from exc
    servers = mcp_config.get("mcpServers") if isinstance(mcp_config, dict) else None
    if not isinstance(servers, dict) or set(servers) != {MCP_SERVER_NAME}:
        raise ReleaseError("MCP package declaration must contain only mail-mcp")
    server = servers[MCP_SERVER_NAME]
    expected_args = [
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        "${CLAUDE_PLUGIN_ROOT}/mcp/run-server.ps1",
    ]
    if (
        not isinstance(server, dict)
        or set(server) != {"type", "command", "args"}
        or server.get("type") != "stdio"
        or str(server.get("command", "")).lower() != "powershell.exe"
        or server.get("args") != expected_args
    ):
        raise ReleaseError("MCP package declaration does not match the portable mail launcher")
    try:
        example = json.loads((project_root / "config" / "settings.example.json").read_text("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"configuration example is unavailable or invalid: {exc}") from exc
    if not isinstance(example, dict) or "password" in json.dumps(example).lower():
        raise ReleaseError("configuration example must not contain a password")
    return files


def _generated_start_here(project_root: Path, *, archive_name: str) -> bytes:
    """Render the user-facing HTML entry point from the reviewed source.

    Keeping the source in the repository makes it reviewable, while replacing
    the archive placeholder during a build prevents a stale versioned example
    from being shipped in a later release.
    """
    source = (project_root / "START-HERE.html").read_text(encoding="utf-8")
    rendered = source.replace("mail-mcp-server-*-windows-x64.zip", archive_name)
    if archive_name not in rendered or VERSION not in rendered:
        raise ReleaseError("START-HERE.html does not contain the package archive placeholder")
    return rendered.encode("utf-8")


def _git_commit(project_root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip().lower()
    return value if COMMIT_RE.fullmatch(value) else None


def _runtime_source_label(runtime_dir: Path | None, runtime_source: str | None) -> str:
    if runtime_source and runtime_source.strip():
        value = runtime_source.strip()
        if len(value) > 256 or any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ReleaseError("runtime source provenance must be a short single-line value")
        return value
    if runtime_dir is not None:
        return "publisher-supplied-portable-python"
    return "not-bundled-local-candidate"


def _runtime_files(runtime_dir: Path) -> list[tuple[PurePosixPath, Path]]:
    _assert_path_chain_has_no_links(runtime_dir, "runtime directory")
    try:
        if _is_link_or_reparse(runtime_dir, str(runtime_dir)):
            raise ReleaseError(f"runtime directory is a link or reparse point: {runtime_dir}")
    except FileNotFoundError as exc:
        raise ReleaseError(f"runtime directory is not available: {runtime_dir}") from exc
    runtime_dir = runtime_dir.resolve(strict=True)
    if not runtime_dir.is_dir():
        raise ReleaseError(f"runtime directory is not a directory: {runtime_dir}")
    pairs: list[tuple[PurePosixPath, Path]] = []
    folded: dict[str, str] = {}
    for path in sorted(runtime_dir.rglob("*")):
        relative = path.relative_to(runtime_dir).as_posix()
        safe = _safe_relative(relative, label="runtime path")
        folded_key = safe.as_posix().casefold()
        previous = folded.get(folded_key)
        if previous is not None and previous != safe.as_posix():
            raise ReleaseError(
                f"runtime paths collide on Windows: {previous} and {safe.as_posix()}"
            )
        folded[folded_key] = safe.as_posix()
        if any(part.lower() in FORBIDDEN_PARTS for part in safe.parts):
            raise ReleaseError(f"forbidden runtime path: {relative}")
        if any(part.casefold() in FORBIDDEN_RUNTIME_PARTS for part in safe.parts):
            raise ReleaseError(f"development or test runtime path is forbidden: {relative}")
        if any(part.casefold() in FORBIDDEN_RUNTIME_NAMES for part in safe.parts):
            raise ReleaseError(f"package-manager path is forbidden in runtime: {relative}")
        if path.suffix.casefold() in FORBIDDEN_RUNTIME_SUFFIXES:
            raise ReleaseError(f"development runtime file is forbidden: {relative}")
        if _is_link_or_reparse(path, relative):
            raise ReleaseError(f"runtime link or reparse point is forbidden: {relative}")
        if path.is_dir():
            continue
        _assert_regular(path, relative)
        pairs.append((safe, path))
    if not pairs:
        raise ReleaseError("the supplied runtime directory is empty")
    executable = [path for relative, path in pairs if relative.as_posix().lower() == "python.exe"]
    if not executable:
        raise ReleaseError("a bundled Windows runtime must contain payload/runtime/python.exe")
    return pairs


def _runtime_manifest(
    runtime_dir: Path | None,
    *,
    runtime_source: str | None,
    runtime_version: str | None,
    windows_gate: bool,
    source_commit: str | None,
) -> tuple[dict[str, object], list[tuple[str, bytes]]]:
    if runtime_dir is not None:
        runtime_dir = Path(runtime_dir)
    normalized_runtime_version = runtime_version.strip() if isinstance(runtime_version, str) else None
    if normalized_runtime_version and not RUNTIME_VERSION_RE.fullmatch(normalized_runtime_version):
        raise ReleaseError("runtime version must use a concrete semantic version")
    if windows_gate and not normalized_runtime_version:
        raise ReleaseError("the Windows-gated package requires --runtime-version")
    if runtime_dir is None:
        if windows_gate:
            raise ReleaseError("the Windows-gated package requires --runtime-dir")
        if normalized_runtime_version:
            raise ReleaseError("--runtime-version requires --runtime-dir")
        manifest: dict[str, object] = {
            "schema_version": 1,
            "kind": "python",
            "target": {"system": "windows", "machine": "x64"},
            "bundled": False,
            "status": "unverified",
            "source": _runtime_source_label(None, runtime_source),
            "version": normalized_runtime_version,
            "pointer_bits": None,
            "executable_relative_path": None,
            "executable_sha256": None,
            "source_commit": source_commit,
            "license_relative_path": None,
            "file_count": 0,
        }
        return manifest, [(RUNTIME_MANIFEST, json_bytes(manifest))]

    pairs = _runtime_files(runtime_dir)
    copied: list[tuple[str, bytes]] = []
    executable_digest = ""
    executable_pointer_bits: int | None = None
    for relative, source in pairs:
        payload = source.read_bytes()
        target = f"payload/runtime/{relative.as_posix()}"
        copied.append((target, payload))
        if relative.as_posix().lower() == "python.exe":
            executable_digest = bytes_sha256(payload)
            executable_pointer_bits = _pe_pointer_bits(payload)
    license_candidates = [
        target for target, _ in copied if PurePosixPath(target).name.casefold() == "license.txt"
    ]
    if windows_gate and not license_candidates:
        raise ReleaseError(
            "the Windows-gated package requires the runtime supplier's LICENSE.txt"
        )
    if windows_gate:
        for license_path in license_candidates:
            relative = PurePosixPath(license_path)
            source_path = runtime_dir / Path(*relative.relative_to("payload/runtime").parts)
            try:
                if source_path.stat().st_size == 0:
                    raise ReleaseError("the Windows-gated runtime LICENSE.txt must not be empty")
            except OSError as exc:
                raise ReleaseError("cannot inspect the Windows-gated runtime LICENSE.txt") from exc
    if windows_gate and executable_pointer_bits != 64:
        raise ReleaseError("the Windows-gated package must contain a PE32+ x64 python.exe")
    manifest = {
        "schema_version": 1,
        "kind": "python",
        "target": {"system": "windows", "machine": "x64"},
        "bundled": True,
        "status": "verified" if windows_gate else "candidate",
        "source": _runtime_source_label(runtime_dir, runtime_source),
        "version": normalized_runtime_version or "unreported",
        "pointer_bits": executable_pointer_bits,
        "executable_relative_path": "payload/runtime/python.exe",
        "executable_sha256": executable_digest,
        "source_commit": source_commit,
        "license_relative_path": license_candidates[0] if license_candidates else None,
        "file_count": len(copied),
    }
    copied.append((RUNTIME_MANIFEST, json_bytes(manifest)))
    return manifest, copied


def _sbom_bytes(runtime_manifest: dict[str, object], source_commit: str | None) -> bytes:
    return json_bytes(
        {
            "schema_version": 1,
            "package": PACKAGE_NAME,
            "version": VERSION,
            "source_commit": source_commit,
            "components": [
                {
                    "name": "python-runtime",
                    "type": "bundled-runtime",
                    "bundled": runtime_manifest.get("bundled", False),
                    "source": runtime_manifest.get("source"),
                    "version": runtime_manifest.get("version"),
                    "pointer_bits": runtime_manifest.get("pointer_bits"),
                    "license_evidence": runtime_manifest.get("license_relative_path"),
                    "executable_sha256": runtime_manifest.get("executable_sha256"),
                }
            ],
        }
    )


def build_metadata(*, windows_gate: bool, source_commit: str | None) -> dict[str, object]:
    """Return gate metadata; retained as a small public helper for CI/tests."""
    system = normalized_system()
    machine = normalized_machine()
    commit = source_commit.lower() if isinstance(source_commit, str) else None
    if windows_gate:
        if system != "windows" or machine != "x64":
            raise ReleaseError("the gated artifact must be built on Windows x64")
        if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get(
            "RUNNER_ENVIRONMENT"
        ) != "github-hosted":
            raise ReleaseError("the gated artifact requires a GitHub-hosted runner")
        if commit is None or not COMMIT_RE.fullmatch(commit):
            raise ReleaseError("the gated artifact requires an exact 40-character source commit")
    return {
        "schema_version": 1,
        "package_name": PACKAGE_NAME,
        "display_name": DISPLAY_NAME,
        "mcp_server_name": MCP_SERVER_NAME,
        "version": VERSION,
        "source_commit": commit,
        "build_host": {"system": system, "machine": machine},
        "target": {"system": "windows", "machine": "x64"},
        "release_channel": "windows-native-gated" if windows_gate else "local-unverified",
        "cross_built": system != "windows" or machine != "x64",
        "target_mcp_smoke_tested": bool(windows_gate),
    }


def _file_entry(path: str, payload: bytes) -> dict[str, object]:
    return {"path": path, "size": len(payload), "sha256": bytes_sha256(payload)}


def _checksum_text(files: Iterable[tuple[str, bytes]]) -> bytes:
    lines = [f"{bytes_sha256(payload)}  {path}" for path, payload in files]
    return ("\n".join(sorted(lines)) + "\n").encode("ascii")


def _zip_write(bundle: zipfile.ZipFile, name: str, payload: bytes) -> None:
    # A fixed timestamp and explicit regular-file mode make archives reproducible
    # and prevent accidental symlink entries on platforms that support them.
    info = zipfile.ZipInfo(name)
    info.date_time = (2020, 1, 1, 0, 0, 0)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    bundle.writestr(info, payload, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def build_release(
    project_root: Path,
    output_directory: Path,
    *,
    force: bool = False,
    windows_gate: bool = False,
    source_commit: str | None = None,
    runtime_dir: Path | None = None,
    runtime_source: str | None = None,
    runtime_version: str | None = None,
) -> tuple[Path, Path]:
    project_root = Path(project_root)
    if _is_link_or_reparse(project_root, str(project_root)):
        raise ReleaseError(f"project root is a link or reparse point: {project_root}")
    _assert_path_chain_has_no_links(project_root, "project root")
    project_root = project_root.resolve(strict=True)
    output_directory = output_directory.resolve()
    files = validated_files(project_root)
    if source_commit is not None:
        if not isinstance(source_commit, str) or not COMMIT_RE.fullmatch(source_commit.lower()):
            raise ReleaseError("source commit must be a complete lowercase commit hash")
    commit = (source_commit or _git_commit(project_root) or None)
    if commit is not None:
        commit = commit.lower()
    metadata = build_metadata(windows_gate=windows_gate, source_commit=commit)
    runtime_manifest, runtime_payloads = _runtime_manifest(
        runtime_dir,
        runtime_source=runtime_source,
        runtime_version=runtime_version,
        windows_gate=windows_gate,
        source_commit=commit,
    )

    package_payloads: dict[str, bytes] = {}
    folded_payload_paths: dict[str, str] = {}

    def add_payload(relative: str, payload: bytes) -> None:
        safe = _safe_relative(relative, label="package path").as_posix()
        folded = safe.casefold()
        previous = folded_payload_paths.get(folded)
        if previous is not None:
            raise ReleaseError(f"package paths collide on Windows: {previous} and {safe}")
        folded_payload_paths[folded] = safe
        package_payloads[safe] = payload

    archive_name = GATED_ARCHIVE_NAME if windows_gate else LOCAL_ARCHIVE_NAME
    for relative in files:
        if relative.as_posix() == "START-HERE.html":
            add_payload(
                relative.as_posix(),
                _generated_start_here(project_root, archive_name=archive_name),
            )
        else:
            add_payload(relative.as_posix(), (project_root / relative).read_bytes())
    for relative, payload in runtime_payloads:
        add_payload(relative, payload)
    add_payload(SBOM_PATH, _sbom_bytes(runtime_manifest, commit))

    # The release manifest describes every payload file except the two proof
    # files themselves.  Keeping the manifest out of its own entry avoids a
    # circular hash; SHA256SUMS includes the manifest for external checking.
    manifest_entries = [
        _file_entry(path, package_payloads[path]) for path in sorted(package_payloads)
    ]
    release_manifest = {
        **metadata,
        "runtime": runtime_manifest,
        "files": manifest_entries,
    }
    manifest_payload = json_bytes(release_manifest)
    sums_input = list(package_payloads.items()) + [("release-manifest.json", manifest_payload)]
    sums_payload = _checksum_text(sums_input)

    output_directory.mkdir(parents=True, exist_ok=True)
    archive = output_directory / archive_name
    sidecar = Path(f"{archive}.sha256")
    if not force and (archive.exists() or sidecar.exists()):
        raise ReleaseError(f"refusing to overwrite {archive}; pass --force")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{archive_name}.", suffix=".tmp", dir=output_directory
    )
    os.close(descriptor)
    temporary_archive = Path(temporary_name)
    temporary_sidecar = Path(f"{temporary_archive}.sha256")
    try:
        with zipfile.ZipFile(
            temporary_archive,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as bundle:
            for relative in sorted(package_payloads):
                _zip_write(bundle, f"{BUNDLE_NAME}/{relative}", package_payloads[relative])
            _zip_write(bundle, f"{BUNDLE_NAME}/release-manifest.json", manifest_payload)
            _zip_write(bundle, f"{BUNDLE_NAME}/SHA256SUMS.txt", sums_payload)
        digest = sha256(temporary_archive)
        temporary_sidecar.write_bytes(f"{digest}  {archive_name}\n".encode("ascii"))
        os.replace(temporary_archive, archive)
        os.replace(temporary_sidecar, sidecar)
    finally:
        temporary_archive.unlink(missing_ok=True)
        temporary_sidecar.unlink(missing_ok=True)
    return archive, sidecar


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--windows-gate", action="store_true")
    parser.add_argument("--source-commit")
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--runtime-source")
    parser.add_argument("--runtime-version")
    arguments = parser.parse_args()
    project_root = Path(__file__).resolve().parents[1]
    try:
        archive, sidecar = build_release(
            project_root,
            arguments.output_dir,
            force=arguments.force,
            windows_gate=arguments.windows_gate,
            source_commit=arguments.source_commit,
            runtime_dir=arguments.runtime_dir,
            runtime_source=arguments.runtime_source,
            runtime_version=arguments.runtime_version,
        )
    except (OSError, ValueError, json.JSONDecodeError, ReleaseError) as exc:
        print(f"ERROR: {exc}")
        return 2
    qualifier = "Windows-gated input" if arguments.windows_gate else "UNVERIFIED local candidate"
    print(f"Built {qualifier}: {archive}")
    print(f"Checksum: {sidecar}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
