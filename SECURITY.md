# Security

Switchboard is a loopback coordination board. It is not a sandbox, identity provider, or spend gate.

## Report a vulnerability

Email security reports to the KE Studios maintainer listed on the GitHub repository. Do not file public issues that include tokens, transcripts, or private paths.

## What this software does

- Binds to `127.0.0.1` by default
- Requires a same-origin control token for mutating routes
- Stores all board state under `SWITCHBOARD_HOME` (default `~/.switchboard`); it writes nowhere else
- Routes nothing automatically by default: incidents and messages wait for the operator
- Starts `codex`/`claude` processes only after an explicit opt-in (`--allow-launch` or `SWITCHBOARD_ALLOW_LAUNCH=1`, plus `boardctl.py routing --mode auto` and an agent registered with `--wake-mode resume`), in the agent's own project directory, without any permission-bypass flag
- Never grants deploy or credential authority

## Prompt injection

Incident titles, details and room messages are untrusted text. When you enable
launches, incident text is included in the prompt given to the resumed agent. Only
enable launches when you trust every source that can file incidents (anything that
can reach the loopback API with the control token, or run `boardctl.py`).

## What to assume

Any process on the same user account can read the data directory. Treat the board as a trusted-local operator surface.
