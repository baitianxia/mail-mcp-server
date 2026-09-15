from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = load_module("build_release", ROOT / "scripts" / "build-release.py")
verifier = load_module("verify_release", ROOT / "scripts" / "verify-release.py")


class ReleaseTests(unittest.TestCase):
    @staticmethod
    def _write_fake_pe64(path: Path) -> None:
        payload = bytearray(128)
        payload[:2] = b"MZ"
        payload[0x3C:0x40] = (64).to_bytes(4, "little")
        payload[64:68] = b"PE\0\0"
        payload[68:70] = (0x8664).to_bytes(2, "little")
        payload[88:90] = (0x20B).to_bytes(2, "little")
        path.write_bytes(payload)

    def test_local_archive_has_one_root_allowlist_and_proofs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive, sidecar = release.build_release(ROOT, Path(temporary))
            self.assertEqual(release.LOCAL_ARCHIVE_NAME, archive.name)
            self.assertIn("UNVERIFIED", archive.name)
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            self.assertEqual(f"{digest}  {archive.name}\n".encode("ascii"), sidecar.read_bytes())
            with zipfile.ZipFile(archive) as bundle:
                names = set(bundle.namelist())
                prefix = release.BUNDLE_NAME + "/"
                self.assertTrue(names)
                self.assertTrue(all(name.startswith(prefix) for name in names))
                relative = {name[len(prefix) :] for name in names}
                expected = set(release.EXACT_FILES) | set(release.GENERATED_FILES) | {
                    release.RUNTIME_MANIFEST,
                    release.SBOM_PATH,
                }
                self.assertEqual(expected, relative)
                self.assertFalse(any("__pycache__" in name for name in names))
                manifest = json.loads(bundle.read(f"{release.BUNDLE_NAME}/release-manifest.json"))
                runtime = json.loads(bundle.read(f"{release.BUNDLE_NAME}/{release.RUNTIME_MANIFEST}"))
            self.assertEqual("mail-mcp-server", manifest["package_name"])
            self.assertEqual("mail-mcp", manifest["mcp_server_name"])
            self.assertEqual("local-unverified", manifest["release_channel"])
            self.assertFalse(manifest["target_mcp_smoke_tested"])
            self.assertFalse(runtime["bundled"])

    def test_internal_verifier_rejects_corruption_and_gate_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive, _ = release.build_release(ROOT, root)
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(root / "extracted")
            package_root = root / "extracted" / release.BUNDLE_NAME
            verifier.verify(package_root, require_windows_gate=False, allow_python_runtime=False)
            with self.assertRaises(verifier.VerificationError):
                verifier.verify(package_root, require_windows_gate=True, allow_python_runtime=False)
            (package_root / "README.md").write_text("corrupted\n", encoding="utf-8")
            with self.assertRaises(verifier.VerificationError):
                verifier.verify(package_root, require_windows_gate=False, allow_python_runtime=False)

    def test_bundled_runtime_candidate_is_copied_and_hashed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            runtime.mkdir()
            (runtime / "python.exe").write_bytes(b"portable-python-fixture")
            (runtime / "python311.dll").write_bytes(b"fixture-dll")
            archive, _ = release.build_release(
                ROOT,
                root / "dist",
                runtime_dir=runtime,
                runtime_source="test-fixture",
            )
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(root / "extracted")
            package_root = root / "extracted" / release.BUNDLE_NAME
            verifier.verify(package_root, require_windows_gate=False, allow_python_runtime=False)
            marker = json.loads((package_root / release.RUNTIME_MANIFEST).read_text(encoding="utf-8"))
            self.assertTrue(marker["bundled"])
            self.assertEqual("test-fixture", marker["source"])
            self.assertEqual(
                hashlib.sha256((package_root / "payload/runtime/python.exe").read_bytes()).hexdigest(),
                marker["executable_sha256"],
            )

    def test_windows_gated_fixture_requires_and_verifies_runtime_evidence(self) -> None:
        commit = "b" * 40
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            runtime.mkdir()
            self._write_fake_pe64(runtime / "python.exe")
            (runtime / "LICENSE.txt").write_text("Python runtime license\n", encoding="utf-8")
            with mock.patch.object(release, "normalized_system", return_value="windows"), mock.patch.object(
                release, "normalized_machine", return_value="x64"
            ), mock.patch.dict(
                os.environ,
                {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted"},
                clear=False,
            ):
                archive, _ = release.build_release(
                    ROOT,
                    root / "dist",
                    windows_gate=True,
                    source_commit=commit,
                    runtime_dir=runtime,
                    runtime_source="test-fixture",
                    runtime_version="3.13.5",
                )
            self.assertEqual(release.GATED_ARCHIVE_NAME, archive.name)
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(root / "extracted")
            package_root = root / "extracted" / release.BUNDLE_NAME
            verifier.verify(package_root, require_windows_gate=True, allow_python_runtime=False)

    def test_local_start_here_names_the_actual_candidate_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive, _ = release.build_release(ROOT, Path(temporary))
            with zipfile.ZipFile(archive) as bundle:
                html = bundle.read(f"{release.BUNDLE_NAME}/START-HERE.html").decode("utf-8")
            self.assertIn(release.LOCAL_ARCHIVE_NAME, html)
            self.assertNotIn(release.GATED_ARCHIVE_NAME, html)

    def test_runtime_package_manager_paths_and_empty_package_directories_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            (runtime / "npm").mkdir(parents=True)
            (runtime / "npm" / "shim.exe").write_bytes(b"not allowed")
            (runtime / "python.exe").write_bytes(b"fixture")
            with self.assertRaises(release.ReleaseError):
                release.build_release(ROOT, root / "dist", runtime_dir=runtime)

            archive, _ = release.build_release(ROOT, root / "clean-dist")
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(root / "extracted")
            package_root = root / "extracted" / release.BUNDLE_NAME
            (package_root / "unexpected-empty-directory").mkdir()
            with self.assertRaises(verifier.VerificationError):
                verifier.verify(package_root, require_windows_gate=False, allow_python_runtime=False)

    def test_runtime_development_and_test_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            (runtime / "Lib" / "test").mkdir(parents=True)
            (runtime / "Lib" / "test" / "test_email.py").write_text("fixture", encoding="utf-8")
            (runtime / "python.exe").write_bytes(b"fixture")
            with self.assertRaises(release.ReleaseError):
                release.build_release(ROOT, root / "dist", runtime_dir=runtime)

    def test_public_allowlist_does_not_ship_source_tests(self) -> None:
        self.assertFalse(any(path.startswith("tests/") for path in release.EXACT_FILES))
        self.assertFalse(any(path.startswith("tests/") for path in verifier.PUBLIC_SOURCE_FILES))

    def test_release_path_validation_rejects_del_control_character(self) -> None:
        with self.assertRaises(release.ReleaseError):
            release._safe_relative("file\x7f.txt")
        with self.assertRaises(verifier.VerificationError):
            verifier.safe_relative("file\x7f.txt")

    def test_windows_gate_metadata_requires_exact_host_runner_and_commit(self) -> None:
        commit = "a" * 40
        with mock.patch.object(release, "normalized_system", return_value="windows"), mock.patch.object(
            release, "normalized_machine", return_value="x64"
        ), mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted"}, clear=False):
            metadata = release.build_metadata(windows_gate=True, source_commit=commit)
        self.assertEqual("windows-native-gated", metadata["release_channel"])
        self.assertTrue(metadata["target_mcp_smoke_tested"])
        self.assertEqual(commit, metadata["source_commit"])
        with self.assertRaises(release.ReleaseError):
            release.build_metadata(windows_gate=True, source_commit="short")

    def test_python_descriptor_records_actual_executable_and_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "python-runtime.json"
            result = subprocess.run(
                [sys.executable, "-B", "-I", str(ROOT / "mcp" / "describe-python.py"), "--output", str(output)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, result.returncode, msg=result.stderr)
            payload = json.loads(output.read_text(encoding="utf-8"))
            executable = Path(payload["executable"])
            self.assertTrue(executable.is_file())
            self.assertEqual(hashlib.sha256(executable.read_bytes()).hexdigest(), payload["executable_sha256"])
            self.assertGreaterEqual(payload["version_info"][:2], [3, 10])

    def test_staged_config_validator_requires_schema_and_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "provider": "coremail",
                        "transport": "windows_simple_mapi",
                        "username": "gate@example.invalid",
                        "allowed_from": ["gate@example.invalid"],
                        "sent_copy_mode": "none",
                        "attachment_roots": [],
                    }
                ),
                encoding="utf-8",
            )
            completed = subprocess.run(
                [sys.executable, "-B", "-I", str(ROOT / "mcp" / "validate-config.py"), str(config)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, completed.returncode, msg=completed.stderr)

    def test_user_scope_registration_self_test(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-B", "-I", str(ROOT / "scripts" / "register_claude_user_mcp.py"), "self-test"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, completed.returncode, msg=completed.stderr)
        self.assertIn("SELF-TEST PASSED", completed.stdout)

    def test_lifecycle_is_project_owned_and_never_elevates(self) -> None:
        lifecycle_files = [
            ROOT / "scripts" / name
            for name in ("install.ps1", "uninstall.ps1", "configure-account.ps1", "windows-lifecycle-common.ps1")
        ]
        combined = "\n".join(path.read_text(encoding="utf-8") for path in lifecycle_files)
        normalized = " ".join(combined.lower().split())
        self.assertIn("mail-mcp-server", normalized)
        self.assertIn("versions", normalized)
        self.assertIn("move-coremaildirectoryatomically", normalized)
        self.assertIn("[io.directory]::move", normalized)
        self.assertIn("[io.fileshare]::none", normalized)
        self.assertIn("register_claude_user_mcp.py", normalized)
        self.assertNotIn("claudetools", normalized)
        self.assertNotIn(".claude\\skills", normalized)
        self.assertNotIn("runas", normalized)
        self.assertNotIn("icacls", normalized)
        self.assertNotIn("read-host", normalized)

    def test_lifecycle_uses_bundled_runtime_and_transactional_config(self) -> None:
        installer = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
        uninstaller = (ROOT / "scripts" / "uninstall.ps1").read_text(encoding="utf-8")
        launcher = (ROOT / "mcp" / "run-server.ps1").read_text(encoding="utf-8")
        self.assertIn("payload\\runtime\\python.exe", installer)
        self.assertIn("python-runtime.json", launcher)
        self.assertIn("executable_sha256", launcher)
        self.assertIn("Assert-CoremailRelease", installer)
        self.assertIn("Save-CoremailFileSnapshot", installer)
        self.assertIn("Restore-CoremailFileSnapshot", installer)
        self.assertIn("versions", installer)
        self.assertIn("unregister", uninstaller)
        self.assertIn("retained", uninstaller.lower())
        self.assertNotIn("COREMAIL_PYTHON", installer + uninstaller + launcher)
        self.assertNotIn("Move-Item", installer + uninstaller)

    def test_account_configuration_order_and_secret_boundary(self) -> None:
        setup = (ROOT / "scripts" / "setup-account.ps1").read_text(encoding="utf-8")
        configure = (ROOT / "scripts" / "configure-account.ps1").read_text(encoding="utf-8")
        self.assertLess(setup.index("Staged account configuration validation"), setup.index("Write-CoremailCredential"))
        self.assertLess(setup.index("Write-CoremailCredential"), setup.index("Publish-CoremailFileAtomically"))
        self.assertIn("Remove-CoremailCredential", setup)
        self.assertIn("RETIRED previous mail credential", configure)
        self.assertIn("Live mail connection verification failed", configure)
        self.assertIn("Run CONFIGURE.cmd again", configure)
        self.assertLess(configure.index("RETIRED previous mail credential"), configure.index("-CheckConnection"))
        self.assertIn("-not $SkipConnectionCheck -and $previousCredentialTarget", configure)
        self.assertIn("MAIL_RELEASE_GATE_TESTING", setup)
        self.assertNotIn("COREMAIL_PASSWORD", setup)

    def test_old_claude_is_supported_by_capability_not_version_floor(self) -> None:
        installer = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
        common = (ROOT / "scripts" / "windows-lifecycle-common.ps1").read_text(encoding="utf-8")
        self.assertNotIn("Assert-CoremailClaudeMinimumVersion", installer + common)
        self.assertNotIn("2.1.157", installer + common)
        self.assertIn("mcp', '--help'", installer)
        self.assertIn("[switch]$QuietOnSuccess", common)
        self.assertIn("mail-mcp", installer)

    def test_windows_gate_persists_release_and_standard_user_evidence(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "windows-release-gate.yml").read_text(
            encoding="utf-8"
        )
        orchestrator = (ROOT / "tests" / "run-windows-release-gate.ps1").read_text(
            encoding="utf-8"
        )
        for evidence in (
            "release-manifest.json",
            "SHA256SUMS.txt",
            "runtime-manifest.json",
            "runtime-LICENSE.txt",
            "sbom.json",
            "build-evidence.json",
        ):
            self.assertIn(evidence, workflow)
        self.assertIn("mail-mcp-server-windows-gate-evidence", workflow)
        self.assertIn("if: ${{ always() }}", workflow)
        self.assertIn("stage-windows-python-runtime.ps1", workflow)
        self.assertIn("-EvidenceDirectory", workflow)
        self.assertIn("Save-CoremailGateEvidence", orchestrator)
        self.assertIn("gate-evidence.json", orchestrator)
        self.assertIn("$sourceNodeModules", orchestrator)
        self.assertIn("$stagedNodeModules = Join-Path $claudeFixtureRoot 'node_modules'", orchestrator)
        self.assertIn("Copy-Item -Destination $stagedNodeModules -Recurse -Force", orchestrator)

    def test_release_refuses_to_overwrite_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            release.build_release(ROOT, output)
            with self.assertRaises(release.ReleaseError):
                release.build_release(ROOT, output)


if __name__ == "__main__":
    unittest.main()
