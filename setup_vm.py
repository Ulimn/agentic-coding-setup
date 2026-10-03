"""Collect setup preferences for an agentic coding VM."""

from dataclasses import dataclass
import re
import sys

try:
    from prompt_toolkit import prompt
    from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
    from prompt_toolkit.validation import Validator
except ModuleNotFoundError:
    sys.exit("Missing dependencies. Run: python -m pip install -r requirements.txt")


TOOLS = (
    "Visual Studio Code Server",
    "Codex",
    "Claude Code",
    "Antigravity-CLI",
    "GitHub CLI",
    "GitLab CLI",
    "Forgejo CLI",
)


@dataclass(frozen=True)
class SetupAnswers:
    git_name: str
    git_email: str
    generate_ssh_key: bool
    tools: tuple[str, ...]


def valid_name(value: str) -> bool:
    return bool(value.strip()) and all(character.isprintable() for character in value)


def valid_email(value: str) -> bool:
    """Check basic address shape without enforcing a public domain or mailbox lookup."""
    return (
        all(character.isprintable() for character in value)
        and re.fullmatch(r"[^@\s<>]+@[^@\s<>]+", value.strip()) is not None
    )


def select_tools() -> tuple[str, ...]:
    """Show ASCII checkboxes; arrows move, Space toggles, and Enter submits."""
    selected: set[str] = set()
    cursor = 0
    bindings = KeyBindings()

    def menu() -> list[tuple[str, str]]:
        lines = [
            ("bold", "Select tools to install:\n"),
            ("", "Up/Down: move | Space: toggle | Enter: continue | Ctrl+C/D: cancel\n"),
        ]
        for index, tool in enumerate(TOOLS):
            pointer = ">" if index == cursor else " "
            checkbox = "[x]" if tool in selected else "[ ]"
            style = "reverse" if index == cursor else ""
            lines.append((style, f"{pointer} {checkbox} {tool}\n"))
        return lines

    @bindings.add("up")
    def move_up(event: KeyPressEvent) -> None:
        nonlocal cursor
        cursor = (cursor - 1) % len(TOOLS)

    @bindings.add("down")
    def move_down(event: KeyPressEvent) -> None:
        nonlocal cursor
        cursor = (cursor + 1) % len(TOOLS)

    @bindings.add(" ")
    def toggle(event: KeyPressEvent) -> None:
        tool = TOOLS[cursor]
        if tool in selected:
            selected.remove(tool)
        else:
            selected.add(tool)

    @bindings.add("enter")
    def submit(event: KeyPressEvent) -> None:
        event.app.exit(result=tuple(tool for tool in TOOLS if tool in selected))

    @bindings.add("c-c")
    def interrupt(event: KeyPressEvent) -> None:
        event.app.exit(exception=KeyboardInterrupt)

    @bindings.add("c-d")
    def end_input(event: KeyPressEvent) -> None:
        event.app.exit(exception=EOFError)

    @bindings.add("<any>")
    def ignore_typing(event: KeyPressEvent) -> None:
        pass

    return prompt(menu, key_bindings=bindings)


def collect_answers() -> SetupAnswers:
    git_name = prompt(
        "Git user name: ",
        validator=Validator.from_callable(
            valid_name, error_message="Enter a non-empty name without control characters."
        ),
    ).strip()
    git_email = prompt(
        "Git user email: ",
        validator=Validator.from_callable(
            valid_email, error_message="Enter an email address, for example name@example.com."
        ),
    ).strip()
    ssh_answer = prompt(
        "Generate an Ed25519 SSH key? [y/N]: ",
        validator=Validator.from_callable(
            lambda value: value.strip().lower() in ("", "y", "yes", "n", "no"),
            error_message="Enter y or n (Enter defaults to no).",
        ),
    ).strip().lower()
    return SetupAnswers(git_name, git_email, ssh_answer in ("y", "yes"), select_tools())


def format_summary(answers: SetupAnswers) -> str:
    lines = [
        "\nSetup choices",
        f"Git user name: {answers.git_name}",
        f"Git user email: {answers.git_email}",
        f"Generate Ed25519 SSH key: {'Yes' if answers.generate_ssh_key else 'No'}",
        "Tools to install:",
    ]
    lines.extend(f"  - {tool}" for tool in answers.tools)
    if not answers.tools:
        lines.append("  None")
    lines.append("\nQuestionnaire complete. No configuration, key generation, or installation was performed.")
    return "\n".join(lines)


def main() -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("Run this script in an interactive terminal (use ssh -t for remote sessions).", file=sys.stderr)
        return 1
    print("Agentic Coding VM Setup\nThis round collects your preferences only.\n")
    try:
        answers = collect_answers()
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled. No changes were made.")
        return 0
    print(format_summary(answers))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
