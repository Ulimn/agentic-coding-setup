import contextlib
import io
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import environment_installers as environment
import setup_vm
import tool_installers as installers


class TailscaleInstallerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.key = self.root / 'keyrings' / 'tailscale.gpg'
        self.repository = self.root / 'sources' / 'tailscale.list'
        self.key_data = b'\x99' + b'k' * 200
        self.repository_text = (f'# official packages\ndeb [signed-by={self.key}] '
                                f'{environment.TAILSCALE_SOURCE} resolute main\n')
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(environment, 'TAILSCALE_KEY', self.key))
        self.stack.enter_context(patch.object(environment, 'TAILSCALE_REPOSITORY', self.repository))
        self.stack.enter_context(patch.object(environment, 'tailscale_prerequisites', return_value='resolute'))
        self.present = self.stack.enter_context(patch.object(environment, 'tailscale_package_present', return_value=False))
        self.ready = self.stack.enter_context(patch.object(environment, 'tailscale_service_ready', return_value=False))
        self.stack.enter_context(patch.object(environment.shutil, 'which', return_value='/usr/bin/tool'))
        self.fetch = self.stack.enter_context(patch.object(environment, 'fetch_bytes', side_effect=lambda url: self.key_data if url.endswith('.gpg') else self.repository_text.encode()))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def test_fresh_plan_has_official_repository_sudo_service_and_manual_login(self):
        plan = environment.prepare_tailscale()
        self.assertEqual(plan.packages, ('tailscale',))
        self.assertEqual(plan.script, self.key_data)
        self.assertEqual([call.args[0] for call in self.fetch.call_args_list], [
            environment.TAILSCALE_SOURCE + '/resolute.noarmor.gpg',
            environment.TAILSCALE_SOURCE + '/resolute.tailscale-keyring.list',
        ])
        answers = setup_vm.SetupAnswers('Test', 'a@b', False, ('Tailscale',))
        text = setup_vm.format_plan(setup_vm.SetupPlan(answers, (), (), None, (plan,)))
        self.assertIn('sudo', text)
        self.assertIn('enable/start tailscaled.service', text)
        self.assertIn('login remains manual', text)

    def test_complete_install_needs_no_sudo_or_mutations(self):
        self.present.return_value = True
        self.ready.return_value = True
        with patch.object(environment.shutil, 'which', side_effect=lambda name: None if name == 'sudo' else name):
            plan = environment.prepare_tailscale()
        self.assertIsNotNone(plan.existing)
        with patch.object(environment, 'run_checked') as run:
            environment.install_tailscale(plan)
        run.assert_not_called()
        self.assertFalse(self.repository.exists())

    def test_stopped_service_plan_keeps_package_and_does_not_run_apt(self):
        self.present.return_value = True
        plan = environment.prepare_tailscale()
        self.assertEqual(plan.packages, ())
        self.assertIsNone(plan.existing)
        self.ready.side_effect = [False, True]
        with patch.object(environment, 'run_checked') as run:
            environment.install_tailscale(plan)
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ['sudo', '-v'], ['sudo', 'systemctl', 'enable', '--now', 'tailscaled.service'],
        ])
        self.assertFalse(self.repository.exists())

    def test_missing_sudo_stops_preparation(self):
        with patch.object(environment.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(installers.InstallerError, 'requires sudo'):
                environment.prepare_tailscale()

    def test_conflicting_repository_and_symlink_are_preserved(self):
        self.repository.parent.mkdir()
        self.repository.write_text('custom', encoding='utf-8')
        with self.assertRaisesRegex(installers.InstallerError, 'not be overwritten'):
            environment.prepare_tailscale()
        self.assertEqual(self.repository.read_text(encoding='utf-8'), 'custom')
        self.repository.unlink()
        self.repository.symlink_to(self.root / 'missing')
        with self.assertRaisesRegex(installers.InstallerError, 'not be overwritten'):
            environment.prepare_tailscale()
        self.assertTrue(self.repository.is_symlink())

    def test_matching_repository_and_key_are_preserved(self):
        self.key.parent.mkdir()
        self.repository.parent.mkdir()
        self.key.write_bytes(self.key_data)
        self.repository.write_text(self.repository_text, encoding='utf-8')
        original = (self.key.stat().st_mtime_ns, self.repository.stat().st_mtime_ns)
        self.present.return_value = self.ready.return_value = True
        plan = environment.prepare_tailscale()
        environment.install_tailscale(plan)
        self.assertEqual((self.key.stat().st_mtime_ns, self.repository.stat().st_mtime_ns), original)

    def test_repository_changed_after_planning_is_refused_before_sudo(self):
        plan = environment.prepare_tailscale()
        self.repository.parent.mkdir()
        self.repository.write_text('new custom source', encoding='utf-8')
        with patch.object(environment, 'run_checked') as run:
            with self.assertRaisesRegex(installers.InstallerError, 'not be overwritten'):
                environment.install_tailscale(plan)
        run.assert_not_called()
        self.assertEqual(self.repository.read_text(encoding='utf-8'), 'new custom source')

    def test_bad_key_and_wrong_repository_are_refused(self):
        self.key_data = b'<html>bad response</html>'
        with self.assertRaisesRegex(installers.InstallerError, 'OpenPGP'):
            environment.prepare_tailscale()
        self.key_data = b'\x99' + b'k' * 200
        self.repository_text = 'deb https://example.com jammy main\n'
        with self.assertRaisesRegex(installers.InstallerError, 'differs'):
            environment.prepare_tailscale()

    def simulate_command(self, command, **_kwargs):
        if command[:2] == ['sudo', 'install']:
            Path(command[-1]).mkdir(parents=True, exist_ok=True)
        if command[:2] == ['sudo', 'cp']:
            destination = Path(command[-1])
            if not destination.exists():
                with destination.open('xb') as output:
                    output.write(Path(command[-2]).read_bytes())
        return subprocess.CompletedProcess(command, 0, '', '')

    def test_new_install_sequence_and_no_login_or_preferences_commands(self):
        plan = environment.prepare_tailscale()
        self.present.side_effect = [False, True]
        self.ready.side_effect = [False, True]
        with patch.object(environment, 'run_checked', side_effect=self.simulate_command) as run:
            environment.install_tailscale(plan)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[-3:], [
            ['sudo', 'apt-get', 'update'], ['sudo', 'apt-get', 'install', '-y', 'tailscale'],
            ['sudo', 'systemctl', 'enable', '--now', 'tailscaled.service'],
        ])
        self.assertEqual(self.key.read_bytes(), plan.script)
        self.assertEqual(self.repository.read_text(encoding='utf-8'), plan.repository)
        self.assertFalse(any('up' in command or 'set' in command or 'logout' in command for command in commands))

    def test_apt_failure_does_not_enable_service(self):
        plan = environment.prepare_tailscale()
        def fail_apt(command, **kwargs):
            if command[:3] == ['sudo', 'apt-get', 'install']:
                raise installers.InstallerError('apt failed')
            return self.simulate_command(command, **kwargs)
        with patch.object(environment, 'run_checked', side_effect=fail_apt) as run:
            with self.assertRaisesRegex(installers.InstallerError, 'apt failed'):
                environment.install_tailscale(plan)
        self.assertFalse(any('systemctl' in call.args[0] for call in run.call_args_list))

    def test_service_failure_and_failed_verification_stop_setup(self):
        self.present.return_value = True
        plan = environment.prepare_tailscale()
        with patch.object(environment, 'run_checked', side_effect=[None, installers.InstallerError('service failed')]):
            with self.assertRaisesRegex(installers.InstallerError, 'service failed'):
                environment.install_tailscale(plan)
        with patch.object(environment, 'run_checked'):
            with self.assertRaisesRegex(installers.InstallerError, 'verification failed'):
                environment.install_tailscale(plan)

    def test_changed_state_requires_replanning_before_sudo(self):
        self.present.return_value = self.ready.return_value = True
        plan = environment.prepare_tailscale()
        self.ready.return_value = False
        with patch.object(environment, 'run_checked') as run:
            with self.assertRaisesRegex(installers.InstallerError, 'state changed'):
                environment.install_tailscale(plan)
        run.assert_not_called()
        with patch.object(environment, 'tailscale_prerequisites', return_value='noble'), patch.object(environment, 'run_checked') as run:
            with self.assertRaisesRegex(installers.InstallerError, 'release changed'):
                environment.install_tailscale(plan)
        run.assert_not_called()

    def test_menu_dispatch_and_manual_login_output(self):
        self.assertIn('Tailscale', setup_vm.TOOLS)
        with patch.object(installers, 'ubuntu_arch', return_value='arm64'):
            plans = installers.prepare_tools(('Tailscale',))
        output = io.StringIO()
        with patch.object(environment, 'install_tailscale') as install, contextlib.redirect_stdout(output):
            installers.install_tools(plans)
        install.assert_called_once_with(plans[0])
        self.assertIn('sudo tailscale up', output.getvalue())
        self.assertIn('browser URL', output.getvalue())
        self.assertIn('tailscale status and tailscale ip', output.getvalue())


class TailscaleStateTests(unittest.TestCase):
    def test_prerequisites_accept_supported_versions_and_reject_unsupported(self):
        for version, codename in (('22.04', 'jammy'), ('24.04', 'noble'), ('26.04', 'resolute')):
            release = {'ID': 'ubuntu', 'VERSION_ID': version, 'VERSION_CODENAME': codename}
            with self.subTest(version=version), patch.object(environment.os, 'getuid', return_value=1000), patch.object(environment.platform, 'freedesktop_os_release', return_value=release), patch.object(environment.shutil, 'which', return_value='tool'), patch.object(Path, 'is_dir', return_value=True), patch.object(Path, 'stat') as status:
                status.return_value.st_mode = stat.S_IFCHR
                self.assertEqual(environment.tailscale_prerequisites(), codename)
        for release in ({'ID': 'debian'}, {'ID': 'ubuntu', 'VERSION_ID': '26.04', 'VERSION_CODENAME': 'noble'}):
            with patch.object(environment.os, 'getuid', return_value=1000), patch.object(environment.platform, 'freedesktop_os_release', return_value=release):
                with self.assertRaisesRegex(installers.InstallerError, 'supports Ubuntu'):
                    environment.tailscale_prerequisites()

    def test_root_systemd_and_tun_prerequisites(self):
        with patch.object(environment.os, 'getuid', return_value=0):
            with self.assertRaisesRegex(installers.InstallerError, 'normal VM user'):
                environment.tailscale_prerequisites()
        release = {'ID': 'ubuntu', 'VERSION_ID': '26.04', 'VERSION_CODENAME': 'resolute'}
        with patch.object(environment.os, 'getuid', return_value=1000), patch.object(environment.platform, 'freedesktop_os_release', return_value=release), patch.object(environment.shutil, 'which', return_value='tool'):
            with patch.object(Path, 'is_dir', return_value=False):
                with self.assertRaisesRegex(installers.InstallerError, 'systemd'):
                    environment.tailscale_prerequisites()
            with patch.object(Path, 'is_dir', return_value=True), patch.object(Path, 'stat', side_effect=FileNotFoundError):
                with self.assertRaisesRegex(installers.InstallerError, 'modprobe tun'):
                    environment.tailscale_prerequisites()

    def test_custom_install_is_not_overwritten(self):
        with patch.object(environment, 'inspect_bundle_packages', return_value=set()), patch.object(environment, 'find_executable', return_value='/usr/local/bin/tailscale'):
            with self.assertRaisesRegex(installers.InstallerError, 'custom Tailscale'):
                environment.tailscale_package_present()
        with patch.object(environment, 'inspect_bundle_packages', return_value={'tailscale'}), patch.object(environment, 'find_executable', side_effect=lambda command: '/custom/' + command):
            with self.assertRaisesRegex(installers.InstallerError, 'custom Tailscale'):
                environment.tailscale_package_present()

    def test_package_version_is_checked_even_on_reruns(self):
        with patch.object(environment, 'inspect_bundle_packages', return_value={'tailscale'}), patch.object(environment, 'find_executable', side_effect=environment.TAILSCALE_EXECUTABLES.get), patch.object(environment, 'tailscale_version') as version:
            self.assertTrue(environment.tailscale_package_present())
        version.assert_called_once()

    def test_version_uses_supported_subcommand_and_rejects_bad_output(self):
        with patch.object(Path, 'is_file', return_value=True), patch.object(Path, 'is_symlink', return_value=False), patch.object(os, 'access', return_value=True):
            with patch.object(environment, 'run_checked', return_value=subprocess.CompletedProcess([], 0, '1.104.1\nextra metadata\n', '')) as run:
                self.assertEqual(environment.tailscale_version(), '1.104.1')
            self.assertEqual(run.call_args.args[0], ['/usr/bin/tailscale', 'version'])
            with patch.object(environment, 'run_checked', return_value=subprocess.CompletedProcess([], 0, '', '')):
                with self.assertRaisesRegex(installers.InstallerError, 'valid version'):
                    environment.tailscale_version()
        with patch.object(Path, 'is_file', return_value=False):
            with self.assertRaisesRegex(installers.InstallerError, 'Repair'):
                environment.tailscale_version()

    def test_service_active_enabled_stopped_missing_masked_and_errors(self):
        with patch.object(environment, 'run_checked', return_value=subprocess.CompletedProcess([], 0, 'loaded\n', '')):
            for active, enabled, expected in (('active', 'enabled', True), ('inactive', 'disabled', False)):
                results = [subprocess.CompletedProcess([], 0 if expected else 3, active, ''), subprocess.CompletedProcess([], 0 if expected else 1, enabled, '')]
                with self.subTest(active=active), patch.object(environment.subprocess, 'run', side_effect=results):
                    self.assertEqual(environment.tailscale_service_ready(), expected)
            with patch.object(environment.subprocess, 'run', side_effect=OSError('systemctl failed')):
                with self.assertRaisesRegex(installers.InstallerError, 'inspect'):
                    environment.tailscale_service_ready()
        with patch.object(environment, 'run_checked', return_value=subprocess.CompletedProcess([], 0, 'not-found', '')):
            self.assertFalse(environment.tailscale_service_ready())
        with patch.object(environment, 'run_checked', return_value=subprocess.CompletedProcess([], 0, 'masked', '')):
            with self.assertRaisesRegex(installers.InstallerError, 'masked'):
                environment.tailscale_service_ready()
