"""Configure Git identity and an optional SSH key for an agentic coding VM."""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

try:
    from prompt_toolkit import prompt
    from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
    from prompt_toolkit.validation import Validator
except ModuleNotFoundError:
    sys.exit("Missing dependencies. Run: .venv/bin/python -m pip install -r requirements.txt")


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


@dataclass(frozen=True)
class SetupPlan:
    answers: SetupAnswers
    previous_name: tuple[str, ...]
    previous_email: tuple[str, ...]
    key_path: Path | None


class SetupError(Exception):
    """An actionable setup failure that should not display a traceback."""


def ask_yes_no(message: str) -> bool:
    answer = prompt(
        f"{message} [y/N]: ",
        validator=Validator.from_callable(
            lambda value: value.strip().lower() in ("", "y", "yes", "n", "no"),
            error_message="Enter y or n (Enter defaults to no).",
        ),
    )
    return answer.strip().lower() in ("y", "yes")


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
    generate_key = ask_yes_no("Generate an Ed25519 SSH key?")
    return SetupAnswers(git_name, git_email, generate_key, select_tools())


def format_summary(answers: SetupAnswers) -> str:
    lines = [
        "\nSetup choices",
        f"Git user name: {answers.git_name}",
        f"Git user email: {answers.git_email}",
        f"Generate Ed25519 SSH key: {'Yes' if answers.generate_ssh_key else 'No'}",
        "Selected tools (installation not implemented yet):",
    ]
    lines.extend(f"  - {tool}" for tool in answers.tools)
    if not answers.tools:
        lines.append("  None")
    return "\n".join(lines)


def require_program(program: str) -> None:
    if shutil.which(program) is None:
        package = "git" if program == "git" else "openssh-client"
        raise SetupError(f"{program} is missing. On Ubuntu, run: sudo apt install {package}")


def read_git_values(key: str) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "config", "--global", "--includes", "--get-all", key],
        capture_output=True, text=True,
    )
    if result.returncode == 1:
        return ()
    if result.returncode != 0:
        raise SetupError(f"Could not read global Git {key}: {result.stderr.strip()}")
    return tuple(result.stdout.splitlines())


def key_files_exist(path: Path) -> bool:
    # lexists also protects dangling symlinks and a public key without a private key.
    return os.path.lexists(path) or os.path.lexists(f"{path}.pub")


def build_plan(answers: SetupAnswers) -> SetupPlan:
    require_program("git")
    key_path = Path.home() / ".ssh" / "id_ed25519" if answers.generate_ssh_key else None
    if key_path is not None and not key_files_exist(key_path):
        require_program("ssh-keygen")
    return SetupPlan(
        answers, read_git_values("user.name"), read_git_values("user.email"), key_path,
    )


def format_plan(plan: SetupPlan) -> str:
    lines = [format_summary(plan.answers), "\nSetup plan (current user; no sudo):"]
    for key, previous, desired in (
        ("user.name", plan.previous_name, plan.answers.git_name),
        ("user.email", plan.previous_email, plan.answers.git_email),
    ):
        current = ", ".join(repr(value) for value in previous) if previous else "not set"
        action = "Keep" if previous[-1:] == (desired,) else "Set"
        lines.append(f"  {action} global Git {key}: {current} -> {desired!r}")
    if plan.key_path is None:
        lines.append("  Skip SSH key generation.")
    elif key_files_exist(plan.key_path):
        lines.append(f"  Skip SSH key generation: {plan.key_path} or its .pub file already exists.")
    else:
        lines.append(f"  Generate Ed25519 SSH key: {plan.key_path}")
        lines.append("  ssh-keygen will ask for a passphrase and confirmation (Enter allows no passphrase).")
    return "\n".join(lines)


def configure_git(answers: SetupAnswers) -> None:
    for key, desired in (("user.name", answers.git_name), ("user.email", answers.git_email)):
        if read_git_values(key)[-1:] == (desired,):
            print(f"Skipped: global Git {key} already matches.")
            continue
        result = subprocess.run(
            ["git", "config", "--global", "--replace-all", "--", key, desired],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise SetupError(f"Could not configure global Git {key}: {result.stderr.strip()}")
        if read_git_values(key)[-1:] != (desired,):
            raise SetupError(f"Global Git {key} verification failed. Check included Git config files.")
        print(f"Configured and verified: global Git {key}.")


def generate_ssh_key(path: Path, email: str) -> None:
    if key_files_exist(path):
        print(f"Skipped: {path} or its .pub file already exists; existing files were preserved.")
        return
    require_program("ssh-keygen")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Generate in a private directory, then publish with exclusive hard links.
    # Even a file created after the initial check cannot be overwritten.
    with tempfile.TemporaryDirectory(prefix="vm-setup-", dir=path.parent) as directory:
        temporary_key = Path(directory) / "id_ed25519"
        print("Choose an SSH key passphrase below; input is hidden and not stored by this script.")
        result = subprocess.run([
            "ssh-keygen", "-t", "ed25519", "-C", email, "-f", str(temporary_key),
        ])
        if result.returncode != 0:
            raise SetupError("SSH key generation failed. Check the ssh-keygen message above and rerun.")
        public_path = Path(f"{temporary_key}.pub")
        public_key = public_path.read_text().strip()
        if not public_key.startswith("ssh-ed25519 "):
            raise SetupError("Generated public key is not Ed25519; no key was published.")
        verification = subprocess.run(
            ["ssh-keygen", "-l", "-f", str(public_path)], capture_output=True, text=True,
        )
        if verification.returncode != 0:
            raise SetupError("Generated SSH key verification failed; no key was published.")
        temporary_key.chmod(0o600)
        public_path.chmod(0o644)
        try:
            os.link(temporary_key, path)
            os.link(public_path, f"{path}.pub")
        except FileExistsError:
            raise SetupError(
                f"A key file appeared at {path} or {path}.pub during setup. Nothing was overwritten. "
                "Inspect both paths before rerunning; a newly generated private key may already exist."
            ) from None
    print(f"Generated and verified: {path}")
    print(f"Fingerprint: {verification.stdout.strip()}")
    print(f"Public key ({path}.pub):\n{public_key}")
    print("Add this public key to your Git hosting accounts. Keep the private key inside the VM.")


def apply_setup(plan: SetupPlan) -> None:
    configure_git(plan.answers)
    if plan.key_path is not None:
        generate_ssh_key(plan.key_path, plan.answers.git_email)
    else:
        print("Skipped: SSH key generation was not selected.")
    for tool in plan.answers.tools:
        print(f"Pending: {tool} installation is not implemented yet.")
    print("\nGit/SSH setup complete. Tool installation and account login remain manual.")


def main() -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("Run this script in an interactive terminal (use ssh -t for remote sessions).", file=sys.stderr)
        return 1
    print("Agentic Coding VM Setup\nConfigure Git and an optional SSH key for this VM user.\n")
    execution_started = False
    try:
        answers = collect_answers()
        plan = build_plan(answers)
        print(format_plan(plan))
        if not ask_yes_no("Apply this Git/SSH setup plan?"):
            print("\nSetup cancelled. No changes were made.")
            return 0
        execution_started = True
        apply_setup(plan)
    except (KeyboardInterrupt, EOFError):
        if execution_started:
            print("\nSetup interrupted. Completed changes may remain; rerun to resume.")
        else:
            print("\nSetup cancelled. No changes were made.")
        return 0
    except (SetupError, OSError) as error:
        print(f"\nSetup failed: {error}", file=sys.stderr)
        if execution_started:
            print("Completed changes may remain. Resolve the error and rerun to resume.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
