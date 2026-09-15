#!/usr/bin/env python3
"""Verify a mail-mcp-server package and, when requested, its Windows gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any


PACKAGE_NAME = "mail-mcp-server"
DISPLAY_NAME = "邮件助手"
MCP_SERVER_NAME = "mail-mcp"
VERSION = "0.9.0"
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RUNTIME_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
OPTIONAL_INSTALLED_FILE = "mcp/python-runtime.json"
RUNTIME_MANIFEST = "payload/runtime/runtime-manifest.json"
SBOM_PATH = "payload/sbom.json"
REQUIRED_FILES = {
    "README.md",
    "START-HERE.html",
    "INSTALL.cmd",
    "CONFIGURE.cmd",
    "OPEN-CONFIG.cmd",
    "UNINSTALL.cmd",
    "config/settings.example.json",
    ".claude-plugin/plugin.json",
    ".mcp.json",
    "release-manifest.json",
    "SHA256SUMS.txt",
    RUNTIME_MANIFEST,
    SBOM_PATH,
}
# Files outside the bundled runtime are publisher-reviewed and fixed.  Keeping
# this list in the verifier (which is shipped inside the package) means a
# modified archive cannot add a second executable, installer, or configuration
# file while also rewriting the manifest and checksum proofs.
PUBLIC_SOURCE_FILES = {
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
}
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


class VerificationError(RuntimeError):
    pass


def _path_key(value: str | Path) -> str:
    text = str(value).replace("/", "\\")
    if os.name == "nt":
        return text.rstrip("\\").casefold()
    return text


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise VerificationError(f"cannot inspect package path for links: {path}") from exc
    if path.is_symlink():
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _pe_pointer_bits(path: Path) -> int | None:
    """Read the PE machine/optional-header width without executing the file."""
    try:
        with path.open("rb") as handle:
            payload = handle.read(4096)
    except OSError as exc:
        raise VerificationError(f"cannot read bundled runtime executable: {path}") from exc
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


def _assert_path_chain_has_no_links(path: Path, label: str) -> None:
    """Check every existing ancestor without resolving away a junction."""
    if os.name != "nt":
        # macOS development environments commonly expose /var through a
        # compatibility symlink.  The package's strict ancestor policy is for
        # Windows reparse points; still reject a directly linked executable.
        if is_link_or_reparse(path):
            raise VerificationError(f"{label} is a link or reparse point: {path}")
        return
    current = Path(os.path.abspath(str(path)))
    while True:
        if is_link_or_reparse(current):
            raise VerificationError(f"{label} traverses a link or reparse point: {current}")
        parent = current.parent
        if parent == current:
            return
        current = parent


def require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise VerificationError(f"{label} must be an object")
    return value


def safe_relative(value: str, *, label: str = "path") -> PurePosixPath:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or "\x00" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(character in value for character in '<>:"|?*')
    ):
        raise VerificationError(f"{label} must be a non-empty POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise VerificationError(f"unsafe {label}: {value}")
    if path.as_posix() != value:
        raise VerificationError(f"non-canonical {label}: {value}")
    for part in path.parts:
        if not part or part.endswith((" ", ".")):
            raise VerificationError(f"unsafe {label}: {value}")
        if part.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
            raise VerificationError(f"Windows reserved name in {label}: {value}")
    return path


def walk_regular_files(root: Path) -> set[str]:
    actual: set[str] = set()
    directories: set[str] = set()
    seen_casefolded: dict[str, str] = {}

    def record_entry(relative: str) -> None:
        key = relative.casefold()
        previous = seen_casefolded.get(key)
        if previous is not None and previous != relative:
            raise VerificationError(
                f"package paths collide on Windows: {previous} and {relative}"
            )
        seen_casefolded[key] = relative

    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        kept_dirs: list[str] = []
        for name in sorted(dirnames):
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            safe = safe_relative(relative)
            record_entry(safe.as_posix())
            if any(part.lower() in FORBIDDEN_PARTS for part in safe.parts):
                raise VerificationError(f"forbidden package path: {relative}")
            if any(part.casefold() in FORBIDDEN_RUNTIME_NAMES for part in safe.parts):
                raise VerificationError(f"package-manager path is forbidden in runtime: {relative}")
            if is_link_or_reparse(path):
                raise VerificationError(f"link or reparse point is forbidden: {relative}")
            if not path.is_dir():
                raise VerificationError(f"package directory entry is not a directory: {relative}")
            directories.add(safe.as_posix())
            kept_dirs.append(name)
        dirnames[:] = kept_dirs
        for name in sorted(filenames):
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            safe = safe_relative(relative)
            record_entry(safe.as_posix())
            if any(part.lower() in FORBIDDEN_PARTS for part in safe.parts):
                raise VerificationError(f"forbidden package path: {relative}")
            if any(part.casefold() in FORBIDDEN_RUNTIME_NAMES for part in safe.parts):
                raise VerificationError(f"package-manager path is forbidden in runtime: {relative}")
            if is_link_or_reparse(path):
                raise VerificationError(f"link or reparse point is forbidden: {relative}")
            if not path.is_file():
                raise VerificationError(f"package file is not regular: {relative}")
            actual.add(safe.as_posix())
    for relative in sorted(directories):
        prefix = relative + "/"
        if not any(path.startswith(prefix) for path in actual):
            raise VerificationError(f"empty package directory is not allowed: {relative}")
    return actual


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError(f"cannot read {label}: {exc}") from exc
    return require_object(value, label)


def _validate_manifest_entries(manifest: dict[str, Any]) -> dict[str, tuple[int, str]]:
    if manifest.get("schema_version") != 1:
        raise VerificationError("unsupported release manifest schema")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise VerificationError("release manifest must contain a non-empty files array")
    expected: dict[str, tuple[int, str]] = {}
    for item in entries:
        entry = require_object(item, "release manifest entry")
        path_value = entry.get("path")
        safe = safe_relative(path_value, label="manifest path")
        path_text = safe.as_posix()
        if path_text in {"release-manifest.json", "SHA256SUMS.txt"} or path_text in expected:
            raise VerificationError(f"unsafe or duplicate manifest path: {path_text}")
        size = entry.get("size")
        digest = entry.get("sha256")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise VerificationError(f"invalid file size for {path_text}")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise VerificationError(f"invalid SHA-256 for {path_text}")
        expected[path_text] = (size, digest)
    return expected


def _validate_identity(manifest: dict[str, Any]) -> None:
    expected_keys = {
        "schema_version",
        "package_name",
        "display_name",
        "mcp_server_name",
        "version",
        "source_commit",
        "build_host",
        "target",
        "release_channel",
        "cross_built",
        "target_mcp_smoke_tested",
        "runtime",
        "files",
    }
    if set(manifest) != expected_keys:
        raise VerificationError("release manifest contains unexpected or missing fields")
    expected = {
        "package_name": PACKAGE_NAME,
        "display_name": DISPLAY_NAME,
        "mcp_server_name": MCP_SERVER_NAME,
        "version": VERSION,
        "target": {"system": "windows", "machine": "x64"},
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise VerificationError(f"release identity mismatch: {key}")
    channel = manifest.get("release_channel")
    if channel not in {"local-unverified", "windows-native-gated"}:
        raise VerificationError("unsupported release channel")
    if not isinstance(manifest.get("cross_built"), bool):
        raise VerificationError("release cross_built flag is invalid")
    if not isinstance(manifest.get("target_mcp_smoke_tested"), bool):
        raise VerificationError("release smoke-test flag is invalid")
    build_host = require_object(manifest.get("build_host"), "build host metadata")
    if build_host.get("system") not in {"windows", "linux", "darwin"}:
        raise VerificationError("release build host system is invalid")
    if not isinstance(build_host.get("machine"), str) or not build_host.get("machine"):
        raise VerificationError("release build host machine is invalid")
    commit = manifest.get("source_commit")
    if commit is not None and (not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit)):
        raise VerificationError("release source commit must be a complete lowercase commit hash")
    expected_cross_built = not (
        build_host.get("system") == "windows" and build_host.get("machine") == "x64"
    )
    if manifest.get("cross_built") != expected_cross_built:
        raise VerificationError("release cross_built flag does not match its build host")
    channel = manifest.get("release_channel")
    if manifest.get("target_mcp_smoke_tested") != (channel == "windows-native-gated"):
        raise VerificationError("release smoke-test flag does not match its channel")


def _validate_runtime(root: Path, manifest: dict[str, Any], actual: set[str], *, require_gate: bool) -> None:
    runtime = require_object(manifest.get("runtime"), "runtime metadata")
    expected_runtime_keys = {
        "schema_version",
        "kind",
        "target",
        "bundled",
        "status",
        "source",
        "version",
        "pointer_bits",
        "executable_relative_path",
        "executable_sha256",
        "source_commit",
        "license_relative_path",
        "file_count",
    }
    if set(runtime) != expected_runtime_keys:
        raise VerificationError("runtime metadata contains unexpected or missing fields")
    if runtime.get("schema_version") != 1 or runtime.get("kind") != "python":
        raise VerificationError("unsupported runtime metadata")
    if runtime.get("target") != {"system": "windows", "machine": "x64"}:
        raise VerificationError("runtime target must be Windows x64")
    bundled = runtime.get("bundled")
    if not isinstance(bundled, bool):
        raise VerificationError("runtime bundled flag is invalid")
    status = runtime.get("status")
    if status not in ({"candidate", "verified"} if bundled else {"unverified"}):
        raise VerificationError("runtime status is inconsistent with bundled flag")
    source = runtime.get("source")
    if (
        not isinstance(source, str)
        or not source.strip()
        or source != source.strip()
        or len(source) > 256
        or any(ord(character) < 32 or ord(character) == 127 for character in source)
    ):
        raise VerificationError("runtime source provenance is missing or invalid")
    runtime_version = runtime.get("version")
    pointer_bits = runtime.get("pointer_bits")
    if bundled:
        if runtime_version != "unreported" and (
            not isinstance(runtime_version, str) or not RUNTIME_VERSION_RE.fullmatch(runtime_version)
        ):
            raise VerificationError("bundled runtime version is invalid")
        if require_gate and (
            not isinstance(runtime_version, str)
            or not RUNTIME_VERSION_RE.fullmatch(runtime_version)
        ):
            raise VerificationError("Windows-gated package must name a concrete runtime version")
        if pointer_bits not in {None, 32, 64} or isinstance(pointer_bits, bool):
            raise VerificationError("bundled runtime pointer width is invalid")
        if require_gate and pointer_bits != 64:
            raise VerificationError("Windows-gated package must contain a 64-bit runtime")
    elif runtime_version is not None:
        raise VerificationError("unbundled runtime must not claim a version")
    elif pointer_bits is not None:
        raise VerificationError("unbundled runtime must not claim a pointer width")
    runtime_commit = runtime.get("source_commit")
    if runtime_commit != manifest.get("source_commit"):
        raise VerificationError("runtime provenance commit does not match release metadata")
    license_path = runtime.get("license_relative_path")
    if not bundled and license_path is not None:
        raise VerificationError("unbundled runtime must not claim runtime license evidence")
    if license_path is not None:
        license_path = safe_relative(license_path, label="runtime license path").as_posix()
        if (
            not license_path.startswith("payload/runtime/")
            or license_path not in actual
            or PurePosixPath(license_path).name.casefold() != "license.txt"
        ):
            raise VerificationError("runtime license evidence is unavailable or invalid")
        license_file = root / Path(*PurePosixPath(license_path).parts)
        try:
            if license_file.stat().st_size == 0:
                raise VerificationError("runtime license evidence must not be empty")
        except OSError as exc:
            raise VerificationError("cannot inspect runtime license evidence") from exc
    elif bundled and require_gate:
        raise VerificationError("Windows-gated package must include runtime LICENSE.txt evidence")
    file_count = runtime.get("file_count")
    if isinstance(file_count, bool) or not isinstance(file_count, int) or file_count < 0:
        raise VerificationError("runtime file count is invalid")
    runtime_marker = root / RUNTIME_MANIFEST
    marker = _load_json(runtime_marker, "runtime manifest")
    if marker != runtime:
        raise VerificationError("release runtime metadata does not match runtime manifest")
    if bundled:
        executable = runtime.get("executable_relative_path")
        if not isinstance(executable, str):
            raise VerificationError("bundled runtime executable path is missing")
        executable = safe_relative(executable, label="runtime executable").as_posix()
        if executable not in actual or executable != "payload/runtime/python.exe":
            raise VerificationError("bundled runtime must provide payload/runtime/python.exe")
        digest = runtime.get("executable_sha256")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise VerificationError("bundled runtime executable hash is invalid")
        executable_path = root / executable
        if is_link_or_reparse(executable_path) or not executable_path.is_file():
            raise VerificationError("bundled runtime executable is unavailable or linked")
        if sha256(executable_path) != digest:
            raise VerificationError("bundled runtime executable hash mismatch")
        detected_pointer_bits = _pe_pointer_bits(executable_path)
        if detected_pointer_bits is not None and pointer_bits != detected_pointer_bits:
            raise VerificationError("bundled runtime pointer width does not match python.exe")
        if require_gate and detected_pointer_bits != 64:
            raise VerificationError("Windows-gated package must contain a PE32+ x64 python.exe")
        runtime_files = {
            path
            for path in actual
            if path.startswith("payload/runtime/") and path != RUNTIME_MANIFEST
        }
        for runtime_file in runtime_files:
            runtime_path = PurePosixPath(runtime_file)
            if any(part.casefold() in FORBIDDEN_RUNTIME_PARTS for part in runtime_path.parts):
                raise VerificationError(
                    f"development or test runtime path is forbidden: {runtime_file}"
                )
            if runtime_path.suffix.casefold() in FORBIDDEN_RUNTIME_SUFFIXES:
                raise VerificationError(f"development runtime file is forbidden: {runtime_file}")
            if runtime_path.name.casefold() in FORBIDDEN_RUNTIME_NAMES:
                raise VerificationError(
                    f"package-manager executable is forbidden in runtime: {runtime_file}"
                )
        if file_count != len(runtime_files) or file_count < 1:
            raise VerificationError("runtime file count does not match the bundled payload")
    else:
        if runtime.get("executable_relative_path") is not None:
            raise VerificationError("unbundled runtime must not name an executable")
        if runtime.get("executable_sha256") is not None:
            raise VerificationError("unbundled runtime must not contain an executable hash")
        if file_count != 0:
            raise VerificationError("unbundled runtime must have an empty payload")
        runtime_files = {
            path
            for path in actual
            if path.startswith("payload/runtime/") and path != RUNTIME_MANIFEST
        }
        if runtime_files:
            raise VerificationError("unbundled runtime contains unexpected payload files")
        if require_gate:
            raise VerificationError("Windows-gated package must bundle its runtime")


def _validate_sbom(root: Path, manifest: dict[str, Any]) -> None:
    sbom = _load_json(root / SBOM_PATH, "software bill of materials")
    if set(sbom) != {"schema_version", "package", "version", "source_commit", "components"}:
        raise VerificationError("software bill of materials contains unexpected or missing fields")
    if sbom.get("schema_version") != 1 or sbom.get("package") != PACKAGE_NAME or sbom.get("version") != VERSION:
        raise VerificationError("software bill of materials identity mismatch")
    if sbom.get("source_commit") != manifest.get("source_commit"):
        raise VerificationError("software bill of materials provenance mismatch")
    components = sbom.get("components")
    if not isinstance(components, list) or len(components) != 1:
        raise VerificationError("software bill of materials must contain one runtime component")
    component = require_object(components[0], "software bill of materials component")
    expected_component_keys = {
        "name",
        "type",
        "bundled",
        "source",
        "version",
        "pointer_bits",
        "license_evidence",
        "executable_sha256",
    }
    if set(component) != expected_component_keys:
        raise VerificationError("software bill of materials component contains unexpected fields")
    runtime = require_object(manifest.get("runtime"), "runtime metadata")
    expected = {
        "name": "python-runtime",
        "type": "bundled-runtime",
        "bundled": runtime.get("bundled"),
        "source": runtime.get("source"),
        "version": runtime.get("version"),
        "pointer_bits": runtime.get("pointer_bits"),
        "license_evidence": runtime.get("license_relative_path"),
        "executable_sha256": runtime.get("executable_sha256"),
    }
    for key, value in expected.items():
        if component.get(key) != value:
            raise VerificationError(f"software bill of materials mismatch: {key}")


def _validate_sums(root: Path, actual: set[str], *, allow_python_runtime: bool) -> None:
    sums_path = root / "SHA256SUMS.txt"
    try:
        raw = sums_path.read_bytes()
    except OSError as exc:
        raise VerificationError(f"cannot read SHA256SUMS.txt: {exc}") from exc
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise VerificationError("SHA256SUMS.txt must be ASCII") from exc
    if not text.endswith("\n") or "\r" in text:
        raise VerificationError("SHA256SUMS.txt must use LF line endings")
    listed: dict[str, str] = {}
    for line in text.splitlines():
        if not line:
            raise VerificationError("SHA256SUMS.txt contains an empty line")
        match = re.fullmatch(r"([0-9a-f]{64})  ([^\n]+)", line)
        if not match:
            raise VerificationError(f"invalid checksum line: {line!r}")
        digest, path_value = match.groups()
        path_text = safe_relative(path_value, label="checksum path").as_posix()
        if path_text == "SHA256SUMS.txt" or path_text in listed:
            raise VerificationError(f"duplicate or self checksum entry: {path_text}")
        listed[path_text] = digest
    unhashed = {OPTIONAL_INSTALLED_FILE} if allow_python_runtime else set()
    expected = actual - {"SHA256SUMS.txt"} - unhashed
    if set(listed) != expected:
        raise VerificationError(
            f"checksum file set mismatch; missing={sorted(expected - set(listed))}; "
            f"extra={sorted(set(listed) - expected)}"
        )
    for path_text, digest in listed.items():
        path = root / Path(*PurePosixPath(path_text).parts)
        if sha256(path) != digest:
            raise VerificationError(f"SHA-256 mismatch: {path_text}")


def _validate_python_descriptor(root: Path, manifest: dict[str, Any]) -> None:
    path = root / OPTIONAL_INSTALLED_FILE
    descriptor = _load_json(path, "installed Python descriptor")
    expected_keys = {
        "schema_version",
        "kind",
        "bundled",
        "executable",
        "version",
        "version_info",
        "pointer_bits",
        "executable_sha256",
    }
    if set(descriptor) != expected_keys:
        raise VerificationError("installed Python descriptor contains unexpected or missing fields")
    if descriptor.get("schema_version") != 1 or descriptor.get("kind") != "python" or descriptor.get("bundled") is not True:
        raise VerificationError("installed Python descriptor schema is unsupported")
    if descriptor.get("pointer_bits") != 64:
        raise VerificationError("installed Python descriptor is not 64-bit")
    descriptor_version = descriptor.get("version")
    descriptor_match = (
        re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)", descriptor_version)
        if isinstance(descriptor_version, str)
        else None
    )
    if descriptor_match is None:
        raise VerificationError("installed Python descriptor version is invalid")
    version_info = descriptor.get("version_info")
    if (
        not isinstance(version_info, list)
        or len(version_info) < 3
        or any(isinstance(value, bool) or not isinstance(value, int) for value in version_info[:3])
        or [int(value) for value in version_info[:3]]
        != [int(part) for part in descriptor_match.groups()]
    ):
        raise VerificationError("installed Python descriptor version info is invalid")
    executable = descriptor.get("executable")
    digest = descriptor.get("executable_sha256")
    if not isinstance(executable, str) or not executable:
        raise VerificationError("installed Python descriptor has no executable")
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise VerificationError("installed Python descriptor hash is invalid")
    runtime = require_object(manifest.get("runtime"), "runtime metadata")
    if runtime.get("bundled") is not True:
        raise VerificationError("installed Python descriptor requires a bundled runtime")
    manifest_digest = runtime.get("executable_sha256")
    if not isinstance(manifest_digest, str) or not SHA256_RE.fullmatch(manifest_digest):
        raise VerificationError("runtime metadata has no valid executable hash for the installed descriptor")
    if digest.lower() != manifest_digest.lower():
        raise VerificationError("installed Python descriptor hash does not match runtime metadata")
    manifest_pointer_bits = runtime.get("pointer_bits")
    if manifest_pointer_bits is not None and descriptor.get("pointer_bits") != manifest_pointer_bits:
        raise VerificationError("installed Python descriptor pointer width does not match runtime metadata")
    manifest_version = runtime.get("version")
    if isinstance(manifest_version, str) and manifest_version != "unreported" and descriptor_version != manifest_version:
        raise VerificationError("installed Python descriptor version does not match runtime metadata")
    executable_path = Path(executable)
    if not executable_path.is_absolute():
        raise VerificationError("installed Python descriptor executable must be absolute")
    _assert_path_chain_has_no_links(executable_path, "installed Python executable")
    try:
        expected_path = (root / "payload/runtime/python.exe").resolve(strict=True)
        actual_path = executable_path.resolve(strict=True)
    except OSError as exc:
        raise VerificationError("pinned Python executable is unavailable") from exc
    if _path_key(actual_path) != _path_key(expected_path):
        raise VerificationError("installed Python descriptor points outside bundled payload/runtime/python.exe")
    if not executable_path.is_file() or is_link_or_reparse(executable_path):
        raise VerificationError("pinned Python executable is unavailable or linked")
    if sha256(executable_path) != digest:
        raise VerificationError("pinned Python executable hash mismatch")
    detected_pointer_bits = _pe_pointer_bits(executable_path)
    if detected_pointer_bits is not None and detected_pointer_bits != 64:
        raise VerificationError("pinned Python executable is not a PE32+ x64 binary")


def verify(root: Path, *, require_windows_gate: bool, allow_python_runtime: bool) -> None:
    root = Path(root)
    _assert_path_chain_has_no_links(root, "release root")
    if is_link_or_reparse(root):
        raise VerificationError("release root must not be a link or reparse point")
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise VerificationError(f"release root is unavailable: {root}") from exc
    if not root.is_dir():
        raise VerificationError(f"release root is not a directory: {root}")
    actual = walk_regular_files(root)
    missing_required = sorted(REQUIRED_FILES - actual)
    if missing_required:
        raise VerificationError(f"required package files are missing: {missing_required}")
    allowed_extra = {OPTIONAL_INSTALLED_FILE} if allow_python_runtime else set()
    expected_proofs = {"release-manifest.json", "SHA256SUMS.txt"}
    source_files = actual - expected_proofs - allowed_extra
    manifest = _load_json(root / "release-manifest.json", "release manifest")
    _validate_identity(manifest)
    entries = _validate_manifest_entries(manifest)
    if set(entries) != source_files:
        raise VerificationError(
            f"release manifest file set mismatch; missing={sorted(source_files - set(entries))}; "
            f"extra={sorted(set(entries) - source_files)}"
        )
    runtime_files = {path for path in actual if path.startswith("payload/runtime/")}
    expected_public = PUBLIC_SOURCE_FILES | {SBOM_PATH}
    actual_public = source_files - runtime_files
    if actual_public != expected_public:
        raise VerificationError(
            "release contains files outside the reviewed public allowlist; "
            f"missing={sorted(expected_public - actual_public)}; "
            f"extra={sorted(actual_public - expected_public)}"
        )
    for relative, (expected_size, expected_digest) in entries.items():
        path = root / Path(*PurePosixPath(relative).parts)
        if is_link_or_reparse(path) or not path.is_file():
            raise VerificationError(f"release file is unavailable or linked: {relative}")
        try:
            actual_size = path.stat().st_size
        except OSError as exc:
            raise VerificationError(f"cannot inspect release file: {relative}") from exc
        if actual_size != expected_size:
            raise VerificationError(f"size mismatch: {relative}")
        if sha256(path) != expected_digest:
            raise VerificationError(f"SHA-256 mismatch: {relative}")
    _validate_sums(root, actual, allow_python_runtime=allow_python_runtime)
    _validate_runtime(root, manifest, actual, require_gate=require_windows_gate)
    _validate_sbom(root, manifest)

    plugin = _load_json(root / ".claude-plugin" / "plugin.json", "plugin manifest")
    if plugin.get("name") != PACKAGE_NAME or plugin.get("version") != VERSION or plugin.get("displayName") != DISPLAY_NAME:
        raise VerificationError("plugin manifest identity mismatch")
    example = _load_json(root / "config/settings.example.json", "configuration example")
    if example.get("schema_version") != 1 or example.get("provider") != "coremail":
        raise VerificationError("configuration example identity mismatch")
    if "password" in json.dumps(example, ensure_ascii=False).lower():
        raise VerificationError("configuration example contains a password field")
    mcp_config = _load_json(root / ".mcp.json", "MCP package declaration")
    if set(mcp_config) != {"mcpServers"}:
        raise VerificationError("MCP package declaration contains unexpected fields")
    servers = mcp_config.get("mcpServers")
    if not isinstance(servers, dict) or set(servers) != {MCP_SERVER_NAME}:
        raise VerificationError("MCP package declaration must contain only mail-mcp")
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
        raise VerificationError("MCP package declaration does not match the portable mail launcher")
    if allow_python_runtime and OPTIONAL_INSTALLED_FILE in actual:
        _validate_python_descriptor(root, manifest)

    if require_windows_gate:
        required = {
            "release_channel": "windows-native-gated",
            "cross_built": False,
            "target_mcp_smoke_tested": True,
            "build_host": {"system": "windows", "machine": "x64"},
        }
        for key, value in required.items():
            if manifest.get(key) != value:
                raise VerificationError(f"Windows-gated metadata mismatch: {key}")
        commit = manifest.get("source_commit")
        if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit.lower()):
            raise VerificationError("Windows-gated metadata has no exact source commit")
        runtime = require_object(manifest.get("runtime"), "runtime metadata")
        if runtime.get("bundled") is not True or runtime.get("status") != "verified":
            raise VerificationError("Windows-gated metadata does not prove a bundled runtime")
    else:
        if manifest.get("release_channel") == "windows-native-gated":
            raise VerificationError("gated channel requires --require-windows-gate")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--require-windows-gate", action="store_true")
    parser.add_argument("--allow-python-runtime", action="store_true")
    arguments = parser.parse_args()
    try:
        verify(
            arguments.root,
            require_windows_gate=arguments.require_windows_gate,
            allow_python_runtime=arguments.allow_python_runtime,
        )
    except (OSError, VerificationError) as exc:
        print(f"INVALID MAIL RELEASE: {exc}", file=sys.stderr)
        return 2
    print("MAIL RELEASE: VERIFIED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
