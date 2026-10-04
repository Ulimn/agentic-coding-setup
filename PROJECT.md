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

1. Git user name (required text only when missing or blank globally).
2. Git user email (required text with basic format validation only when missing
   or blank globally).
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

Read the effective global Git identity, including included config files, before
each identity question. Keep any nonblank configured value, skip its question,
and print a message identifying the value being reused. Handle name and email
independently and use the last configured value when multiple values exist.

The questionnaire collects preferences in memory. Git configuration, SSH key
generation, and selected tool installation are implemented. Authentication and
VS Code server startup remain manual.

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

## Tool installation

- Initial installer target: Ubuntu 22.04+ on x86_64 or aarch64. No tools are
  installed on the developer's machine as part of development or testing.
- Prepare downloads and check prerequisites before showing the setup plan.
  Do not change Git configuration or generate keys if tool preparation fails.
- Install selected tools for the current VM user, without sudo. Show sources
  and installation scope before confirmation. Native installer scripts are
  downloaded into temporary files and executed as subprocess argument lists.
- Use the official native installers for Codex, Claude Code (stable channel),
  and Google's Antigravity CLI (`agy`). Codex runs its installer without
  interactive launch prompts; account login is never started by setup.
- Install GitHub CLI (`gh`), GitLab CLI (`glab`), and the forgejo-contrib Forgejo
  client (`fj`) from upstream release archives into `~/.local/bin`.
- The VS Code option prepares Microsoft's standalone `code` CLI. The user runs
  `code tunnel --disable-telemetry` to accept the server license, sign in, and
  download and start the server. Setup creates no running server or service.
- Fetch release metadata and checksums over HTTPS. Verify GitHub CLI, GitLab
  CLI, and Microsoft archives against upstream SHA-256 checksums. Native
  installers perform their own payload verification. Forgejo's current release
  publishes no checksum; its archive is downloaded directly from Codeberg over
  HTTPS and its executable is verified with `fj version`.
- Copy only the intended regular executable from release archives; do not
  extract arbitrary archive paths or symlinks. Publish with exclusive hard
  links so concurrent files are not overwritten.
- Find existing installations on PATH and in `~/.local/bin`; verify them and
  skip reinstalls. Report broken existing files rather than overwriting them.
- Verify new installations with `--version` (`version` for Forgejo CLI). Stop on errors, preserve completed
  work, and provide recovery guidance. Reruns skip satisfied steps.
- Print the PATH command and manual login/startup commands at the end. No
  credentials are collected. Native installers may manage their own shell
  integration; the Python script does not rewrite shell profiles.
- For Codex on a remote VM, prominently recommend starting `codex` and choosing
  device code sign-in in its initial login menu. Keep the officially documented
  `codex login --device-auth` command as an alternative. Explain the account/workspace setting
  for device code login and the browser code flow. Document SSH forwarding of
  the localhost callback as a fallback for Codex running directly in a VM.
  Authentication remains manual; setup does not initiate account login.
  Give these instructions a separate bold red terminal heading so users can
  easily spot them; use plain text when output is redirected.
- Official source links and supported commands are listed in the README.

## Current implementation and runtime

- Entry point: `setup_vm.py`.
- Runtime target: an interactive Ubuntu 22.04+ VM terminal with Python 3.10+.
  The questionnaire and Git/SSH setup can also run on macOS when no tools are
  selected; tool installation explicitly requires Ubuntu on a supported CPU.
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
- Validation: 45 tests cover input validation, checkbox navigation, cancellation,
  confirmation, Git writes and verification, reruns, and SSH key preservation,
  reuse of complete or partial Git identities, blank values and read failures,
  generation, permissions, and failures. Git tests use an isolated temporary
  config file; SSH commands are mocked and operate on temporary test fixtures.
  An interactive questionnaire run was checked on macOS. The user confirmed a
  successful questionnaire run on an Ubuntu VM,
  including Git identity input, opting into SSH key generation, selecting
  multiple tools, and displaying the summary. The Ubuntu version was not specified.
  The user also confirmed successful Git/SSH setup on that VM: global Git name
  and email were configured and verified, and an Ed25519 key was generated,
  verified, and published at the intended path with its public key displayed.
  Existing-key preservation and reruns remain covered by automated tests;
  they have not yet been confirmed on the user's VM. Installer tests mock
  network and installer execution, and cover checksum failures, architecture
  selection, safe archive handling, existing commands, and verification failures.
  A read-only live check successfully resolved the Microsoft, GitHub, GitLab,
  and Forgejo download plans and available checksum manifests. Official native
  installer scripts were downloaded and reviewed without executing them.
  End-to-end installation is verified in the Ubuntu container described below;
  authentication and VS Code server startup still need testing on the user's VM.
- Container integration test target: Ubuntu 26.04.1 LTS, using the
  official `ubuntu:26.04` Docker image and verifying the installed point release
  inside the container. Run as a normal user in an interactive terminal and
  repeat setup to check existing configuration, key, and tool preservation.
  Container testing complements VM testing for authentication and server startup;
  `Dockerfile.test` installs prerequisites and Python dependencies, checks the
  exact point release, and runs the existing automated tests during the build.
  Its interactive shell runs as `vmtest`, with a separate home and no host
  mounts. `.dockerignore` includes only the required build inputs. The README
  documents building, running, repeating setup, and disposing of the container.
  The image built successfully on ARM64 with Ubuntu 26.04.1 and Python 3.14.4;
  all 41 tests passed in the final build.
  A real interactive run exposed gzip HTTP compression on the Antigravity
  installer response. Metadata downloads now decode that encoding with bounded
  compressed and decoded sizes; three regression tests cover plain/compressed
  responses, size limits, and corrupt gzip data. The real run also revealed that
  Forgejo CLI requires `fj version`; verification now uses its supported
  subcommand and reads the version line rather than its keys-file notice, with
  regression coverage for staged/installed paths and missing version output.
  Real interactive setup configured Git, generated an Ed25519 key with verified
  permissions, and installed all seven tools: code 1.140.0, Codex 0.160.0,
  Claude Code 2.1.285, Antigravity 1.2.16, gh 2.102.0, glab 1.120.0, and fj 0.6.0.
  Recovery after the Forgejo verification failure preserved prior completed work.
  A final interactive rerun skipped all satisfied steps; hashes, inodes,
  modification times, and modes confirmed that Git config, both key files, and
  all seven executables were unchanged. The disposable container was removed.
  AMD64 container execution, account login, and VS Code server startup were not
  tested in this round.
- The README includes usage, Ubuntu prerequisites, virtual environment
  installation, SSH terminal allocation, and Git/SSH setup behavior.
- Latest user VM feedback: Codex installed, but regular browser login returned
  to `127.0.0.1` on the browser's machine; changing the callback to the VM IP did
  not work. The README and setup output now explain device code login and the
  SSH forwarding fallback, checked against official OpenAI authentication docs.
  The user also reports that Forgejo CLI did not install. At the time of that
  report, the tested `fj version` fix and container changes had not been pushed,
  so the remote checkout may have contained the old `--version` verification.
  These changes are included in this update. The exact user error and tested
  revision are pending; this report is not yet diagnosed.
- The user subsequently confirmed successful Codex subscription login through
  the initial device code menu after enabling device code authentication in
  settings. Their report mentions `claude login --device-auth` as failing;
  whether this was a typo for `codex` is unconfirmed. The README and setup
  output now lead with the successful menu route. No failure cause for the
  direct command has been established.

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

The following remain for future iterations:

- Optional automated or guided account login.
- Optional persistent VS Code server operation after manual authentication.
- Additional distributions, architectures, or alternative server products.

Resolve these when implementing the relevant feature and record the decisions
here. Document supported behavior and usage in the README.
