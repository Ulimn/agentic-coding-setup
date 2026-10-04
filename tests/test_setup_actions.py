import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import setup_vm


ANSWERS = setup_vm.SetupAnswers("VM User", "vm@example.com", True, ("Codex",))


class SetupActionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.key = self.home / ".ssh" / "id_ed25519"
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def test_missing_program_has_ubuntu_recovery_command(self):
        with patch.object(setup_vm.shutil, "which", return_value=None):
            with self.assertRaisesRegex(setup_vm.SetupError, "sudo apt install openssh-client"):
                setup_vm.require_program("ssh-keygen")

    def test_git_read_distinguishes_missing_value_from_failure(self):
        with patch.object(setup_vm.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 1, "", "")
            self.assertEqual(setup_vm.read_git_values("user.name"), ())
            run.return_value = subprocess.CompletedProcess([], 128, "", "invalid config")
            with self.assertRaisesRegex(setup_vm.SetupError, "invalid config"):
                setup_vm.read_git_values("user.name")

    def test_git_configuration_in_isolated_file_preserves_other_settings_and_reruns(self):
        # Real Git operates only on this temporary file, never the user's config.
        config = self.home / "gitconfig"
        config.write_text('[user]\n\tname = Old Name\n\temail = old@example.com\n[core]\n\teditor = nano\n')
        with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config)}):
            setup_vm.configure_git(ANSWERS)
            self.assertEqual(setup_vm.read_git_values("user.name"), (ANSWERS.git_name,))
            self.assertEqual(setup_vm.read_git_values("user.email"), (ANSWERS.git_email,))
            self.assertEqual(setup_vm.read_git_values("core.editor"), ("nano",))
            original = config.read_bytes()
            setup_vm.configure_git(ANSWERS)
            self.assertEqual(config.read_bytes(), original)

    def test_failed_git_write_stops_remaining_configuration(self):
        with patch.object(setup_vm, "read_git_values", return_value=()):
            with patch.object(setup_vm.subprocess, "run") as run:
                run.return_value = subprocess.CompletedProcess([], 1, "", "permission denied")
                with self.assertRaisesRegex(setup_vm.SetupError, "permission denied"):
                    setup_vm.configure_git(ANSWERS)
                self.assertEqual(run.call_count, 1)

    def test_git_included_settings_are_preserved_and_effective_identity_is_verified(self):
        included = self.home / "included-config"
        included.write_text('[user]\n\tname = Included Name\n\temail = included@example.com\n')
        config = self.home / "gitconfig"
        config.write_text(f'[include]\n\tpath = {included}\n[user]\n\tname = Old Name\n')
        with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config)}):
            setup_vm.configure_git(ANSWERS)
            self.assertEqual(setup_vm.read_git_values("user.name")[-1], ANSWERS.git_name)
            original = config.read_bytes()
            setup_vm.configure_git(ANSWERS)
            self.assertEqual(config.read_bytes(), original)
        self.assertIn("Included Name", included.read_text())

    def test_failed_git_verification_is_not_success(self):
        with patch.object(setup_vm, "read_git_values", return_value=("different",)):
            with patch.object(setup_vm.subprocess, "run") as run:
                run.return_value = subprocess.CompletedProcess([], 0, "", "")
                with self.assertRaisesRegex(setup_vm.SetupError, "verification failed"):
                    setup_vm.configure_git(ANSWERS)

    def test_existing_private_public_or_dangling_symlink_is_preserved(self):
        self.key.parent.mkdir()
        for kind in ("private", "public", "symlink"):
            target = Path(f"{self.key}.pub") if kind == "public" else self.key
            if kind == "symlink":
                target.symlink_to(self.home / "missing")
            else:
                target.write_text("existing key")
            try:
                with self.subTest(kind=kind), patch.object(setup_vm.subprocess, "run") as run:
                    setup_vm.generate_ssh_key(self.key, ANSWERS.git_email)
                    run.assert_not_called()
                    self.assertTrue(os.path.lexists(target))
                    if kind != "symlink":
                        self.assertEqual(target.read_text(), "existing key")
            finally:
                target.unlink()

    def fake_keygen(self, command, **kwargs):
        if "-t" in command:
            key = Path(command[command.index("-f") + 1])
            key.write_text("test private key fixture")
            Path(f"{key}.pub").write_text("ssh-ed25519 TEST_PUBLIC_KEY vm@example.com\n")
            return subprocess.CompletedProcess(command, 0)
        return subprocess.CompletedProcess(command, 0, "256 SHA256:test vm@example.com (ED25519)\n", "")

    def test_key_generation_inherits_passphrase_prompt_and_sets_permissions(self):
        with patch.object(setup_vm, "require_program"):
            with patch.object(setup_vm.subprocess, "run", side_effect=self.fake_keygen) as run:
                setup_vm.generate_ssh_key(self.key, ANSWERS.git_email)
        command = run.call_args_list[0].args[0]
        self.assertNotIn("-N", command)
        self.assertNotIn("input", run.call_args_list[0].kwargs)
        self.assertNotIn("stdin", run.call_args_list[0].kwargs)
        self.assertEqual(self.key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(f"{self.key}.pub").stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.key.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(list(self.key.parent.glob("vm-setup-*")), [])

    def test_keygen_failure_publishes_no_key(self):
        with patch.object(setup_vm, "require_program"):
            with patch.object(setup_vm.subprocess, "run") as run:
                run.return_value = subprocess.CompletedProcess([], 1)
                with self.assertRaisesRegex(setup_vm.SetupError, "generation failed"):
                    setup_vm.generate_ssh_key(self.key, ANSWERS.git_email)
        self.assertFalse(self.key.exists())
        self.assertEqual(list(self.key.parent.iterdir()), [])

    def test_key_verification_failure_publishes_no_key(self):
        def fail_verification(command, **kwargs):
            if "-l" in command:
                return subprocess.CompletedProcess(command, 1, "", "invalid")
            return self.fake_keygen(command, **kwargs)

        with patch.object(setup_vm, "require_program"):
            with patch.object(setup_vm.subprocess, "run", side_effect=fail_verification):
                with self.assertRaisesRegex(setup_vm.SetupError, "verification failed"):
                    setup_vm.generate_ssh_key(self.key, ANSWERS.git_email)
        self.assertFalse(self.key.exists())

    def test_concurrent_existing_key_is_not_overwritten(self):
        def competing_keygen(command, **kwargs):
            result = self.fake_keygen(command, **kwargs)
            if "-t" in command:
                self.key.write_text("another process's key")
            return result

        with patch.object(setup_vm, "require_program"):
            with patch.object(setup_vm.subprocess, "run", side_effect=competing_keygen):
                with self.assertRaisesRegex(setup_vm.SetupError, "Nothing was overwritten"):
                    setup_vm.generate_ssh_key(self.key, ANSWERS.git_email)
        self.assertEqual(self.key.read_text(), "another process's key")

    def test_declining_confirmation_performs_no_setup(self):
        plan = setup_vm.SetupPlan(ANSWERS, (), (), self.key)
        with patch.object(setup_vm.sys.stdin, "isatty", return_value=True):
            with patch.object(setup_vm.sys.stdout, "isatty", return_value=True):
                with patch.object(setup_vm, "collect_answers", return_value=ANSWERS):
                    with patch.object(setup_vm, "build_plan", return_value=plan):
                        with patch.object(setup_vm, "ask_yes_no", return_value=False):
                            with patch.object(setup_vm, "apply_setup") as apply:
                                self.assertEqual(setup_vm.main(), 0)
                                apply.assert_not_called()

    def test_interruption_after_execution_does_not_claim_no_changes(self):
        plan = setup_vm.SetupPlan(ANSWERS, (), (), self.key)
        with patch.object(setup_vm.sys.stdin, "isatty", return_value=True):
            with patch.object(setup_vm.sys.stdout, "isatty", return_value=True):
                with patch.object(setup_vm, "collect_answers", return_value=ANSWERS):
                    with patch.object(setup_vm, "build_plan", return_value=plan):
                        with patch.object(setup_vm, "ask_yes_no", return_value=True):
                            with patch.object(setup_vm, "apply_setup", side_effect=KeyboardInterrupt):
                                with contextlib.redirect_stdout(io.StringIO()) as output:
                                    with patch.object(output, "isatty", return_value=True):
                                        self.assertEqual(setup_vm.main(), 0)
        self.assertIn("Completed changes may remain", output.getvalue())
        self.assertNotIn("No changes were made", output.getvalue())


if __name__ == "__main__":
    unittest.main()
