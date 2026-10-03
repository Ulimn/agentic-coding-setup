# Agentic Coding VM Setup

A simple interactive Python script to prepare a machine for agentic coding by
configuring Git identity, installing selected coding agents, and setting up a
VS Code server based on the user's answers.

## Usage

Requires Python 3.10+ and an interactive terminal. Run these commands from the
project directory inside your VM.

On Ubuntu, install Python and virtual environment support first:

```sh
sudo apt update
sudo apt install python3 python3-venv git openssh-client
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

Tool installation and account login are not implemented yet. Selected tools are
listed as pending. Run the script as your normal VM user, without `sudo`.

For remote execution, allocate a terminal:

```sh
ssh -t user@vm 'cd /path/to/agentic-coding-setup && .venv/bin/python setup_vm.py'
```

If virtual environment creation reports that `ensurepip` is unavailable, install
`python3-venv` as shown above. If pip reports `externally-managed-environment`,
use `.venv/bin/python -m pip` rather than installing into the system Python.
