import contextlib
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
        for name in ("rootless_prerequisites", "require_rootful_stopped"):
            mock = patch.object(environment, name)
            mock.start()
            self.addCleanup(mock.stop)

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
        repository.write_text("keep me", encoding="utf-8")
        with self.assertRaisesRegex(installers.InstallerError, "will not be overwritten"):
            environment.check_repository_file(repository, b"different repository")
        self.assertEqual(repository.read_text(encoding="utf-8"), "keep me")

    def test_docker_existing_complete_installation_skips_privileged_changes(self):
        plan = installers.ToolPlan("Docker with Compose", "docker", "", "docker", "", existing="/usr/bin/docker")
        with patch.object(environment, "docker_versions") as verify, patch.object(environment, "rootless_ready", return_value=True), patch.object(environment, "rootless_boot_ready", return_value=True):
            with patch.object(environment, "run_checked") as run:
                with contextlib.redirect_stdout(io.StringIO()):
                    environment.install_docker(plan)
        verify.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["/usr/bin/docker", "context", "use", "rootless"])

    def test_docker_apt_failure_stops_before_starting_service(self):
        plan = installers.ToolPlan("Docker with Compose", "docker", environment.DOCKER_SOURCE, "docker", "", script=b"key", packages=environment.DOCKER_PACKAGES, repository="repo")
        commands = []
        def run(command, **_kwargs):
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
            with patch.object(environment, "check_repository_file"), patch.object(environment, "docker_versions") as verify, patch.object(environment, "rootless_ready", return_value=True), patch.object(environment, "rootless_boot_ready", return_value=True):
                with patch.object(environment, "run_checked") as run, contextlib.redirect_stdout(io.StringIO()):
                    environment.install_docker(plan)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(["sudo", "systemctl", "disable", "--now", "docker.service", "docker.socket"], commands)
        self.assertIn(["systemctl", "--user", "enable", "--now", "docker.service"], commands)
        self.assertIn(["/usr/bin/dockerd-rootless-setuptool.sh", "install"], commands)
        self.assertTrue(any(command[:3] == ["sudo", "loginctl", "enable-linger"] for command in commands))
        self.assertFalse(any(command[:2] == ["sudo", "/usr/bin/docker"] for command in commands))
        self.assertFalse(any("usermod" in command for command in commands))
        verify.assert_called_once()

    def test_docker_plan_explicitly_reports_sudo_and_service_scope(self):
        tool = installers.ToolPlan("Docker with Compose", "docker", environment.DOCKER_SOURCE, "docker", "")
        answers = setup_vm.SetupAnswers("Test", "test@example.com", False, (tool.name,))
        plan = setup_vm.SetupPlan(answers, (), (), None, (tool,))
        text = setup_vm.format_plan(plan)
        self.assertIn("Docker uses sudo", text)
        self.assertIn("enable/start user Docker service", text)
        self.assertIn("Docker group unchanged", text)


class RootlessDockerTests(unittest.TestCase):
    def test_active_system_daemon_requires_manual_migration(self):
        with patch.object(environment, "rootful_active", return_value=True):
            with self.assertRaisesRegex(installers.InstallerError, "disable --now"):
                environment.require_rootful_stopped()

    def test_rootless_info_must_explicitly_report_rootless(self):
        for output, expected in (( '["name=rootless", "name=seccomp"]', True), ('["name=seccomp"]', False), ('bad json', False), ('{}', False)):
            with self.subTest(output=output), patch.object(environment.subprocess, "run", side_effect=[subprocess.CompletedProcess([], 0, json.dumps(f"unix:///run/user/{environment.os.getuid()}/docker.sock")), subprocess.CompletedProcess([], 0, output)]) as run:
                self.assertEqual(environment.rootless_ready(), expected)
                self.assertIn("rootless", run.call_args.args[0])

    def test_rootless_context_cannot_point_to_another_daemon(self):
        with patch.object(environment.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, '"unix:///var/run/docker.sock"')) as run:
            with self.assertRaisesRegex(installers.InstallerError, "will not be overwritten"):
                environment.rootless_ready()
        self.assertEqual(run.call_count, 1)

    def test_boot_verification_requires_user_service_linger_and_disabled_system_units(self):
        for user_enabled, linger, system_enabled, expected in (("enabled", "yes", "disabled", True), ("disabled", "yes", "disabled", False), ("enabled", "no", "disabled", False), ("enabled", "yes", "enabled", False)):
            with self.subTest(user=user_enabled, linger=linger, system=system_enabled):
                results = [subprocess.CompletedProcess([], 0 if user_enabled == "enabled" else 1, user_enabled), subprocess.CompletedProcess([], 0, linger), subprocess.CompletedProcess([], 1, system_enabled), subprocess.CompletedProcess([], 1, "disabled")]
                with patch.object(environment, "docker_user", return_value="vmtest"), patch.object(environment.subprocess, "run", side_effect=results):
                    self.assertEqual(environment.rootless_boot_ready(), expected)

    def test_inspection_timeout_has_actionable_error(self):
        with patch.object(environment.subprocess, "run", side_effect=subprocess.TimeoutExpired("docker", 30)):
            with self.assertRaisesRegex(installers.InstallerError, "Could not inspect Docker state"):
                environment.rootless_ready()

    def test_existing_packages_do_not_mean_rootless_is_configured(self):
        with patch.object(environment.shutil, "which", return_value="/test/program"), patch.object(environment, "installed_packages", return_value=set(environment.DOCKER_PACKAGES)), patch.object(environment, "rootless_prerequisites"), patch.object(environment, "require_rootful_stopped"), patch.object(environment, "docker_versions"), patch.object(environment, "rootless_ready", return_value=False), patch.object(environment.platform, "freedesktop_os_release", return_value={"VERSION_CODENAME": "resolute"}), patch.object(environment, "fetch_bytes") as fetch:
            plan = environment.prepare_docker("arm64")
        self.assertIsNone(plan.existing)
        self.assertEqual(plan.packages, ())
        fetch.assert_not_called()

    def test_root_user_is_rejected(self):
        with patch.object(environment.os, "getuid", return_value=0):
            with self.assertRaisesRegex(installers.InstallerError, "normal user"):
                environment.rootless_prerequisites()

    def test_docker_group_access_is_rejected(self):
        import types
        with patch.object(environment.os, "getuid", return_value=1001), patch.object(environment.os, "getgid", return_value=1001), patch.object(environment.os, "getgroups", return_value=[1001, 999]), patch.object(environment.grp, "getgrnam", return_value=types.SimpleNamespace(gr_gid=999, gr_mem=[])):
            with self.assertRaisesRegex(installers.InstallerError, "Docker group access"):
                environment.rootless_prerequisites()

    def test_overriding_docker_endpoint_is_rejected(self):
        with patch.object(environment.os, "getuid", return_value=1001), patch.object(environment.grp, "getgrnam", side_effect=KeyError), patch.dict(environment.os.environ, {"DOCKER_HOST": "unix:///other.sock"}):
            with self.assertRaisesRegex(installers.InstallerError, "Unset DOCKER_HOST"):
                environment.rootless_prerequisites()

    def test_subordinate_ranges_and_user_manager_are_required(self):
        with patch.object(environment.os, "getuid", return_value=1001), patch.object(environment.grp, "getgrnam", side_effect=KeyError), patch.object(environment, "docker_user", return_value="vmtest"), patch.dict(environment.os.environ, {}, clear=True), patch.object(environment.Path, "read_text", return_value="vmtest:100000:65536\n"), patch.object(environment.Path, "is_dir", return_value=True), patch.object(environment, "run_checked") as run:
            environment.rootless_prerequisites()
            self.assertEqual(run.call_args.args[0], ["systemctl", "--user", "show-environment"])
            with patch.object(environment.Path, "read_text", return_value="other:100000:65536\nvmtest:200000:100\n"):
                with self.assertRaisesRegex(installers.InstallerError, "subordinate ID range"):
                    environment.rootless_prerequisites()

    def test_failed_setup_stops_before_claiming_rootless_success(self):
        plan = installers.ToolPlan("Docker with Compose", "docker", "", "docker", "")
        commands = []
        def run(command, **_kwargs):
            commands.append(command)
            if command == ["/usr/bin/dockerd-rootless-setuptool.sh", "check"]:
                raise installers.InstallerError("prerequisite check failed")
        with patch.object(environment, "rootless_prerequisites"), patch.object(environment, "require_rootful_stopped"), patch.object(environment, "installed_packages", return_value=set(environment.DOCKER_PACKAGES)), patch.object(environment, "run_checked", side_effect=run), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(installers.InstallerError, "prerequisite check failed"):
                environment.install_docker(plan)
        self.assertFalse(any(command[-1] == "install" for command in commands))

    def test_failed_final_verification_is_reported(self):
        plan = installers.ToolPlan("Docker with Compose", "docker", "", "docker", "")
        with patch.object(environment, "rootless_prerequisites"), patch.object(environment, "require_rootful_stopped"), patch.object(environment, "installed_packages", return_value=set(environment.DOCKER_PACKAGES)), patch.object(environment, "run_checked"), patch.object(environment, "docker_versions"), patch.object(environment, "rootless_ready", return_value=False), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(installers.InstallerError, "could not be verified"):
                environment.install_docker(plan)


if __name__ == "__main__":
    unittest.main()


class AptBundleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        home = patch.object(Path, "home", return_value=self.root)
        home.start()
        self.addCleanup(home.stop)

    def test_package_inspection_accepts_partial_and_arch_qualified_results(self):
        result = subprocess.CompletedProcess([], 1, "git\tinstalled\npython3:arm64\tinstalled\nhtop\tnot-installed\n", "missing")
        with patch.object(environment.subprocess, "run", return_value=result):
            self.assertEqual(environment.inspect_bundle_packages(("git", "python3", "htop")), {"git", "python3"})

    def test_package_inspection_failure_is_actionable(self):
        with patch.object(environment.subprocess, "run", side_effect=OSError("unavailable")):
            with self.assertRaisesRegex(installers.InstallerError, "Could not inspect"):
                environment.inspect_bundle_packages(("git",))
        with patch.object(environment.subprocess, "run", return_value=subprocess.CompletedProcess([], 2, "", "database broken")):
            with self.assertRaisesRegex(installers.InstallerError, "database broken"):
                environment.inspect_bundle_packages(("git",))

    def test_partial_java_plan_installs_only_missing_packages_and_reports_sudo(self):
        with patch.object(environment.shutil, "which", return_value="/usr/bin/tool"), patch.object(environment, "inspect_bundle_packages", return_value={"default-jdk"}):
            plan = environment.prepare_apt_bundle("Java Tools")
        self.assertEqual(plan.packages, ("maven", "gradle"))
        self.assertIn("sudo required", installers.describe_tool(plan))
        self.assertIsNone(plan.existing)

    def test_complete_bundle_and_alias_need_no_sudo_or_writes(self):
        alias_file = self.root / ".bash_aliases"
        alias_file.write_text(environment.VIM_ALIAS + "\n", encoding="utf-8")
        before = alias_file.read_bytes()
        packages = set(environment.APT_BUNDLES["Base dev tools"])
        with patch.object(environment.shutil, "which", side_effect=lambda name: None if name == "sudo" else name), patch.object(environment, "inspect_bundle_packages", return_value=packages), patch.object(environment, "run_checked") as run:
            plan = environment.prepare_apt_bundle("Base dev tools")
            environment.install_apt_bundle(plan)
        self.assertIsNotNone(plan.existing)
        run.assert_not_called()
        self.assertEqual(alias_file.read_bytes(), before)

    def test_base_alias_preserves_content_and_is_not_duplicated(self):
        alias_file = self.root / ".bash_aliases"
        alias_file.write_text("alias ll='ls -l'", encoding="utf-8")
        packages = set(environment.APT_BUNDLES["Base dev tools"])
        with patch.object(environment.shutil, "which", return_value="tool"), patch.object(environment, "inspect_bundle_packages", return_value=packages), patch.object(environment, "run_checked") as run:
            plan = environment.prepare_apt_bundle("Base dev tools")
            self.assertIsNone(plan.existing)
            self.assertEqual(plan.packages, ())
            environment.install_apt_bundle(plan)
            environment.install_apt_bundle(plan)
        contents = alias_file.read_text(encoding="utf-8")
        self.assertTrue(contents.startswith("alias ll='ls -l'\n"))
        self.assertEqual(contents.count(environment.VIM_ALIAS), 1)
        run.assert_not_called()

    def test_conflicting_alias_and_symlink_are_preserved(self):
        alias_file = self.root / ".bash_aliases"
        alias_file.write_text("alias vim='other-editor'\n", encoding="utf-8")
        with self.assertRaisesRegex(installers.InstallerError, "different vim alias"):
            environment.vim_alias_present()
        alias_file.unlink()
        target = self.root / "other"
        target.write_text("keep", encoding="utf-8")
        alias_file.symlink_to(target)
        with self.assertRaisesRegex(installers.InstallerError, "regular file"):
            environment.vim_alias_present()
        self.assertEqual(target.read_text(encoding="utf-8"), "keep")

    def test_install_sequence_and_post_install_verification(self):
        plan = installers.ToolPlan("Java Tools", "", "Ubuntu", "apt", "", packages=("maven", "gradle"))
        with patch.object(environment, "inspect_bundle_packages", side_effect=[{"default-jdk"}, set(environment.APT_BUNDLES[plan.name])]), patch.object(environment, "run_checked") as run:
            environment.install_apt_bundle(plan)
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["sudo", "-v"], ["sudo", "apt-get", "update"],
            ["sudo", "apt-get", "install", "-y", "maven", "gradle"],
        ])

    def test_failed_install_does_not_add_alias(self):
        plan = installers.ToolPlan("Base dev tools", "", "Ubuntu", "apt", "", packages=environment.APT_BUNDLES["Base dev tools"])
        with patch.object(environment, "inspect_bundle_packages", return_value=set()), patch.object(environment, "run_checked", side_effect=[None, None, installers.InstallerError("apt failed")]):
            with self.assertRaisesRegex(installers.InstallerError, "apt failed"):
                environment.install_apt_bundle(plan)
        self.assertFalse((self.root / ".bash_aliases").exists())

    def test_unfulfilled_install_and_state_change_are_reported(self):
        plan = installers.ToolPlan("Java Tools", "", "Ubuntu", "apt", "", packages=("maven", "gradle"))
        with patch.object(environment, "inspect_bundle_packages", return_value={"default-jdk"}), patch.object(environment, "run_checked"):
            with self.assertRaisesRegex(installers.InstallerError, "verification failed"):
                environment.install_apt_bundle(plan)
        with patch.object(environment, "inspect_bundle_packages", return_value=set()), patch.object(environment, "run_checked") as run:
            with self.assertRaisesRegex(installers.InstallerError, "changed since planning"):
                environment.install_apt_bundle(plan)
        run.assert_not_called()

    def test_menu_and_dispatch_include_both_bundles(self):
        names = ("Base dev tools", "Java Tools")
        self.assertTrue(set(names) <= set(setup_vm.TOOLS))
        with patch.object(installers, "ubuntu_arch", return_value="amd64"), patch.object(environment, "prepare_apt_bundle", side_effect=lambda name: installers.ToolPlan(name, "", "Ubuntu", "apt", "")) as prepare:
            plans = installers.prepare_tools(names)
        self.assertEqual([call.args[0] for call in prepare.call_args_list], list(names))
        with patch.object(environment, "install_apt_bundle") as install, contextlib.redirect_stdout(io.StringIO()):
            installers.install_tools(plans)
        self.assertEqual([call.args[0].name for call in install.call_args_list], list(names))


class PlaywrightChromiumTests(unittest.TestCase):
    def test_plan_keeps_existing_node_commands_but_includes_chromium_and_sudo(self):
        tool = installers.ToolPlan("Node Tools", "node", "nodejs.org", "node", "", existing="/test/node")
        answers = setup_vm.SetupAnswers("Alice", "a@b", False, ("Node Tools",), True)
        with patch.object(setup_vm, "require_program"), patch.object(setup_vm, "read_git_values", return_value=()), patch.object(setup_vm.shutil, "which", return_value="/usr/bin/sudo"), patch.object(setup_vm, "prepare_tools", return_value=(tool,)):
            plan = setup_vm.build_plan(answers)
        self.assertTrue(plan.tools[0].playwright_chromium)
        self.assertIsNone(plan.tools[0].existing)
        text = setup_vm.format_plan(plan)
        self.assertIn("Playwright Chromium and system dependencies: Yes", text)
        self.assertIn("keep all six installed commands", text)
        self.assertIn("--with-deps chromium", text)
        self.assertIn("sudo required", text)

    def test_missing_sudo_and_node_selection_stop_planning(self):
        for names, message in ((("Node Tools",), "require sudo"), ((), "requires selecting Node Tools")):
            answers = setup_vm.SetupAnswers("Alice", "a@b", False, names, True)
            with self.subTest(names=names), patch.object(setup_vm, "require_program"), patch.object(setup_vm.shutil, "which", return_value=None), patch.object(setup_vm, "prepare_tools") as prepare:
                with self.assertRaisesRegex(setup_vm.SetupError, message):
                    setup_vm.build_plan(answers)
            prepare.assert_not_called()

    def test_opt_out_runs_no_browser_commands(self):
        plan = installers.ToolPlan("Node Tools", "node", "", "node", "")
        with patch.object(environment, "install_node_commands") as install, patch.object(environment, "run_checked") as run:
            environment.install_node(plan)
        install.assert_called_once_with(plan)
        run.assert_not_called()

    def test_opt_in_runs_after_existing_cli_check_with_downloads_enabled(self):
        plan = installers.ToolPlan("Node Tools", "node", "", "node", "", playwright_chromium=True)
        with patch.object(environment, "missing_node_commands", return_value=()), patch.object(environment, "find_executable", return_value="/test/playwright"), patch.object(environment, "run_checked") as run, contextlib.redirect_stdout(io.StringIO()):
            environment.install_node(plan)
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["/test/playwright", "install", "--with-deps", "chromium"],
            ["/test/playwright", "install-deps", "--dry-run", "chromium"],
        ])
        self.assertNotIn("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD", run.call_args_list[0].kwargs["env"])
        self.assertTrue(run.call_args_list[0].kwargs["env"]["PATH"].startswith(str(installers.local_bin())))

    def test_cli_failure_prevents_browser_install(self):
        plan = installers.ToolPlan("Node Tools", "node", "", "node", "", playwright_chromium=True)
        with patch.object(environment, "install_node_commands", side_effect=installers.InstallerError("CLI failed")), patch.object(environment, "run_checked") as run:
            with self.assertRaisesRegex(installers.InstallerError, "CLI failed"):
                environment.install_node(plan)
        run.assert_not_called()

    def test_browser_install_or_dependency_verification_failures_are_not_success(self):
        plan = installers.ToolPlan("Node Tools", "node", "", "node", "", playwright_chromium=True)
        for results in ([installers.InstallerError("install failed")], [None, installers.InstallerError("verification failed")]):
            output = io.StringIO()
            with self.subTest(results=results), patch.object(environment, "install_node_commands"), patch.object(environment, "find_executable", return_value="/test/playwright"), patch.object(environment, "run_checked", side_effect=results), contextlib.redirect_stdout(output):
                with self.assertRaises(installers.InstallerError):
                    environment.install_node(plan)
            self.assertNotIn("Installed: Playwright Chromium", output.getvalue())

    def test_missing_cli_is_actionable(self):
        plan = installers.ToolPlan("Node Tools", "node", "", "node", "", playwright_chromium=True)
        with patch.object(environment, "install_node_commands"), patch.object(environment, "find_executable", return_value=None), patch.object(environment, "run_checked") as run:
            with self.assertRaisesRegex(installers.InstallerError, "Playwright disappeared"):
                environment.install_node(plan)
        run.assert_not_called()
