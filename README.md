# Agentic Coding VM Setup

A simple interactive Python script to prepare a machine for agentic coding by
configuring Git identity, installing selected coding agents, and setting up a
VS Code server based on the user's answers.

## Usage

Requires Ubuntu 22.04+ on x86_64 or aarch64, Python 3.10+, and an interactive
terminal for tool installation. Run these commands from the project directory
inside your VM.

On Ubuntu, install Python and virtual environment support first:

```sh
sudo apt update
sudo apt install python3 python3-venv git openssh-client curl tar coreutils bash
```

Create a virtual environment, install dependencies, and start the questionnaire:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python setup_vm.py
```

Enter your Git name and email, choose whether to generate an Ed25519 SSH key,
then select tools. Use **Up/Down** to move, **Space** to toggle checkboxes, and
**Enter** to continue. **Ctrl+C** cancels.

If your global Git name or email is already set, setup keeps it, skips that
question, and prints a message showing the reused value. It asks only for
missing or blank values, including when just one of the two is configured.

Review the setup plan and confirm to apply it. Git name and email are configured
globally for the current VM user. Existing values are shown before confirmation;
unrelated Git settings are preserved, and matching values are skipped on reruns.

If selected, an Ed25519 SSH key is generated at `~/.ssh/id_ed25519`.
`ssh-keygen` asks for a passphrase and confirmation; press Enter twice to use no
passphrase. The script displays the public key so you can add it to your Git
hosting accounts. It does not store or log the passphrase.

If either `~/.ssh/id_ed25519` or `~/.ssh/id_ed25519.pub` already exists, key
generation is skipped without overwriting files. Partial failures leave completed
steps in place; resolve the reported error and rerun.

Selected tools are installed for the current user, with no `sudo` required.
Existing commands are verified and skipped. Run the script as your normal VM
user, without `sudo`. Downloads are prepared before confirmation; an installation
failure stops subsequent steps, and rerunning resumes from existing installations.

## Installed tools

| Selection | Command | Installation source | Manual login or startup |
| --- | --- | --- | --- |
| Visual Studio Code Server | `code` | [Microsoft standalone CLI](https://code.visualstudio.com/docs/remote/tunnels) | `code tunnel --disable-telemetry` |
| Codex | `codex` | [Official native installer](https://learn.chatgpt.com/docs/codex/cli) | `codex`, then choose device code sign-in |
| Claude Code | `claude` | [Official native installer, stable channel](https://code.claude.com/docs/en/setup) | `claude` |
| Antigravity-CLI | `agy` | [Google's official native installer](https://www.antigravity.google/docs/cli/install/) | `agy` |
| GitHub CLI | `gh` | [Official GitHub releases](https://github.com/cli/cli/releases) | `gh auth login` |
| GitLab CLI | `glab` | [Official GitLab releases](https://gitlab.com/gitlab-org/cli/-/releases) | `glab auth login` |
| Forgejo CLI | `fj` | [forgejo-contrib releases](https://codeberg.org/forgejo-contrib/forgejo-cli/releases) | `fj auth login` |

Commands are installed in `~/.local/bin`; native installers also manage their
own files and may configure shell integration. If your shell cannot find the
commands, run:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

Add this line to your shell profile if needed for future sessions.

Setup ends with a reminder to run `source ~/.bashrc` if you use Bash. This
reloads any profile changes made by the installers. The PATH export above makes
the commands available immediately even if your `.bashrc` does not include it.

The VS Code selection prepares Microsoft's Remote Tunnels CLI. Run
`code tunnel --disable-telemetry` yourself to accept the server license, sign in,
and download and start the server. It prints a connection URL for your browser
or local VS Code client. Setup does not start a tunnel, open a listening port,
or create a background service. Account login and SSH key registration remain
manual for all tools.

## Codex login on a remote VM

Enable device code login in ChatGPT's security settings first (a workspace
administrator may need to enable it in workspace permissions). Then start
Codex from the VM terminal:

```sh
codex
```

In the initial login menu, choose **Sign in with Device Code**. Open the printed
link in your local browser and enter the one-time code to sign in with your
ChatGPT subscription account. This route has been confirmed working on the
user's VM and does not require a callback to the VM.

OpenAI also documents `codex login --device-auth` as a direct way to start the
same flow. If that command fails, use the initial login menu as described above.

The regular browser login uses a localhost callback. Opening that link in a
browser on your computer sends the callback to your computer's loopback address,
while Codex is waiting inside the VM. Replacing `127.0.0.1` with the VM's IP does
not make that callback flow work.

If device code login is unavailable, forward the callback port over SSH.
Run this on the computer where your browser runs:

```sh
ssh -o ExitOnForwardFailure=yes -L 127.0.0.1:1455:localhost:1455 user@vm
```

Inside that SSH session, run `codex login`, then open its printed login link in
your local browser. Keep the SSH session open through login. These instructions
assume Codex runs directly inside the VM; for a Docker test container, use the
device code flow.

See [OpenAI's authentication documentation](https://learn.chatgpt.com/docs/auth)
for account settings and supported login methods.

For remote execution, allocate a terminal:

```sh
ssh -t user@vm 'cd /path/to/agentic-coding-setup && .venv/bin/python setup_vm.py'
```

If virtual environment creation reports that `ensurepip` is unavailable, install
`python3-venv` as shown above. If pip reports `externally-managed-environment`,
use `.venv/bin/python -m pip` rather than installing into the system Python.

## Test in an Ubuntu container

Requires a running Docker engine (Docker Desktop or OrbStack on macOS).
Build the test image from the project directory:

```sh
docker build --pull -f Dockerfile.test -t agentic-vm-setup-test .
docker run --rm -it agentic-vm-setup-test
```

The image uses the official `ubuntu:26.04` image and checks that its point
release is **26.04.1**. The build fails if the tag moves to another point release.
It installs prerequisites and Python dependencies, runs the automated tests,
and opens a shell as the normal user `vmtest`.

Inside that shell, start setup:

```sh
python setup_vm.py
```

Use a test Git name and email, request an SSH key, and select the tools you want
to test. Run the same command again with the same choices to check that matching
Git settings, the existing key, and installed tools are preserved. Keep both
runs in the same container shell. Use `exit` when finished; `--rm` removes the
container and its configuration, keys, and installed tools. A new `docker run`
starts fresh. Rebuild the image after changing the project files.

The container uses its own home directory and does not mount your host files
or credentials. It tests setup and installation; account login and persistent
VS Code server operation still need testing in the target VM.
