from __future__ import annotations

import json
import imaplib
import os
import sqlite3
import smtplib
import sys
import tempfile
import unittest
from dataclasses import replace
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MCP_DIR = ROOT / "mcp"
sys.path.insert(0, str(MCP_DIR))

from coremail_backend import (  # noqa: E402
    ConfigError,
    CoremailBackend,
    CoremailError,
    Endpoint,
    PreparedMessageError,
    PreparedStore,
    Settings,
    StaleMessageError,
    build_email,
    fetch_raw_chunk_with_client,
    search_messages_with_client,
    set_flags_with_client,
    copy_or_move_with_client,
    manage_folder_with_client,
    imap_utf7_decode,
    imap_utf7_encode,
    imap_session,
    load_settings,
    default_config_path,
    parse_message,
    prepare_message,
    select_folder,
    _smtp_authenticate,
    smtp_session,
    MailProtocolError,
    WindowsMapiError,
)
from local_discovery import discover_local  # noqa: E402


def settings_for(directory: Path) -> Settings:
    return Settings(
        config_path=directory / "config.json",
        transport="imap_smtp",
        username="sender@example.com",
        credential_target="test-target",
        imap=Endpoint("imap.example.com", 993, "ssl"),
        smtp=Endpoint("smtp.example.com", 465, "ssl"),
        allowed_from=("sender@example.com",),
        drafts_folder="Drafts",
        sent_folder="Sent",
        sent_copy_mode="none",
        ca_file=None,
        attachment_roots=(directory.resolve(),),
        max_message_bytes=10 * 1024 * 1024,
        max_body_chars=50_000,
        max_attachment_bytes=25 * 1024 * 1024,
        max_recipients=100,
        timeout_seconds=20,
    )


def managed_config_path_for_test(profile: Path) -> Path:
    candidate = profile / "mail-mcp-server" / "config" / "settings.json"
    # The production path helper deliberately preserves Windows lexical paths
    # so junctions/reparse points cannot be hidden by resolution.  POSIX
    # development hosts keep their canonical path behavior.
    return candidate if os.name == "nt" else candidate.resolve()


def mapi_settings_for(directory: Path) -> Settings:
    return Settings(
        config_path=directory / "config.json",
        transport="windows_simple_mapi",
        username="sender@example.com",
        credential_target=None,
        imap=None,
        smtp=None,
        allowed_from=("sender@example.com",),
        drafts_folder=None,
        sent_folder=None,
        sent_copy_mode="none",
        ca_file=None,
        attachment_roots=(directory.resolve(),),
        max_message_bytes=10 * 1024 * 1024,
        max_body_chars=50_000,
        max_attachment_bytes=25 * 1024 * 1024,
        max_recipients=100,
        timeout_seconds=20,
    )


class SettingsTests(unittest.TestCase):
    def test_file_configuration_requires_explicit_schema_and_provider(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            path = Path(raw_directory) / "config.json"
            path.write_text(json.dumps({"username": "sender@example.com"}), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "schema_version"):
                load_settings(path, environ={})

    def test_loads_non_secret_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            config = {
                "schema_version": 1,
                "provider": "coremail",
                "username": "sender@example.com",
                "credential_target": "MailMcp.Coremail:sender@example.com",
                "imap": {"host": "imap.example.com", "port": 993, "security": "ssl"},
                "smtp": {"host": "smtp.example.com", "port": 587, "security": "starttls"},
                "allowed_from": ["sender@example.com"],
                "attachment_roots": [str(directory)],
            }
            path = directory / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            loaded = load_settings(path, environ={"CLAUDE_PROJECT_DIR": str(directory)})
            summary = loaded.public_summary()
            self.assertEqual(loaded.smtp.security, "starttls")
            self.assertEqual(loaded.transport, "imap_smtp")
            self.assertNotIn("password", json.dumps(summary).lower())
            self.assertEqual(summary["username"], "sender@example.com")

    def test_default_config_path_is_project_owned(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            path = default_config_path({"USERPROFILE": raw_directory})
            self.assertEqual(
                managed_config_path_for_test(Path(raw_directory)),
                path,
            )

    def test_loads_password_free_simple_mapi_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            path = directory / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "provider": "coremail",
                        "transport": "windows_simple_mapi",
                        "username": "sender@example.com",
                        "allowed_from": ["sender@example.com"],
                        "sent_copy_mode": "none",
                    }
                ),
                encoding="utf-8",
            )
            loaded = load_settings(path, environ={})
            self.assertEqual(loaded.transport, "windows_simple_mapi")
            self.assertIsNone(loaded.credential_target)
            self.assertIsNone(loaded.imap)
            self.assertIsNone(loaded.smtp)
            self.assertNotIn("password", json.dumps(loaded.public_summary()).lower())

    def test_rejects_protocol_or_alias_fields_in_simple_mapi_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            base = {
                "schema_version": 1,
                "provider": "coremail",
                "transport": "windows_simple_mapi",
                "username": "sender@example.com",
                "allowed_from": ["sender@example.com"],
            }
            path = directory / "config.json"
            path.write_text(
                json.dumps({**base, "credential_target": "unexpected"}),
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                load_settings(path, environ={})
            path.write_text(
                json.dumps({**base, "allowed_from": ["alias@example.com"]}),
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                load_settings(path, environ={})

    def test_rejects_plaintext_protocol_mode(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            path = directory / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "provider": "coremail",
                        "username": "sender@example.com",
                        "imap": {"host": "imap.example.com", "port": 143, "security": "plain"},
                        "smtp": {"host": "smtp.example.com", "port": 465, "security": "ssl"},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                load_settings(path, environ={})

    def test_loads_oauth_auth_method_and_download_directory_without_secret(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            path = directory / "config.json"
            path.write_text(json.dumps({
                "schema_version": 1, "provider": "coremail", "username": "sender@example.com",
                "auth_method": "xoauth2", "download_directory": "downloads",
                "imap": {"host": "imap.example.com", "port": 993, "security": "ssl"},
                "smtp": {"host": "smtp.example.com", "port": 465, "security": "ssl"},
            }), encoding="utf-8")
            loaded = load_settings(path, environ={})
            self.assertEqual(loaded.auth_method, "xoauth2")
            self.assertEqual(loaded.download_directory, (directory / "downloads").resolve())

    def test_rejects_non_integer_json_limits_and_ports(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            base = {
                "schema_version": 1,
                "provider": "coremail",
                "username": "sender@example.com",
                "imap": {"host": "imap.example.com", "port": 993, "security": "ssl"},
                "smtp": {"host": "smtp.example.com", "port": 465, "security": "ssl"},
            }
            path = directory / "config.json"
            for field, value in (
                ("schema_version", 1.0),
                ("max_recipients", "100"),
                ("timeout_seconds", "20"),
            ):
                candidate = dict(base)
                candidate[field] = value
                path.write_text(json.dumps(candidate), encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_settings(path, environ={})
            candidate = dict(base)
            candidate["imap"] = {**base["imap"], "port": 993.5}
            path.write_text(json.dumps(candidate), encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_settings(path, environ={})

    def test_rejects_secret_or_url_fields_in_non_secret_config(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            base = {
                "schema_version": 1,
                "provider": "coremail",
                "username": "sender@example.com",
                "imap": {"host": "imap.example.com", "port": 993, "security": "ssl"},
                "smtp": {"host": "smtp.example.com", "port": 465, "security": "ssl"},
            }
            path = directory / "config.json"
            path.write_text(json.dumps({**base, "password": "must-not-be-here"}), encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_settings(path, environ={})
            base["imap"]["host"] = "imaps://user:secret@imap.example.com"
            path.write_text(json.dumps(base), encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_settings(path, environ={})

    def test_config_status_and_configure_are_secret_free_and_reloadable(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            profile = Path(raw_directory)
            backend = CoremailBackend()
            with patch.dict("os.environ", {"USERPROFILE": str(profile)}, clear=False):
                initial = backend.config_status()
                self.assertFalse(initial["configured"])
                self.assertEqual(
                    managed_config_path_for_test(profile),
                    Path(initial["config_path"]),
                )
                self.assertIn("username", initial["missing_fields"])
                result = backend.configure(
                    {
                        "transport": "windows_simple_mapi",
                        "username": "sender@example.com",
                        "allowed_from": ["sender@example.com"],
                        "attachment_roots": [],
                    }
                )
                self.assertTrue(result["configured"])
                self.assertEqual(result["provider"], "coremail")
                self.assertEqual(result["missing_fields"], [])
                content = (profile / "mail-mcp-server" / "config" / "settings.json").read_text()
                self.assertNotIn("password", content.lower())
                reloaded = backend.reload_config()
                self.assertTrue(reloaded["configured"])
                with self.assertRaises(ConfigError):
                    backend.configure({"password": "secret"})
                with self.assertRaises(ConfigError):
                    backend.configure({"config_path": str(profile / "outside" / "settings.json")})

    def test_configure_can_switch_from_imap_to_simple_mapi(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            profile = Path(raw_directory)
            backend = CoremailBackend()
            with patch.dict("os.environ", {"USERPROFILE": str(profile)}, clear=False):
                backend.configure(
                    {
                        "transport": "imap_smtp",
                        "username": "sender@example.com",
                        "imap": {"host": "imap.example.com", "port": 993, "security": "ssl"},
                        "smtp": {"host": "smtp.example.com", "port": 465, "security": "ssl"},
                        "allowed_from": ["sender@example.com"],
                        "credential_target": "MailMcp.Coremail:old",
                        "sent_copy_mode": "append",
                    }
                )
                result = backend.configure(
                    {
                        "transport": "windows_simple_mapi",
                        "username": "sender@example.com",
                    }
                )
                self.assertTrue(result["configured"])
                self.assertEqual(result["transport"], "windows_simple_mapi")
                raw = json.loads(
                    (profile / "mail-mcp-server" / "config" / "settings.json").read_text(
                        encoding="utf-8"
                    )
                )
                for stale_field in (
                    "credential_target",
                    "imap",
                    "smtp",
                    "ca_file",
                    "drafts_folder",
                    "sent_folder",
                ):
                    self.assertNotIn(stale_field, raw)
                self.assertEqual(raw["sent_copy_mode"], "none")
                self.assertEqual(raw["allowed_from"], ["sender@example.com"])

    def test_config_status_reports_invalid_fields_and_next_step_without_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            profile = Path(raw_directory)
            config_path = profile / "mail-mcp-server" / "config" / "settings.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "provider": "coremail",
                        "transport": "unsupported",
                        "username": "sender@example.com",
                        "credential_target": "super-secret-target",
                    }
                ),
                encoding="utf-8",
            )
            backend = CoremailBackend()
            with patch.dict("os.environ", {"USERPROFILE": str(profile)}, clear=False):
                status = backend.config_status()
            self.assertFalse(status["configured"])
            self.assertIn("transport", status["missing_fields"])
            self.assertIn("transport", status["invalid_fields"])
            self.assertEqual(status["next_command"], status["next_step"])
            self.assertNotIn("super-secret-target", json.dumps(status, ensure_ascii=False))

    def test_missing_optional_transport_uses_documented_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            profile = Path(raw_directory)
            config_path = profile / "mail-mcp-server" / "config" / "settings.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "provider": "coremail",
                        "username": "sender@example.com",
                        "imap": {"host": "imap.example.com", "port": 993, "security": "ssl"},
                        "smtp": {"host": "smtp.example.com", "port": 465, "security": "ssl"},
                    }
                ),
                encoding="utf-8",
            )
            backend = CoremailBackend()
            with patch.dict("os.environ", {"USERPROFILE": str(profile)}, clear=False):
                status = backend.config_status()
            self.assertTrue(status["configured"])
            self.assertEqual(status["transport"], "imap_smtp")
            self.assertNotIn("transport", status["missing_fields"])

class MailEncodingTests(unittest.TestCase):
    def test_modified_utf7_round_trip(self) -> None:
        for value in ("INBOX", "草稿箱", "项目 & 归档", "已发送/2026"):
            self.assertEqual(imap_utf7_decode(imap_utf7_encode(value)), value)

    def test_parse_html_and_attachment_without_executing_content(self) -> None:
        raw = (
            b"From: sender@example.com\r\n"
            b"To: user@example.com\r\n"
            b"Subject: Example\r\n"
            b"MIME-Version: 1.0\r\n"
            b"Content-Type: multipart/mixed; boundary=x\r\n\r\n"
            b"--x\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
            b"<p>Hello</p><script>ignore me</script><p>World</p>\r\n"
            b"--x\r\nContent-Type: application/octet-stream\r\n"
            b"Content-Disposition: attachment; filename=test.bin\r\n\r\nabc\r\n"
            b"--x--\r\n"
        )
        parsed = parse_message(raw, 1000)
        self.assertIn("Hello", parsed["body"])
        self.assertNotIn("ignore me", parsed["body"])
        self.assertEqual(parsed["attachments"][0]["filename"], "test.bin")
        self.assertTrue(parsed["content_is_untrusted"])

    def test_parse_exposes_both_mime_body_variants_and_skips_html_attachment(self) -> None:
        raw = (
            b"From: sender@example.com\r\n"
            b"Subject: Formats\r\n"
            b"MIME-Version: 1.0\r\n"
            b"Content-Type: multipart/alternative; boundary=x\r\n\r\n"
            b"--x\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
            b"Plain version\r\n"
            b"--x\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
            b"<p>HTML <strong>version</strong></p>\r\n"
            b"--x\r\nContent-Type: text/html; charset=utf-8\r\n"
            b"Content-Disposition: attachment; filename=fragment.html\r\n\r\n"
            b"<p>Attached fragment</p>\r\n"
            b"--x--\r\n"
        )
        parsed = parse_message(raw, 1000)
        self.assertIn("Plain version", parsed["body_text"])
        self.assertIn("<p>HTML <strong>version</strong></p>", parsed["body_html"])
        self.assertNotIn("Attached fragment", parsed["body_html"])
        self.assertFalse(parsed["body_html_truncated"])

    def test_parse_body_variants_have_independent_limits(self) -> None:
        raw = (
            b"MIME-Version: 1.0\r\nContent-Type: multipart/alternative; boundary=x\r\n\r\n"
            b"--x\r\nContent-Type: text/plain\r\n\r\n0123456789\r\n"
            b"--x\r\nContent-Type: text/html\r\n\r\n<strong>0123456789</strong>\r\n"
            b"--x--\r\n"
        )
        parsed = parse_message(raw, 5)
        self.assertEqual(parsed["body_text"], "01234")
        self.assertEqual(parsed["body_html"], "<stro")
        self.assertTrue(parsed["body_text_truncated"])
        self.assertTrue(parsed["body_html_truncated"])

    def test_parse_preserves_calendar_body_and_mime_tree(self) -> None:
        raw = (
            b"MIME-Version: 1.0\r\nContent-Type: text/calendar; method=REQUEST\r\n\r\n"
            b"BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nEND:VCALENDAR\r\n"
        )
        parsed = parse_message(raw, 1000)
        self.assertIn("METHOD:REQUEST", parsed["body_calendar"])
        self.assertTrue(any(part["content_type"] == "text/calendar" for part in parsed["mime_parts"]))


class PreparedMessageTests(unittest.TestCase):
    def test_build_email_preserves_html_only_and_multipart_alternative(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            settings = settings_for(Path(raw_directory))
            cases = (
                ({"body_html": "<p>Only HTML</p>"}, ("text/html",)),
                (
                    {"body_text": "纯文本", "body_html": "<p>富文本 ✓</p>"},
                    ("text/plain", "text/html"),
                ),
            )
            for values, expected_formats in cases:
                with self.subTest(values=values):
                    prepared = prepare_message(
                        settings,
                        {"to": ["recipient@example.com"], **values},
                    )
                    self.assertEqual(prepared.body_formats, expected_formats)
                    parsed = BytesParser(policy=policy.default).parsebytes(
                        build_email(prepared).as_bytes(policy=policy.SMTP)
                    )
                    self.assertEqual(
                        [part.get_content_type() for part in parsed.walk() if not part.is_multipart()],
                        list(expected_formats),
                    )
                    self.assertEqual(prepared.summary(settings)["body_html"], values.get("body_html"))

    def test_prepare_supports_reply_to_calendar_and_inline_related_part(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            image = directory / "logo.png"
            image.write_bytes(b"png-data")
            settings = settings_for(directory)
            message = prepare_message(settings, {
                "to": ["recipient@example.com"],
                "reply_to": ["Replies <reply@example.com>"],
                "body_text": "Plain",
                "body_html": '<img src="cid:logo">',
                "body_calendar": "BEGIN:VCALENDAR\r\nEND:VCALENDAR",
                "attachments": [{"path": str(image), "disposition": "inline", "content_id": "logo"}],
            })
            parsed = BytesParser(policy=policy.default).parsebytes(build_email(message).as_bytes())
            self.assertEqual(parsed["Reply-To"], "Replies <reply@example.com>")
            self.assertIn("text/calendar", [part.get_content_type() for part in parsed.walk()])
            self.assertTrue(any(part.get("Content-ID") == "<logo>" and part.get_content_disposition() == "inline" for part in parsed.walk()))
    def test_simple_mapi_rejects_html_before_creating_a_prepared_token(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            with self.assertRaisesRegex(CoremailError, "supports text/plain only"):
                prepare_message(
                    mapi_settings_for(Path(raw_directory)),
                    {"to": ["recipient@example.com"], "body_html": "<p>Rich</p>"},
                )

    def test_attachment_outside_authorized_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root, tempfile.TemporaryDirectory() as raw_outside:
            root = Path(raw_root)
            outside = Path(raw_outside) / "outside.txt"
            outside.write_text("not authorized", encoding="utf-8")
            with self.assertRaisesRegex(CoremailError, "outside the authorized roots"):
                prepare_message(
                    settings_for(root),
                    {"to": ["recipient@example.com"], "attachments": [str(outside)]},
                )

    def test_preparation_and_attachment_hash_are_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            attachment = directory / "report.txt"
            attachment.write_text("approved", encoding="utf-8")
            settings = settings_for(directory)
            prepared = prepare_message(
                settings,
                {
                    "to": ["Recipient <recipient@example.com>"],
                    "bcc": ["audit@example.com"],
                    "subject": "Review",
                    "body_text": "Please review.",
                    "attachments": [str(attachment)],
                },
            )
            message = build_email(prepared)
            self.assertEqual(message["Subject"], "Review")
            self.assertIn("audit@example.com", str(message["Bcc"]))
            attachment.write_text("changed", encoding="utf-8")
            with self.assertRaises(PreparedMessageError):
                build_email(prepared)

    def test_store_expires_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            message = prepare_message(
                settings_for(Path(raw_directory)),
                {"to": ["recipient@example.com"], "subject": "x", "body_text": "y"},
            )
            store = PreparedStore(ttl_seconds=1)
            with patch("coremail_backend.time.monotonic", side_effect=[10.0, 12.0]):
                token, _ = store.put(message)
                with self.assertRaises(PreparedMessageError):
                    store.get(token)

    def test_send_requires_exact_confirmation_before_consuming_token(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            settings = settings_for(Path(raw_directory))
            message = prepare_message(
                settings,
                {"to": ["recipient@example.com"], "subject": "x", "body_text": "y"},
            )
            backend = CoremailBackend()
            token, _ = backend.store.put(message)
            with self.assertRaises(PreparedMessageError):
                backend.send_prepared({"prepared_token": token, "confirmation": "yes"})
            self.assertIs(backend.store.get(token), message)
            with patch("coremail_backend.load_settings", return_value=settings), patch(
                "coremail_backend.send_message", return_value={"smtp_submitted": True}
            ) as send:
                result = backend.send_prepared({"prepared_token": token, "confirmation": "确认发送"})
            self.assertTrue(result["smtp_submitted"])
            send.assert_called_once_with(settings, message)
            with self.assertRaises(PreparedMessageError):
                backend.store.get(token)

    def test_prepared_token_requires_the_reviewed_settings_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            original_settings = settings_for(directory)
            changed_settings = replace(
                original_settings,
                username="other@example.com",
                allowed_from=("other@example.com",),
            )
            backend = CoremailBackend()
            with patch.object(
                backend,
                "_load_settings",
                side_effect=[original_settings, changed_settings],
            ):
                prepared = backend.prepare(
                    {"to": ["recipient@example.com"], "subject": "reviewed"}
                )
                token = prepared["prepared_token"]
                with patch("coremail_backend.send_message") as send:
                    with self.assertRaisesRegex(PreparedMessageError, "configuration changed"):
                        backend.send_prepared(
                            {"prepared_token": token, "confirmation": "确认发送"}
                        )
                send.assert_not_called()
                # A stale review remains available so the caller can prepare a
                # new message after consciously fixing the configuration.
                self.assertIsNotNone(backend.store.get(token))

    def test_mapi_send_uses_verified_attachment_snapshot(self) -> None:
        class RecordingMapiClient:
            def __init__(self) -> None:
                self.values = None
                self.snapshot_content = None

            def send(self, **values):
                self.values = values
                self.snapshot_content = Path(values["attachments"][0][0]).read_text(encoding="utf-8")
                return {"mapi_submitted": True}

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            original = directory / "report.txt"
            original.write_text("approved", encoding="utf-8")
            settings = mapi_settings_for(directory)
            message = prepare_message(
                settings,
                {"to": ["recipient@example.com"], "attachments": [str(original)]},
            )
            client = RecordingMapiClient()
            backend = CoremailBackend(mapi_client=client)
            token, _ = backend.store.put(message)
            with patch("coremail_backend.load_settings", return_value=settings):
                result = backend.send_prepared(
                    {"prepared_token": token, "confirmation": "确认发送"}
                )
            self.assertTrue(result["mapi_submitted"])
            self.assertEqual(client.snapshot_content, "approved")
            self.assertNotEqual(Path(client.values["attachments"][0][0]), original)
            self.assertFalse(Path(client.values["attachments"][0][0]).exists())

    def test_mapi_unsupported_draft_or_threading_does_not_consume_token(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            settings = mapi_settings_for(directory)
            backend = CoremailBackend()
            draft = prepare_message(settings, {"to": ["recipient@example.com"]})
            draft_token, _ = backend.store.put(draft)
            with patch("coremail_backend.load_settings", return_value=settings):
                with self.assertRaisesRegex(CoremailError, "Saving drafts"):
                    backend.save_draft({"prepared_token": draft_token})
            self.assertIs(backend.store.get(draft_token), draft)

            reply = prepare_message(
                settings,
                {"to": ["recipient@example.com"], "in_reply_to": "<prior@example.com>"},
            )
            reply_token, _ = backend.store.put(reply)
            with patch("coremail_backend.load_settings", return_value=settings):
                with self.assertRaisesRegex(PreparedMessageError, "cannot preserve"):
                    backend.send_prepared(
                        {"prepared_token": reply_token, "confirmation": "确认发送"}
                    )
            self.assertIs(backend.store.get(reply_token), reply)

    def test_mapi_subject_limit_prevents_provider_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            settings = mapi_settings_for(Path(raw_directory))
            with self.assertRaisesRegex(CoremailError, "255-character"):
                prepare_message(
                    settings,
                    {"to": ["recipient@example.com"], "subject": "x" * 256},
                )


class ImapIdentityTests(unittest.TestCase):
    class FakeImap:
        def select(self, mailbox: str, readonly: bool = False):
            return "OK", [b"3"]

        def response(self, name: str):
            return "UIDVALIDITY", [b"42"]

    def test_uidvalidity_mismatch_stops_action(self) -> None:
        with self.assertRaises(StaleMessageError):
            select_folder(self.FakeImap(), "INBOX", readonly=True, expected_uidvalidity="41")

    def test_folder_control_characters_are_rejected_before_imap(self) -> None:
        with self.assertRaisesRegex(CoremailError, "control character"):
            select_folder(self.FakeImap(), "INBOX\r\nUID SEARCH ALL", readonly=True)

    class CapabilityImap(FakeImap):
        capabilities = (b"IMAP4rev1", b"UIDPLUS", b"MOVE")

        def __init__(self) -> None:
            self.calls = []

        def uid(self, command: str, *args):
            self.calls.append((command, args))
            if command == "SEARCH":
                return "OK", [b"1 2 3"]
            if command == "FETCH" and "HEADER.FIELDS" in str(args):
                return "OK", [(b"* 3 FETCH (UID 3 FLAGS (\\Seen) RFC822.SIZE 4)", b"Subject: test\\r\\n\\r\\n")]
            if command == "FETCH" and "RFC822.SIZE" in str(args):
                return "OK", [(b"* 3 FETCH (RFC822.SIZE 4)", None)]
            if command == "FETCH":
                return "OK", [(b"* 3 FETCH (BODY[]<0> {4})", b"test")]
            return "OK", [b"done"]

        def store(self, *args):
            return "OK", [b"done"]

        def create(self, *args):
            return "OK", [b"done"]

    def test_search_supports_header_or_and_cursor_primitives(self) -> None:
        client = self.CapabilityImap()
        result = search_messages_with_client(client, folder="INBOX", query={"or": [{"from": "a"}, {"header": {"name": "X-Test", "value": "v"}}]}, limit=2)
        self.assertEqual(result["snapshot_uid"], 3)
        self.assertTrue(any(call[0] == "SEARCH" for call in client.calls))

    def test_search_or_preserves_all_branches(self) -> None:
        client = self.CapabilityImap()
        search_messages_with_client(
            client,
            folder="INBOX",
            query={"or": [{"from": "a"}, {"subject": "b"}, {"text": "c"}]},
            limit=2,
        )
        search_call = next(call for call in client.calls if call[0] == "SEARCH")
        rendered = " ".join(str(item) for item in search_call[1])
        self.assertGreaterEqual(rendered.count("OR"), 2)
        self.assertIn('FROM "a"', rendered)
        self.assertIn('SUBJECT "b"', rendered)
        self.assertIn('TEXT "c"', rendered)

    def test_smtp_auth_callbacks_support_initial_response_without_challenge(self) -> None:
        class FakeSmtp:
            def __init__(self) -> None:
                self.calls = []

            def auth(self, mechanism, authobject):
                self.calls.append((mechanism, authobject()))
                self.calls.append((mechanism, authobject(b"challenge")))

        client = FakeSmtp()
        _smtp_authenticate(
            client,
            replace(settings_for(Path(".")), auth_method="xoauth2"),
            "access-token",
        )
        self.assertEqual(client.calls[0][0], "XOAUTH2")
        self.assertIn("auth=Bearer access-token", client.calls[0][1])
        self.assertEqual(client.calls[1][1], "")

    def test_flags_and_raw_chunks_are_bounded(self) -> None:
        client = self.CapabilityImap()
        flags = set_flags_with_client(client, folder="INBOX", uid="3", expected_uidvalidity="42", add=["\\Flagged", "Project"], remove=[])
        self.assertEqual(flags["added"], ["\\Flagged", "Project"])
        raw = fetch_raw_chunk_with_client(client, folder="INBOX", uid="3", expected_uidvalidity="42", offset=0, length=4)
        self.assertEqual(raw["data_base64"], "dGVzdA==")

    def test_folder_management_rejects_nonempty_delete_without_override(self) -> None:
        client = self.CapabilityImap()
        with self.assertRaisesRegex(CoremailError, "not empty"):
            manage_folder_with_client(client, action="delete", folder="Archive")


class AuthenticationGuidanceTests(unittest.TestCase):
    def test_imap_authentication_failure_includes_mcp_reconfiguration_guidance(self) -> None:
        class FakeImap:
            def login(self, username, secret):
                raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] invalid credentials")

            def logout(self):
                return "BYE", [b"logout"]

        with tempfile.TemporaryDirectory() as raw_directory:
            settings = settings_for(Path(raw_directory))
            with patch("coremail_backend.get_password", return_value="expired-password"), patch(
                "coremail_backend.imaplib.IMAP4_SSL", return_value=FakeImap()
            ):
                with self.assertRaises(MailProtocolError) as raised:
                    with imap_session(settings):
                        pass
        message = str(raised.exception)
        self.assertIn("CONFIGURE.cmd", message)
        self.assertIn("mail_config_reload", message)
        self.assertNotIn("expired-password", message)

    def test_smtp_authentication_failure_includes_mcp_reconfiguration_guidance(self) -> None:
        class FakeSmtp:
            def login(self, username, secret):
                raise smtplib.SMTPAuthenticationError(535, b"5.7.8 authentication failed")

            def quit(self):
                return 221, b"bye"

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as raw_directory:
            settings = settings_for(Path(raw_directory))
            with patch("coremail_backend.get_password", return_value="expired-password"), patch(
                "coremail_backend.smtplib.SMTP_SSL", return_value=FakeSmtp()
            ):
                with self.assertRaises(MailProtocolError) as raised:
                    with smtp_session(settings):
                        pass
        message = str(raised.exception)
        self.assertIn("CONFIGURE.cmd", message)
        self.assertIn("mail_config_reload", message)
        self.assertNotIn("expired-password", message)

    def test_connection_status_guides_missing_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            settings = settings_for(Path(raw_directory))
            backend = CoremailBackend()
            with patch.object(backend, "config_status", return_value={"configured": True, "client_interface": {}}), patch.object(
                backend, "_load_settings", return_value=settings
            ), patch("coremail_backend.credential_available", return_value=False):
                result = backend.mail_connection_status()
        self.assertIn("CONFIGURE.cmd", result["next_step"])
        self.assertIn("mail_config_reload", result["next_step"])

    def test_simple_mapi_session_failure_guides_client_reauthentication(self) -> None:
        with self.assertRaises(MailProtocolError) as raised:
            CoremailBackend._raise_mapi(
                WindowsMapiError("Windows Simple MAPI status failed (code 19: invalid or expired session)")
            )
        message = str(raised.exception)
        self.assertIn("Coremail/Windows mail client", message)
        self.assertIn("update the expired password/token", message)


class LocalDiscoveryTests(unittest.TestCase):
    def test_deep_discovery_redacts_secrets_and_reads_sqlite_schema_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            (directory / "account.json").write_text(
                json.dumps(
                    {
                        "username": "user@example.com",
                        "server_url": (
                            "https://user:url-secret@mail.example.com:8443/private/access-token"
                            "?token=top-secret"
                        ),
                        "password": "must-not-escape",
                        "cookie": "also-secret",
                    }
                ),
                encoding="utf-8",
            )
            (directory / "legacy.ini").write_text(
                "username=user@example.com\npassword=secret-password@example.net\n",
                encoding="utf-8",
            )
            database = directory / "mail.sqlite"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE messages (subject TEXT, sender TEXT, body TEXT)")
            connection.commit()
            connection.close()

            result = discover_local({"roots": [str(directory)], "deep": True, "max_files": 100})
            rendered = json.dumps(result, ensure_ascii=False)
            self.assertNotIn("must-not-escape", rendered)
            self.assertNotIn("also-secret", rendered)
            self.assertNotIn("top-secret", rendered)
            self.assertNotIn("url-secret", rendered)
            self.assertNotIn("access-token", rendered)
            self.assertNotIn("secret-password@example.net", rendered)
            self.assertIn("https://mail.example.com:8443", rendered)
            self.assertIn("<redacted>", rendered)
            self.assertIn("messages", rendered)
            self.assertFalse(result["secret_values_returned"])
            self.assertTrue(result["read_only"])


if __name__ == "__main__":
    unittest.main()
