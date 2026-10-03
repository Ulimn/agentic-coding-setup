import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import tool_installers as installers


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.bin = Path(self.directory.name) / "bin"
        self.bin.mkdir()
        local = patch.object(installers, "local_bin", return_value=self.bin)
        local.start()
        self.addCleanup(local.stop)

    def archive(self, name="bin/gh", kind=tarfile.REGTYPE):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w:gz") as archive:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.linkname = "/unrelated"
            content = b"test executable fixture"
            member.size = len(content) if kind == tarfile.REGTYPE else 0
            archive.addfile(member, io.BytesIO(content) if member.size else None)
        return data.getvalue()

    def plan(self, payload, checksum=None):
        return installers.ToolPlan(
            "GitHub CLI", "gh", "https://api.github.com/repos/cli/cli/releases/latest",
            "archive", "gh auth login", archive_url="https://github.com/cli/cli/archive.tar.gz",
            checksum=checksum or hashlib.sha256(payload).hexdigest(),
        )

    def test_unselected_tools_need_no_os_checks_or_network(self):
        with patch.object(installers, "ubuntu_arch") as check:
            self.assertEqual(installers.prepare_tools(()), ())
            check.assert_not_called()

    def test_unsupported_platform_rejected_before_downloading(self):
        with patch.object(installers.platform, "system", return_value="Darwin"):
            with patch.object(installers, "fetch_bytes") as fetch:
                with self.assertRaisesRegex(installers.InstallerError, "Ubuntu 22.04"):
                    installers.prepare_tools(("Codex",))
                fetch.assert_not_called()

    def test_existing_executable_is_verified_without_downloading(self):
        executable = self.bin / "codex"
        executable.write_text("fixture")
        executable.chmod(0o755)
        with patch.object(installers, "ubuntu_arch", return_value="amd64"):
            with patch.object(installers.shutil, "which", return_value=None):
                with patch.object(installers, "verify_executable", return_value="codex 1.0"):
                    with patch.object(installers, "fetch_bytes") as fetch:
                        plans = installers.prepare_tools(("Codex",))
                        self.assertEqual(plans[0].existing, str(executable))
                        fetch.assert_not_called()

    def test_non_executable_existing_file_is_preserved(self):
        target = self.bin / "gh"
        target.write_text("keep me")
        with patch.object(installers.shutil, "which", return_value=None):
            with self.assertRaisesRegex(installers.InstallerError, "not executable"):
                installers.find_executable("gh")
        self.assertEqual(target.read_text(), "keep me")

    def test_http_downloads_are_rejected(self):
        with self.assertRaisesRegex(installers.InstallerError, "HTTPS"):
            installers.request("http://example.com/tool")

    def test_checksum_parser_requires_matching_filename(self):
        digest = "a" * 64
        self.assertEqual(installers.checksum_for("gh.tar.gz", f"{digest} *gh.tar.gz\n"), digest)
        with self.assertRaises(installers.InstallerError):
            installers.checksum_for("gh.tar.gz", f"{digest} other.tar.gz\n")

    def test_github_and_gitlab_metadata_use_correct_arch_and_checksum(self):
        digest = "a" * 64
        for name, binary in (("GitHub CLI", "gh"), ("GitLab CLI", "glab")):
            asset_name = f"{binary}_1.2.3_linux_arm64.tar.gz"
            checksum_name = "gh_1.2.3_checksums.txt" if binary == "gh" else "checksums.txt"
            assets = [
                {"name": asset_name, "url": "https://example.com/binary"},
                {"name": checksum_name, "url": "https://example.com/checksums"},
            ]
            if binary == "glab":
                assets = {"links": assets}
            metadata = {"tag_name": "v1.2.3", "assets": assets}
            with self.subTest(name=name):
                with patch.object(installers, "fetch_bytes", side_effect=[json.dumps(metadata).encode(), f"{digest}  {asset_name}".encode()]):
                    plan = installers.release_plan(name, "arm64")
            self.assertEqual(plan.archive_url, "https://example.com/binary")
            self.assertEqual(plan.checksum, digest)

    def test_vscode_uses_microsoft_checksum_and_correct_arch(self):
        metadata = {"url": "https://example.com/code.tar.gz", "sha256hash": "b" * 64}
        with patch.object(installers, "fetch_bytes", return_value=json.dumps(metadata).encode()) as fetch:
            plan = installers.release_plan("Visual Studio Code Server", "amd64")
        self.assertIn("cli-linux-x64", fetch.call_args.args[0])
        self.assertEqual(plan.executable, "code")
        self.assertEqual(plan.checksum, "b" * 64)

    def test_archive_checksum_failure_never_executes_or_publishes(self):
        payload = self.archive()
        plan = self.plan(payload, "0" * 64)
        with patch.object(installers, "request", return_value=io.BytesIO(payload)):
            with patch.object(installers, "verify_executable") as verify:
                with self.assertRaisesRegex(installers.InstallerError, "checksum"):
                    installers.install_archive(plan, Path(self.directory.name))
                verify.assert_not_called()
        self.assertFalse((self.bin / "gh").exists())

    def test_archive_member_path_cannot_escape_destination(self):
        payload = self.archive("../../gh")
        with patch.object(installers, "request", return_value=io.BytesIO(payload)):
            with patch.object(installers, "verify_executable", return_value="gh 1.0"):
                installers.install_archive(self.plan(payload), Path(self.directory.name))
        self.assertEqual((self.bin / "gh").read_bytes(), b"test executable fixture")
        self.assertEqual((self.bin / "gh").stat().st_mode & 0o777, 0o755)

    def test_archive_symlink_is_rejected(self):
        payload = self.archive(kind=tarfile.SYMTYPE)
        with patch.object(installers, "request", return_value=io.BytesIO(payload)):
            with self.assertRaisesRegex(installers.InstallerError, "regular executable"):
                installers.install_archive(self.plan(payload), Path(self.directory.name))
        self.assertFalse((self.bin / "gh").exists())

    def test_native_installer_failure_stops_remaining_tools(self):
        plan = installers.ToolPlan("Codex", "codex", "https://chatgpt.com/codex/install.sh", "sh", "codex", script=b"#!/bin/sh\n")
        with patch.object(installers, "find_executable", return_value=None):
            with patch.object(installers.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)) as run:
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(installers.InstallerError, "installer failed"):
                        installers.install_tools((plan, plan))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs["env"]["CODEX_NON_INTERACTIVE"], "1")
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_completed_native_installer_must_pass_version_check(self):
        plan = installers.ToolPlan("Claude Code", "claude", "https://claude.ai/install.sh", "bash", "claude", script=b"#!/bin/bash\n")
        with patch.object(installers, "find_executable", side_effect=[None, "/test/claude"]):
            with patch.object(installers.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
                with patch.object(installers, "verify_executable", side_effect=installers.InstallerError("bad version")):
                    with contextlib.redirect_stdout(io.StringIO()):
                        with self.assertRaisesRegex(installers.InstallerError, "bad version"):
                            installers.install_tools((plan,))
        self.assertEqual(run.call_args.args[0][-1], "stable")


if __name__ == "__main__":
    unittest.main()
