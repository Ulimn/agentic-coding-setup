# Project brief

## Purpose and scope

Build a simple Python command-line script that helps a user prepare a machine
for agentic coding. It asks questions using text input, single-select prompts,
and multi-select prompts, then performs the setup selected by the user.

The script runs inside the target VM, where the coding agents and their work
are intended to be contained. It does not provision or remotely manage VMs.

The initial scope is:

- Configure Git user name and email.
- Optionally generate an Ed25519 SSH key.
- Install the selected coding agent or agents.
- Install and configure a VS Code server.
- Install selected Git hosting CLI tools.

Additional setup tasks may be added later. The initial implementation should
remain small and understandable, with room to add setup tasks as needed.

## First round: questionnaire

Ask these questions in order:

1. Git user name (required text).
2. Git user email (required text with basic format validation).
3. Whether to generate an Ed25519 SSH key (yes/no, default no).
4. Which tools to install (multi-select, initially all unchecked):

   - Visual Studio Code Server
   - Codex
   - Claude Code
   - Antigravity-CLI
   - GitHub CLI
   - GitLab CLI
   - Forgejo CLI

The tool list displays `[ ]` and `[x]` checkboxes. Arrow keys move the cursor,
Space toggles the highlighted option, and Enter submits. Selecting no tools is
allowed. Display a summary after collecting the answers.

The questionnaire collects preferences in memory. Git configuration and SSH key
generation are now implemented; installation and authentication remain pending.

## Git and SSH setup

- Show existing global Git identity values and the proposed changes before
  asking for confirmation. The confirmation defaults to no.
- Configure `user.name` and `user.email` globally for the current VM user.
  Preserve unrelated Git settings, skip matching values, and verify each change.
- If requested, generate an Ed25519 SSH key at `~/.ssh/id_ed25519` with the
  supplied email as its comment.
- Let `ssh-keygen` prompt directly for a passphrase and confirmation. Empty
  passphrases are allowed; the script never receives, stores, or logs them.
- Skip generation if either the private or public key path already exists,
  including dangling symlinks. Generate in a temporary private directory and
  publish using exclusive hard links so concurrent files cannot be overwritten.
- Verify the generated public key and show its fingerprint and public contents
  for manual registration with Git hosting accounts.
- Create new SSH directories with mode 700, private keys with mode 600, and
  public keys with mode 644. Existing directory permissions are left unchanged.
- Require Git and, when generating a new key, OpenSSH's `ssh-keygen` before
  applying changes. On Ubuntu these are provided by `git` and `openssh-client`.
- Report failures and interruptions honestly: completed changes may remain.
  Reruns skip satisfied steps; no rollback of completed configuration is attempted.
- Tool selections remain visible as pending; no tools are installed in this round.

## Current implementation and runtime

- Entry point: `setup_vm.py`.
- Initial runtime target: an interactive Linux VM terminal with Python 3.10+.
  The questionnaire can also run on macOS; installers will need an explicit
  supported Linux distribution before implementation.
- Dependency: `prompt_toolkit`, declared in `requirements.txt`, for terminal
  input, validation, and keyboard navigation.
- Run locally in the target VM:

  ```sh
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
  .venv/bin/python setup_vm.py
  ```

- Ctrl+C and end-of-input cancel without a traceback. Before applying the plan,
  cancellation makes no changes; during execution, completed steps may remain.
- Validation covers input validation, checkbox navigation, cancellation,
  confirmation, Git writes and verification, reruns, and SSH key preservation,
  generation, permissions, and failures. Git tests use an isolated temporary
  config file; SSH commands are mocked and operate on temporary test fixtures.
  An interactive questionnaire run was checked on macOS. The user confirmed a
  successful questionnaire run on an Ubuntu VM,
  including Git identity input, opting into SSH key generation, selecting
  multiple tools, and displaying the summary. The Ubuntu version was not specified.
  The new Git/SSH setup still needs verification on the user's Ubuntu VM.
- The README includes usage, Ubuntu prerequisites, virtual environment
  installation, SSH terminal allocation, and Git/SSH setup behavior.

## High-level implementation plan

1. Inspect the environment and check prerequisites before making changes.
2. Collect and validate the user's choices through an interactive questionnaire.
3. Build and display a setup plan, including configuration scope and privileges.
4. Apply the confirmed steps, reporting progress and actionable failures.
5. Verify the results and print a summary with any remaining manual steps.

## User experience requirements

- Use clear questions, sensible defaults, and explicit selection labels.
- Allow users to skip optional setup components.
- Validate inputs before executing dependent commands.
- Explain what will change before applying the setup plan.
- Treat cancellation and end-of-input as normal exits, with no traceback.
- Show which steps succeeded, failed, or were skipped, and how to recover.
- Keep installation separate from authentication. Explain any interactive login
  steps that must be completed by the user.

## Setup behavior requirements

- Check for existing installations and configuration before changing them.
- Make reruns safe: skip satisfied steps and avoid duplicate configuration.
- Preserve unrelated settings. Show existing Git identity values before proposing
  replacements, and make the chosen configuration scope explicit.
- Require confirmation in the script before applying its displayed setup plan.
  This is runtime behavior for the setup tool, not a requirement to ask for
  permission before routine development work.
- Use elevated privileges only for steps that need them. Explain those steps and
  do not run the entire script as root by default.
- Use official installation sources; do not silently execute unreviewed remote
  scripts.
- Prefer the tool's supported login flow. If secret input is necessary, hide it
  and do not persist it by default.
- Default the server to local or private access. Public exposure must be an
  explicit choice with authentication and secure transport configured.
- Verify each completed step with an appropriate command or configuration check.
- Stop dependent steps when a prerequisite fails. Give recovery instructions
  rather than attempting speculative cleanup or rollback.

## Open decisions

The following are not yet specified:

- Supported Linux distribution and installer prerequisites.
- Installation methods and authentication needs for the selected tools.
- Exact Antigravity-CLI project and its official installation source.
- Which VS Code server product is intended, and how it will run and be accessed.

Resolve these when implementing the relevant feature and record the decisions
here. Document supported behavior and usage in the README.
