# Agent guidelines

## Project context

- Read [PROJECT.md](PROJECT.md) before working on this repository. It contains
  the project scope, business requirements, high-level plan, and open decisions.
- Keep requirements and planning in `PROJECT.md`; keep contributor rules here.
- Keep `CLAUDE.md` as a short pointer to this file rather than duplicating rules.

## Development rules

- Keep changes focused on the user's request. Do not implement future features
  just because they appear in `PROJECT.md`.
- Do not silently assume cross-platform support, a particular cloud provider, or
  a particular server product. Establish a documented initial target first.
- Ask focused questions when a decision materially affects expected behavior;
  otherwise use a simple, documented implementation choice.
- Prefer the Python standard library. Add a focused prompt library if it makes
  text, single-select, and multi-select input substantially easier to maintain.
- Keep dependencies minimal and declare them in one place.
- Use readable names, small functions, and type hints where they clarify interfaces.
- Separate question collection, plan construction, and execution so validation
  can be checked without modifying the developer's machine.
- Keep setup operations in small functions or modules. Start with a single entry
  point; split files only when that improves clarity.
- Keep operating-system-specific commands behind explicit environment checks.
- Avoid speculative abstractions, plugin systems, daemons, and unrelated features.
- Execute commands with argument lists using `subprocess`; avoid `shell=True` and
  interpolation of user input into shell commands.
- Verify current installation instructions against official documentation when
  implementing or changing an installer. Do not invent download URLs.
- Never hard-code, commit, or log credentials.
- Always keep `PROJECT.md` up to date. Review it for every task and update it
  whenever scope, requirements, plans, implementation progress, or decisions
  change. Make those updates as part of the same task.
- Update the README when adding supported environments, setup options, dependencies,
  or commands. Include how to run the script and what it changes.

## Validation and collaboration rules

- Inspect the repository and existing changes before editing. Preserve work that
  is unrelated to the task.
- Do not add agent attribution to commits, pull requests, or project content,
  including co-author trailers or generated-by notices.
- Test meaningful behavior: input validation, plan construction, existing-state
  handling, and command failures. Mock commands that install software or modify
  system configuration; do not provision the developer's machine during tests.
- Run the relevant checks for the change. If a check cannot run, state why.
- Report what changed, what was verified, and any unresolved decisions or limitations.
- Do not claim success based only on starting a command; verify its result.
