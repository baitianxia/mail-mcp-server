from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "mcp" / "server.py"


class ProtocolTests(unittest.TestCase):
    def test_mcp_layout_is_portable_without_a_skill_install(self) -> None:
        manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertTrue(all(byte < 128 for byte in (ROOT / ".claude-plugin" / "plugin.json").read_bytes()))
        self.assertEqual(manifest["name"], "mail-mcp-server")
        self.assertEqual(manifest["displayName"], "邮件助手")
        self.assertEqual(manifest["version"], "0.9.0")

        user_skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(user_skill.startswith("---\nname: mail-mcp-server\n"))
        self.assertIn("确认发送", user_skill)

        coremail_skill = (ROOT / "skills" / "coremail" / "SKILL.md").read_text(encoding="utf-8")
        browser_skill = (ROOT / "skills" / "web-to-coremail" / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(coremail_skill.startswith("---\nname: mail-provider-coremail\n"))
        self.assertTrue(browser_skill.startswith("---\nname: web-to-mail\n"))
        mcp_config = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(set(mcp_config["mcpServers"]), {"mail-mcp"})
        server = mcp_config["mcpServers"]["mail-mcp"]
        self.assertEqual(server["command"].lower(), "powershell.exe")
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/mcp/run-server.ps1", server["args"])
        self.assertNotIn("Bypass", server["args"])
        self.assertNotIn(str(ROOT), json.dumps(mcp_config))
        for launcher in ("INSTALL.cmd", "CONFIGURE.cmd", "OPEN-CONFIG.cmd", "UNINSTALL.cmd"):
            self.assertTrue((ROOT / launcher).is_file())

        installer = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
        registrar = (ROOT / "scripts" / "register_claude_user_mcp.py").read_text(encoding="utf-8")
        discovery = (ROOT / "scripts" / "windows-tool-discovery.ps1").read_text(encoding="utf-8")
        self.assertIn("mail-mcp-server", installer)
        self.assertIn("versions", installer)
        self.assertIn("payload\\runtime\\python.exe", installer)
        self.assertNotIn(".claude\\skills", installer)
        self.assertNotIn("plugin-backups", installer)
        self.assertNotIn("RunAs", installer)
        self.assertIn("scripts\\mcp-healthcheck.ps1", installer)
        self.assertIn("register_claude_user_mcp.py", installer)
        self.assertIn("Claude Code CLI was not found", installer)
        self.assertIn("-ClaudeCommand", (ROOT / "INSTALL.cmd").read_text(encoding="utf-8"))
        self.assertIn(".claude\\local", discovery)
        self.assertIn("$env:APPDATA", discovery)
        lifecycle_gate = (ROOT / "tests" / "windows-lifecycle.ps1").read_text(encoding="utf-8")
        self.assertIn("Automatic Claude Code discovery", lifecycle_gate)
        self.assertIn("Resolve-ClaudeCodeInvocation", lifecycle_gate)
        self.assertIn("--scope", registrar)
        self.assertIn("user-scope MCP", installer)
        self.assertNotIn("plugin validate", installer.lower())
        self.assertNotIn("plugin enable", installer.lower())
        self.assertNotIn("plugin list", installer.lower())
        launch_sources = installer + (ROOT / "INSTALL.cmd").read_text(encoding="utf-8")
        launch_sources += (ROOT / "scripts" / "mcp-healthcheck.ps1").read_text(encoding="utf-8")
        self.assertIn("ExecutionPolicy Bypass", launch_sources)

    def test_browser_orchestration_is_isolated_and_send_gated(self) -> None:
        browser_skill = (ROOT / "skills" / "web-to-coremail" / "SKILL.md").read_text(encoding="utf-8")
        normalized = " ".join(browser_skill.lower().split())
        self.assertIn("do not install, configure, wrap, or merge", normalized)
        self.assertIn("do not send email bodies", normalized)
        self.assertIn("never fetch additional browser content", normalized)
        self.assertIn("确认发送", browser_skill)

        implementation = "\n".join(
            path.read_text(encoding="utf-8")
            for directory in (ROOT / "mcp", ROOT / "scripts")
            for path in directory.glob("*")
            if path.suffix in {".py", ".ps1"}
        ).lower()
        self.assertNotIn("coremail_password", implementation)
        for browser_dependency in ("playwright", "puppeteer", "selenium", "chromedriver"):
            self.assertNotIn(browser_dependency, implementation)

    def test_initialize_tools_and_offline_status(self) -> None:
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "unit-test", "version": "1"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "mail_connection_status", "arguments": {}},
            },
        ]
        payload = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in requests)
        completed = subprocess.run(
            [sys.executable, "-B", "-I", str(SERVER)],
            input=payload,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=10,
            check=True,
        )
        responses = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
        self.assertEqual([response["id"] for response in responses], [1, 2, 3])
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "mail-mcp-server")
        self.assertEqual(responses[0]["result"]["serverInfo"]["version"], "0.9.0")
        self.assertIn("确认发送", responses[0]["result"]["instructions"])
        names = {tool["name"] for tool in responses[1]["result"]["tools"]}
        self.assertEqual(len(names), 22)
        self.assertIn("mail_discover_local", names)
        self.assertIn("mail_send_prepared", names)
        self.assertFalse(any("click" in name or "screenshot" in name or "window" in name for name in names))
        tools_by_name = {tool["name"]: tool for tool in responses[1]["result"]["tools"]}
        prepare_schema = tools_by_name["mail_prepare_message"]["inputSchema"]
        self.assertEqual(prepare_schema["properties"]["body_html"]["type"], "string")
        self.assertEqual(prepare_schema["properties"]["body_html"]["maxLength"], 500000)
        self.assertIn("text/html", tools_by_name["mail_get_message"]["description"])
        status = json.loads(responses[2]["result"]["content"][0]["text"])
        self.assertFalse(status["mail_client_interface_selected"])
        self.assertFalse(status["mail_client_interface_used"])
        self.assertFalse(status["mail_ui_automation_used"])
        self.assertIn("client_interface", status)

    def test_windows_stdio_bom_is_tolerated_only_at_stream_start(self) -> None:
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "bom-test", "version": "1"},
            },
        }
        later_bom_request = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "ping",
            "params": {},
        }
        completed = subprocess.run(
            [sys.executable, "-B", "-I", str(SERVER)],
            input=(
                "\ufeff"
                + json.dumps(initialize)
                + "\n\ufeff"
                + json.dumps(later_bom_request)
                + "\n"
            ),
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=10,
            check=True,
        )
        responses = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual(1, responses[0]["id"])
        self.assertEqual(
            "mail-mcp-server", responses[0]["result"]["serverInfo"]["name"]
        )
        self.assertIsNone(responses[1]["id"])
        self.assertEqual(-32700, responses[1]["error"]["code"])

    def test_implementation_has_no_desktop_automation_primitives(self) -> None:
        sources = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "mcp").glob("*")
            if path.suffix in {".py", ".ps1"}
        ).lower()
        for forbidden in ("uiautomationclient", "setcursorpos", "sendinput", "copyfromscreen", "mapi_dialog"):
            self.assertNotIn(forbidden, sources)

    def test_setup_is_interface_first_and_password_fallback_is_secure(self) -> None:
        setup = (ROOT / "scripts" / "setup-account.ps1").read_text(encoding="utf-8").lower()
        mapi = (ROOT / "mcp" / "windows_mapi.py").read_text(encoding="utf-8").lower()
        self.assertIn("mapilogon", mapi)
        self.assertIn("null profile/password and zero flags", mapi)
        self.assertIn("self._logon(0, none, none, 0, 0", " ".join(mapi.split()))
        self.assertIn("'--probe-json'", setup)
        self.assertIn("get-pinnedpythonruntime", setup)
        self.assertIn("windows_simple_mapi", setup)
        self.assertIn("imap_smtp", setup)
        self.assertIn("-assecurestring", setup)
        self.assertIn("windows credential manager", setup)
        self.assertNotIn("mapi_dialog", setup)

    def test_credential_writer_uses_unambiguous_filetime_type(self) -> None:
        setup = (ROOT / "scripts" / "windows-credential.ps1").read_text(
            encoding="utf-8"
        ).lower()
        self.assertIn(
            "public system.runtime.interopservices.comtypes.filetime lastwritten;",
            setup,
        )
        self.assertNotIn("public filetime lastwritten;", setup)

    def test_install_activation_does_not_import_temporary_directory_acl(self) -> None:
        installer = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8").lower()
        normalized = " ".join(installer.split())
        self.assertIn("staging", normalized)
        self.assertIn(
            "copy-coremailplugintree -source $sourceroot -destination $stageplugin",
            normalized,
        )
        self.assertIn(
            "move-coremaildirectoryatomically -source $stageplugin -destination $activeroot",
            normalized,
        )
        self.assertIsNone(re.search(r"(?<![a-z])move-item\b", normalized))
        self.assertIn("register_claude_user_mcp.py", normalized)

    def test_uninstall_fails_closed_on_lock_or_acl_denial(self) -> None:
        uninstaller = (ROOT / "scripts" / "uninstall.ps1").read_text(encoding="utf-8").lower()
        common = (ROOT / "scripts" / "windows-lifecycle-common.ps1").read_text(
            encoding="utf-8"
        ).lower()
        lifecycle = uninstaller + common
        self.assertIn("unauthorizedaccessexception", lifecycle)
        self.assertIn("io.ioexception", lifecycle)
        self.assertIn("ambiguous state", common)
        self.assertIn("guid", uninstaller)
        self.assertIn("fileshare]::none", common)
        self.assertIn("unregister", uninstaller)
        self.assertIsNone(re.search(r"(?<![a-z])move-item\b", lifecycle))
        self.assertNotIn("takeown", uninstaller)
        self.assertNotIn("icacls", uninstaller)
        self.assertNotIn(".claude\\skills", lifecycle)

    def test_python_version_probe_avoids_native_output_and_quote_loss(self) -> None:
        probe = ROOT / "mcp" / "check-python.py"
        completed = subprocess.run(
            [sys.executable, "-B", "-I", str(probe)],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr, "")

        sources = [
            (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8"),
            (ROOT / "mcp" / "run-server.ps1").read_text(encoding="utf-8"),
        ]
        for source in sources:
            normalized = " ".join(source.lower().split())
            self.assertNotIn("-c 'import sys", normalized)
            self.assertNotIn('print("%d.%d"', normalized)
        installer = " ".join(sources[0].lower().split())
        launcher = " ".join(sources[1].lower().split())
        self.assertIn("describe-python.py", installer)
        self.assertIn("python-runtime.json", launcher)
        self.assertIn("executable_sha256", launcher)
        self.assertNotIn("coremail_python", launcher)

        smoke = (ROOT / "scripts" / "mcp-healthcheck.ps1").read_text(encoding="utf-8")
        self.assertNotIn("StandardInputEncoding", smoke)
        self.assertNotIn("PYTHONDONTWRITEBYTECODE", smoke)
        self.assertIn('$startInfo.Arguments = "-B -I', smoke)


if __name__ == "__main__":
    unittest.main()
