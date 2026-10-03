# Project brief

## Purpose and scope

Build a simple Python command-line script that helps a user prepare a machine
for agentic coding. It asks questions using text input, single-select prompts,
and multi-select prompts, then performs the setup selected by the user.

The initial scope is:

- Configure Git user name and email.
- Install the selected coding agent or agents.
- Install and configure a VS Code server.

Additional setup tasks may be added later. The initial implementation should
remain small and understandable, with room to add setup tasks as needed.

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

- Supported operating system, distribution, and Python version.
- Whether setup runs directly on the target machine or manages a remote VM.
- Available coding agents, their installation methods, and authentication needs.
- Which VS Code server product is intended, and how it will run and be accessed.
- Git configuration scope: global or repository-local.

Resolve these when implementing the relevant feature and record the decisions
here. Document supported behavior and usage in the README.
