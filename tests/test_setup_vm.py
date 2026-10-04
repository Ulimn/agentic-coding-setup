import contextlib
import io
import unittest
from unittest.mock import patch

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

import setup_vm


class QuestionnaireTests(unittest.TestCase):
    def select(self, keys: str) -> tuple[str, ...]:
        with create_pipe_input() as terminal_input:
            with create_app_session(input=terminal_input, output=DummyOutput()):
                terminal_input.send_text(keys)
                return setup_vm.select_tools()

    def test_name_validation(self):
        self.assertTrue(setup_vm.valid_name(" Zoë Example "))
        for value in ("", "   ", "Alice\nOther", "Alice\x1b"):
            with self.subTest(value=value):
                self.assertFalse(setup_vm.valid_name(value))

    def test_email_validation(self):
        for value in ("alice+vm@example.com", " alice@localhost "):
            self.assertTrue(setup_vm.valid_email(value))
        for value in ("", "alice", "@example.com", "alice@", "a@@b", "a b@c", "a@b\n"):
            with self.subTest(value=value):
                self.assertFalse(setup_vm.valid_email(value))

    def test_no_tools_selected_by_default(self):
        self.assertEqual(self.select("\r"), ())

    def test_space_toggles_and_arrows_navigate(self):
        # Toggle the first item on and off, select Codex, then wrap to Node Tools.
        keys = "  \x1b[B \x1b[A\x1b[A \r"
        self.assertEqual(self.select(keys), ("Codex", "Node Tools"))

    def test_all_requested_tools_can_be_selected(self):
        self.assertEqual(self.select(" \x1b[B" * len(setup_vm.TOOLS) + "\r"), setup_vm.TOOLS)

    def test_selection_cancellation(self):
        for key, error in (("\x03", KeyboardInterrupt), ("\x04", EOFError)):
            with self.subTest(key=key), self.assertRaises(error):
                self.select(key)

    def test_collect_answers_trims_identity_and_defaults_ssh_to_no(self):
        with patch.object(setup_vm, "require_program"), patch.object(setup_vm, "read_git_values", return_value=()):
            with patch.object(setup_vm, "prompt", side_effect=[" Alice ", " a@b ", "", ()]):
                answers = setup_vm.collect_answers()
        self.assertEqual(answers, setup_vm.SetupAnswers("Alice", "a@b", False, ()))

    def test_collect_answers_keeps_existing_effective_identity_without_prompting(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(setup_vm, "require_program"):
            with patch.object(setup_vm, "read_git_values", side_effect=[("Earlier", "Alice"), ("a@b",)]):
                with patch.object(setup_vm, "prompt", side_effect=["", ()]) as prompt:
                    answers = setup_vm.collect_answers()
        self.assertEqual(answers, setup_vm.SetupAnswers("Alice", "a@b", False, ()))
        self.assertEqual(prompt.call_count, 2)
        self.assertIn("user.name is already set to 'Alice'; keeping it", output.getvalue())
        self.assertIn("user.email is already set to 'a@b'; keeping it", output.getvalue())

    def test_collect_answers_asks_only_for_missing_identity_field(self):
        for previous, entered, label in (
            ([("Alice",), ()], "a@b", "Git user email: "),
            ([(), ("a@b",)], "Alice", "Git user name: "),
        ):
            with self.subTest(label=label), contextlib.redirect_stdout(io.StringIO()):
                with patch.object(setup_vm, "require_program"), patch.object(setup_vm, "read_git_values", side_effect=previous):
                    with patch.object(setup_vm, "prompt", side_effect=[entered, "", ()]) as prompt:
                        answers = setup_vm.collect_answers()
            self.assertEqual(answers, setup_vm.SetupAnswers("Alice", "a@b", False, ()))
            self.assertEqual(prompt.call_args_list[0].args[0], label)
            self.assertEqual(prompt.call_count, 3)

    def test_collect_answers_treats_blank_identity_values_as_missing(self):
        with patch.object(setup_vm, "require_program"), patch.object(setup_vm, "read_git_values", side_effect=[("",), ("   ",)]):
            with patch.object(setup_vm, "prompt", side_effect=[" Alice ", " a@b ", "", ()]):
                answers = setup_vm.collect_answers()
        self.assertEqual(answers, setup_vm.SetupAnswers("Alice", "a@b", False, ()))

    def test_collect_answers_stops_on_git_read_failure(self):
        with patch.object(setup_vm, "require_program"), patch.object(setup_vm, "read_git_values", side_effect=setup_vm.SetupError("read failed")):
            with patch.object(setup_vm, "prompt") as prompt:
                with self.assertRaisesRegex(setup_vm.SetupError, "read failed"):
                    setup_vm.collect_answers()
                prompt.assert_not_called()

    def test_collect_answers_accepts_ssh_yes(self):
        with patch.object(setup_vm, "require_program"), patch.object(setup_vm, "read_git_values", return_value=()):
            with patch.object(setup_vm, "prompt", side_effect=["Alice", "a@b", " YES ", ("Codex",)]):
                answers = setup_vm.collect_answers()
        self.assertTrue(answers.generate_ssh_key)
        self.assertEqual(answers.tools, ("Codex",))

    def test_summary_distinguishes_choices_from_performed_setup(self):
        summary = setup_vm.format_summary(setup_vm.SetupAnswers("Alice", "a@b", True, ()))
        self.assertIn("Generate Ed25519 SSH key: Yes", summary)
        self.assertIn("Selected tools:\n  None", summary)

    def test_main_cancels_without_traceback_or_summary(self):
        for error in (KeyboardInterrupt, EOFError):
            output = io.StringIO()
            with self.subTest(error=error), contextlib.redirect_stdout(output):
                with patch.object(setup_vm.sys.stdin, "isatty", return_value=True):
                    with patch.object(output, "isatty", return_value=True):
                        with patch.object(setup_vm, "collect_answers", side_effect=error):
                            self.assertEqual(setup_vm.main(), 0)
            self.assertIn("Setup cancelled", output.getvalue())
            self.assertNotIn("Setup choices", output.getvalue())


if __name__ == "__main__":
    unittest.main()
