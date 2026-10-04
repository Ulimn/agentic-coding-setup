import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import environment_installers as environment
import tool_installers as installers
import setup_vm


class EnvironmentInstallerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_node_partial_bundle_is_not_skipped_when_node_exists(self):
        paths = {"node": "/test/node", "npm": "/test/npm"}
        with patch.object(environment, "find_executable", side_effect=paths.get):
            with patch.object(environment, "verify_executable", return_value="v24.1.0"):
                with patch.object(environment, "fetch_bytes") as fetch:
                    plan = environment.prepare_node("amd64")
        self.assertEqual(plan.packages, ("pnpm", "yarn", "jest", "playwright"))
        self.assertIsNone(plan.existing)
        fetch.assert_not_called()

    def test_existing_node_bundle_is_verified_without_downloads(self):
        with patch.object(environment, "find_executable", side_effect=lambda name: f"/test/{name}"):
            with patch.object(environment, "verify_executable", return_value="v24.1.0") as verify:
                with patch.object(environment, "fetch_bytes") as fetch:
                    plan = environment.prepare_node("arm64")
        self.assertEqual(plan.existing, "/test/node")
        self.assertEqual(verify.call_count, 6)
        fetch.assert_not_called()

    def test_old_existing_node_is_not_overwritten(self):
        with patch.object(environment, "find_executable", return_value="/test/node"):
            with patch.object(environment, "verify_executable", return_value="v18.20.0"):
                with self.assertRaisesRegex(installers.InstallerError, "Upgrade it manually"):
                    environment.prepare_node("amd64")

    def test_node_uses_latest_lts_and_matching_architecture_checksum(self):
        metadata = [{"version": "v26.1.0", "lts": False}, {"version": "v24.1.0", "lts": "LTS"}, {"version": "v22.1.0", "lts": "LTS"}]
        filename = "node-v24.1.0-linux-arm64.tar.gz"
        with patch.object(environment, "missing_node_commands", return_value=environment.NODE_COMMANDS):
            with patch.object(environment, "fetch_bytes", side_effect=[json.dumps(metadata).encode(), f"{'a' * 64}  {filename}\n".encode()]):
                plan = environment.prepare_node("arm64")
        self.assertEqual(plan.archive_url, f"https://nodejs.org/dist/v24.1.0/{filename}")
        self.assertEqual(plan.checksum, "a" * 64)

    def make_node_archive(self, name="node-v24.1.0-linux-arm64/bin/node", kind=tarfile.REGTYPE):
        archive = self.root / "node.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.linkname = "/outside"
            member.size = 4 if kind == tarfile.REGTYPE else 0
            bundle.addfile(member, io.BytesIO(b"node") if member.size else None)
        return archive

    def test_node_archive_rejects_traversal_and_selected_symlinks(self):
        for name, kind in (("release/../../outside", tarfile.REGTYPE), ("release/bin/node", tarfile.SYMTYPE)):
            with self.subTest(name=name):
                archive = self.make_node_archive(name, kind)
                with self.assertRaises(installers.InstallerError):
                    environment.unpack_node(archive, self.root / "stage")
        self.assertFalse((self.root / "outside").exists())

    def test_node_archive_accepts_root_directory_and_copies_only_runtime_files(self):
        archive = self.root / "node.tar.gz"
        files = ("bin/node", "lib/node_modules/npm/bin/npm-cli.js", "lib/node_modules/npm/bin/npx-cli.js", "unrelated-file")
        with tarfile.open(archive, "w:gz") as bundle:
            root = tarfile.TarInfo("node-release")
            root.type = tarfile.DIRTYPE
            bundle.addfile(root)
            for file in files:
                member = tarfile.TarInfo(f"node-release/{file}")
                member.size = 4
                member.mode = 0o755
                bundle.addfile(member, io.BytesIO(b"node"))
        destination = self.root / "stage"
        environment.unpack_node(archive, destination)
        self.assertEqual((destination / "bin/node").read_bytes(), b"node")
        self.assertFalse((destination / "unrelated-file").exists())

    def test_node_checksum_failure_publishes_no_commands(self):
        plan = installers.ToolPlan("Node Tools", "node", "", "node", "", archive_url="https://example.com/node.tar.gz", checksum="0" * 64)
        with patch.object(environment.Path, "home", return_value=self.root):
            with patch.object(environment, "request", return_value=io.BytesIO(b"bad archive")):
                with self.assertRaisesRegex(installers.InstallerError, "checksum"):
                    environment.install_node_runtime(plan, ("node", "npm"))
        self.assertFalse((self.root / ".local/bin/node").exists())
        self.assertEqual(list((self.root / ".local/share/vm-setup/node").iterdir()), [])

    def test_node_package_failure_stops_bundle_and_uses_user_prefix(self):
        plan = installers.ToolPlan("Node Tools", "node", "", "node", "", packages=("pnpm", "jest"))
        paths = {"node": "/test/node", "npm": "/test/npm"}
        with patch.object(environment, "missing_node_commands", return_value=("pnpm", "jest")):
            with patch.object(environment, "find_executable", side_effect=paths.get):
                with patch.object(environment.Path, "home", return_value=self.root):
                    with patch.object(environment.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)) as run:
                        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(installers.InstallerError):
                            environment.install_node(plan)
        self.assertEqual(run.call_count, 1)
        command = run.call_args.args[0]
        self.assertEqual(command[-1], "pnpm")
        self.assertEqual(command[command.index("--prefix") + 1], str(self.root / ".local"))
        self.assertNotIn("sudo", command)
        self.assertEqual(run.call_args.kwargs["env"]["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"], "1")

    def test_node_rerun_does_not_reinstall_complete_bundle(self):
        plan = installers.ToolPlan("Node Tools", "node", "", "node", "")
        with patch.object(environment, "missing_node_commands", return_value=()):
            with patch.object(environment, "run_checked") as run:
                with contextlib.redirect_stdout(io.StringIO()):
                    environment.install_node(plan)
        run.assert_not_called()

    def test_yarn_setup_enables_only_yarn_and_does_not_change_a_project(self):
        plan = installers.ToolPlan("Node Tools", "node", "", "node", "", packages=("yarn",))
        paths = {name: f"/test/{name}" for name in ("node", "npm", "pnpm", "jest", "playwright", "corepack")}
        with patch.object(environment, "missing_node_commands", side_effect=[("yarn",), ()]):
            with patch.object(environment, "find_executable", side_effect=paths.get):
                with patch.object(environment, "verify_executable"):
                    with patch.object(environment, "run_checked") as run, contextlib.redirect_stdout(io.StringIO()):
                        environment.install_node(plan)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0][-1], "yarn")
        self.assertEqual(commands[1], ["/test/corepack", "install", "--global", "yarn@stable"])
        self.assertFalse(any("use" in command or "--force" in command for command in commands))

    def test_complete_docker_packages_require_engine_and_both_plugins(self):
        with patch.object(environment.shutil, "which", return_value="/test/program"):
            with patch.object(environment, "installed_packages", return_value=set(environment.DOCKER_PACKAGES)):
                with patch.object(environment, "docker_versions", side_effect=installers.InstallerError("compose missing")):
                    with self.assertRaisesRegex(installers.InstallerError, "compose missing"):
                        environment.prepare_docker("amd64")

    def test_docker_conflicts_stop_before_download_or_sudo(self):
        with patch.object(environment.shutil, "which", return_value="/test/program"):
            with patch.object(environment, "installed_packages", return_value={"docker.io"}):
                with patch.object(environment, "fetch_bytes") as fetch:
                    with self.assertRaisesRegex(installers.InstallerError, "Conflicting Docker packages"):
                        environment.prepare_docker("arm64")
        fetch.assert_not_called()

    def test_partial_docker_install_prepares_only_missing_compose_from_signed_repo(self):
        installed = set(environment.DOCKER_PACKAGES) - {"docker-compose-plugin"}
        with patch.object(environment.shutil, "which", return_value="/test/program"):
            with patch.object(environment, "installed_packages", return_value=installed):
                with patch.object(environment.Path, "is_dir", return_value=True):
                    with patch.object(environment.platform, "freedesktop_os_release", return_value={"VERSION_CODENAME": "resolute"}):
                        with patch.object(environment, "DOCKER_KEY", self.root / "docker.asc"), patch.object(environment, "DOCKER_REPOSITORY", self.root / "docker.sources"):
                            with patch.object(environment, "fetch_bytes", return_value=b"-----BEGIN PGP PUBLIC KEY BLOCK-----\nkey"):
                                plan = environment.prepare_docker("arm64")
        self.assertEqual(plan.packages, ("docker-compose-plugin",))
        self.assertIn("Suites: resolute", plan.repository)
        self.assertIn("Architectures: arm64", plan.repository)
        self.assertIn("Signed-By:", plan.repository)
        self.assertIn("https://download.docker.com/linux/ubuntu", plan.repository)

    def test_docker_repository_files_are_preserved(self):
        repository = self.root / "docker.sources"
        repository.write_text("keep me")
        with self.assertRaisesRegex(installers.InstallerError, "will not be overwritten"):
            environment.check_repository_file(repository, b"different repository")
        self.assertEqual(repository.read_text(), "keep me")

    def test_docker_existing_complete_installation_skips_privileged_changes(self):
        plan = installers.ToolPlan("Docker with Compose", "docker", "", "docker", "", existing="/usr/bin/docker")
        with patch.object(environment, "docker_versions") as verify:
            with patch.object(environment, "run_checked") as run:
                with contextlib.redirect_stdout(io.StringIO()):
                    environment.install_docker(plan)
        verify.assert_called_once()
        run.assert_not_called()

    def test_docker_apt_failure_stops_before_starting_service(self):
        plan = installers.ToolPlan("Docker with Compose", "docker", environment.DOCKER_SOURCE, "docker", "", script=b"key", packages=environment.DOCKER_PACKAGES, repository="repo")
        commands = []
        def run(command, **kwargs):
            commands.append(command)
            if command[:3] == ["sudo", "apt-get", "install"]:
                raise installers.InstallerError("apt failure")
        with patch.object(environment, "installed_packages", return_value=set()):
            with patch.object(environment, "check_repository_file"):
                with patch.object(environment, "run_checked", side_effect=run):
                    with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(installers.InstallerError, "apt failure"):
                        environment.install_docker(plan)
        self.assertFalse(any("systemctl" in command for command in commands))
        self.assertFalse(any("usermod" in command for command in commands))

    def test_docker_success_starts_service_and_verifies_daemon_without_group_change(self):
        plan = installers.ToolPlan("Docker with Compose", "docker", environment.DOCKER_SOURCE, "docker", "", script=b"key", packages=environment.DOCKER_PACKAGES, repository="repo")
        with patch.object(environment, "installed_packages", return_value=set()):
            with patch.object(environment, "check_repository_file"), patch.object(environment, "docker_versions") as verify:
                with patch.object(environment, "run_checked") as run, contextlib.redirect_stdout(io.StringIO()):
                    environment.install_docker(plan)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(["sudo", "systemctl", "enable", "--now", "docker"], commands)
        self.assertIn(["sudo", "/usr/bin/docker", "info"], commands)
        self.assertFalse(any("usermod" in command for command in commands))
        verify.assert_called_once()

    def test_docker_plan_explicitly_reports_sudo_and_service_scope(self):
        tool = installers.ToolPlan("Docker with Compose", "docker", environment.DOCKER_SOURCE, "docker", "")
        answers = setup_vm.SetupAnswers("Test", "test@example.com", False, (tool.name,))
        plan = setup_vm.SetupPlan(answers, (), (), None, (tool,))
        text = setup_vm.format_plan(plan)
        self.assertIn("Docker uses sudo", text)
        self.assertIn("enable/start Docker service", text)
        self.assertIn("Docker group unchanged", text)


if __name__ == "__main__":
    unittest.main()
